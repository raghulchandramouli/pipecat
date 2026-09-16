"""Deterministic state tests for the interview transcript ledger."""

from math import inf, nan

import pytest

from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.ledger import TranscriptLedger
from demo.interview.sarvam_events import (
    ManualBoundaryEvent,
    SarvamFinalObservation,
    SarvamManualBoundary,
)


def boundary(
    boundary_id: int, candidate_turn_id: int, event: ManualBoundaryEvent
) -> SarvamManualBoundary:
    """Build a successfully delivered local boundary for a current connection."""
    return SarvamManualBoundary(1, boundary_id, candidate_turn_id, event, True)


def final(segment_id: SegmentId, candidate_turn_id: int, text: str = "answer") -> SegmentFinal:
    """Build a text final owned by the current candidate turn."""
    return SegmentFinal(segment_id, candidate_turn_id, SegmentFinalOutcome.TEXT, text)


def test_orders_segments_by_local_boundary_not_provider_index() -> None:
    """Out-of-order provider IDs retain local speech-boundary chronology."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    first, second = SegmentId(1, 99), SegmentId(1, 2)
    for boundary_id, segment_id in ((7, first), (3, second)):
        assert ledger.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_START), now=0
        )
        assert ledger.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_END), now=1
        )
        assert ledger.bind_boundary(boundary_id, segment_id)
    assert ledger.record_final(final(second, 4, "second"))
    assert ledger.record_final(final(first, 4, "first"))
    assert ledger.snapshot.text == "first second"
    assert [segment.segment_id for segment in ledger.snapshot.segments] == [first, second]
    assert ledger.readiness


def test_empty_final_resolves_closed_boundary_without_text() -> None:
    """Empty finals terminate the transcript wait without fabricating content."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    ledger.record_final(SegmentFinal(segment_id, 4, SegmentFinalOutcome.EMPTY))
    assert ledger.readiness
    assert ledger.snapshot.text == ""


def test_stale_events_and_duplicate_final_do_not_change_current_snapshot() -> None:
    """Stale generations and identical duplicate finals do not alter ledger state."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    value = final(segment_id, 4)
    assert ledger.record_final(value)
    revision = ledger.snapshot.revision
    assert ledger.record_final(value)
    assert not ledger.record_final(final(SegmentId(2, 2), 4))
    assert ledger.snapshot.revision == revision


def test_timeout_and_speech_resume_keep_pending_final_but_invalidate_response() -> None:
    """A response token changes on speech resumption without clearing final state."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    token = ledger.response_token
    ledger.speech_started()
    assert ledger.response_token > token
    assert ledger.record_final(final(segment_id, 4))
    assert not ledger.readiness
    ledger.record_boundary(boundary(1, 4, ManualBoundaryEvent.SPEECH_START), now=2)
    ledger.record_boundary(boundary(1, 4, ManualBoundaryEvent.SPEECH_END), now=3)
    assert ledger.expire(13.0)
    assert ledger.recovery_error == "transcript_final_timeout"


def test_conflicting_final_is_sticky_and_unbound_final_blocks_readiness() -> None:
    """A conflicting provider result becomes recoverable instead of being guessed away."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    assert ledger.record_final(final(segment_id, 4, "first"))
    assert not ledger.record_final(final(segment_id, 4, "second"))
    assert ledger.recovery_error == "conflicting_segment_final"

    second = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    second.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    observation = SarvamFinalObservation(
        connection_generation=1,
        utterance_idx=3,
        outcome=SegmentFinalOutcome.EMPTY,
        text="",
        raw_message={"event": "transcript.final", "utterance_idx": 3},
    )
    assert second.observe_unbound(observation)
    assert not second.readiness


@pytest.mark.parametrize("timeout", [0.0, -1.0, inf, nan])
def test_ledger_rejects_nonfinite_or_nonpositive_timeout(timeout: float) -> None:
    """Transcript-final deadlines must always provide a usable expiration."""
    with pytest.raises(ValueError):
        TranscriptLedger(connection_generation=1, transcript_final_timeout=timeout)


def test_failed_final_and_orphan_final_enter_recovery() -> None:
    """A provider failure or unresolvable orphan cannot unlock dispatch readiness."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    assert ledger.record_final(
        SegmentFinal(segment_id, 4, SegmentFinalOutcome.FAILED, error="provider disconnected")
    )
    assert ledger.recovery_error == "segment_final_failed"

    orphan = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    orphan.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    assert orphan.observe_unbound(
        SarvamFinalObservation(1, 5, SegmentFinalOutcome.EMPTY, "", {"utterance_idx": 5})
    )
    assert orphan.recovery_error == "unexpected_unbound_final"


