"""Integration coverage for transcript admission before Gemini-style dispatch."""

from __future__ import annotations

import asyncio

import pytest

from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.ledger import TranscriptLedger
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.interview.transcript_gate import (
    InterviewUserAggregator,
    TranscriptGatedLLMMixin,
    TranscriptLedgerProcessor,
)
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMMessagesAppendFrame,
    LLMMessagesTransformFrame,
    LLMMessagesUpdateFrame,
    LLMRunFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import UserTurnSpeculation
from pipecat.turns.user_stop import UserTurnStoppedParams
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


def _boundary(boundary_id: int, event: ManualBoundaryEvent) -> SarvamManualBoundary:
    return SarvamManualBoundary(1, boundary_id, 7, event, True)


def _ready(ledger: TranscriptLedger, *values: tuple[int, str]) -> None:
    """Record closed, explicitly bound transcript segments in local boundary order."""
    for boundary_id, (utterance_idx, text) in enumerate(values):
        ledger.record_boundary(_boundary(boundary_id, ManualBoundaryEvent.SPEECH_START), now=0)
        ledger.record_boundary(_boundary(boundary_id, ManualBoundaryEvent.SPEECH_END), now=1)
        segment_id = SegmentId(1, utterance_idx)
        assert ledger.bind_boundary(boundary_id, segment_id)
        assert ledger.record_final(SegmentFinal(segment_id, 7, SegmentFinalOutcome.TEXT, text))


