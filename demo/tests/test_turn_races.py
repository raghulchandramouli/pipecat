"""Race and bounded-wait checks for the assembled interview turn policy."""

import asyncio

import pytest

from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.tests.test_turn_integration import _coordinator, _ready, _yield
from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection


@pytest.mark.asyncio
async def test_repeated_short_pauses_never_dispatch_and_late_final_still_gates():
    """Each actual resumption restarts the floor without losing earlier final segments."""
    clock, gate, _guard, _replies, calls, spoken, decisions = await _coordinator()
    try:
        await _ready(gate, clock)
        for boundary, gap in enumerate((0.5, 1.0, 2.0), start=1):
            clock.now += gap
            gate.changed()
            await _yield()
            assert calls == spoken == decisions == []
            gate.speech_started()
            for event in (ManualBoundaryEvent.SPEECH_START, ManualBoundaryEvent.SPEECH_END):
                gate.ledger.record_boundary(
                    SarvamManualBoundary(1, boundary, 7, event, True), now=clock.now
                )
            gate.ledger.bind_boundary(boundary, SegmentId(1, boundary + 10))
            if boundary < 3:
                gate.ledger.record_final(
                    SegmentFinal(SegmentId(1, boundary + 10), 7, SegmentFinalOutcome.TEXT, "more")
                )
            gate.speech_stopped()
            gate.changed()
        clock.now += 2.5
        gate.changed()
        await _yield()
        assert calls == spoken == decisions == []
        gate.ledger.record_final(
            SegmentFinal(SegmentId(1, 13), 7, SegmentFinalOutcome.TEXT, "last")
        )
        gate.changed()
        await _yield()
        assert len(calls) == len(decisions) == 1
        assert calls[0][0].context.messages[-1]["content"] == "full answer more more last"
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_provider_watchdog_cancels_hung_request_then_only_checkin_is_eligible():
    """A provider that never returns cannot disable the watchdog or accept an answer."""
    clock, gate, guard, _replies, calls, spoken, decisions = await _coordinator()
    cancelled = asyncio.Event()
    rejected = []

    @gate.event_handler("on_reply_rejected")
    def reject(_gate, result):
        rejected.append(result)

    async def hanging(frame, direction):
        calls.append((frame, direction))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    gate._provider = hanging
    try:
        await _ready(gate, clock)
        clock.now = 2.5
        gate.changed()
        await _yield()
        assert len(calls) == 1
        clock.now = 47.5
        gate.changed()
        await _yield()
        assert cancelled.is_set()
        assert rejected[0].reason == "provider_timeout"
        assert spoken == decisions == []
        assert gate.ledger.snapshot.text == "full answer"
        assert gate.policy.next_deadline == 55.5
        old = calls[0][0].metadata
        for frame in (
            LLMFullResponseStartFrame(),
            LLMTextFrame("● Late"),
            LLMFullResponseEndFrame(),
        ):
            frame.metadata.update(old)
            await guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert spoken == decisions == []
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_malformed_response_recovers_with_checkin_and_no_replay_timer():
    """Invalid replies can retry safely, while replayed valid replies cannot rearm timers."""
    clock, gate, guard, replies, calls, spoken, decisions = await _coordinator()
    replies[:] = ["bad", "● Continue when ready."]
    try:
        await _ready(gate, clock)
        clock.now = 2.5
        gate.changed()
        await _yield()
        assert spoken == decisions == []
        assert gate.policy.next_deadline == 10.5
        clock.now = 10.5
        gate.changed()
        await _yield()
        assert len(decisions) == 1 and not decisions[0].accept_answer
        assert gate.policy.next_deadline is None
        for frame in (
            LLMFullResponseStartFrame(),
            LLMTextFrame("● Replayed"),
            LLMFullResponseEndFrame(),
        ):
            frame.metadata.update(calls[-1][0].metadata)
            await guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert len(decisions) == 1
        assert gate.policy.next_deadline is None
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_recovery_has_no_overdue_policy_wake_loop():
    """A transcript timeout leaves the watcher waiting for external recovery, not spinning."""
    clock, gate, _guard, _replies, calls, spoken, decisions = await _coordinator()
    try:
        gate.speech_started()
        for event in (ManualBoundaryEvent.SPEECH_START, ManualBoundaryEvent.SPEECH_END):
            gate.ledger.record_boundary(SarvamManualBoundary(1, 0, 7, event, True), now=0)
        gate.speech_stopped()
        clock.now = 100
        gate.changed()
        await _yield()
        assert gate.ledger.recovery_error == "transcript_final_timeout"
        assert gate._next_wake_deadline() is None
        assert calls == spoken == decisions == []
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_thinking_cancels_active_provider_and_restarts_with_checkin_only():
    """Thinking grace invalidates an active request while keeping candidate transcript ownership."""
    clock, gate, _guard, _replies, calls, spoken, decisions = await _coordinator()
    cancelled = []

    async def hanging(frame, direction):
        calls.append((frame, direction))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(frame)
            raise

    gate._provider = hanging
    try:
        await _ready(gate, clock)
        clock.now = 2.5
        gate.changed()
        await _yield()
        clock.now = 3
        gate.request_thinking(requested_at=3)
        await _yield()
        assert len(cancelled) == 1
        assert gate.lookup_authorization(calls[0][0].metadata) is None
        clock.now = 32.99
        gate.changed()
        await _yield()
        assert len(calls) == 1
        clock.now = 33
        gate.changed()
        await _yield()
        assert len(calls) == 2
        assert calls[1][0].metadata["interview_reply_kind"] == "check_in"
        assert not gate.lookup_authorization(calls[1][0].metadata).answer_allowed
        gate.speech_started()
        await _yield()
        assert len(cancelled) == 2
        assert spoken == decisions == []
        assert gate.ledger.snapshot.text == "full answer"
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_slow_valid_reply_is_not_superseded_by_overdue_idle_timer():
    """A completed slow response consumes older quiet timers before speech release."""
    clock, gate, guard, _replies, calls, spoken, decisions = await _coordinator()
    release = asyncio.Event()

    async def slow(frame, direction):
        calls.append((frame, direction))
        await release.wait()
        for emitted in (
            LLMFullResponseStartFrame(),
            LLMTextFrame("● Approved"),
            LLMFullResponseEndFrame(),
        ):
            emitted.metadata.update(frame.metadata)
            await guard.process_frame(emitted, direction)

    gate._provider = slow
    try:
        await _ready(gate, clock)
        clock.now = 2.5
        gate.changed()
        await _yield()
        clock.now = 20
        release.set()
        gate.changed()
        await _yield()
        assert len(calls) == len(decisions) == 1
        assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
            "Approved"
        ]
        assert gate.policy.next_deadline is None
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_no_speech_idle_notifies_without_inventing_a_finalized_answer():
    """An unanswered question produces an idle notification while model admission stays closed."""
    clock, gate, _guard, _replies, calls, spoken, decisions = await _coordinator()
    actions = []

    @gate.event_handler("on_policy_action")
    def action(_gate, result):
        actions.append(result)

    try:
        gate.begin_waiting()
        clock.now = 14.9
        gate.changed()
        await _yield()
        assert actions == []
        clock.now = 15
        gate.changed()
        await _yield()
        assert [item.reason for item in actions] == ["user_idle_timeout"]
        assert calls == spoken == decisions == []
        assert gate.ledger.snapshot.segments == ()
    finally:
        await gate.cleanup()