def test_source_records_are_immutable_when_derived_evidence_is_excluded() -> None:
    """Evidence exclusions have their own revision and cannot rewrite source provenance."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    ledger.record_final(final(segment_id, 4, "original answer"))

    before = ledger.snapshot
    assert before.source_records[0].final.text == "original answer"
    assert ledger.commit_exclusions((segment_id,))
    after = ledger.snapshot

    assert after.text == ""
    assert after.source_records == before.source_records
    assert after.source_revision == before.source_revision
    assert after.revision == before.revision
    assert after.disposition_revision > before.disposition_revision


def test_partial_source_exclusion_preserves_a_single_final_for_span_provenance() -> None:
    """Mixed answer/help can exclude a range inside one immutable STT final."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    raw = "I resolved the outage. Can you explain conflict?"
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    ledger.record_final(final(segment_id, 4, raw))

    before = ledger.snapshot
    help_start = raw.index("Can you")
    assert ledger.commit_span_exclusions(before.source_revision, ((help_start, len(raw)),))
    after = ledger.snapshot

    assert after.source_text == raw
    assert after.source_records[0].final.text == raw
    assert after.text == "I resolved the outage."
    assert after.revision == before.revision
    assert after.disposition_revision > before.disposition_revision
    assert not ledger.commit_span_exclusions(before.source_revision, ((help_start, len(raw)),))
    assert not ledger.commit_span_exclusions(before.source_revision - 1, ((0, 1),))


def test_derived_spans_project_to_raw_source_after_prior_help_is_excluded() -> None:
    """A later model sees derived text but commits its help offsets to raw source."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    raw = "Please explain. I helped the team. What means conflict?"
    first_help_end = len("Please explain. ")
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    ledger.record_final(final(segment_id, 4, raw))
    first = ledger.snapshot
    assert ledger.commit_span_exclusions(first.source_revision, ((0, first_help_end),))

    derived = ledger.snapshot
    assert derived.text == "I helped the team. What means conflict?"
    second_help = "What means conflict?"
    start = derived.text.index(second_help)
    projected = ledger.raw_spans_for_derived_spans(
        derived.source_revision,
        derived.disposition_revision,
        ((start, start + len(second_help)),),
    )
    assert projected == ((raw.index(second_help), len(raw)),)
    assert ledger.commit_span_exclusions(derived.source_revision, projected)
    assert ledger.snapshot.text == "I helped the team."
    assert ledger.snapshot.source_text == raw
    assert (
        ledger.raw_spans_for_derived_spans(
            derived.source_revision, derived.disposition_revision, ((0, 1),)
        )
        is None
    )


def test_derived_projection_splits_a_range_across_an_already_excluded_gap() -> None:
    """Derived adjacency never turns two raw ranges around help into one raw range."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    segment_id = SegmentId(1, 2)
    raw = "Answer one. Old help. Answer two."
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, segment_id)
    ledger.record_final(final(segment_id, 4, raw))
    first = ledger.snapshot
    old_start = raw.index("Old help.")
    next_answer = raw.index("Answer two.")
    assert ledger.commit_span_exclusions(first.source_revision, ((old_start, next_answer),))

    derived = ledger.snapshot
    assert derived.text == "Answer one. Answer two."
    assert ledger.project_derived_span_exclusions(
        derived.source_revision,
        derived.disposition_revision,
        ((0, len(derived.text)),),
    ) == ((0, old_start), (next_answer, len(raw)))