class _Recorder(FrameProcessor):
    """A provider base that records only context frames admitted by its mixin."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[LLMContextFrame, FrameDirection]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, LLMContextFrame):
            self.calls.append((frame, direction))
        await super().process_frame(frame, direction)


class _GatedRecorder(TranscriptGatedLLMMixin, _Recorder):
    """Real admission mixin over a network-free provider implementation."""


async def _components(*, with_provider: bool = True):
    context = LLMContext(messages=[{"role": "system", "content": "canonical history"}])
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=1.0)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    gate = TranscriptLedgerProcessor(ledger=ledger, context=context)
    provider = _GatedRecorder(coordinator=gate) if with_provider else None
    await gate.setup(frame_processor_setup(TaskManager()))
    await gate.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    return context, ledger, gate, provider


async def _settle() -> None:
    """Give the coordinator's managed watcher one event-loop turn to admit work."""
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_provider_is_not_called_for_open_or_unresolved_speech_then_admits_once_in_order():
    """A context request waits for all final outcomes and uses canonical segment order once."""
    context, ledger, gate, provider = await _components()
    try:
        ledger.record_boundary(_boundary(0, ManualBoundaryEvent.SPEECH_START), now=0)
        gate.changed()
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await _settle()
        assert provider.calls == []
        ledger.record_boundary(_boundary(0, ManualBoundaryEvent.SPEECH_END), now=1)
        ledger.bind_boundary(0, SegmentId(1, 9))
        ledger.record_final(SegmentFinal(SegmentId(1, 9), 7, SegmentFinalOutcome.TEXT, "first"))
        ledger.record_boundary(_boundary(1, ManualBoundaryEvent.SPEECH_START), now=1)
        ledger.record_boundary(_boundary(1, ManualBoundaryEvent.SPEECH_END), now=2)
        ledger.bind_boundary(1, SegmentId(1, 2))
        ledger.record_final(SegmentFinal(SegmentId(1, 2), 7, SegmentFinalOutcome.TEXT, "second"))
        gate.changed()
        # The second speech boundary invalidated the earlier intent, so the
        # completed answer needs a fresh explicit inference request.
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await _settle()
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages[-1]["content"] == "first second"
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await _settle()
        assert len(provider.calls) == 2
        assert context.messages == [{"role": "system", "content": "canonical history"}]
        assert all(call[0].metadata["interview_candidate_turn_id"] == 7 for call in provider.calls)
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_empty_final_unblocks_admission_without_inventing_user_message():
    """An empty terminal final resolves the wait but never reaches the provider as text."""
    context, ledger, gate, provider = await _components()
    try:
        ledger.record_boundary(_boundary(0, ManualBoundaryEvent.SPEECH_START), now=0)
        ledger.record_boundary(_boundary(0, ManualBoundaryEvent.SPEECH_END), now=1)
        segment_id = SegmentId(1, 4)
        ledger.bind_boundary(0, segment_id)
        ledger.record_final(SegmentFinal(segment_id, 7, SegmentFinalOutcome.EMPTY))
        gate.changed()
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await _settle()
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages == context.messages
        assert ledger.snapshot.text == ""
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_new_speech_invalidates_a_pending_generation_and_late_provider_output():
    """Speech resumption cancels a managed provider task while retaining its final transcript."""
    _context, ledger, gate, _provider = await _components(with_provider=False)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    output: list[str] = []

    async def slow_provider(frame: LLMContextFrame, direction: FrameDirection) -> None:
        started.set()
        try:
            await asyncio.sleep(1)
            output.append("late")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    gate.set_provider(slow_provider)
    try:
        _ready(ledger, (9, "first"), (2, "second"))
        gate.changed()
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        gate.speech_started()
        await _settle()
        assert cancelled.is_set()
        assert output == []
        assert ledger.snapshot.text == "first second"
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_user_aggregator_routes_context_runs_to_the_gate_without_committing_transcripts():
    """The real user aggregator's dispatch override cannot bypass transcript admission."""
    context, ledger, gate, provider = await _components()
    user = InterviewUserAggregator(context, coordinator=gate)
    try:
        _ready(ledger, (9, "first"), (2, "second"))
        gate.changed()
        await user._push_aggregation()
        await _settle()
        # The real override emits a context run which the mixin routes back to the gate.
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages[-1]["content"] == "first second"
        assert context.messages == [{"role": "system", "content": "canonical history"}]
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_raw_transcription_never_reaches_user_aggregator_or_provider():
    """Only ledger snapshots can supply user text; raw Pipecat finals are suppressed."""
    context, _ledger, gate, provider = await _components()
    user = InterviewUserAggregator(context, coordinator=gate)
    frame = TranscriptionFrame(text="raw", user_id="candidate", timestamp="now", finalized=True)
    try:
        await user.process_frame(frame, FrameDirection.DOWNSTREAM)
        await _settle()
        assert provider.calls == []
        assert context.messages == [{"role": "system", "content": "canonical history"}]
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_mixin_admits_assistant_upstream_context_once_and_replaces_speculation():
    """The assistant-side upstream path has the same canonical admission boundary."""
    context, ledger, gate, provider = await _components()
    try:
        _ready(ledger, (3, "canonical"))
        gate.changed()
        provisional = LLMContext(messages=[{"role": "user", "content": "speculative bypass"}])
        await provider.process_frame(
            LLMContextFrame(context=provisional, speculation=True), FrameDirection.UPSTREAM
        )
        await _settle()
        assert len(provider.calls) == 1
        frame, direction = provider.calls[0]
        assert direction is FrameDirection.UPSTREAM
        assert frame.context.messages[-1]["content"] == "canonical"
        assert all("speculative bypass" not in str(message) for message in frame.context.messages)
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_user_aggregator_speculation_and_controller_context_push_use_canonical_snapshot():
    """Neither eager speculation nor a controller-originated context frame can carry raw text."""
    context, ledger, gate, provider = await _components()
    user = InterviewUserAggregator(context, coordinator=gate)
    try:
        _ready(ledger, (8, "final answer"))
        gate.changed()
        await user._run_speculative_inference(UserTurnSpeculation("eager invented answer"))
        await _settle()
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages[-1]["content"] == "final answer"
        # The callback used by real turn strategies queues a frame in a live
        # pipeline. Its context payload is still routed through this override.
        await user.push_frame(LLMContextFrame(context=LLMContext()), FrameDirection.DOWNSTREAM)
        await _settle()
        assert len(provider.calls) == 2
        assert all(
            call[0].context.messages[-1]["content"] == "final answer" for call in provider.calls
        )
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        LLMRunFrame(),
        LLMMessagesAppendFrame(
            messages=[{"role": "developer", "content": "retry once"}], run_llm=True
        ),
        LLMMessagesUpdateFrame(
            messages=[{"role": "developer", "content": "updated retry"}], run_llm=True
        ),
        LLMMessagesTransformFrame(
            transform=lambda messages: [
                *messages,
                {"role": "developer", "content": "transformed retry"},
            ],
            run_llm=True,
        ),
    ],
)
async def test_user_aggregator_dispatch_frames_cannot_bypass_gate(frame: Frame):
    """Every user-side dispatch API routes its resulting context through canonical admission."""
    context, ledger, gate, provider = await _components()
    user = InterviewUserAggregator(
        context,
        coordinator=gate,
        params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
    )
    try:
        _ready(ledger, (5, "canonical answer"))
        gate.changed()
        await user.process_frame(frame, FrameDirection.DOWNSTREAM)
        await _settle()
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages[-1]["content"] == "canonical answer"
        assert provider.calls[0][0].context.messages is not context.messages
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_deferred_user_stop_and_assistant_upstream_retry_preserve_canonical_history():
    """Both aggregator halves submit retries through the same admission callback."""
    context, ledger, gate, provider = await _components()
    user = InterviewUserAggregator(
        context,
        coordinator=gate,
        params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
    )
    assistant = LLMAssistantAggregator(context)

    async def send_to_provider(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        await provider.process_frame(frame, direction)

    assistant.push_frame = send_to_provider
    try:
        _ready(ledger, (5, "canonical answer"))
        context.add_message({"role": "developer", "content": "retry with evidence"})
        gate.changed()
        await user._on_user_turn_stopped(None, None, UserTurnStoppedParams(False))
        await _settle()
        await assistant._handle_llm_run(LLMRunFrame())
        await _settle()
        assert len(provider.calls) == 2
        assert [direction for _, direction in provider.calls] == [
            FrameDirection.DOWNSTREAM,
            FrameDirection.UPSTREAM,
        ]
        for admitted, _direction in provider.calls:
            assert admitted.context.messages[-2:] == [
                {"role": "developer", "content": "retry with evidence"},
                {"role": "user", "content": "canonical answer"},
            ]
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_strategy_on_push_frame_queue_cannot_bypass_admission():
    """A real controller callback queues its context run and still reaches the gate."""
    context, ledger, gate, provider = await _components()
    user = InterviewUserAggregator(
        context,
        coordinator=gate,
        params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
    )
    await user.setup(frame_processor_setup(TaskManager()))
    try:
        _ready(ledger, (6, "queued canonical"))
        gate.changed()
        await user.queue_frame(StartFrame())
        await _settle()
        await user._on_push_frame(None, LLMContextFrame(context=LLMContext()))
        await _settle()
        assert len(provider.calls) == 1
        assert provider.calls[0][0].context.messages[-1]["content"] == "queued canonical"
    finally:
        await user.cleanup()
        await gate.cleanup()


@pytest.mark.asyncio
async def test_idle_callback_reports_without_creating_an_ungated_llm_run():
    """The real idle callback is observational; it cannot bypass pending transcript admission."""
    context, _ledger, gate, provider = await _components()
    user = InterviewUserAggregator(
        context,
        coordinator=gate,
        params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
    )
    idle = asyncio.Event()

    @user.event_handler("on_user_turn_idle")
    async def observed(_user) -> None:
        idle.set()

    try:
        await user._on_user_turn_idle(None)
        await asyncio.wait_for(idle.wait(), timeout=0.2)
        await _settle()
        assert provider.calls == []
    finally:
        await gate.cleanup()


@pytest.mark.asyncio
async def test_deadline_recovery_and_teardown_prevent_provider_dispatch():
    """A missing final enters recovery, and close cancels any later admission."""
    context = LLMContext()
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=0.01)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    gate = TranscriptLedgerProcessor(ledger=ledger, context=context)
    provider = _GatedRecorder(coordinator=gate)
    recoveries: list[str] = []

    @gate.event_handler("on_transcript_recovery")
    def recovered(_gate, reason: str) -> None:
        recoveries.append(reason)

    await gate.setup(frame_processor_setup(TaskManager()))
    await gate.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    try:
        ledger.record_boundary(_boundary(0, ManualBoundaryEvent.SPEECH_START), now=0)
        ledger.record_boundary(
            _boundary(0, ManualBoundaryEvent.SPEECH_END), now=asyncio.get_running_loop().time()
        )
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        gate.changed()
        await asyncio.sleep(0.05)
        assert ledger.recovery_error == "transcript_final_timeout"
        assert recoveries == ["transcript_final_timeout"]
        assert provider.calls == []
        await gate.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        gate.request_dispatch(LLMContextFrame(context=LLMContext()))
        await _settle()
        assert provider.calls == []
    finally:
        await gate.cleanup()
