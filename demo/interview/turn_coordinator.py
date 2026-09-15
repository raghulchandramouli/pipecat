"""Coordinate interview timing, transcript admission, and reply authorization."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .config import InterviewDeadlines
from .contracts import CompletionStatus, ReplyKind
from .reply_guard import ReplyAuthorization, ReplyDecision, ReplyGuardProcessor, ReplyRejection
from .sarvam_events import InterviewSarvamRealtimeSTTService, ManualBoundaryEvent
from .transcript_gate import InterviewUserAggregator, TranscriptLedgerProcessor
from .turn_policy import PolicyAction, PolicyActionKind, TurnPolicy


class InterviewTurnCoordinator(TranscriptLedgerProcessor):
    """Own one timer schedule and the purposes allowed for each model response."""

    strict_replies = True

    def __init__(
        self,
        *,
        deadlines: InterviewDeadlines | None = None,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        """Initialize policy using the same monotonic clock as transcript deadlines."""
        super().__init__(clock=clock, **kwargs)
        self.deadlines = deadlines or InterviewDeadlines()
        self.policy = TurnPolicy(deadlines=self.deadlines, clock=clock)
        self._provider_deadline: float | None = None
        self._action: PolicyAction | None = None
        self._authorization: ReplyAuthorization | None = None
        self._terminal_authorization: ReplyAuthorization | None = None
        self._speaking = False
        self._stopped = False
        self._register_event_handler("on_reply_decision", sync=True)
        self._register_event_handler("on_reply_rejected", sync=True)
        self._register_event_handler("on_policy_action", sync=True)

    def speech_started(self) -> None:
        """Invalidate all response authorization while preserving candidate segments."""
        if not self._speaking:
            self.policy.speech_started()
        self._speaking = True
        self._stopped = False
        self._action = None
        self._authorization = None
        self._provider_deadline = None
        super().speech_started()

    def begin_waiting(self) -> None:
        """Arm quiet-time notifications when the controller finishes asking a question."""
        self.policy.begin_waiting()
        self._authorization = None
        self._provider_deadline = None
        self._pending = None
        self._action = None
        self._serial += 1
        self._cancel_requested = True
        self.changed()

    def speech_stopped(self) -> None:
        """Anchor the pause floor once per actual speech boundary."""
        if not self._stopped:
            self.policy.speech_stopped()
        self._speaking = False
        self._stopped = True
        self.changed()

    def request_thinking(self, *, requested_at: float | None = None) -> None:
        """Grant thinking time from the controller's explicit request timestamp."""
        self.policy.request_thinking(requested_at=requested_at)
        self._action = None
        self._pending = None
        self._authorization = None
        self._provider_deadline = None
        self._serial += 1
        self._cancel_requested = True
        self.changed()

    def attach_sarvam(self, service: InterviewSarvamRealtimeSTTService) -> None:
        """Listen for successful local speech stops in addition to transcript events."""
        super().attach_sarvam(service)

        @service.event_handler("on_manual_boundary")
        def stopped(_service, event):
            if (
                event.connection_generation == self.ledger.connection_generation
                and event.event is ManualBoundaryEvent.SPEECH_END
                and event.sent
            ):
                self.speech_stopped()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Observe VAD stops without treating early final transcripts as turn completion."""
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self.speech_stopped()
        if isinstance(frame, InterruptionFrame):
            self._authorization = None
            self._provider_deadline = None
            self._pending = None
            self._action = None
            self._serial += 1
            self._cancel_requested = True
            self.policy.response_completed()
            self.changed()
        await super().process_frame(frame, direction)

    def request_dispatch(
        self, frame: LLMContextFrame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        """Treat framework inference triggers as wake-ups; only policy grants purpose."""
        self.changed()

    def _next_wake_deadline(self) -> float | None:
        if self._closed or self.ledger.recovery_error:
            return self.ledger.next_deadline
        values = [self.ledger.next_deadline, self.policy.next_deadline, self._provider_deadline]
        return min((value for value in values if value is not None), default=None)

    def _on_wake(self) -> None:
        if self._closed or self.ledger.recovery_error:
            return
        if self._provider_deadline is not None and self._clock() >= self._provider_deadline:
            authorization = self._authorization
            self._provider_deadline = None
            self._authorization = None
            self._pending = None
            self._action = None
            self._serial += 1
            self._cancel_requested = True
            self.policy.incomplete()
            self.create_task(self._report_timeout(authorization), name="interview-response-timeout")
        action = self.policy.poll()
        if action is not None:
            # A due check-in supersedes an undelivered probe; it cannot accept an answer.
            self._action = action
            self.create_task(
                self._call_event_handler("on_policy_action", action), name="interview-policy-event"
            )
            super().request_dispatch(LLMContextFrame(self.context))

    def _dispatch_is_ready(self) -> bool:
        return self.ledger.readiness and self.policy.can_dispatch() and self._action is not None

    def _prepare_dispatch(self, frame: LLMContextFrame) -> None:
        action = self._action
        if action is None:
            raise RuntimeError("Interview dispatch has no policy authorization")
        snapshot = self.ledger.snapshot
        self._authorization = ReplyAuthorization(
            response_token=frame.metadata["interview_response_token"],
            dispatch_id=frame.metadata["interview_dispatch_id"],
            candidate_turn_id=snapshot.candidate_turn_id,
            reply_kind=action.reply_kind,
            transcript_revision=snapshot.revision,
            answer_allowed=(action.kind is PolicyActionKind.SEMANTIC_PROBE and bool(snapshot.text)),
        )
        self._terminal_authorization = None
        frame.metadata["interview_reply_kind"] = action.reply_kind.value
        frame.metadata["interview_policy_reason"] = action.reason
        if action.reply_kind is ReplyKind.CHECK_IN:
            frame.context.add_message(
                {
                    "role": "developer",
                    "content": "This response is a check-in only. Start with ● and gently invite "
                    "the candidate to continue, in one short sentence. Do not evaluate, grade, "
                    "advance questions, or treat the pending answer as accepted.",
                }
            )
        self.policy.dispatched()
        self._provider_deadline = self._clock() + self.deadlines.user_turn_stop_timeout
        self._action = None

    async def _report_timeout(self, authorization: ReplyAuthorization | None) -> None:
        await self.push_error("Interview response watchdog expired; pending answer retained")
        await self._call_event_handler(
            "on_reply_rejected", ReplyRejection(authorization, "provider_timeout")
        )

    def _dispatch_failed(self) -> None:
        if self._provider_task is asyncio.current_task():
            self._provider_deadline = None
            self._authorization = None
            self.policy.incomplete()

    def lookup_authorization(self, metadata: dict[str, Any]) -> ReplyAuthorization | None:
        """Resolve trusted admission records without accepting model-supplied reply kinds."""
        authorization = self._authorization
        if authorization is None:
            return None
        if (
            metadata.get("interview_response_token") == authorization.response_token
            and metadata.get("interview_dispatch_id") == authorization.dispatch_id
        ):
            return authorization
        return None

    def authorization_is_current(self, authorization: ReplyAuthorization) -> bool:
        """Validate response and transcript identity immediately before output or mutation."""
        return (
            authorization == self._authorization
            and self.response_is_current(authorization.response_token, authorization.dispatch_id)
            and authorization.transcript_revision == self.ledger.snapshot.revision
            and authorization.candidate_turn_id == self.ledger.snapshot.candidate_turn_id
        )

    def create_reply_guard(self, **kwargs: Any) -> ReplyGuardProcessor:
        """Create the required pre-TTS guard and connect its validated decisions to timers."""
        guard = ReplyGuardProcessor(
            lookup_authorization=self.lookup_authorization,
            is_current=self.authorization_is_current,
            **kwargs,
        )

        @guard.event_handler("on_decision")
        async def decision(_guard, result: ReplyDecision):
            if (
                not self._owns_policy_response(result.authorization)
                or not self.authorization_is_current(result.authorization)
                or result.authorization == self._terminal_authorization
            ):
                return
            self._terminal_authorization = result.authorization
            self._provider_deadline = None
            if result.completion.status is CompletionStatus.INCOMPLETE:
                self.policy.incomplete(long=result.completion.long_wait)
            else:
                self.policy.response_completed()
            self.changed()
            await self._call_event_handler("on_reply_decision", result)

        @guard.event_handler("on_rejected")
        async def rejected(_guard, result: ReplyRejection):
            if (
                result.authorization is None
                or not self._owns_policy_response(result.authorization)
                or not self.authorization_is_current(result.authorization)
                or result.authorization == self._terminal_authorization
            ):
                return
            self._terminal_authorization = result.authorization
            self._provider_deadline = None
            self.policy.incomplete()
            self._authorization = None
            self.changed()
            await self._call_event_handler("on_reply_rejected", result)

        return guard

    def _owns_policy_response(self, authorization: ReplyAuthorization) -> bool:
        """Distinguish timed inference from application-owned local prompt delivery."""
        return True

    def create_user_aggregator(self, **kwargs: Any) -> InterviewUserAggregator:
        """Use VAD start and external stop with response timers owned by this coordinator."""
        params = LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()], stop=[ExternalUserTurnStopStrategy()]
            ),
            user_turn_stop_timeout=float("inf"),
            user_idle_timeout=0,
            **kwargs,
        )
        return InterviewUserAggregator(self.context, coordinator=self, params=params)
