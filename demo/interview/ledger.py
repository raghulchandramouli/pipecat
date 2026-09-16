"""Synchronous, provenance-preserving transcript state for one candidate turn."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from .contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from .sarvam_events import (
    ConnectionEventKind,
    SarvamConnectionEvent,
    SarvamCorrelationFailure,
    SarvamFinalObservation,
    SarvamManualBoundary,
)

MAX_SOURCE_BYTES = 32 * 1024
MAX_SOURCE_SEGMENTS = 256


@dataclass(frozen=True)
class TranscriptSourceRecord:
    """An immutable provider final in local boundary order.

    Parameters:
        boundary_id: Ledger-local boundary that owns the final.
        segment_id: Provider segment identity scoped to the STT generation.
        final: Normalized terminal provider outcome.
        closed_at: Local timestamp at the end of the corresponding speech interval.
    """

    boundary_id: int
    segment_id: SegmentId
    final: SegmentFinal
    closed_at: float | None


@dataclass(frozen=True)
class TranscriptRepairObligation:
    """A retired failed interval and the fresh intervals that must replace it."""

    retired_boundary_ids: tuple[int, ...]
    replacement_boundary_ids: tuple[int, ...]


@dataclass(frozen=True)
class TranscriptSnapshot:
    """Read-only view of a candidate transcript and its immutable source records.

    ``segments`` and ``text`` retain the historic derived evidence view. Consumers
    that validate spans must use ``source_records`` so exclusions never rewrite the
    source transcript.
    """

    candidate_turn_id: int | None
    question_id: str | None
    text: str
    segments: tuple[SegmentFinal, ...]
    revision: int
    ready: bool
    recovery_error: str | None
    source_records: tuple[TranscriptSourceRecord, ...] = ()
    source_text: str = ""
    source_revision: int = 0
    disposition_revision: int = 0
    source_overflow: bool = False
    repair_obligation: TranscriptRepairObligation | None = None


@dataclass
class _BoundaryState:
    """Ledger-owned record for one local Sarvam boundary."""

    boundary_id: int
    candidate_turn_id: int
    start_sent: bool
    end_sent: bool | None = None
    closed_at: float | None = None
    segment_id: SegmentId | None = None
    final: SegmentFinal | None = None
    captured_source: bool = False
    retired: bool = False


class TranscriptLedger:
    """Controller-owned strict gate with bounded immutable transcript provenance.

    Local boundaries establish transcript order. Provider identities become part of
    that order only through ``bind_boundary``. Repair retires a known incomplete
    boundary, then keeps readiness blocked until fresh replacement speech has been
    explicitly confirmed substantive by the controller.
    """

    def __init__(
        self,
        *,
        connection_generation: int,
        transcript_final_timeout: float,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        max_source_segments: int = MAX_SOURCE_SEGMENTS,
    ):
        """Initialize an empty ledger.

        Args:
            connection_generation: Current non-negative STT connection generation.
            transcript_final_timeout: Positive timeout for a terminal final.
            max_source_bytes: Maximum UTF-8 bytes preserved for pending source.
            max_source_segments: Maximum terminal source records preserved.
        """
        if connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if not isfinite(transcript_final_timeout) or transcript_final_timeout <= 0:
            raise ValueError("transcript_final_timeout must be finite and positive")
        if max_source_bytes < 1:
            raise ValueError("max_source_bytes must be at least one")
        if max_source_segments < 1:
            raise ValueError("max_source_segments must be at least one")
        self._connection_generation = connection_generation
        self._transcript_final_timeout = transcript_final_timeout
        self._max_source_bytes = max_source_bytes
        self._max_source_segments = max_source_segments
        self._candidate_turn_id: int | None = None
        self._question_id: str | None = None
        self._speech_open = False
        self._boundaries: dict[int, _BoundaryState] = {}
        self._boundary_order: list[int] = []
        self._boundary_by_segment: dict[SegmentId, int] = {}
        self._unbound: dict[SegmentId, SarvamFinalObservation] = {}
        self._unbound_deadlines: dict[SegmentId, float] = {}
        self._excluded_ranges: list[tuple[int, int]] = []
        self._retired_segments: set[SegmentId] = set()
        self._recovery_error: str | None = None
        self._repair_obligation: TranscriptRepairObligation | None = None
        self._resolved_repair_boundaries: set[int] = set()
        self._source_bytes = 0
        self._source_segment_count = 0
        self._source_overflow = False
        self._source_revision = 0
        self._disposition_revision = 0
        self._response_token = 0

    @property
    def connection_generation(self) -> int:
        """Return the generation accepted by this ledger."""
        return self._connection_generation

    @property
    def response_token(self) -> int:
        """Return the token invalidated by new speech and repair."""
        return self._response_token

    @property
    def recovery_error(self) -> str | None:
        """Return the condition currently blocking normal transcript readiness."""
        return self._recovery_error

    @property
    def source_records(self) -> tuple[TranscriptSourceRecord, ...]:
        """Return immutable source finals, unaffected by evidence exclusions."""
        return tuple(
            TranscriptSourceRecord(
                boundary_id=boundary.boundary_id,
                segment_id=boundary.segment_id,
                final=boundary.final,
                closed_at=boundary.closed_at,
            )
            for boundary_id in self._boundary_order
            if (boundary := self._boundaries[boundary_id]).captured_source
            and not boundary.retired
            and boundary.segment_id is not None
            and boundary.final is not None
        )

    @property
    def repair_obligation(self) -> TranscriptRepairObligation | None:
        """Return the unresolved repair obligation, if selective repair is active."""
        return self._repair_obligation

    @property
    def repairable_boundary_ids(self) -> tuple[int, ...]:
        """Return every known missing or failed boundary eligible for repair.

        Correlation and connection failures deliberately do not appear here: their
        ownership cannot be repaired safely on this connection.
        """
        if self._repair_obligation is not None or self._source_overflow:
            return ()
        if self._recovery_error not in {"transcript_final_timeout", "segment_final_failed"}:
            return ()
        return tuple(
            boundary_id
            for boundary_id in self._boundary_order
            if self._is_repairable_boundary(self._boundaries[boundary_id])
        )

    @property
    def repair_replacement_records(self) -> tuple[TranscriptSourceRecord, ...]:
        """Return finals from the current repair's fresh replacement intervals."""
        obligation = self._repair_obligation
        if obligation is None:
            return ()
        records = {record.boundary_id: record for record in self.source_records}
        return tuple(
            records[boundary_id]
            for boundary_id in obligation.replacement_boundary_ids
            if boundary_id in records
        )

    @property
    def repair_replacement_ready(self) -> bool:
        """Return whether all fresh replacement intervals have complete text finals.

        This intentionally ignores the retired gap, letting a recovery-control
        window inspect only new speech while ordinary answer readiness stays false.
        It does not classify acknowledgments as substantive.
        """
        obligation = self._repair_obligation
        if obligation is None or self._speech_open or not obligation.replacement_boundary_ids:
            return False
        boundaries = tuple(
            self._boundaries[boundary_id] for boundary_id in obligation.replacement_boundary_ids
        )
        return all(boundary.final is not None for boundary in boundaries) and any(
            boundary.final.outcome is SegmentFinalOutcome.TEXT and bool(boundary.final.text.strip())
            for boundary in boundaries
        )

    @property
    def readiness(self) -> bool:
        """Return whether every required boundary is final and repair is resolved."""
        if (
            self._candidate_turn_id is None
            or self._speech_open
            or self._recovery_error is not None
            or self._repair_obligation is not None
            or self._source_overflow
            or not self._boundary_order
            or self._unbound
        ):
            return False
        for boundary_id in self._boundary_order:
            boundary = self._boundaries[boundary_id]
            if boundary.retired:
                continue
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
        if self._recovery_error is not None or self._source_overflow:
            return None
        deadlines = [
            boundary.closed_at + self._transcript_final_timeout
            for boundary_id in self._boundary_order
            if not (boundary := self._boundaries[boundary_id]).retired
            and boundary.end_sent is True
            and boundary.closed_at is not None
            and boundary.final is None
        ]
        deadlines.extend(self._unbound_deadlines.values())
        return min(deadlines, default=None)

    @property
    def snapshot(self) -> TranscriptSnapshot:
        """Return a source-stable snapshot and its derived evidence view."""
        source_records = self.source_records
        source_text, ranges = self._source_text_and_ranges(source_records)
        segments = self._derived_segments(source_records, ranges)
        text = self._exclude_ranges(source_text).strip()
        return TranscriptSnapshot(
            candidate_turn_id=self._candidate_turn_id,
            question_id=self._question_id,
            text=text,
            segments=segments,
            revision=self._source_revision,
            ready=self.readiness,
            recovery_error=self._recovery_error,
            source_records=source_records,
            source_text=source_text,
            source_revision=self._source_revision,
            disposition_revision=self._disposition_revision,
            source_overflow=self._source_overflow,
            repair_obligation=self._repair_obligation,
        )

    def begin_candidate_turn(self, *, candidate_turn_id: int, question_id: str) -> None:
        """Start a fresh candidate-owned answer without accepting a prior one."""
        if candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if not question_id.strip():
            raise ValueError("question_id must not be blank")
        if self._candidate_turn_id is not None:
            raise RuntimeError("replace an existing candidate turn through recovery_reset")
        self._candidate_turn_id = candidate_turn_id
        self._question_id = question_id
        self._response_token += 1
        self._touch_source()

    def speech_started(self) -> int:
        """Invalidate a pending response while retaining current source state."""
        if not self._speech_open:
            self._speech_open = True
            self._response_token += 1
            self._touch_source()
        return self._response_token

    def speech_stopped(self) -> None:
        """Mark candidate speech quiet without accepting the pending answer."""
        if self._speech_open:
            self._speech_open = False
            self._touch_source()

    def record_boundary(self, boundary: SarvamManualBoundary, *, now: float) -> bool:
        """Record one local boundary event and its delivery result."""
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
            state = _BoundaryState(boundary.boundary_id, boundary.candidate_turn_id, boundary.sent)
            self._boundaries[boundary.boundary_id] = state
            self._boundary_order.append(boundary.boundary_id)
            self._assign_repair_replacement(boundary.boundary_id)
            if not boundary.sent:
                self._set_recovery_error("speech_start_send_failed")
            self._touch_source()
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
        self._touch_source()
        return True

    def bind_boundary(self, boundary_id: int, segment_id: SegmentId) -> bool:
        """Associate one observed provider segment with one local boundary."""
        if segment_id.connection_generation != self._connection_generation:
            return False
        if segment_id in self._retired_segments:
            return False
        boundary = self._boundaries.get(boundary_id)
        if boundary is None:
            self._set_recovery_error("unknown_boundary_binding")
            return False
        if boundary.retired:
            self._retired_segments.add(segment_id)
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
        self._touch_source()
        return True

    def observe_unbound(
        self, observation: SarvamFinalObservation, *, now: float | None = None
    ) -> bool:
        """Record an unbound raw final until explicit binding resolves it."""
        if (
            observation.connection_generation != self._connection_generation
            or observation.utterance_idx is None
        ):
            return False
        segment_id = SegmentId(self._connection_generation, observation.utterance_idx)
        if segment_id in self._retired_segments:
            return False
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
        self._touch_source()
        return True

    def record_final(self, final: SegmentFinal) -> bool:
        """Record a terminal outcome for an explicitly bound current segment."""
        if final.segment_id.connection_generation != self._connection_generation:
            return False
        if final.segment_id in self._retired_segments:
            return False
        if self._candidate_turn_id is None or final.candidate_turn_id != self._candidate_turn_id:
            return False
        boundary_id = self._boundary_by_segment.get(final.segment_id)
        if boundary_id is None:
            self._set_recovery_error("final_without_boundary_binding")
            return False
        boundary = self._boundaries[boundary_id]
        if boundary.retired:
            self._retired_segments.add(final.segment_id)
            return False
        if boundary.final is not None:
            if boundary.final == final:
                return True
            self._set_recovery_error("conflicting_segment_final")
            return False
        boundary.final = final
        self._unbound.pop(final.segment_id, None)
        self._unbound_deadlines.pop(final.segment_id, None)
        self._capture_source(boundary)
        if final.outcome is SegmentFinalOutcome.FAILED:
            self._set_recovery_error("segment_final_failed")
        elif self._unbound and self.next_deadline is None:
            self._set_recovery_error("unexpected_unbound_final")
        self._touch_source()
        return True

    def begin_repair(self, boundary_ids: int | tuple[int, ...]) -> bool:
        """Atomically retire every unresolved failed boundary for fresh speech.

        The supplied IDs must be exactly the known repairable set. This prevents a
        caller from superseding one gap while accidentally treating another gap as
        resolved. The adapter must retire the same boundaries after preflighting its
        interval trust and before it sends replacement speech.
        """
        requested = (boundary_ids,) if isinstance(boundary_ids, int) else tuple(boundary_ids)
        repairable = self.repairable_boundary_ids
        if (
            not requested
            or len(set(requested)) != len(requested)
            or set(requested) != set(repairable)
        ):
            return False
        for boundary_id in requested:
            boundary = self._boundaries[boundary_id]
            boundary.retired = True
            if boundary.segment_id is not None:
                self._retired_segments.add(boundary.segment_id)
        self._repair_obligation = TranscriptRepairObligation(tuple(requested), ())
        self._recovery_error = "repair_replacement_required"
        self._response_token += 1
        self._touch_source()
        return True

    def resolve_repair_replacement(self, boundary_id: int, *, substantive: bool) -> bool:
        """Resolve one fresh repair interval after controller-owned validation.

        A false or premature validation leaves the obligation in place. This method
        deliberately does not infer that short acknowledgments are substantive.
        """
        obligation = self._repair_obligation
        if obligation is None or boundary_id not in obligation.replacement_boundary_ids:
            return False
        if not self.repair_replacement_ready:
            return False
        boundary = self._boundaries[boundary_id]
        if (
            not substantive
            or boundary.final is None
            or boundary.final.outcome is not SegmentFinalOutcome.TEXT
            or not boundary.final.text.strip()
        ):
            return False
        self._repair_obligation = None
        self._resolved_repair_boundaries.clear()
        self._recovery_error = None
        self._touch_source()
        return True

    def correlation_failure(self, failure: SarvamCorrelationFailure) -> bool:
        """Make current-generation correlation corruption block readiness."""
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
        """Enter recovery when the earliest unresolved closed boundary expires."""
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
        self._excluded_ranges.clear()
        self._retired_segments.clear()
        self._recovery_error = None
        self._repair_obligation = None
        self._resolved_repair_boundaries.clear()
        self._source_bytes = 0
        self._source_segment_count = 0
        self._source_overflow = False
        self._response_token += 1
        self._touch_source()
        self._touch_disposition()

    def exclude_control_segment(self, segment_id: SegmentId) -> bool:
        """Keep a final for source provenance while excluding it from evidence."""
        return self.commit_exclusions((segment_id,))

    def commit_exclusions(self, segment_ids: tuple[SegmentId, ...]) -> bool:
        """Commit verified derived evidence exclusions without revising source."""
        if not segment_ids:
            return True
        _source, ranges = self._source_text_and_ranges(self.source_records)
        requested = []
        for segment_id in segment_ids:
            source_range = ranges.get(segment_id)
            if source_range is None:
                return False
            requested.append(source_range)
        return self.commit_span_exclusions(self._source_revision, tuple(requested))

    def commit_span_exclusions(self, source_revision: int, spans: tuple[object, ...]) -> bool:
        """Commit exact derived exclusions against the current immutable source.

        ``spans`` may contain ``(start, end)`` pairs or objects with integer
        ``start`` and ``end`` attributes, such as the interaction ``SourceSpan``.
        Existing exclusions are never removed or overlapped, so a later model
        reply cannot reclaim previously excluded help as evidence.
        """
        if source_revision != self._source_revision:
            return False
        if not spans:
            return True
        source_text, _ranges = self._source_text_and_ranges(self.source_records)
        requested: list[tuple[int, int]] = []
        for span in spans:
            if isinstance(span, tuple) and len(span) == 2:
                start, end = span
            else:
                start = getattr(span, "start", None)
                end = getattr(span, "end", None)
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(source_text)
            ):
                return False
            requested.append((start, end))
        requested.sort()
        if any(
            right > next_left
            for (_left, right), (next_left, _next_right) in zip(requested, requested[1:])
        ):
            return False
        if any(
            start < old_end and old_start < end
            for start, end in requested
            for old_start, old_end in self._excluded_ranges
        ):
            return False
        self._excluded_ranges.extend(requested)
        self._excluded_ranges.sort()
        self._touch_disposition()
        return True

    def raw_spans_for_derived_spans(
        self,
        source_revision: int,
        disposition_revision: int,
        spans: tuple[object, ...],
    ) -> tuple[tuple[int, int], ...] | None:
        """Project current derived-text ranges onto immutable raw source offsets.

        The model sees :attr:`TranscriptSnapshot.text`, which omits previously
        committed help. This method validates offsets against that exact trimmed
        derived text, then returns the corresponding raw ranges. A derived range
        spanning an older exclusion becomes multiple raw ranges, preserving the
        immutable source without reclaiming the old exclusion.
        """
        if (
            source_revision != self._source_revision
            or disposition_revision != self._disposition_revision
        ):
            return None
        text, raw_offsets = self._derived_text_and_raw_offsets()
        requested = self._validated_span_ranges(spans, len(text))
        if requested is None:
            return None
        projected: list[tuple[int, int]] = []
        for start, end in requested:
            first = raw_offsets[start]
            previous = first
            for raw_offset in raw_offsets[start + 1 : end]:
                if raw_offset != previous + 1:
                    projected.append((first, previous + 1))
                    first = raw_offset
                previous = raw_offset
            projected.append((first, previous + 1))
        return tuple(projected)

    def project_derived_span_exclusions(
        self,
        source_revision: int,
        disposition_revision: int,
        spans: tuple[object, ...],
    ) -> tuple[tuple[int, int], ...] | None:
        """Alias for projecting derived exclusion ranges before release-time commit."""
        return self.raw_spans_for_derived_spans(source_revision, disposition_revision, spans)

    def commit_derived_span_exclusions(
        self,
        source_revision: int,
        disposition_revision: int,
        spans: tuple[object, ...],
    ) -> bool:
        """Project and commit derived ranges when the same source view is current."""
        projected = self.raw_spans_for_derived_spans(source_revision, disposition_revision, spans)
        return projected is not None and self.commit_span_exclusions(source_revision, projected)

    def segment_closed_at(self, segment_id: SegmentId) -> float | None:
        """Return the local speech-end timestamp for one bound provider segment."""
        boundary_id = self._boundary_by_segment.get(segment_id)
        return None if boundary_id is None else self._boundaries[boundary_id].closed_at

    def replace_connection(
        self, *, connection_generation: int, candidate_turn_id: int, question_id: str
    ) -> None:
        """Start a replacement connection without carrying unresolved source state."""
        if connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if connection_generation <= self._connection_generation:
            raise ValueError("replacement connection_generation must increase")
        self.recovery_reset()
        self._connection_generation = connection_generation
        self.begin_candidate_turn(candidate_turn_id=candidate_turn_id, question_id=question_id)

    def _assign_repair_replacement(self, boundary_id: int) -> None:
        obligation = self._repair_obligation
        if obligation is None:
            return
        self._repair_obligation = TranscriptRepairObligation(
            obligation.retired_boundary_ids,
            (*obligation.replacement_boundary_ids, boundary_id),
        )

    def _is_repairable_boundary(self, boundary: _BoundaryState) -> bool:
        return (
            not boundary.retired
            and boundary.start_sent
            and boundary.end_sent is True
            and (boundary.final is None or boundary.final.outcome is SegmentFinalOutcome.FAILED)
        )

    def _capture_source(self, boundary: _BoundaryState) -> None:
        assert boundary.final is not None
        separator = (
            1 if boundary.final.outcome is SegmentFinalOutcome.TEXT and self._source_bytes else 0
        )
        text_bytes = (
            len(boundary.final.text.encode("utf-8"))
            if boundary.final.outcome is SegmentFinalOutcome.TEXT
            else 0
        )
        if (
            self._source_segment_count >= self._max_source_segments
            or self._source_bytes + separator + text_bytes > self._max_source_bytes
        ):
            self._source_overflow = True
            self._set_recovery_error("source_overflow")
            return
        boundary.captured_source = True
        self._source_segment_count += 1
        self._source_bytes += separator + text_bytes

    def _set_recovery_error(self, reason: str) -> None:
        if self._recovery_error is None:
            self._recovery_error = reason
            self._touch_source()

    def _touch_source(self) -> None:
        self._source_revision += 1

    def _touch_disposition(self) -> None:
        self._disposition_revision += 1

    def _source_text_and_ranges(
        self, records: tuple[TranscriptSourceRecord, ...]
    ) -> tuple[str, dict[SegmentId, tuple[int, int]]]:
        parts: list[str] = []
        ranges: dict[SegmentId, tuple[int, int]] = {}
        offset = 0
        for record in records:
            final = record.final
            if final.outcome is not SegmentFinalOutcome.TEXT or not final.text:
                continue
            if parts:
                offset += 1
            start = offset
            parts.append(final.text)
            offset += len(final.text)
            ranges[record.segment_id] = (start, offset)
        return " ".join(parts), ranges

    def _exclude_ranges(self, source_text: str) -> str:
        if not self._excluded_ranges:
            return source_text
        pieces: list[str] = []
        cursor = 0
        for start, end in self._excluded_ranges:
            pieces.append(source_text[cursor:start])
            cursor = end
        pieces.append(source_text[cursor:])
        return "".join(pieces)

    def _derived_text_and_raw_offsets(self) -> tuple[str, tuple[int, ...]]:
        """Return the exact derived snapshot text and each character's raw offset."""
        source_text, _ranges = self._source_text_and_ranges(self.source_records)
        characters: list[str] = []
        raw_offsets: list[int] = []
        exclusion_index = 0
        for raw_offset, character in enumerate(source_text):
            while (
                exclusion_index < len(self._excluded_ranges)
                and self._excluded_ranges[exclusion_index][1] <= raw_offset
            ):
                exclusion_index += 1
            if (
                exclusion_index < len(self._excluded_ranges)
                and self._excluded_ranges[exclusion_index][0]
                <= raw_offset
                < self._excluded_ranges[exclusion_index][1]
            ):
                continue
            characters.append(character)
            raw_offsets.append(raw_offset)
        untrimmed = "".join(characters)
        left = len(untrimmed) - len(untrimmed.lstrip())
        right = len(untrimmed.rstrip())
        return untrimmed[left:right], tuple(raw_offsets[left:right])

    @staticmethod
    def _validated_span_ranges(
        spans: tuple[object, ...], source_length: int
    ) -> list[tuple[int, int]] | None:
        """Validate ordered non-overlapping ranges against one exact text view."""
        requested: list[tuple[int, int]] = []
        previous_end = 0
        for span in spans:
            if isinstance(span, tuple) and len(span) == 2:
                start, end = span
            else:
                start = getattr(span, "start", None)
                end = getattr(span, "end", None)
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < previous_end
                or end <= start
                or end > source_length
            ):
                return None
            requested.append((start, end))
            previous_end = end
        return requested

    def _derived_segments(
        self,
        records: tuple[TranscriptSourceRecord, ...],
        ranges: dict[SegmentId, tuple[int, int]],
    ) -> tuple[SegmentFinal, ...]:
        values: list[SegmentFinal] = []
        for record in records:
            final = record.final
            source_range = ranges.get(record.segment_id)
            if source_range is None:
                values.append(final)
                continue
            start, end = source_range
            local = [
                (max(start, excluded_start) - start, min(end, excluded_end) - start)
                for excluded_start, excluded_end in self._excluded_ranges
                if excluded_start < end and start < excluded_end
            ]
            if not local:
                values.append(final)
                continue
            pieces: list[str] = []
            cursor = 0
            for left, right in local:
                pieces.append(final.text[cursor:left])
                cursor = right
            pieces.append(final.text[cursor:])
            retained = "".join(pieces)
            if retained:
                values.append(replace(final, text=retained))
        return tuple(values)