def test_source_overflow_retains_only_bounded_prefix_and_blocks_readiness() -> None:
    """A pending source that exceeds either resource bound cannot reach inference."""
    bytes_limited = TranscriptLedger(
        connection_generation=1,
        transcript_final_timeout=10.0,
        max_source_bytes=4,
    )
    bytes_limited.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    for boundary_id, text in enumerate(("abc", "d")):
        segment_id = SegmentId(1, boundary_id)
        bytes_limited.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_START), now=boundary_id
        )
        bytes_limited.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_END), now=boundary_id + 1
        )
        bytes_limited.bind_boundary(boundary_id, segment_id)
        bytes_limited.record_final(final(segment_id, 4, text))

    assert bytes_limited.snapshot.text == "abc"
    assert bytes_limited.snapshot.source_overflow
    assert bytes_limited.recovery_error == "source_overflow"
    assert not bytes_limited.readiness

    segments_limited = TranscriptLedger(
        connection_generation=1,
        transcript_final_timeout=10.0,
        max_source_segments=1,
    )
    segments_limited.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    for boundary_id in range(2):
        segment_id = SegmentId(1, boundary_id)
        segments_limited.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_START), now=boundary_id
        )
        segments_limited.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_END), now=boundary_id + 1
        )
        segments_limited.bind_boundary(boundary_id, segment_id)
        segments_limited.record_final(final(segment_id, 4, str(boundary_id)))
    assert segments_limited.snapshot.source_overflow
    assert len(segments_limited.source_records) == 1


def test_repair_retirement_requires_a_fresh_substantive_replacement() -> None:
    """A retired gap cannot be reclaimed by a late final or an acknowledgment."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    old_segment = SegmentId(1, 2)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_START), now=0)
    ledger.record_boundary(boundary(0, 4, ManualBoundaryEvent.SPEECH_END), now=1)
    ledger.bind_boundary(0, old_segment)
    assert ledger.expire(11)
    token = ledger.response_token

    assert ledger.begin_repair(0)
    assert ledger.response_token > token
    assert not ledger.record_final(final(old_segment, 4, "late old final"))
    assert not ledger.readiness

    new_segment = SegmentId(1, 3)
    ledger.record_boundary(boundary(1, 4, ManualBoundaryEvent.SPEECH_START), now=12)
    ledger.record_boundary(boundary(1, 4, ManualBoundaryEvent.SPEECH_END), now=13)
    ledger.bind_boundary(1, new_segment)
    ledger.record_final(final(new_segment, 4, "okay"))

    assert ledger.repair_replacement_ready
    assert not ledger.resolve_repair_replacement(1, substantive=False)
    assert not ledger.readiness
    later_segment = SegmentId(1, 4)
    ledger.record_boundary(boundary(2, 4, ManualBoundaryEvent.SPEECH_START), now=14)
    ledger.record_boundary(boundary(2, 4, ManualBoundaryEvent.SPEECH_END), now=15)
    ledger.bind_boundary(2, later_segment)
    ledger.record_final(final(later_segment, 4, "I handled the outage."))
    assert ledger.repair_replacement_ready
    assert ledger.resolve_repair_replacement(2, substantive=True)
    assert ledger.readiness
    assert ledger.snapshot.text == "okay I handled the outage."


def test_repair_requires_the_complete_known_gap_set() -> None:
    """A caller cannot retire one of several missing intervals and bypass the rest."""
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=4, question_id="q1")
    for boundary_id in (0, 1):
        ledger.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_START), now=boundary_id
        )
        ledger.record_boundary(
            boundary(boundary_id, 4, ManualBoundaryEvent.SPEECH_END), now=boundary_id + 1
        )
    assert ledger.expire(11)
    assert ledger.repairable_boundary_ids == (0, 1)
    assert not ledger.begin_repair(0)
    assert ledger.begin_repair((0, 1))
    assert ledger.repair_obligation is not None
