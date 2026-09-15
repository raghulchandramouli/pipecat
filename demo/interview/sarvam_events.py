"""Application-level Sarvam final-transcript events for interview coordination.

Sarvam's manual ``speech_start`` and ``speech_end`` messages do not carry a
client correlation value. This extension therefore keeps local VAD boundaries
separate from provider utterance IDs: an application must explicitly bind an
observed ``utterance_idx`` to a recorded local boundary. It never infers that
binding from message arrival order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
from pipecat.services.websocket_service import ReportErrorCallback

from .contracts import SegmentFinal, SegmentFinalOutcome, SegmentId


class ManualBoundaryEvent(StrEnum):
    """Manual Sarvam boundary event sent by the application."""

    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


class ConnectionEventKind(StrEnum):
    """Connection lifecycle event observed by the application extension."""

    CONNECT_FAILED = "connect_failed"
    RECEIVE_FAILED = "receive_failed"
    RECEIVE_ENDED = "receive_ended"
    PROVIDER_ERROR = "provider_error"
    INTENTIONAL_SHUTDOWN = "intentional_shutdown"


@dataclass(frozen=True)
class SarvamManualBoundary:
    """A locally owned manual boundary and the result of sending it.

    Parameters:
        connection_generation: STT connection generation fixed for this service.
        boundary_id: Local boundary identifier. It is not a provider utterance ID.
        candidate_turn_id: Candidate turn assigned before local speech start.
        event: Manual event sent to Sarvam.
        sent: Whether the websocket accepted the boundary message.
    """

    connection_generation: int
    boundary_id: int
    candidate_turn_id: int
    event: ManualBoundaryEvent
    sent: bool


@dataclass(frozen=True)
class SarvamUtteranceBinding:
    """Explicit association of an observed provider ID with a local boundary.

    Parameters:
        boundary_id: Local boundary whose owner was captured at speech start.
        segment_id: Provider identity scoped to the connection.
        candidate_turn_id: Captured candidate owner.
    """

    boundary_id: int
    segment_id: SegmentId
    candidate_turn_id: int


@dataclass(frozen=True)
class SarvamFinalObservation:
    """Raw provider final observed before Pipecat emits its transcript frame.

    Parameters:
        connection_generation: STT connection generation fixed for this service.
        utterance_idx: Numeric Sarvam ID, if the provider supplied a valid one.
        outcome: Normalized text or empty terminal outcome.
        text: Stripped transcript text for a text outcome.
        raw_message: Provider message retained for explicit correlation handling.
    """

    connection_generation: int
    utterance_idx: int | None
    outcome: SegmentFinalOutcome
    text: str
    raw_message: dict[str, Any]


@dataclass(frozen=True)
class SarvamCorrelationFailure:
    """A final or requested binding that cannot be correlated safely.

    Parameters:
        connection_generation: STT connection generation fixed for this service.
        reason: Stable explanation of why correlation was refused.
        utterance_idx: Provider ID involved, if valid and supplied.
        boundary_id: Local boundary involved, if supplied.
    """

    connection_generation: int
    reason: str
    utterance_idx: int | None = None
    boundary_id: int | None = None


@dataclass(frozen=True)
class SarvamConnectionEvent:
    """Connection state that requires application recovery or shutdown handling.

    Parameters:
        connection_generation: STT connection generation fixed for this service.
        kind: Observed connection lifecycle event.
        message: Provider or websocket diagnostic message.
    """

    connection_generation: int
    kind: ConnectionEventKind
    message: str


@dataclass(frozen=True)
class _BoundaryRecord:
    """Stored immutable owner for a local boundary.

    Parameters:
        boundary_id: Local boundary identifier.
        candidate_turn_id: Candidate turn captured at speech start.
    """

    boundary_id: int
    candidate_turn_id: int


class InterviewSarvamRealtimeSTTService(SarvamRealtimeSTTService):
    """Sarvam realtime STT with explicit interview segment-correlation events.

    Event handlers for final observations and segment finals run synchronously so
    a small ledger update can happen before the base service pushes a transcript
    frame. Handlers must remain fast; they must not await provider work or a
    missing final.

    Args:
        connection_generation: Non-negative generation supplied by the session
            when it creates this non-reconnecting service instance.
        max_unbound_finals: Maximum raw finals retained while the application
            awaits an explicit provider-ID-to-boundary binding.
        **kwargs: Arguments passed to :class:`SarvamRealtimeSTTService`.
    """

    def __init__(
        self,
        *,
        connection_generation: int,
        max_unbound_finals: int = 32,
        **kwargs: Any,
    ):
        """Initialize event correlation state without changing Sarvam behavior.

        Args:
            connection_generation: Non-negative STT connection generation.
            max_unbound_finals: Maximum retained final observations.
            **kwargs: Arguments passed to :class:`SarvamRealtimeSTTService`.
        """
        if connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if max_unbound_finals < 1:
            raise ValueError("max_unbound_finals must be at least one")
        endpointing = kwargs.setdefault("endpointing", "manual")
        if endpointing != "manual":
            raise ValueError("InterviewSarvamRealtimeSTTService requires manual endpointing")
        super().__init__(**kwargs)
        self._connection_generation = connection_generation
        self._max_unbound_finals = max_unbound_finals
        self._candidate_turn_for_next_boundary: int | None = None
        self._active_boundary_id: int | None = None
        self._next_boundary_id = 0
        self._boundaries: dict[int, _BoundaryRecord] = {}
        self._failed_boundary_ids: set[int] = set()
        self._boundary_by_utterance_idx: dict[int, int] = {}
        self._observed_utterance_idxs: set[int] = set()
        self._unbound_finals: dict[int, list[SarvamFinalObservation]] = {}
        self._unbound_final_count = 0
        self._receive_terminal_event_reported = False

        self._register_event_handler("on_speech_start_requested", sync=True)
        self._register_event_handler("on_utterance_bound", sync=True)
        self._register_event_handler("on_manual_boundary", sync=True)
        self._register_event_handler("on_final_observed", sync=True)
        self._register_event_handler("on_unbound_final", sync=True)
        self._register_event_handler("on_segment_final", sync=True)
        self._register_event_handler("on_correlation_failure", sync=True)
        self._register_event_handler("on_connection_event", sync=True)

    @property
    def connection_generation(self) -> int:
        """Return the connection generation assigned to this service instance."""
        return self._connection_generation

    @property
    def candidate_turn_for_next_boundary(self) -> int | None:
        """Return the owner armed for the next local speech start."""
        return self._candidate_turn_for_next_boundary

    def set_candidate_turn(self, candidate_turn_id: int) -> None:
        """Assign the candidate turn that the next local speech start will own.

        The controller must call this before forwarding a
        :class:`VADUserStartedSpeakingFrame`. A later binding derives ownership
        from this recorded boundary, never from a currently active turn.
        """
        if candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if self._candidate_turn_for_next_boundary is not None:
            raise RuntimeError("a candidate turn is already waiting for a speech boundary")
        self._candidate_turn_for_next_boundary = candidate_turn_id

    async def bind_provider_utterance(self, *, boundary_id: int, utterance_idx: int) -> bool:
        """Bind an observed Sarvam ID to a local boundary and replay retained finals.

        Args:
            boundary_id: Existing local boundary ID from ``on_manual_boundary``.
            utterance_idx: Numeric ID observed in an actual Sarvam message.

        Returns:
            ``True`` when the association is valid, otherwise ``False`` after
            publishing a correlation failure.
        """
        if not self._is_utterance_idx(utterance_idx):
            await self._emit_correlation_failure("invalid_utterance_idx", boundary_id=boundary_id)
            return False
        if utterance_idx not in self._observed_utterance_idxs:
            await self._emit_correlation_failure(
                "unobserved_utterance_idx", utterance_idx=utterance_idx, boundary_id=boundary_id
            )
            return False
        boundary = self._boundaries.get(boundary_id)
        if boundary is None:
            await self._emit_correlation_failure(
                "unknown_boundary", utterance_idx=utterance_idx, boundary_id=boundary_id
            )
            return False
        if boundary_id in self._failed_boundary_ids:
            await self._emit_correlation_failure(
                "boundary_send_failed", utterance_idx=utterance_idx, boundary_id=boundary_id
            )
            return False
        bound_boundary_id = self._boundary_by_utterance_idx.get(utterance_idx)
        if bound_boundary_id is not None and bound_boundary_id != boundary_id:
            await self._emit_correlation_failure(
                "ambiguous_utterance_binding",
                utterance_idx=utterance_idx,
                boundary_id=boundary_id,
            )
            return False

        self._boundary_by_utterance_idx[utterance_idx] = boundary_id
        if bound_boundary_id is None:
            await self._call_event_handler(
                "on_utterance_bound",
                SarvamUtteranceBinding(
                    boundary_id,
                    SegmentId(self._connection_generation, utterance_idx),
                    boundary.candidate_turn_id,
                ),
            )
        observations = self._unbound_finals.pop(utterance_idx, [])
        self._unbound_final_count -= len(observations)
        for observation in observations:
            await self._emit_segment_final(boundary, observation)
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Track local manual boundaries while preserving the base send order."""
        if self._endpointing == "manual" and isinstance(frame, VADUserStartedSpeakingFrame):
            await self._call_event_handler("on_speech_start_requested", self._connection_generation)
            await self._begin_local_boundary()
        elif self._endpointing == "manual" and isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._active_boundary_id is None:
                await self._emit_correlation_failure("speech_end_without_local_boundary")

        await super().process_frame(frame, direction)

        if self._endpointing == "manual" and isinstance(frame, VADUserStoppedSpeakingFrame):
            self._active_boundary_id = None

    async def _send_json(self, payload: dict[str, Any]) -> bool:
        """Publish local-boundary delivery only after the base send attempt."""
        event = payload.get("event")
        if event not in {ManualBoundaryEvent.SPEECH_START, ManualBoundaryEvent.SPEECH_END}:
            return await super()._send_json(payload)
        boundary_id = self._active_boundary_id
        if boundary_id is None:
            return await super()._send_json(payload)
        try:
            sent = await super()._send_json(payload)
        except Exception:
            await self._publish_manual_boundary(boundary_id, ManualBoundaryEvent(event), sent=False)
            raise
        await self._publish_manual_boundary(boundary_id, ManualBoundaryEvent(event), sent=sent)
        return sent

    async def _handle_message(self, message: dict[str, Any]):
        """Remember only provider IDs actually observed in transcript messages."""
        if message.get("event") in {"transcript.partial", "transcript.final"}:
            utterance_idx = message.get("utterance_idx")
            if self._is_utterance_idx(utterance_idx):
                self._observed_utterance_idxs.add(utterance_idx)
        await super()._handle_message(message)

    async def _handle_final_transcript(self, message: dict[str, Any]):
        """Observe and correlate a final before retaining base metric/frame behavior."""
        utterance_idx = message.get("utterance_idx")
        if self._is_utterance_idx(utterance_idx):
            self._observed_utterance_idxs.add(utterance_idx)
        observation = self._final_observation(message)
        await self._call_event_handler("on_final_observed", observation)

        if observation.utterance_idx is None:
            await self._call_event_handler("on_unbound_final", observation)
            await self._emit_correlation_failure("missing_or_invalid_utterance_idx")
        else:
            boundary_id = self._boundary_by_utterance_idx.get(observation.utterance_idx)
            if boundary_id is None:
                await self._retain_unbound_final(observation)
                await self._call_event_handler("on_unbound_final", observation)
            else:
                if boundary_id in self._failed_boundary_ids:
                    await self._call_event_handler("on_unbound_final", observation)
                    await self._emit_correlation_failure(
                        "boundary_send_failed",
                        utterance_idx=observation.utterance_idx,
                        boundary_id=boundary_id,
                    )
                else:
                    await self._emit_segment_final(self._boundaries[boundary_id], observation)

        await super()._handle_final_transcript(message)

    async def _handle_error(self, message: dict[str, Any]):
        """Expose provider error events while preserving the base error frame."""
        await self._emit_connection_event(
            ConnectionEventKind.PROVIDER_ERROR,
            str(message.get("message") or message.get("code") or "Sarvam provider error"),
        )
        await super()._handle_error(message)

    async def _connect_websocket(self):
        """Expose a failed websocket connect that the base service handles internally."""
        await super()._connect_websocket()
        if self._websocket is None:
            await self._emit_connection_event(
                ConnectionEventKind.CONNECT_FAILED, "Sarvam websocket connection failed"
            )

    async def _receive_task_handler(self, report_error: ReportErrorCallback):
        """Classify actual receive errors before forwarding the base report callback."""
        self._receive_terminal_event_reported = False

        async def report_receive_error(
            error: ErrorFrame, force_treat_as_permanent: bool = False
        ) -> None:
            self._receive_terminal_event_reported = True
            kind = (
                ConnectionEventKind.RECEIVE_ENDED
                if error.exception is None
                else ConnectionEventKind.RECEIVE_FAILED
            )
            await self._emit_connection_event(kind, error.error)
            await report_error(error, force_treat_as_permanent=force_treat_as_permanent)

        await super()._receive_task_handler(report_receive_error)
        if not self._disconnecting and not self._receive_terminal_event_reported:
            await self._emit_connection_event(
                ConnectionEventKind.RECEIVE_ENDED, "Sarvam receive loop ended"
            )

    async def _disconnect(self):
        """Report application-requested shutdown separately from connection failure."""
        was_disconnecting = self._disconnecting
        await super()._disconnect()
        if not was_disconnecting:
            await self._emit_connection_event(
                ConnectionEventKind.INTENTIONAL_SHUTDOWN, "Sarvam websocket shutdown requested"
            )

    async def _begin_local_boundary(self) -> None:
        if self._active_boundary_id is not None:
            self._failed_boundary_ids.add(self._active_boundary_id)
            await self._emit_correlation_failure("speech_start_while_boundary_active")
            return
        candidate_turn_id = self._candidate_turn_for_next_boundary
        self._candidate_turn_for_next_boundary = None
        if candidate_turn_id is None:
            await self._emit_correlation_failure("speech_start_without_candidate_turn")
            return
        boundary_id = self._next_boundary_id
        self._next_boundary_id += 1
        self._boundaries[boundary_id] = _BoundaryRecord(boundary_id, candidate_turn_id)
        self._active_boundary_id = boundary_id

    async def _publish_manual_boundary(
        self, boundary_id: int, event: ManualBoundaryEvent, *, sent: bool
    ) -> None:
        boundary = self._boundaries[boundary_id]
        if not sent:
            self._failed_boundary_ids.add(boundary_id)
        await self._call_event_handler(
            "on_manual_boundary",
            SarvamManualBoundary(
                connection_generation=self._connection_generation,
                boundary_id=boundary.boundary_id,
                candidate_turn_id=boundary.candidate_turn_id,
                event=event,
                sent=sent,
            ),
        )

    def _final_observation(self, message: dict[str, Any]) -> SarvamFinalObservation:
        text = (message.get("text") or "").strip()
        utterance_idx = message.get("utterance_idx")
        valid_utterance_idx = utterance_idx if self._is_utterance_idx(utterance_idx) else None
        return SarvamFinalObservation(
            connection_generation=self._connection_generation,
            utterance_idx=valid_utterance_idx,
            outcome=SegmentFinalOutcome.TEXT if text else SegmentFinalOutcome.EMPTY,
            text=text,
            raw_message=dict(message),
        )

    async def _retain_unbound_final(self, observation: SarvamFinalObservation) -> None:
        if self._unbound_final_count >= self._max_unbound_finals:
            # The raw observation already reached the application. Retaining more
            # would grow without bound while a controller is unavailable.
            await self._emit_correlation_failure("unbound_final_retention_exhausted")
            return
        assert observation.utterance_idx is not None
        self._unbound_finals.setdefault(observation.utterance_idx, []).append(observation)
        self._unbound_final_count += 1

    async def _emit_segment_final(
        self, boundary: _BoundaryRecord, observation: SarvamFinalObservation
    ) -> None:
        assert observation.utterance_idx is not None
        await self._call_event_handler(
            "on_segment_final",
            SegmentFinal(
                segment_id=SegmentId(self._connection_generation, observation.utterance_idx),
                candidate_turn_id=boundary.candidate_turn_id,
                outcome=observation.outcome,
                text=observation.text,
            ),
        )

    async def _emit_correlation_failure(
        self,
        reason: str,
        *,
        utterance_idx: int | None = None,
        boundary_id: int | None = None,
    ) -> None:
        await self._call_event_handler(
            "on_correlation_failure",
            SarvamCorrelationFailure(
                connection_generation=self._connection_generation,
                reason=reason,
                utterance_idx=utterance_idx,
                boundary_id=boundary_id,
            ),
        )

    async def _emit_connection_event(self, kind: ConnectionEventKind, message: str) -> None:
        await self._call_event_handler(
            "on_connection_event",
            SarvamConnectionEvent(self._connection_generation, kind, message),
        )

    @staticmethod
    def _is_utterance_idx(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
