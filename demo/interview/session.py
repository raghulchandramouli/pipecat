"""End-to-end controller integration at the transcript and guarded text boundaries."""

from __future__ import annotations

import asyncio
import json
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

from .coaching import build_coaching_prompt, parse_coaching_reply, validate_coaching_reply
from .config import InterviewConfig
from .contracts import AcceptedAnswer, CompletionStatus, ReplyKind, SegmentFinal
from .controller import ControllerPrompt, InterviewControl, InterviewController, InterviewPhase
from .ledger import TranscriptLedger
from .reply_guard import ParsedCompletion, ReplyAuthorization, ReplyDecision
from .transcript_gate import ProviderDispatch
from .turn_coordinator import InterviewTurnCoordinator
from .voice_controls import VoiceControl, parse_voice_control


class InterviewSession(InterviewTurnCoordinator):
    """Connect question progression to validated transcripts and guarded reply release."""

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
        self._thinking_at: float | None = None
        self._staged_coaching: dict[int, Any] = {}
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
        self._register_event_handler("on_session_ended", sync=True)
        self._register_event_handler("on_playback_invalidated", sync=True)

    def set_provider(self, provider: ProviderDispatch) -> None:
        """Handle explicit lack of experience through the normal authorized reply guard."""

        async def dispatch(frame: LLMContextFrame, direction: FrameDirection) -> None:
            text = " ".join(self.ledger.snapshot.text.split()).strip(" .!?।")
            no_experience = text in {
                "இல்ல நான் இப்ப வரைக்கும் வாழ்க்கையில ஒண்ணுமே பண்ணதில்ல",
                "நான் இப்ப வரைக்கும் வாழ்க்கையில ஒண்ணுமே பண்ணதில்ல",
                "எனக்கு அனுபவம் இல்லை",
                "எனக்கு அனுபவம் இல்ல",
                "நான் எதுவும் பண்ணதில்லை",
                "நான் எதுவும் பண்ணதில்ல",
            }
            if (
                self._language != "tanglish"
                or not no_experience
                or self._authorization is None
                or self._authorization.reply_kind is not ReplyKind.FOLLOW_UP
            ):
                await provider(frame, direction)
                return
            answer = self._current_answer()
            quote = answer.transcript.strip()
            question = self.controller.current_question
            follow_up = self.controller.phase is InterviewPhase.QUESTION
            speak = (
                f"“{quote}”ன்னு சொன்னீங்க. பரவாயில்லை. வீட்டுல ஒருத்தர் உதவி கேட்டா, நீங்க என்ன செய்வீங்க?"
                if follow_up
                else "பரவாயில்லை. நாம அடுத்த விஷயத்தைப் பத்திப் பேசலாம்."
            )
            payload = {
                "speak": speak,
                "evidence": [
                    {
                        "candidate_turn_id": answer.candidate_turn_id,
                        "quote": quote,
                        "competency": question.competency,
                        "observation": "The candidate reports no prior experience.",
                        "suggestion": "Offer a simple everyday situation.",
                        "uncertainty": "No behaviour can be inferred from this statement alone.",
                    }
                ],
            }
            for outgoing in (
                LLMFullResponseStartFrame(),
                LLMTextFrame("● " + json.dumps(payload, ensure_ascii=False)),
                LLMFullResponseEndFrame(),
            ):
                outgoing.metadata.update(frame.metadata)
                await self.guard.process_frame(outgoing, direction)

        super().set_provider(dispatch)

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
        if isinstance(frame, (InterruptionFrame, CancelFrame, StopFrame)):
            self._playback_epoch += 1
            await self._call_event_handler("on_playback_invalidated", self.playback_epoch)
        if isinstance(frame, InterruptionFrame):
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
        super().speech_started()

    def record_final(self, final: SegmentFinal) -> bool:
        """Exclude exact command-only final segments before candidate evidence is assembled."""
        accepted = super().record_final(final)
        if not accepted or not self._started or final.segment_id in self._handled_controls:
            return accepted
        command = parse_voice_control(final.text)
        if command is not None:
            self._handled_controls.add(final.segment_id)
            self.ledger.exclude_control_segment(final.segment_id)
            self.handle_control(
                command, requested_at=self.ledger.segment_closed_at(final.segment_id)
            )
        self.changed()
        return accepted

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
        self.changed()

    def _invalidate_output(self) -> None:
        self._playback_epoch += 1
        self._notify_playback_invalidated()
        if self._running and (
            self._output_interruption_task is None or self._output_interruption_task.done()
        ):
            self._output_interruption_task = self.create_task(
                self.broadcast_interruption(), name="interview-output-interruption"
            )
        self._authorization = None
        self._local_authorization = None
        self._provider_deadline = None
        self._pending = None
        self._action = None
        self._serial += 1
        self._cancel_requested = True
        self._staged_coaching.clear()

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
        policy_deadline = (
            super()._next_wake_deadline()
            if self._local_prompt is None
            else self.ledger.next_deadline
        )
        return min((d for d in (deadline, policy_deadline) if d is not None), default=None)

    def _on_wake(self) -> None:
        if self._closed or not self._started:
            return
        closing = self.controller.tick()
        if closing is not None and closing != self._local_prompt:
            self._invalidate_output()
            self._local_prompt = closing
        if self.controller.phase is InterviewPhase.ENDED:
            return
        if self._local_prompt is not None:
            if not self._speaking and (self._local_task is None or self._local_task.done()):
                self._local_task = self.create_task(
                    self._emit_local(), name="interview-controller-prompt"
                )
            return
        if self.controller.phase not in {InterviewPhase.CLOSING, InterviewPhase.INTRODUCTION}:
            super()._on_wake()

    def _dispatch_is_ready(self) -> bool:
        return (
            self._local_prompt is None
            and self.controller.current_prompt is None
            and self.controller.phase in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}
            and (self.controller.deadline is None or self._clock() < self.controller.deadline)
            and super()._dispatch_is_ready()
        )

    def _current_answer(self) -> AcceptedAnswer:
        snapshot = self.ledger.snapshot
        if not snapshot.ready or not snapshot.text or snapshot.candidate_turn_id is None:
            raise ValueError("no complete substantive candidate answer")
        return AcceptedAnswer(
            snapshot.candidate_turn_id,
            snapshot.question_id,
            tuple(final.segment_id for final in snapshot.segments),
            snapshot.text,
        )

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
                current_question=question.text,
                rubric=rubric,
                accepted_answers=self.controller.accepted_answers,
                current_answer=answer,
                require_follow_up=self.controller.phase is InterviewPhase.QUESTION,
            )
            frame.context.add_message({"role": "developer", "content": prompt})

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
        answer = self._current_answer()
        question = self.controller.current_question
        if question is None:
            raise ValueError("no active interview question")
        validated = validate_coaching_reply(
            parse_coaching_reply(parsed.text),
            rubric=(self.config.question_rubric[question.index],),
            accepted_answers=self.controller.accepted_answers,
            current_answer=answer,
            require_follow_up=self.controller.phase is InterviewPhase.QUESTION,
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
                self._new_candidate()
                if self._thinking_at is not None:
                    self.request_thinking(requested_at=self._thinking_at)
            elif self._thinking_at is None:
                self.begin_waiting()
        elif decision.accept_answer:
            answer = self._current_answer()
            next_prompt = self.controller.release(authorization.dispatch_id)
            if answer not in self.controller.accepted_answers:
                return
            validated = self._staged_coaching.pop(authorization.dispatch_id)
            self.coaching.append(validated)
            self.context.add_message({"role": "user", "content": answer.transcript})
            self.context.add_message({"role": "assistant", "content": decision.completion.text})
            self._local_prompt = next_prompt
            if next_prompt is not None:
                self._serial = max(self._serial, next_prompt.response_id)
            else:
                self._new_candidate()
        self.changed()

    def _new_candidate(self) -> None:
        question = self.controller.current_question
        self.ledger.recovery_reset()
        self._candidate_id += 1
        self.ledger.begin_candidate_turn(
            candidate_turn_id=self._candidate_id, question_id=question.question_id
        )
        self.controller.expect_candidate_turn(self._candidate_id)
        self._handled_controls.clear()
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
