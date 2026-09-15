"""Exercise the real Google service up to a mocked SDK request boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.google_llm import InterviewGoogleLLMService
from demo.interview.ledger import TranscriptLedger
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.interview.transcript_gate import TranscriptLedgerProcessor
from pipecat.frames.frames import LLMContextFrame, LLMContextSummaryRequestFrame, StartFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


async def _empty_stream(**kwargs):
    async def chunks():
        if False:
            yield

    return chunks()


async def _setup():
    context = LLMContext(messages=[{"role": "system", "content": "Interview"}])
    ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    gate = TranscriptLedgerProcessor(ledger=ledger, context=context)
    sdk = AsyncMock(side_effect=_empty_stream)
    with patch.object(InterviewGoogleLLMService, "create_client"):
        service = InterviewGoogleLLMService(coordinator=gate, api_key="fixture")
    service._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=sdk), aclose=AsyncMock())
    )
    setup = frame_processor_setup(TaskManager())
    await gate.setup(setup)
    await service.setup(setup)
    await gate.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await service.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    return context, ledger, gate, service, sdk


def _complete(ledger):
    for event in (ManualBoundaryEvent.SPEECH_START, ManualBoundaryEvent.SPEECH_END):
        ledger.record_boundary(SarvamManualBoundary(1, 0, 7, event, True), now=0)
    ledger.bind_boundary(0, SegmentId(1, 4))
    ledger.record_final(SegmentFinal(SegmentId(1, 4), 7, SegmentFinalOutcome.TEXT, "My answer"))


@pytest.mark.asyncio
async def test_real_google_sdk_waits_and_receives_only_complete_canonical_snapshot():
    """The real SDK boundary receives the selected settings and complete pending answer."""
    context, ledger, gate, service, sdk = await _setup()
    try:
        await service.process_frame(LLMContextFrame(LLMContext()), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.03)
        sdk.assert_not_awaited()
        _complete(ledger)
        gate.changed()
        await service.process_frame(LLMContextFrame(LLMContext()), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        sdk.assert_awaited_once()
        args = sdk.await_args.kwargs
        assert args["model"] == "gemini-3.8-flash"
        assert args["config"].thinking_config.thinking_level.value.lower() == "low"
        assert args["contents"][-1].parts[0].text == "My answer"
        assert context.messages == [{"role": "system", "content": "Interview"}]
    finally:
        await gate.cleanup()
        await service.cleanup()


@pytest.mark.asyncio
async def test_speech_resuming_after_admission_prevents_actual_sdk_call():
    """A response invalidated after snapshot creation cannot start a provider request."""
    _, ledger, gate, service, sdk = await _setup()
    try:
        _complete(ledger)

        @gate.event_handler("on_dispatch")
        def resume(_gate, frame):
            gate.speech_started()

        gate.changed()
        await service.process_frame(LLMContextFrame(LLMContext()), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.05)
        sdk.assert_not_awaited()
        assert ledger.snapshot.text == "My answer"
    finally:
        await gate.cleanup()
        await service.cleanup()


@pytest.mark.asyncio
async def test_direct_inference_and_unauthorized_stream_cannot_bypass_gate():
    """Out-of-band entry points cannot issue unadmitted requests."""
    _, _, gate, service, sdk = await _setup()
    try:
        with pytest.raises(RuntimeError, match="gated LLMContextFrame"):
            await service.run_inference(LLMContext())
        with pytest.raises(asyncio.CancelledError):
            await service._stream_content(LLMContext())
        with patch.object(service, "push_error", new_callable=AsyncMock) as error:
            await service.process_frame(
                LLMContextSummaryRequestFrame("summary", LLMContext(), 2, 100, "Summarize"),
                FrameDirection.UPSTREAM,
            )
            error.assert_awaited_once()
        sdk.assert_not_awaited()
    finally:
        await gate.cleanup()
        await service.cleanup()


@pytest.mark.asyncio
async def test_cancel_resistant_provider_output_is_dropped_and_context_is_isolated():
    """Late provider output is suppressed even if a provider swallows cancellation."""
    from pipecat.frames.frames import LLMTextFrame
    from pipecat.services.llm_service import LLMService

    context, ledger, gate, service, _sdk = await _setup()
    started = asyncio.Event()
    finished = asyncio.Event()
    emitted = []

    async def capture(_service, frame, direction=FrameDirection.DOWNSTREAM):
        emitted.append(frame)

    async def delayed(snapshot):
        snapshot.messages[0]["content"] = "mutated private history"
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await service.push_frame(LLMTextFrame("stale response"))
            finished.set()

    try:
        _complete(ledger)
        with (
            patch.object(service, "_process_context", delayed),
            patch.object(LLMService, "push_frame", capture),
        ):
            gate.changed()
            await service.process_frame(LLMContextFrame(LLMContext()), FrameDirection.DOWNSTREAM)
            await asyncio.wait_for(started.wait(), timeout=1)
            gate.speech_started()
            await asyncio.wait_for(finished.wait(), timeout=1)
        assert not any(isinstance(frame, LLMTextFrame) for frame in emitted)
        assert context.messages == [{"role": "system", "content": "Interview"}]
        assert ledger.snapshot.text == "My answer"
    finally:
        await gate.cleanup()
        await service.cleanup()
