"""Exercise strict Gemini streams through the interview coordinator and reply guard."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from demo.interview.config import InterviewDeadlines
from demo.interview.contracts import ReplyKind, SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.google_llm import InterviewGoogleLLMService
from demo.interview.ledger import TranscriptLedger
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.interview.turn_coordinator import InterviewTurnCoordinator
from pipecat.frames.frames import (
    ErrorFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    UserTurnInferenceCompletedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


class Clock:
    """A test-owned monotonic clock advanced without wall-clock sleeps."""

    def __init__(self) -> None:
        """Initialize the clock at the start of a candidate turn."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the selected monotonic time."""
        return self.now


async def _yield() -> None:
    """Allow managed coordinator and provider tasks to run after an explicit wake-up."""
    for _ in range(30):
        await asyncio.sleep(0)


def _text_chunk(text: str) -> SimpleNamespace:
    """Build the subset of a Gemini stream chunk consumed by the real service."""
    return _chunk(text=text)


def _function_chunk(name: str) -> SimpleNamespace:
    """Build one Gemini function-call chunk without SDK network objects."""
    return _chunk(function_call=SimpleNamespace(id="tool-1", name=name, args={}))


def _chunk(*, text: str | None = None, function_call=None) -> SimpleNamespace:
    """Build one ordinary candidate part for ``GoogleLLMService._process_context``."""
    part = SimpleNamespace(
        text=text,
        thought=False,
        function_call=function_call,
        inline_data=None,
        thought_signature=None,
    )
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[part]), grounding_metadata=None, finish_reason=None
    )
    return SimpleNamespace(usage_metadata=None, candidates=[candidate])


async def _stream(chunks: list[SimpleNamespace]):
    """Return an SDK-shaped asynchronous stream from fixed test chunks."""

    async def iterator():
        for chunk in chunks:
            yield chunk

    return iterator()


async def _setup(chunks: list[SimpleNamespace]):
    """Create a live coordinator, strict Gemini service, and pre-TTS reply guard."""
    clock = Clock()
    context = LLMContext(messages=[{"role": "system", "content": "Interview"}])
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    coordinator = InterviewTurnCoordinator(
        ledger=ledger,
        context=context,
        deadlines=InterviewDeadlines(),
        clock=clock,
    )

    async def generate_content_stream(**_kwargs):
        return await _stream(chunks)

    sdk = AsyncMock(side_effect=generate_content_stream)
    with patch.object(InterviewGoogleLLMService, "create_client"):
        service = InterviewGoogleLLMService(coordinator=coordinator, api_key="fixture")
    service._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=sdk), aclose=AsyncMock())
    )
    guard = coordinator.create_reply_guard()
    raw, spoken, decisions = [], [], []

    async def capture_spoken(frame, direction=FrameDirection.DOWNSTREAM):
        spoken.append((frame, direction))

    guard.push_frame = capture_spoken

    @coordinator.event_handler("on_reply_decision")
    def decided(_coordinator, decision):
        decisions.append(decision)

    setup = frame_processor_setup(TaskManager())
    await coordinator.setup(setup)
    await service.setup(setup)
    await guard.setup(setup)
    await coordinator.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await service.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    return clock, coordinator, service, guard, sdk, raw, spoken, decisions


async def _make_answer_ready(coordinator: InterviewTurnCoordinator, clock: Clock) -> None:
    """Record one canonical local final and advance to its permitted semantic probe."""
    coordinator.speech_started()
    start = SarvamManualBoundary(1, 0, 7, ManualBoundaryEvent.SPEECH_START, True)
    stop = SarvamManualBoundary(1, 0, 7, ManualBoundaryEvent.SPEECH_END, True)
    assert coordinator.ledger.record_boundary(start, now=clock.now)
    assert coordinator.ledger.record_boundary(stop, now=clock.now)
    coordinator.speech_stopped()
    segment = SegmentId(1, 4)
    assert coordinator.ledger.bind_boundary(0, segment)
    assert coordinator.ledger.record_final(
        SegmentFinal(segment, 7, SegmentFinalOutcome.TEXT, "candidate answer")
    )
    clock.now = 2.5
    coordinator.changed()


async def _cleanup(coordinator, service, guard) -> None:
    """Release all processor-owned tasks after a strict stream test."""
    await guard.cleanup()
    await coordinator.cleanup()
    await service.cleanup()


