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
