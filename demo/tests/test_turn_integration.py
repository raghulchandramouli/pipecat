"""Managed-task integration coverage for interview pause, gate, and reply policy."""

from __future__ import annotations

import asyncio

import pytest

from demo.interview.config import InterviewDeadlines
from demo.interview.contracts import ReplyKind, SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.ledger import TranscriptLedger
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.interview.turn_coordinator import InterviewTurnCoordinator
from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


class Clock:
    """A test-owned monotonic clock advanced without sleeping."""

    def __init__(self) -> None:
        """Initialize the test clock at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the selected monotonic time."""
        return self.now


async def _yield() -> None:
    """Allow managed processor tasks to consume an explicit coordinator wake-up."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _ready(coordinator: InterviewTurnCoordinator, clock: Clock) -> None:
    """Create one closed, explicitly bound candidate segment owned by the live ledger."""
    ledger = coordinator.ledger
    coordinator.speech_started()
    start = SarvamManualBoundary(1, 0, 7, ManualBoundaryEvent.SPEECH_START, True)
    stop = SarvamManualBoundary(1, 0, 7, ManualBoundaryEvent.SPEECH_END, True)
    assert ledger.record_boundary(start, now=clock.now)
    assert ledger.record_boundary(stop, now=clock.now)
    coordinator.speech_stopped()
    segment = SegmentId(1, 4)
    assert ledger.bind_boundary(0, segment)
    assert ledger.record_final(SegmentFinal(segment, 7, SegmentFinalOutcome.TEXT, "full answer"))
    coordinator.changed()


async def _coordinator():
    """Build a running coordinator, strict guard, and deterministic fake provider callback."""
    clock = Clock()
    context = LLMContext(messages=[{"role": "system", "content": "interview"}])
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    coordinator = InterviewTurnCoordinator(
        ledger=ledger,
        context=context,
        clock=clock,
        deadlines=InterviewDeadlines(),
    )
    guard = coordinator.create_reply_guard()
    spoken, decisions, provider_calls = [], [], []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        spoken.append((frame, direction))

    guard.push_frame = capture

    @coordinator.event_handler("on_reply_decision")
    def decision(_coordinator, result):
        decisions.append(result)

    replies: list[str] = ["● accepted"]

    async def provider(frame, direction):
        provider_calls.append((frame, direction))
        response = replies.pop(0)
        for cls, text in (
            (LLMFullResponseStartFrame, None),
            (LLMTextFrame, response),
            (LLMFullResponseEndFrame, None),
        ):
            emitted = cls(text) if text is not None else cls()
            emitted.metadata.update(frame.metadata)
            await guard.process_frame(emitted, direction)

    coordinator.set_provider(provider)
    await coordinator.setup(frame_processor_setup(TaskManager()))
    await coordinator.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    return clock, coordinator, guard, replies, provider_calls, spoken, decisions


@pytest.mark.asyncio
async def test_pause_floor_and_early_final_hold_every_dispatch_until_policy_authorizes():
    """Transcript readiness alone and direct context requests cannot bypass the 2.5-second pause."""
    clock, coordinator, _guard, _replies, calls, spoken, decisions = await _coordinator()
    try:
        await _ready(coordinator, clock)
        for moment in (0.5, 1.0, 2.0):
            clock.now = moment
            coordinator.request_dispatch(
                LLMContextFrame(LLMContext(), speculation=moment == 1.0), FrameDirection.UPSTREAM
            )
            coordinator.changed()
            await _yield()
            assert calls == spoken == decisions == []
        clock.now = 2.5
        coordinator.changed()
        await _yield()
        assert len(calls) == len(decisions) == 1
        assert decisions[0].authorization.reply_kind is ReplyKind.FOLLOW_UP
        assert decisions[0].accept_answer is True
        assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
            "accepted"
        ]
    finally:
        await coordinator.cleanup()


@pytest.mark.asyncio
async def test_incomplete_short_retry_produces_checkin_without_answer_eligibility():
    """An incomplete marker schedules the eight-second retry and preserves the candidate answer."""
    clock, coordinator, _guard, replies, calls, spoken, decisions = await _coordinator()
    replies[:] = ["◐", "● Please continue."]
    try:
        await _ready(coordinator, clock)
        clock.now = 2.5
        coordinator.changed()
        await _yield()
        assert len(calls) == 1 and spoken == []
        assert decisions[0].completion.long_wait is False
        assert coordinator.ledger.snapshot.text == "full answer"
        clock.now = 10.4
        coordinator.changed()
        await _yield()
        assert len(calls) == 1
        clock.now = 10.5
        coordinator.changed()
        await _yield()
        assert len(calls) == 2
        assert decisions[-1].authorization.reply_kind is ReplyKind.CHECK_IN
        assert decisions[-1].accept_answer is False
        assert coordinator.ledger.snapshot.text == "full answer"
        assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
            "Please continue."
        ]
    finally:
        await coordinator.cleanup()


@pytest.mark.asyncio
async def test_thinking_suppresses_direct_retries_then_emits_only_a_checkin():
    """Thinking time cancels active response authorization and hides all competing timers."""
    clock, coordinator, _guard, replies, calls, spoken, decisions = await _coordinator()
    replies[:] = ["◐", "● Still thinking?"]
    try:
        await _ready(coordinator, clock)
        clock.now = 2.5
        coordinator.changed()
        await _yield()
        assert len(calls) == 1
        clock.now = 3.0
        coordinator.request_thinking(requested_at=3.0)
        for moment in (10.5, 15.0, 32.9):
            clock.now = moment
            coordinator.request_dispatch(LLMContextFrame(LLMContext()))
            coordinator.changed()
            await _yield()
            assert len(calls) == 1
        clock.now = 33.0
        coordinator.changed()
        await _yield()
        assert len(calls) == 2
        assert decisions[-1].authorization.reply_kind is ReplyKind.CHECK_IN
        assert decisions[-1].accept_answer is False
        assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
            "Still thinking?"
        ]
    finally:
        await coordinator.cleanup()


@pytest.mark.asyncio
async def test_malformed_reply_has_no_speech_or_acceptance_and_resume_invalidates_output():
    """Malformed output is fail-closed, while resumed speech invalidates an admitted generation."""
    clock, coordinator, guard, replies, calls, spoken, decisions = await _coordinator()
    replies[:] = ["unmarked response"]
    try:
        await _ready(coordinator, clock)
        clock.now = 2.5
        coordinator.changed()
        await _yield()
        assert len(calls) == 1
        assert spoken == decisions == []
        assert coordinator.ledger.snapshot.text == "full answer"
        coordinator.speech_started()
        coordinator.changed()
        await _yield()
        assert coordinator.lookup_authorization(calls[0][0].metadata) is None
        assert guard._authorization is None
    finally:
        await coordinator.cleanup()