@pytest.mark.asyncio
async def test_strict_google_stream_reaches_guard_raw_and_releases_only_clean_complete_reply():
    """The real Gemini stream bypasses built-in marker parsing and uses guard authorization."""
    clock, coordinator, service, guard, sdk, raw, spoken, decisions = await _setup(
        [_text_chunk("● "), _text_chunk("A clean follow-up")]
    )

    async def capture(_service, frame, direction=FrameDirection.DOWNSTREAM):
        raw.append((frame, direction))
        await guard.process_frame(frame, direction)

    try:
        # A runtime setting must not reactivate the base service's marker parser or timers.
        service._filter_incomplete_user_turns = True
        service._settings.filter_incomplete_user_turns = True
        with patch.object(LLMService, "push_frame", capture):
            await _make_answer_ready(coordinator, clock)
            await _yield()

        sdk.assert_awaited_once()
        raw_text = [frame.text for frame, _ in raw if isinstance(frame, LLMTextFrame)]
        assert raw_text == ["● ", "A clean follow-up"]
        assert not any(isinstance(frame, UserTurnInferenceCompletedFrame) for frame, _ in raw)
        assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
            "A clean follow-up"
        ]
        assert not any(
            marker in frame.text
            for frame, _ in spoken
            if isinstance(frame, LLMTextFrame)
            for marker in ("●", "◐", "○")
        )
        assert len(decisions) == 1
        assert decisions[0].authorization == coordinator.lookup_authorization(
            next(frame.metadata for frame, _ in raw if isinstance(frame, LLMFullResponseStartFrame))
        )
        assert decisions[0].authorization.reply_kind is ReplyKind.FOLLOW_UP
        assert decisions[0].accept_answer is True
    finally:
        await _cleanup(coordinator, service, guard)


@pytest.mark.asyncio
async def test_strict_google_buffers_until_end_and_drops_a_malformed_marker_suffix():
    """An early plausible marker remains non-speech until the complete stream is validated."""
    first_chunk = asyncio.Event()
    release_suffix = asyncio.Event()

    async def paused_stream(**_kwargs):
        async def iterator():
            first_chunk.set()
            yield _text_chunk("● answer")
            await release_suffix.wait()
            yield _text_chunk(" ◐")

        return iterator()

    clock, coordinator, service, guard, sdk, raw, spoken, decisions = await _setup([])
    sdk.side_effect = paused_stream

    async def capture(_service, frame, direction=FrameDirection.DOWNSTREAM):
        raw.append((frame, direction))
        await guard.process_frame(frame, direction)

    try:
        with patch.object(LLMService, "push_frame", capture):
            await _make_answer_ready(coordinator, clock)
            await asyncio.wait_for(first_chunk.wait(), timeout=1)
            await _yield()
            assert [frame for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == []
            release_suffix.set()
            await _yield()

        assert [frame.text for frame, _ in raw if isinstance(frame, LLMTextFrame)] == [
            "● answer",
            " ◐",
        ]
        assert not any(isinstance(frame, UserTurnInferenceCompletedFrame) for frame, _ in raw)
        assert [frame for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == []
        assert decisions == []
    finally:
        await _cleanup(coordinator, service, guard)


@pytest.mark.asyncio
async def test_strict_google_function_calls_cannot_run_registered_callbacks():
    """A model tool call reaches strict ``run_function_calls`` but cannot mutate application state."""
    clock, coordinator, service, guard, _sdk, raw, _spoken, decisions = await _setup(
        [_function_chunk("mutate")]
    )
    mutated = False

    async def mutate(_params) -> None:
        nonlocal mutated
        mutated = True

    service.register_function("mutate", mutate)

    async def capture(_service, frame, direction=FrameDirection.DOWNSTREAM):
        raw.append((frame, direction))
        await guard.process_frame(frame, direction)

    try:
        with patch.object(LLMService, "push_frame", capture):
            await _make_answer_ready(coordinator, clock)
            await _yield()

        assert mutated is False
        assert not any(isinstance(frame, FunctionCallsStartedFrame) for frame, _ in raw)
        assert not any(isinstance(frame, UserTurnInferenceCompletedFrame) for frame, _ in raw)
        assert any(isinstance(frame, ErrorFrame) for frame, _ in raw)
        assert decisions == []
    finally:
        await _cleanup(coordinator, service, guard)
