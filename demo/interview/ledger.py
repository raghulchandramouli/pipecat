"""Synchronous transcript state for one candidate interview turn.

The ledger deliberately stops before LLM dispatch or answer acceptance. It gives
an asynchronous controller a deterministic readiness decision and deadline while
preserving the candidate turn when a bot response becomes stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from .sarvam_events import (
    ConnectionEventKind,
    SarvamConnectionEvent,
    SarvamCorrelationFailure,
    SarvamFinalObservation,
    SarvamManualBoundary,
)


@dataclass(frozen=True)
class TranscriptSnapshot:
    """Read-only view of the current candidate answer transcript.

    Parameters:
        candidate_turn_id: Candidate turn represented by this snapshot.
        question_id: Interview question represented by this snapshot.
        text: Text finals joined in local boundary order.
        segments: Terminal finals in local boundary order, including empty finals.
        revision: Monotonic state revision for cache and dispatch invalidation.
        ready: Whether the transcript gate can permit a completion decision.
        recovery_error: Recoverable condition that blocks the gate, if present.
    """

    candidate_turn_id: int | None
    question_id: str | None
    text: str
    segments: tuple[SegmentFinal, ...]
    revision: int
    ready: bool
    recovery_error: str | None


@dataclass
class _BoundaryState:
    """Ledger-owned record for one local Sarvam boundary.

    Parameters:
        boundary_id: Local boundary ID, whose insertion order is transcript order.
        candidate_turn_id: Owner captured at the local speech start.
        start_sent: Whether Sarvam accepted the speech-start message.
        end_sent: Whether Sarvam accepted the speech-end message.
        closed_at: Controller monotonic timestamp at local speech end.
        segment_id: Explicitly bound provider segment ID.
        final: Terminal result for the bound segment.
    """

    boundary_id: int
    candidate_turn_id: int
    start_sent: bool
    end_sent: bool | None = None
    closed_at: float | None = None
    segment_id: SegmentId | None = None
    final: SegmentFinal | None = None


class TranscriptLedger:
    """Controller-owned strict gate for one current candidate answer.

    ``record_boundary`` preserves local boundary chronology. Provider IDs only
    become part of that order through ``bind_boundary``; no provider index sorting
    or arrival-order correlation is used. Every method is synchronous so a caller
    can invoke it from a fast frame/event path and schedule waits separately.

    Args:
        connection_generation: Current non-negative STT connection generation.
        transcript_final_timeout: Seconds after a closed boundary before missing
            correlation or finalization becomes a recoverable error.
    """

    def __init__(self, *, connection_generation: int, transcript_final_timeout: float):
        """Initialize an empty ledger with no candidate turn in progress.

        Args:
            connection_generation: Current non-negative STT connection generation.
            transcript_final_timeout: Positive timeout for a terminal final.
        """
        if connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if not isfinite(transcript_final_timeout) or transcript_final_timeout <= 0:
            raise ValueError("transcript_final_timeout must be finite and positive")
        self._connection_generation = connection_generation
        self._transcript_final_timeout = transcript_final_timeout
        self._candidate_turn_id: int | None = None
        self._question_id: str | None = None
        self._speech_open = False
        self._boundaries: dict[int, _BoundaryState] = {}
        self._boundary_order: list[int] = []
        self._boundary_by_segment: dict[SegmentId, int] = {}
        self._unbound: dict[SegmentId, SarvamFinalObservation] = {}
        self._unbound_deadlines: dict[SegmentId, float] = {}
        self._excluded_segments: set[SegmentId] = set()
        self._recovery_error: str | None = None
        self._revision = 0
        self._response_token = 0

    @property
    def connection_generation(self) -> int:
        """Return the generation accepted by this ledger."""
        return self._connection_generation

    @property
    def response_token(self) -> int:
        """Return the token invalidated by a newly started candidate speech turn."""
        return self._response_token

    @property
    def recovery_error(self) -> str | None:
        """Return the sticky recoverable condition currently blocking readiness."""
        return self._recovery_error

    @property
    def readiness(self) -> bool:
        """Return whether every closed boundary has a terminal final and speech is quiet."""
        if (
            self._candidate_turn_id is None
            or self._speech_open
            or self._recovery_error is not None
            or not self._boundary_order
            or self._unbound
        ):
            return False
        for boundary_id in self._boundary_order:
            boundary = self._boundaries[boundary_id]
            if (
                not boundary.start_sent
                or boundary.end_sent is not True
                or boundary.segment_id is None
                or boundary.final is None
            ):
                return False
        return True

    @property
    def next_deadline(self) -> float | None:
        """Return the earliest missing-final deadline, if one is active."""
        if self._recovery_error is not None:
            return None
        deadlines = [
            boundary.closed_at + self._transcript_final_timeout
            for boundary_id in self._boundary_order
            if (boundary := self._boundaries[boundary_id]).end_sent is True
            and boundary.closed_at is not None
            and boundary.final is None
        ]
        deadlines.extend(self._unbound_deadlines.values())
        return min(deadlines, default=None)

    @property
    def snapshot(self) -> TranscriptSnapshot:
        """Return the current immutable transcript view in local boundary order."""
        segments = tuple(
            boundary.final
            for boundary_id in self._boundary_order
            if (boundary := self._boundaries[boundary_id]).final is not None
            and boundary.segment_id not in self._excluded_segments
        )
        text = " ".join(
            segment.text for segment in segments if segment.outcome is SegmentFinalOutcome.TEXT
        )
        return TranscriptSnapshot(
            candidate_turn_id=self._candidate_turn_id,
            question_id=self._question_id,
            text=text,
            segments=segments,
            revision=self._revision,
            ready=self.readiness,
            recovery_error=self._recovery_error,
        )

    def begin_candidate_turn(self, *, candidate_turn_id: int, question_id: str) -> None:
        """Start a fresh candidate-owned answer without accepting any prior one.

        Args:
            candidate_turn_id: Non-negative candidate turn identifier.
            question_id: Non-blank interview question identifier.
        """
        if candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if not question_id.strip():
            raise ValueError("question_id must not be blank")
        if self._candidate_turn_id is not None:
            raise RuntimeError("replace an existing candidate turn through recovery_reset")
        self._candidate_turn_id = candidate_turn_id
        self._question_id = question_id
        self._response_token += 1
        self._touch()

    def speech_started(self) -> int:
        """Invalidate a pending response while retaining current answer state.

        Returns:
            The new response token.
        """
        if not self._speech_open:
            self._speech_open = True
            self._response_token += 1
            self._touch()
        return self._response_token

    def speech_stopped(self) -> None:
        """Mark candidate speech quiet without accepting the pending answer."""
        if self._speech_open:
            self._speech_open = False
            self._touch()

    def record_boundary(self, boundary: SarvamManualBoundary, *, now: float) -> bool:
        """Record a local boundary event and its delivery result.

        Args:
            boundary: Boundary emitted by the Sarvam application extension.
            now: Monotonic controller timestamp for an end boundary.

        Returns:
            Whether the event belongs to the current ledger generation and turn.
        """
        if boundary.connection_generation != self._connection_generation:
            return False
        if self._candidate_turn_id is None or boundary.candidate_turn_id != self._candidate_turn_id:
            return False
        existing = self._boundaries.get(boundary.boundary_id)
        if boundary.event.value == "speech_start":
            if existing is not None:
                if (
                    existing.candidate_turn_id == boundary.candidate_turn_id
                    and existing.start_sent == boundary.sent
                ):
                    return True
                self._set_recovery_error("conflicting_boundary_start")
                return False
            if any(state.end_sent is None for state in self._boundaries.values()):
                self._set_recovery_error("overlapping_local_boundaries")
                return False
            self.speech_started()
            self._boundaries[boundary.boundary_id] = _BoundaryState(
                boundary.boundary_id, boundary.candidate_turn_id, boundary.sent
            )
            self._boundary_order.append(boundary.boundary_id)
            if not boundary.sent:
                self._set_recovery_error("speech_start_send_failed")
            self._touch()
            return True

        if existing is None:
            self._set_recovery_error("speech_end_without_boundary")
            return False
        if existing.end_sent is not None:
            if existing.end_sent == boundary.sent:
                return True
            self._set_recovery_error("conflicting_boundary_end")
            return False
        existing.end_sent = boundary.sent
        existing.closed_at = now
        self.speech_stopped()
        if not boundary.sent:
            self._set_recovery_error("speech_end_send_failed")
        self._touch()
        return True

    def bind_boundary(self, boundary_id: int, segment_id: SegmentId) -> bool:
        """Associate an observed provider segment with one local boundary.

        Args:
            boundary_id: Local boundary ID that owns the provider segment.
            segment_id: Explicit provider segment identity.

        Returns:
            Whether the binding is consistent with the current candidate turn.
        """
        if segment_id.connection_generation != self._connection_generation:
            return False
        boundary = self._boundaries.get(boundary_id)
        if boundary is None:
            self._set_recovery_error("unknown_boundary_binding")
            return False
        if boundary.segment_id is not None:
            if boundary.segment_id == segment_id:
                return True
            self._set_recovery_error("conflicting_boundary_binding")
            return False
        existing_boundary_id = self._boundary_by_segment.get(segment_id)
        if existing_boundary_id is not None and existing_boundary_id != boundary_id:
            self._set_recovery_error("ambiguous_segment_binding")
            return False
        boundary.segment_id = segment_id
        self._boundary_by_segment[segment_id] = boundary_id
        self._unbound.pop(segment_id, None)
        self._unbound_deadlines.pop(segment_id, None)
        self._touch()
        return True

    def observe_unbound(
        self, observation: SarvamFinalObservation, *, now: float | None = None
    ) -> bool:
        """Record an unbound raw final until explicit boundary binding resolves it.

        Args:
            observation: Raw Sarvam final observed by the application extension.
            now: Optional monotonic timestamp for an orphan-final deadline.

        Returns:
            Whether the observation is from the current connection with a valid ID.
        """
        if (
            observation.connection_generation != self._connection_generation
            or observation.utterance_idx is None
        ):
            return False
        segment_id = SegmentId(self._connection_generation, observation.utterance_idx)
        existing = self._unbound.get(segment_id)
        if existing is not None:
            if existing.outcome is observation.outcome and existing.text == observation.text:
                return True
            self._set_recovery_error("conflicting_unbound_final")
            return False
        self._unbound[segment_id] = observation
        if now is not None:
            self._unbound_deadlines[segment_id] = now + self._transcript_final_timeout
        elif self.next_deadline is None:
            self._set_recovery_error("unexpected_unbound_final")
        self._touch()
        return True

    def record_final(self, final: SegmentFinal) -> bool:
        """Record a terminal outcome for an explicitly bound current segment.

        Args:
            final: Normalized provider terminal outcome.

        Returns:
            Whether the final is consistent with current connection ownership.
        """
        if final.segment_id.connection_generation != self._connection_generation:
            return False
        if self._candidate_turn_id is None or final.candidate_turn_id != self._candidate_turn_id:
            return False
        boundary_id = self._boundary_by_segment.get(final.segment_id)
        if boundary_id is None:
            self._set_recovery_error("final_without_boundary_binding")
            return False
        boundary = self._boundaries[boundary_id]
        if boundary.final is not None:
            if boundary.final == final:
                return True
            self._set_recovery_error("conflicting_segment_final")
            return False
        boundary.final = final
        self._unbound.pop(final.segment_id, None)
        self._unbound_deadlines.pop(final.segment_id, None)
        if final.outcome is SegmentFinalOutcome.FAILED:
            self._set_recovery_error("segment_final_failed")
        elif self._unbound and self.next_deadline is None:
            self._set_recovery_error("unexpected_unbound_final")
        self._touch()
        return True

    def correlation_failure(self, failure: SarvamCorrelationFailure) -> bool:
        """Make a current-generation correlation failure a recoverable gate error."""
        if failure.connection_generation != self._connection_generation:
            return False
        self._set_recovery_error(f"correlation_failure:{failure.reason}")
        return True

    def connection_event(self, event: SarvamConnectionEvent) -> bool:
        """Make current provider connection failures block transcript readiness."""
        if event.connection_generation != self._connection_generation:
            return False
        if event.kind is not ConnectionEventKind.INTENTIONAL_SHUTDOWN:
            self._set_recovery_error(f"connection_event:{event.kind.value}")
        return True

    def check_timeouts(self, now: float) -> bool:
        """Enter recovery when the earliest unresolved closed boundary expires.

        Args:
            now: Monotonic controller timestamp.

        Returns:
            Whether this call transitioned the ledger into recovery.
        """
        deadline = self.next_deadline
        if deadline is None or now < deadline:
            return False
        self._set_recovery_error("transcript_final_timeout")
        return True

    def expire(self, now: float) -> bool:
        """Alias for ``check_timeouts`` used by timer callbacks."""
        return self.check_timeouts(now)

    def recovery_reset(self) -> None:
        """Discard the unresolved current turn after application recovery begins."""
        self._candidate_turn_id = None
        self._question_id = None
        self._speech_open = False
        self._boundaries.clear()
        self._boundary_order.clear()
        self._boundary_by_segment.clear()
        self._unbound.clear()
        self._unbound_deadlines.clear()
        self._excluded_segments.clear()
        self._recovery_error = None
        self._response_token += 1
        self._touch()

    def exclude_control_segment(self, segment_id: SegmentId) -> bool:
        """Keep a command's final outcome for readiness while excluding it from evidence."""
        boundary_id = self._boundary_by_segment.get(segment_id)
        if boundary_id is None or self._boundaries[boundary_id].final is None:
            return False
        if segment_id not in self._excluded_segments:
            self._excluded_segments.add(segment_id)
            self._touch()
        return True

    def segment_closed_at(self, segment_id: SegmentId) -> float | None:
        """Return the local speech-end timestamp for an explicitly bound provider segment."""
        boundary_id = self._boundary_by_segment.get(segment_id)
        return None if boundary_id is None else self._boundaries[boundary_id].closed_at

    def replace_connection(
        self, *, connection_generation: int, candidate_turn_id: int, question_id: str
    ) -> None:
        """Start a replacement STT connection without carrying unresolved transcript state.

        Args:
            connection_generation: New non-negative STT connection generation.
            candidate_turn_id: Candidate turn identifier for the recovered attempt.
            question_id: Current question identifier for the recovered attempt.
        """
        if connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if connection_generation <= self._connection_generation:
            raise ValueError("replacement connection_generation must increase")
        self.recovery_reset()
        self._connection_generation = connection_generation
        self.begin_candidate_turn(candidate_turn_id=candidate_turn_id, question_id=question_id)

    def _set_recovery_error(self, reason: str) -> None:
        if self._recovery_error is None:
            self._recovery_error = reason
            self._touch()

    def _touch(self) -> None:
        self._revision += 1
