"""End-to-end controller integration at the transcript and guarded text boundaries."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import replace
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    StopFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from .coaching import (
    build_coaching_prompt,
    parse_coaching_reply,
    parse_interaction_reply,
    validate_coaching_reply,
)
from .config import InterviewConfig
from .contracts import AcceptedAnswer, AnswerBasis, CompletionStatus, ReplyKind, SegmentFinal
from .controller import ControllerPrompt, InterviewControl, InterviewController, InterviewPhase
from .interaction import (
    InteractionAction,
    InteractionKind,
    RecoveryGrant,
    SourceDisposition,
    SourceSpan,
    answer_text_from_spans,
)
from .ledger import TranscriptLedger
from .reply_guard import ParsedCompletion, ReplyAuthorization, ReplyDecision
from .transcript_gate import ProviderDispatch
from .turn_coordinator import InterviewTurnCoordinator
from .turn_policy import PolicyAction
from .voice_controls import VoiceControl, parse_voice_control


class InterviewSession(InterviewTurnCoordinator):
    """Connect question progression to validated transcripts and guarded reply release."""

    _MAX_PROVIDER_CONTEXT_BYTES = 128 * 1024
    _INTERNAL_OUTPUT_INTERRUPTION_EPOCH = "interview_internal_output_epoch"

    def __init__(
        self,
        *,
        config: InterviewConfig,
        session_id: str,
        connection_generation: int = 1,
        defer_start: bool = False,
        language: str = "english",
        clock=time.monotonic,
        **kwargs: Any,
    ):
        """Create a session whose controller and managed tasks share one monotonic clock.

        Args:
            config: Validated interview configuration.
            session_id: Opaque server-generated session identifier.
            connection_generation: Initial STT connection generation.
            defer_start: Wait for browser readiness before the introduction.
            language: Candidate-facing deterministic prompt language.
            clock: Monotonic clock shared by controller and transcript ledger.
            **kwargs: Additional coordinator options.
        """
        self.config = config
        self.controller = InterviewController(config, session_id, clock, language=language)
        self._tts = None
        self._stt = None
        self._playback_epoch = 0
        self._playback_connection = connection_generation
        self._candidate_id = 0
        self._started = False
        self._client_ready = not defer_start
        self._local_prompt: ControllerPrompt | None = None
        self._local_task: asyncio.Task | None = None
        self._output_interruption_task: asyncio.Task | None = None
        self._local_authorization: ReplyAuthorization | None = None
        self._handled_controls = set()
        self._handled_control_revision = -1
        self._control_window_records = 0
        self._continuation_required = False
        self._continuation_source_revision = -1
        self._continuation_ready = False
        self._continuation_prefix = ""
        self._thinking_at: float | None = None
        self._staged_coaching: dict[int, Any] = {}
        self._staged_nonanswers: dict[int, str] = {}
        self._next_answer_basis = AnswerBasis.REPORTED_EXPERIENCE
        self._interaction_state = "connecting"
        self._state_revision = 0
        self._recovery_token: str | None = None
        self._recovery_purpose: str | None = None
        self._recovery_grant: RecoveryGrant | None = None
        self._response_attempts: dict[tuple[int, int], int] = {}
        self._response_rejections: dict[tuple[int, int], int] = {}
        self._playback_wait_epoch: int | None = None
        self._output_interruption_acknowledgments: dict[int, asyncio.Event] | None = None
        self._question_delivery = "pending"
        self.coaching: list[Any] = []
        ledger = TranscriptLedger(
            connection_generation=connection_generation,
            transcript_final_timeout=config.deadlines.transcript_final_timeout,
        )
        super().__init__(
            ledger=ledger, context=LLMContext(), deadlines=config.deadlines, clock=clock, **kwargs
        )
        self.guard = super().create_reply_guard(
            validate_completion=self._validate_reply,
            output_metadata=lambda: {"interview_playback_epoch": self.playback_epoch},
        )
        self._language = language
        self.guard.add_event_handler("on_released", self._released)
        self.add_event_handler("on_reply_rejected", self._reply_rejected)
        self.add_event_handler("on_transcript_recovery", self._transcript_recovery)
        self._register_event_handler("on_session_ended", sync=True)
        self._register_event_handler("on_playback_invalidated", sync=True)
        self._register_event_handler("on_interaction_changed", sync=True)

    def interaction_snapshot(self) -> dict[str, Any]:
        """Return the browser-safe current interaction state without transcript evidence."""
        phase = self.controller.phase
        actions: list[str] = []
        if phase is not InterviewPhase.ENDED:
            actions.append(InteractionAction.END.value)
        if phase in {
            InterviewPhase.INTRODUCTION,
            InterviewPhase.QUESTION,
            InterviewPhase.FOLLOW_UP,
        }:
            actions.extend((InteractionAction.REPEAT.value, InteractionAction.THINKING.value))
        if phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            actions.extend((InteractionAction.EXPLAIN.value, InteractionAction.SKIP.value))
        if self._recovery_token is not None:
            recovery_actions = {
                "response": (InteractionAction.RETRY_RESPONSE,),
                "response_exhausted": (InteractionAction.RESTART_ANSWER,),
                "transcription": (
                    InteractionAction.RETRY_TRANSCRIPTION,
                    InteractionAction.RESTART_ANSWER,
                ),
            }.get(self._recovery_purpose, ())
            actions.extend(action.value for action in recovery_actions)
        return {
            "prompt_revision": self.controller.prompt_revision,
            "state_revision": self._state_revision,
            "question_text": self.controller.active_prompt_text,
            "interaction_state": self._interaction_state,
            "permitted_actions": actions,
            "recovery_token": self._recovery_token,
            "question_delivery": self._question_delivery,
        }

    def apply_interaction_action(self, action: str, *, recovery_token: str | None = None) -> None:
        """Apply a validated browser action through the same controller path as speech.

        The mutation is intentionally non-awaiting.  Prompt emission remains on
        the session's managed task so a lost browser result cannot roll back it.
        """
        try:
            parsed = InteractionAction(action)
        except ValueError as error:
            raise ValueError("unknown interaction action") from error
        if parsed in {
            InteractionAction.RETRY_RESPONSE,
            InteractionAction.RETRY_TRANSCRIPTION,
            InteractionAction.RESTART_ANSWER,
        }:
            if not self._valid_recovery_grant(parsed, recovery_token):
                raise ValueError("recovery action is unavailable")
            if parsed is InteractionAction.RESTART_ANSWER:
                self._restart_pending_answer()
            else:
                if parsed is InteractionAction.RETRY_TRANSCRIPTION:
                    boundary_ids = self.ledger.repairable_boundary_ids
                    if (
                        not boundary_ids
                        or self._stt is None
                        or not all(
                            self._stt.can_repair_boundary(boundary_id)
                            for boundary_id in boundary_ids
                        )
                    ):
                        raise ValueError("transcription repair is unavailable")
                    self._stt.retire_boundaries(boundary_ids)
                    if not self.ledger.begin_repair(boundary_ids):
                        raise ValueError("transcription repair is unavailable")
                    self._continuation_required = True
                    self._continuation_ready = False
                    self._continuation_source_revision = self.ledger.snapshot.source_revision
                    self._continuation_prefix = self.ledger.snapshot.text
                # A recovery action consumes the failure token. A fresh failure
                # receives a new token and cannot be repaired by this click.
                self._recovery_token = None
                self._recovery_purpose = None
                self._recovery_grant = None
                self._interaction_state = "listening"
                if parsed is InteractionAction.RETRY_RESPONSE:
                    self.policy.retry_answer()
                self.changed()
                self._interaction_changed()
            return
        if recovery_token is not None:
            raise ValueError("ordinary actions do not accept recovery tokens")
        if parsed is InteractionAction.EXPLAIN:
            self._explain_current_question()
            return
        if parsed is InteractionAction.REPEAT:
            self.handle_control(VoiceControl.REPEAT)
        elif parsed is InteractionAction.THINKING:
            self.handle_control(VoiceControl.THINKING)
        elif parsed is InteractionAction.SKIP:
            self.handle_control(VoiceControl.SKIP)
        elif parsed is InteractionAction.END:
            self.handle_control(VoiceControl.END)
        self._interaction_changed()

    def _interaction_changed(self) -> None:
        self._state_revision += 1
        if self._running:
            self.create_task(
                self._call_event_handler("on_interaction_changed", self.interaction_snapshot()),
                name="interview-interaction-state",
            )

    def _valid_recovery_grant(self, action: InteractionAction, token: str | None) -> bool:
        grant = self._recovery_grant
        snapshot = self.ledger.snapshot
        if grant is None or token is None or token != grant.token:
            return False
        if (
            grant.connection_generation != self.ledger.connection_generation
            or grant.candidate_turn_id != snapshot.candidate_turn_id
            or grant.source_revision != snapshot.source_revision
        ):
            return False
        if action is InteractionAction.RETRY_RESPONSE:
            return grant.purpose == "response"
        if action is InteractionAction.RETRY_TRANSCRIPTION:
            return grant.purpose == "transcription"
        return action is InteractionAction.RESTART_ANSWER and grant.purpose in {
            "transcription",
            "response_exhausted",
        }

    def _issue_recovery_grant(self, *, purpose: str, failed_dispatch_id: int | None = None) -> None:
        snapshot = self.ledger.snapshot
        token = secrets.token_urlsafe(18)
        self._recovery_token = token
        self._recovery_purpose = purpose
        self._recovery_grant = RecoveryGrant(
            token=token,
            connection_generation=self.ledger.connection_generation,
            candidate_turn_id=snapshot.candidate_turn_id,
            source_revision=snapshot.source_revision,
            purpose=purpose,
            failed_dispatch_id=failed_dispatch_id,
        )

    def _explain_current_question(self) -> None:
        """Use curated local help; unexplained competencies remain unavailable."""
        question = self.controller.current_question
        if question is None:
            raise ValueError("there is no question to explain")
        if self.controller.phase is InterviewPhase.FOLLOW_UP:
            raise ValueError("an explanation is unavailable while answering a follow-up")
        try:
            from .interaction_copy import get_explanation

            explanation = get_explanation(self._language, question.competency)
        except ImportError:
            explanation = None
        if explanation is None:
            raise ValueError("an explanation is unavailable for this question")
        self._invalidate_output()
        prompt = self.controller.offer_help(explanation)
        if prompt is None:
            raise ValueError("explanation is unavailable in the current state")
        self._local_prompt = prompt
        self._interaction_state = "generating"
        self._interaction_changed()
        self.changed()

    def _restart_pending_answer(self) -> None:
        """Discard only the current unaccepted candidate after a bound recovery request."""
        if self.controller.phase not in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            raise ValueError("there is no pending answer to restart")
        self._invalidate_output()
        self._recovery_token = None
        self._recovery_purpose = None
        self._recovery_grant = None
        self._new_candidate()
        self._interaction_state = "listening"
        self._interaction_changed()

    def set_provider(self, provider: ProviderDispatch) -> None:
        """Register the single guarded provider admission path."""
        super().set_provider(provider)

    def attach_sarvam(self, service) -> None:
        """Retain the bound STT adapter for atomic, trusted transcript repair."""
        self._stt = service
        super().attach_sarvam(service)

    def create_reply_guard(self, **kwargs: Any):
        """Return the session's sole guard so release acknowledgments stay connected."""
        if kwargs:
            raise ValueError("the session owns reply validation")
        return self.guard

    def create_llm(self, *, api_key: str, **kwargs: Any):
        """Build the configured text Gemini service with an explicit low thinking level."""
        from .google_llm import InterviewGoogleLLMService

        if "settings" in kwargs:
            raise ValueError("interview provider settings come from InterviewConfig")
        return InterviewGoogleLLMService(coordinator=self, api_key=api_key, **kwargs)

    @property
    def playback_epoch(self) -> int:
        """Speech ownership that survives ordinary candidate/question advancement."""
        if self._playback_connection != self.ledger.connection_generation:
            self._playback_connection = self.ledger.connection_generation
            self._playback_epoch += 1
        return self._playback_epoch

    def playback_started(self, epoch: int) -> None:
        """Mark controller speech active without starting candidate-idle timing."""
        if epoch != self.playback_epoch:
            return
        self._playback_wait_epoch = epoch
        self._question_delivery = "pending"
        self.policy.response_completed()
        self._interaction_state = "speaking"
        self._interaction_changed()

    def playback_stopped(self, epoch: int) -> None:
        """Start candidate quiet-time accounting after the current output is audible."""
        if epoch != self.playback_epoch or self._playback_wait_epoch != epoch:
            return
        self._playback_wait_epoch = None
        self._question_delivery = "complete"
        if self.controller.phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            self.begin_waiting()
            self._interaction_state = "listening"
        self._interaction_changed()

    def playback_failed(self, epoch: int) -> None:
        """Preserve accepted evidence when the current question cannot be delivered."""
        if epoch != self.playback_epoch:
            return
        self._playback_wait_epoch = None
        self._question_delivery = "unconfirmed"
        if self.controller.phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            self.begin_waiting()
        self._interaction_state = "recovery"
        self._interaction_changed()

    def create_tts(self, *, api_key: str | None = None, **kwargs: Any):
        """Build the session's Sarvam Bulbul streaming speech service."""
        import os

        from .sarvam_tts import InterviewSarvamTTSService

        if self._tts is not None:
            if kwargs or api_key is not None:
                raise ValueError("the session already owns a speech service")
            return self._tts
        settings = self.config.providers.tts
        key = api_key or (
            settings.credentials.api_key.get_secret_value()
            if settings.credentials.api_key is not None
            else os.environ.get("SARVAM_API_KEY", "")
        )
        self._tts = InterviewSarvamTTSService(
            api_key=key,
            current_playback_epoch=lambda: self.playback_epoch,
            model=settings.model,
            speaker=settings.speaker,
            language_code=settings.language_code,
            pace=settings.pace,
            **kwargs,
        )
        return self._tts

    def create_rumik_tts(self, **kwargs: Any):
        """Build Rumik speech output bound to this session's interruption epoch."""
        from .rumik import RumikTTSService

        if self._tts is not None:
            if kwargs:
                raise ValueError("the session already owns a Rumik service")
            return self._tts
        if "current_playback_epoch" in kwargs:
            raise ValueError("the session owns playback generation")
        self._tts = RumikTTSService(current_playback_epoch=lambda: self.playback_epoch, **kwargs)
        return self._tts

    def start_interview(self) -> None:
        """Start introduction and the immutable session duration deadline once."""
        if not self._started:
            self._started = True
            self._local_prompt = self.controller.start()
            self._candidate_id += 1
            self.ledger.begin_candidate_turn(
                candidate_turn_id=self._candidate_id,
                question_id=self.controller.current_question.question_id,
            )
            self._interaction_state = "generating"
            self._interaction_changed()
            self.changed()

    def client_ready(self) -> None:
        """Start the introduction once browser media and the data channel are usable."""
        if self._client_ready:
            return
        self._client_ready = True
        if self._running:
            self.start_interview()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Start the interview and discard interrupted model-only transitions."""
        internal_output_interruption = (
            isinstance(frame, InterruptionFrame)
            and self._INTERNAL_OUTPUT_INTERRUPTION_EPOCH in frame.metadata
        )
        if isinstance(frame, (CancelFrame, StopFrame)) or (
            isinstance(frame, InterruptionFrame) and not internal_output_interruption
        ):
            self._playback_epoch += 1
            await self._call_event_handler("on_playback_invalidated", self.playback_epoch)
        if isinstance(frame, InterruptionFrame) and not internal_output_interruption:
            self._staged_coaching.clear()
            self._local_authorization = None
            if self._local_prompt is None:
                self.controller.invalidate_response()
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._client_ready:
            self.start_interview()
        elif isinstance(frame, InterruptionFrame):
            self.changed()

    def speech_started(self) -> None:
        """Invalidate a staged model transition while retaining substantive pending finals."""
        self._playback_epoch += 1
        self._notify_playback_invalidated()
        if self._local_prompt is None:
            self.controller.invalidate_response()
        self._staged_coaching.clear()
        self._local_authorization = None
        self._thinking_at = None
        self._recovery_token = None
        self._recovery_purpose = None
        self._recovery_grant = None
        if self._continuation_required:
            self._continuation_ready = False
        self._interaction_state = "listening"
        self._control_window_records = len(self.ledger.source_records)
        self._interaction_changed()
        super().speech_started()

    def record_final(self, final: SegmentFinal) -> bool:
        """Record immutable finals; controls are classified only after whole-turn readiness."""
        accepted = super().record_final(final)
        if (
            accepted
            and self._continuation_required
            and self.ledger.snapshot.source_revision > self._continuation_source_revision
            and not self._is_short_continuation_acknowledgment(final.text)
        ):
            self._continuation_ready = True
        if accepted and self.ledger.repair_replacement_ready:
            for record in self.ledger.repair_replacement_records:
                self.ledger.resolve_repair_replacement(
                    record.boundary_id, substantive=self._is_substantive_repair(record.final.text)
                )
        self.changed()
        return accepted

    @staticmethod
    def _is_substantive_repair(text: str) -> bool:
        """Keep acknowledgments from silently resolving a failed transcript interval."""
        if parse_voice_control(
            text
        ) is not None or InterviewSession._is_short_continuation_acknowledgment(text):
            return False
        normalized = " ".join(text.casefold().split()).strip(".?!")
        return len(normalized) >= 12 and normalized not in {
            "okay",
            "ok",
            "yes",
            "no",
            "சரி",
            "ठीक है",
        }

    @staticmethod
    def _is_short_continuation_acknowledgment(text: str) -> bool:
        """Keep common brief acknowledgments out of a new semantic model turn."""
        normalized = " ".join(text.casefold().replace(",", " ").split()).strip(".?!")
        return normalized in {
            "okay",
            "ok",
            "okay got it",
            "got it",
            "understood",
            "i understand",
            "i got it",
            "yes",
            "no",
            "சரி",
            "புரிந்தது",
            "ठीक है",
            "समझ गया",
            "समझ गई",
        }

    def handle_control(
        self, control: VoiceControl | str, *, requested_at: float | None = None
    ) -> None:
        """Apply a controller command, canceling old output without accepting command text."""
        if not self._started or self.controller.phase is InterviewPhase.ENDED:
            return
        control = VoiceControl(control)
        if (
            self.controller.phase is InterviewPhase.INTRODUCTION and control is VoiceControl.SKIP
        ) or (self.controller.phase is InterviewPhase.CLOSING and control is not VoiceControl.END):
            return
        self._invalidate_output()
        self._local_prompt = self.controller.handle_control(InterviewControl(control.value))
        if control is VoiceControl.THINKING:
            self._thinking_at = self._clock() if requested_at is None else requested_at
            self.request_thinking(requested_at=self._thinking_at)
        else:
            self._thinking_at = None
            self.policy.response_completed()
        if control is VoiceControl.SKIP:
            self.ledger.recovery_reset()
        self._interaction_state = "thinking" if control is VoiceControl.THINKING else "generating"
        self._interaction_changed()
        self.changed()

    async def _reply_rejected(self, _coordinator, rejection) -> None:
        """Expose one bounded retry token for a still-current rejected response."""
        authorization = rejection.authorization
        if authorization is None or not self.rejection_is_current(authorization):
            return
        key = (authorization.response_token, authorization.transcript_revision)
        rejected = self._response_rejections.get(key, 0) + 1
        self._response_rejections[key] = rejected
        if rejected < 2:
            return
        self._interaction_state = "recovering"
        self._issue_recovery_grant(
            purpose="response_exhausted", failed_dispatch_id=authorization.dispatch_id
        )
        self._interaction_changed()

    def retry_rejected_response(self, rejection) -> bool:
        """Spend at most one immediate retry for the same complete source revision."""
        authorization = rejection.authorization
        if authorization is None or not self.rejection_is_current(authorization):
            return False
        key = (authorization.response_token, authorization.transcript_revision)
        attempts = self._response_attempts.get(key, 0)
        if attempts >= 1:
            return False
        self._response_attempts[key] = attempts + 1
        self._interaction_state = "generating"
        self._interaction_changed()
        return True

    async def _transcript_recovery(self, _gate, _reason: str) -> None:
        """Issue a source-bound visible repair capability for a recoverable gap."""
        if self.ledger.repairable_boundary_ids:
            self._interaction_state = "recovering"
            self._issue_recovery_grant(purpose="transcription")
            self._interaction_changed()

    def _invalidate_output(self) -> None:
        self._playback_epoch += 1
        self._question_delivery = "pending"
        self._notify_playback_invalidated()
        if self._running and (
            self._output_interruption_task is None or self._output_interruption_task.done()
        ):
            self._output_interruption_task = self.create_task(
                self._broadcast_output_interruption(self._playback_epoch),
                name="interview-output-interruption",
            )
        self._authorization = None
        self._local_authorization = None
        self._provider_deadline = None
        self._pending = None
        self._action = None
        self._serial += 1
        self._cancel_requested = True
        self._staged_coaching.clear()

    async def _broadcast_output_interruption(self, epoch: int) -> None:
        """Interrupt queued output while preserving the epoch that owns the replacement prompt."""
        acknowledgments = self._output_interruption_acknowledgments
        acknowledged = asyncio.Event() if acknowledgments is not None else None
        if acknowledged is not None:
            acknowledgments[epoch] = acknowledged
        await self.stop_all_metrics()
        frame = InterruptionFrame()
        frame.metadata[self._INTERNAL_OUTPUT_INTERRUPTION_EPOCH] = epoch
        try:
            await self.broadcast_frame_instance(frame)
            if acknowledged is not None:
                await acknowledged.wait()
        finally:
            if acknowledgments is not None:
                acknowledgments.pop(epoch, None)

    def enable_output_interruption_acknowledgments(self) -> None:
        """Wait for interruption propagation through transport before local replacement speech."""
        self._output_interruption_acknowledgments = {}

    def output_interruption_processed(self, frame: InterruptionFrame) -> None:
        """Acknowledge a controller interruption after it has cleared queued output."""
        pending = self._output_interruption_acknowledgments
        if pending is None:
            return
        epoch = frame.metadata.get(self._INTERNAL_OUTPUT_INTERRUPTION_EPOCH)
        if epoch in pending:
            pending[epoch].set()

    def _notify_playback_invalidated(self) -> None:
        """Publish the new playback epoch without delaying speech-start processing."""
        if self._running:
            self.create_task(
                self._call_event_handler("on_playback_invalidated", self.playback_epoch),
                name="interview-playback-invalidated",
            )

    def _next_wake_deadline(self) -> float | None:
        if self._closed:
            return None
        deadline = (
            self.controller.deadline
            if self.controller.phase not in {InterviewPhase.CLOSING, InterviewPhase.ENDED}
            else None
        )
        if self._local_prompt is not None:
            policy_deadline = self.ledger.next_deadline
        elif self.ledger.recovery_error is not None:
            policy_deadline = self.policy.next_deadline
        else:
            policy_deadline = super()._next_wake_deadline()
        return min((d for d in (deadline, policy_deadline) if d is not None), default=None)

    def start_local_check_in(self, _action: PolicyAction) -> bool:
        """Speak a fixed idle invitation locally instead of waiting for Gemini."""
        if self._local_prompt is not None or self.controller.phase not in {
            InterviewPhase.QUESTION,
            InterviewPhase.FOLLOW_UP,
        }:
            return False
        try:
            from .interaction_copy import get_copy

            text = get_copy(self._language, "checkin")
        except ImportError:
            text = "Take your time. Is there anything you would like to add?"
        prompt = self.controller.offer_help(text)
        if prompt is None:
            return False
        self._local_prompt = prompt
        self._interaction_state = "generating"
        self.changed()
        self._interaction_changed()
        return True

    def _on_wake(self) -> None:
        if self._closed or not self._started:
            return
        closing = self.controller.tick()
        if closing is not None and closing != self._local_prompt:
            self._invalidate_output()
            self._local_prompt = closing
        if self.controller.phase is InterviewPhase.ENDED:
            return
        if self._handle_ready_whole_turn_control():
            return
        if self._local_prompt is not None:
            if not self._speaking and (self._local_task is None or self._local_task.done()):
                self._local_task = self.create_task(
                    self._emit_local(), name="interview-controller-prompt"
                )
            return
        if self.controller.phase not in {InterviewPhase.CLOSING, InterviewPhase.INTRODUCTION}:
            super()._on_wake()

    def _handle_ready_whole_turn_control(self) -> bool:
        """Recognize an exact command only after the new complete turn reaches its pause floor."""
        snapshot = self.ledger.snapshot
        recovery_records = self.ledger.repair_replacement_records
        recovery_window = self.ledger.repair_replacement_ready
        recovery_pending = self.ledger.recovery_error is not None and not recovery_window
        window_records = snapshot.source_records[self._control_window_records :]
        if (
            not self._started
            or (not snapshot.ready and not recovery_window and not recovery_pending)
            or snapshot.source_revision == self._handled_control_revision
            or not self.policy.can_dispatch()
        ):
            return False
        text = (
            " ".join(record.final.text for record in recovery_records)
            if recovery_window
            else " ".join(record.final.text for record in window_records)
        )
        command = parse_voice_control(text)
        if command is None:
            return False
        if (recovery_window or recovery_pending) and command not in {
            VoiceControl.REPEAT,
            VoiceControl.SKIP,
            VoiceControl.END,
        }:
            return False
        if (
            self.controller.phase is InterviewPhase.INTRODUCTION and command is VoiceControl.SKIP
        ) or (self.controller.phase is InterviewPhase.CLOSING and command is not VoiceControl.END):
            self.ledger.commit_exclusions(
                tuple(segment.segment_id for segment in snapshot.segments)
            )
            self._handled_control_revision = snapshot.source_revision
            return False
        segment_ids = (
            tuple(record.segment_id for record in recovery_records)
            if recovery_window
            else tuple(record.segment_id for record in window_records)
        )
        if not segment_ids or (
            not recovery_window and not self.ledger.commit_exclusions(segment_ids)
        ):
            return False
        self._handled_controls.update(segment_ids)
        self._handled_control_revision = snapshot.source_revision
        requested_at = max(
            (self.ledger.segment_closed_at(segment_id) or 0 for segment_id in segment_ids),
            default=None,
        )
        self.handle_control(command, requested_at=requested_at)
        return True

    def _dispatch_is_ready(self) -> bool:
        return (
            (not self._continuation_required or self._continuation_ready)
            and self._local_prompt is None
            and self.controller.current_prompt is None
            and self.controller.phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}
            and (self.controller.deadline is None or self._clock() < self.controller.deadline)
            and super()._dispatch_is_ready()
        )

    def _current_answer(self) -> AcceptedAnswer:
        snapshot = self.ledger.snapshot
        if self._continuation_required and not self._continuation_ready:
            raise ValueError("a substantive continuation is required after help")
        if not snapshot.ready or not snapshot.text or snapshot.candidate_turn_id is None:
            raise ValueError("no complete substantive candidate answer")
        return AcceptedAnswer(
            snapshot.candidate_turn_id,
            snapshot.question_id,
            tuple(final.segment_id for final in snapshot.segments),
            snapshot.text,
            self._next_answer_basis,
        )

    def _fresh_continuation_start(self) -> int | None:
        """Return the first derived character contributed after a guarded help reply."""
        if not self._continuation_required:
            return None
        text = self.ledger.snapshot.text
        if not text.startswith(self._continuation_prefix):
            return 0
        start = len(self._continuation_prefix)
        while start < len(text) and text[start].isspace():
            start += 1
        return start

    def _prepare_dispatch(self, frame: LLMContextFrame) -> None:
        # Provider IDs remain greater than every controller-local response identity.
        super()._prepare_dispatch(frame)
        if self._authorization.reply_kind is ReplyKind.FOLLOW_UP:
            answer = self._current_answer()
            question = self.controller.current_question
            rubric = (self.config.question_rubric[question.index],)
            prompt = build_coaching_prompt(
                role=self.config.role,
                difficulty=self.config.difficulty,
                duration_minutes=self.config.duration_minutes,
                current_question=self.controller.active_prompt_text or question.text,
                rubric=rubric,
                accepted_answers=self.controller.accepted_answers,
                current_answer=answer,
                require_follow_up=self.controller.phase is InterviewPhase.QUESTION,
                require_substantive_continuation=self._continuation_required,
                fresh_continuation_start=self._fresh_continuation_start(),
            )
            frame.context.add_message({"role": "developer", "content": prompt})
            self._bound_provider_context(frame.context)

    def _bound_provider_context(self, context: LLMContext) -> None:
        """Keep the complete current source while dropping only older whole messages."""
        messages = context.messages
        encoded = lambda items: len(json.dumps(items, ensure_ascii=False).encode("utf-8"))
        if encoded(messages) <= self._MAX_PROVIDER_CONTEXT_BYTES:
            return
        # The transcript gate appends the complete current candidate as its last
        # user message. The coaching instruction is the last developer message.
        required = [
            message for message in messages if message.get("role") in {"user", "developer"}
        ][-2:]
        if encoded(required) > self._MAX_PROVIDER_CONTEXT_BYTES:
            raise ValueError("current interview source exceeds the provider context limit")
        retained = list(required)
        for message in reversed(messages[:-2]):
            candidate = [message, *retained]
            if encoded(candidate) > self._MAX_PROVIDER_CONTEXT_BYTES:
                break
            retained = candidate
        context.messages[:] = retained

    def _validate_reply(
        self, authorization: ReplyAuthorization, parsed: ParsedCompletion
    ) -> ParsedCompletion:
        if authorization == self._local_authorization:
            return parsed
        if authorization.reply_kind is ReplyKind.CHECK_IN:
            if parsed.status is CompletionStatus.COMPLETE:
                return replace(
                    parsed,
                    text=(
                        "அவசரம் இல்ல. இன்னும் ஏதாவது சொல்ல விரும்புறீங்களா?"
                        if self._language == "tanglish"
                        else "Take your time. Is there anything you would like to add?"
                    ),
                )
            return parsed
        if parsed.status is CompletionStatus.INCOMPLETE:
            return parsed
        if not self.authorization_is_current(authorization):
            raise ValueError("stale interview reply")
        interaction = parse_interaction_reply(parsed.text)
        if interaction is not None:
            return self._validate_nonanswer_reply(authorization, parsed, interaction)
        answer = self._current_answer()
        question = self.controller.current_question
        if question is None:
            raise ValueError("no active interview question")
        reply = parse_coaching_reply(parsed.text)
        if self._continuation_required and reply.continuation == "acknowledgment":
            if reply.evidence or len(reply.speak) > 220 or reply.speak.count("?") != 1:
                raise ValueError("a continuation acknowledgment must ask one brief clarification")
            return replace(parsed, text=reply.speak, accept_answer=False)
        validated = validate_coaching_reply(
            reply,
            rubric=(self.config.question_rubric[question.index],),
            accepted_answers=self.controller.accepted_answers,
            current_answer=answer,
            require_follow_up=self.controller.phase is InterviewPhase.QUESTION,
            require_substantive_continuation=self._continuation_required,
            fresh_continuation_start=self._fresh_continuation_start(),
        )
        try:
            self.controller.stage_answer(
                answer,
                authorization.dispatch_id,
                validated.speak,
                evidence=[
                    item.quote
                    for item in validated.evidence
                    if item.candidate_turn_id == answer.candidate_turn_id
                ],
            )
        except RuntimeError as error:
            raise ValueError("controller refused this response transition") from error
        self._staged_coaching[authorization.dispatch_id] = validated
        return replace(parsed, text=validated.speak)

    def _validate_nonanswer_reply(self, authorization, parsed, interaction) -> ParsedCompletion:
        """Narrow a provider reply to help before guard decision or speech release."""
        if interaction.kind is InteractionKind.ANSWER:
            raise ValueError("typed answer replies must use the evidence contract")
        snapshot = self.ledger.snapshot
        source = snapshot.text
        if interaction.kind is InteractionKind.MIXED_HELP:
            answer_text = answer_text_from_spans(source, interaction.spans)
            if not answer_text:
                raise ValueError("mixed help requires retained answer source")
            if any(span.disposition is SourceDisposition.UNCERTAIN for span in interaction.spans):
                raise ValueError("mixed help requires clarification for uncertain source")
            project = getattr(self.ledger, "raw_spans_for_derived_spans", None)
            if project is None:
                raise ValueError("source provenance is unavailable")
            help_spans = tuple(
                span for span in interaction.spans if span.disposition is SourceDisposition.HELP
            )
            if not help_spans:
                raise ValueError("mixed help requires a help source span")
            raw_help_spans = project(
                snapshot.source_revision, snapshot.disposition_revision, help_spans
            )
            if raw_help_spans is None:
                raise ValueError("help source spans are stale or unavailable")
            self._staged_coaching[authorization.dispatch_id] = (
                "span_exclusions",
                snapshot.source_revision,
                raw_help_spans,
            )
        if interaction.kind is InteractionKind.NO_EXAMPLE:
            question = self.controller.current_question
            if question is None:
                raise ValueError("no active question for a practice scenario")
            try:
                from .interaction_copy import get_scenario

                scenario = get_scenario(self._language, question.competency)
            except ImportError:
                scenario = None
            if scenario is None:
                raise ValueError("a practice scenario is unavailable")
            if not self.controller.can_offer_scenario():
                raise ValueError("a scenario has already been offered; repeat or skip instead")
            self._staged_nonanswers[authorization.dispatch_id] = scenario
            return replace(parsed, text=scenario, accept_answer=False)
        if interaction.kind in {InteractionKind.EXPLAIN, InteractionKind.CLARIFY} and source:
            project = getattr(self.ledger, "raw_spans_for_derived_spans", None)
            if project is None:
                raise ValueError("source provenance is unavailable")
            raw_help_spans = project(
                snapshot.source_revision,
                snapshot.disposition_revision,
                (SourceSpan(start=0, end=len(source), disposition=SourceDisposition.HELP),),
            )
            if raw_help_spans is None:
                raise ValueError("help source spans are stale or unavailable")
            self._staged_coaching[authorization.dispatch_id] = (
                "span_exclusions",
                snapshot.source_revision,
                raw_help_spans,
            )
        return replace(parsed, text=interaction.speak, accept_answer=False)

    def _owns_policy_response(self, authorization: ReplyAuthorization) -> bool:
        return authorization != self._local_authorization

    def authorization_is_current(self, authorization: ReplyAuthorization) -> bool:
        """Check local prompt or model authorization against speech, session, and duration."""
        if authorization == self._local_authorization:
            return (
                not self._closed
                and not self._speaking
                and authorization.response_token == self.ledger.response_token
                and authorization.dispatch_id == self._serial
                and self.controller.phase is not InterviewPhase.ENDED
                and (
                    authorization.reply_kind is ReplyKind.CLOSING
                    or self.controller.deadline is None
                    or self._clock() < self.controller.deadline
                )
            )
        return (
            self.controller.phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}
            and (self.controller.deadline is None or self._clock() < self.controller.deadline)
            and super().authorization_is_current(authorization)
        )

    async def _emit_local(self) -> None:
        try:
            if self._output_interruption_task is not None:
                await self._output_interruption_task
                self._output_interruption_task = None
            prompt = self._local_prompt
            if prompt is None or self._closed or self._speaking:
                return
            self._serial = max(self._serial + 1, prompt.response_id)
            snapshot = self.ledger.snapshot
            authorization = ReplyAuthorization(
                self.ledger.response_token,
                self._serial,
                snapshot.candidate_turn_id or 0,
                prompt.reply_kind,
                snapshot.revision,
                False,
            )
            self._authorization = self._local_authorization = authorization
            metadata = {
                "interview_response_token": authorization.response_token,
                "interview_dispatch_id": authorization.dispatch_id,
            }
            for frame in (
                LLMFullResponseStartFrame(),
                LLMTextFrame("● " + prompt.text),
                LLMFullResponseEndFrame(),
            ):
                if not self.authorization_is_current(authorization):
                    return
                frame.metadata.update(metadata)
                await self.guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        finally:
            if self._local_task is asyncio.current_task():
                self._local_task = None
            self.changed()

    async def _released(self, _guard, decision: ReplyDecision) -> None:
        authorization = decision.authorization
        if not self.authorization_is_current(authorization):
            return
        if authorization == self._local_authorization:
            prompt = self._local_prompt
            self._local_prompt = None
            next_prompt = self.controller.release(prompt.response_id)
            self.context.add_message({"role": "assistant", "content": decision.completion.text})
            self._local_authorization = None
            if next_prompt is not None:
                self._local_prompt = next_prompt
                self._serial = max(self._serial, next_prompt.response_id)
            elif self.controller.phase is InterviewPhase.ENDED:
                await self._call_event_handler("on_session_ended", self.controller)
                await self.push_frame(EndFrame())
            elif (
                prompt.reply_kind is ReplyKind.QUESTION
                or self.controller.expected_candidate_turn_id is None
            ):
                self._new_candidate(wait_for_playback=True)
                if self._thinking_at is not None:
                    self.request_thinking(requested_at=self._thinking_at)
            elif self._thinking_at is None:
                # Repeats, explanations, and local check-ins retain the same
                # candidate.  Their idle interval starts after output ends.
                self._playback_wait_epoch = self.playback_epoch
        elif decision.accept_answer:
            answer = self._current_answer()
            next_prompt = self.controller.release(authorization.dispatch_id)
            if answer not in self.controller.accepted_answers:
                return
            self._continuation_required = False
            self._continuation_ready = False
            self._continuation_prefix = ""
            validated = self._staged_coaching.pop(authorization.dispatch_id)
            self.coaching.append(validated)
            self.context.add_message({"role": "user", "content": answer.transcript})
            self.context.add_message({"role": "assistant", "content": decision.completion.text})
            self._local_prompt = next_prompt
            if next_prompt is not None:
                self._serial = max(self._serial, next_prompt.response_id)
            else:
                self._new_candidate(wait_for_playback=True)
        else:
            staged_prompt = self._staged_nonanswers.pop(authorization.dispatch_id, None)
            staged = self._staged_coaching.pop(authorization.dispatch_id, None)
            if staged_prompt is not None:
                prompt = self.controller.offer_scenario(staged_prompt)
                if prompt is None:
                    return
                self.controller.release(prompt.response_id)
                self._new_candidate(wait_for_playback=True)
            elif isinstance(staged, tuple) and staged[0] == "span_exclusions":
                commit = getattr(self.ledger, "commit_span_exclusions", None)
                if commit is not None:
                    try:
                        committed = commit(staged[1], staged[2])
                        if committed and staged[2]:
                            self._continuation_required = True
                            self._continuation_source_revision = (
                                self.ledger.snapshot.source_revision
                            )
                            self._continuation_ready = False
                            self._continuation_prefix = self.ledger.snapshot.text
                    except (TypeError, ValueError):
                        pass
            self._interaction_state = "speaking"
        self.changed()
        self._interaction_changed()

    def _new_candidate(self, *, wait_for_playback: bool = False) -> None:
        question = self.controller.current_question
        self.ledger.recovery_reset()
        self._continuation_required = False
        self._continuation_source_revision = -1
        self._continuation_ready = False
        self._continuation_prefix = ""
        self._recovery_token = None
        self._recovery_purpose = None
        self._recovery_grant = None
        self._candidate_id += 1
        self._next_answer_basis = (
            AnswerBasis.HYPOTHETICAL
            if self.controller.active_scenario is not None
            else AnswerBasis.REPORTED_EXPERIENCE
        )
        self.ledger.begin_candidate_turn(
            candidate_turn_id=self._candidate_id, question_id=question.question_id
        )
        self.controller.expect_candidate_turn(self._candidate_id)
        self._handled_controls.clear()
        self._handled_control_revision = -1
        if wait_for_playback:
            self._playback_wait_epoch = self.playback_epoch
        else:
            self.begin_waiting()

    async def close(self) -> None:
        """Cancel controller output alongside transcript/provider tasks during teardown."""
        await super().close()
        if self._local_task is not None and self._local_task is not asyncio.current_task():
            await self.cancel_task(self._local_task)
        self._local_task = None
        if (
            self._output_interruption_task is not None
            and self._output_interruption_task is not asyncio.current_task()
        ):
            await self.cancel_task(self._output_interruption_task)
        self._output_interruption_task = None
