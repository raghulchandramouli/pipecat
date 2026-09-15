"""Exercise Sarvam event hooks through actual framework receive and boundary paths."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close
from websockets.protocol import State

from demo.interview.contracts import SegmentFinalOutcome, SegmentId
from demo.interview.sarvam_events import (
    ConnectionEventKind,
    InterviewSarvamRealtimeSTTService,
    ManualBoundaryEvent,
)
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class FakeWebsocket:
    """Finite provider script with observable wire sends and injected failures."""

    def __init__(self, messages=()):
        """Store incoming messages and independent connection state."""
        self.messages = messages
        self.state = State.OPEN
        self.sent = []
        self.fail_on = None

    async def send(self, message):
        """Capture successfully sent JSON or simulate a wire failure."""
        payload = json.loads(message)
        if payload["event"] == self.fail_on:
            raise RuntimeError("injected send failure")
        self.sent.append(payload)

    async def close(self):
        """Close without accessing a real provider."""
        self.state = State.CLOSED

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for message in self.messages:
            if isinstance(message, BaseException):
                raise message
            yield json.dumps(message)


def listen(service, event_name, values):
    """Collect a real synchronous service event without introducing a wait."""

    @service.event_handler(event_name)
    def capture(_service, value):
        values.append(value)


@pytest.fixture
def service_factory(monkeypatch):
    """Build real services, faking only provider I/O and outgoing frame sinks."""

    def make(generation=4, **kwargs):
        service = InterviewSarvamRealtimeSTTService(
            api_key="test-key", connection_generation=generation, **kwargs
        )
        service._websocket = FakeWebsocket()
        service._sample_rate = 16000
        monkeypatch.setattr(service, "push_frame", AsyncMock())
        monkeypatch.setattr(service, "emit_stt_usage_metrics", AsyncMock())
        monkeypatch.setattr(service, "_trace_transcription", AsyncMock())
        return service

    return make


async def start_boundary(service, candidate):
    """Capture the public boundary notification produced by a real VAD start."""
    records = []
    listen(service, "on_manual_boundary", records)
    service.set_candidate_turn(candidate)
    await service.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert records[-1].event is ManualBoundaryEvent.SPEECH_START
    return records[-1].boundary_id


async def end_boundary(service):
    """Send the matching local VAD stop through the real processor."""
    await service.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def observe_partial(service, idx):
    """Expose an actual provider ID without completing its transcript."""
    await service._handle_message(
        {"event": "transcript.partial", "utterance_idx": idx, "text": "provisional"}
    )


async def final(service, idx, text="answer"):
    """Drive the real parsed provider message dispatcher."""
    await service._handle_message({"event": "transcript.final", "utterance_idx": idx, "text": text})


@pytest.mark.asyncio
async def test_bound_final_precedes_usage_and_base_transcript(service_factory):
    """Readiness hooks run before the base metrics/frame path without replacing it."""
    service = service_factory()
    boundary_id = await start_boundary(service, 7)
    await end_boundary(service)
    await observe_partial(service, 12)
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=12)
    order, outcomes = [], []
    listen(service, "on_segment_final", outcomes)

    @service.event_handler("on_final_observed")
    def observed(_service, _event):
        order.append("observation")

    @service.event_handler("on_segment_final")
    def bound(_service, _event):
        order.append("bound")

    async def usage():
        order.append("usage")

    async def push(frame):
        if isinstance(frame, TranscriptionFrame):
            order.append("transcript")
            assert frame.text == "Answer"
            assert frame.finalized is True
            assert frame.result["utterance_idx"] == 12

    service.emit_stt_usage_metrics.side_effect = usage
    service.push_frame.side_effect = push
    await final(service, 12, " Answer ")
    assert order == ["observation", "bound", "usage", "transcript"]
    assert outcomes[0].candidate_turn_id == 7
    assert outcomes[0].segment_id == SegmentId(4, 12)
    service._trace_transcription.assert_awaited_once()


@pytest.mark.parametrize("text", ["", "  ", None])
@pytest.mark.asyncio
async def test_empty_final_binding_inside_callback_is_terminal_once(service_factory, text):
    """No-partial empty finals can be bound reentrantly before base usage handling."""
    service = service_factory()
    boundary_id = await start_boundary(service, 7)
    await end_boundary(service)
    outcomes, observations, bind_results = [], [], []
    listen(service, "on_segment_final", outcomes)
    listen(service, "on_final_observed", observations)

    @service.event_handler("on_unbound_final")
    async def bind_empty(_service, observation):
        bind_results.append(
            await service.bind_provider_utterance(
                boundary_id=boundary_id, utterance_idx=observation.utterance_idx
            )
        )

    await final(service, 0, text)
    assert bind_results == [True]
    assert len(observations) == len(outcomes) == 1
    assert outcomes[0].outcome is SegmentFinalOutcome.EMPTY
    assert outcomes[0].candidate_turn_id == 7
    assert outcomes[0].text == ""
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=0)
    assert len(outcomes) == 1
    service.emit_stt_usage_metrics.assert_awaited_once()
    assert not any(
        isinstance(call.args[0], TranscriptionFrame) for call in service.push_frame.call_args_list
    )


@pytest.mark.asyncio
async def test_reversed_delayed_duplicate_finals_keep_two_original_owners(service_factory):
    """Arrival order and a newly armed candidate cannot reassign old segments."""
    service = service_factory()
    first = await start_boundary(service, 20)
    await observe_partial(service, 9)
    assert await service.bind_provider_utterance(boundary_id=first, utterance_idx=9)
    await end_boundary(service)
    second = await start_boundary(service, 21)
    await observe_partial(service, 2)
    assert await service.bind_provider_utterance(boundary_id=second, utterance_idx=2)
    await end_boundary(service)
    service.set_candidate_turn(22)
    outcomes = []
    listen(service, "on_segment_final", outcomes)
    for idx, text in [(2, "second"), (9, "first"), (9, "first")]:
        await final(service, idx, text)
    assert [(item.segment_id.utterance_idx, item.candidate_turn_id) for item in outcomes] == [
        (2, 21),
        (9, 20),
        (9, 20),
    ]
    service.emit_stt_usage_metrics.assert_awaited()
    assert service.emit_stt_usage_metrics.await_count == 3


@pytest.mark.asyncio
async def test_retrospective_binding_replays_duplicates_without_reemitting_base_frames(
    service_factory,
):
    """Late explicit mapping replays retained outcomes while base frames remain unchanged."""
    service = service_factory()
    boundary_id = await start_boundary(service, 7)
    await end_boundary(service)
    outcomes, unbound = [], []
    listen(service, "on_segment_final", outcomes)
    listen(service, "on_unbound_final", unbound)
    await final(service, 40)
    await final(service, 40)
    before = service.push_frame.await_count
    assert outcomes == []
    assert len(unbound) == 2
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=40)
    assert len(outcomes) == 2
    assert all(item.candidate_turn_id == 7 for item in outcomes)
    assert service.push_frame.await_count == before


@pytest.mark.parametrize("idx", [None, True, -1, "2", 1.5])
@pytest.mark.asyncio
async def test_invalid_provider_ids_remain_observable_and_uncorrelated(service_factory, idx):
    """Invalid wire IDs never become fabricated segment identity or candidate ownership."""
    service = service_factory()
    outcomes, observations, failures = [], [], []
    listen(service, "on_segment_final", outcomes)
    listen(service, "on_unbound_final", observations)
    listen(service, "on_correlation_failure", failures)
    await final(service, idx)
    assert outcomes == []
    assert len(observations) == 1
    assert observations[0].utterance_idx is None
    assert failures[0].reason == "missing_or_invalid_utterance_idx"
    service.emit_stt_usage_metrics.assert_awaited_once()


@pytest.mark.asyncio
async def test_unseen_and_conflicting_bindings_cannot_transfer_ownership(service_factory):
    """Only observed IDs bind, and a bound ID cannot migrate to another candidate."""
    service = service_factory()
    first = await start_boundary(service, 20)
    await end_boundary(service)
    second = await start_boundary(service, 21)
    await end_boundary(service)
    failures, outcomes = [], []
    listen(service, "on_correlation_failure", failures)
    listen(service, "on_segment_final", outcomes)
    assert not await service.bind_provider_utterance(boundary_id=first, utterance_idx=50)
    await observe_partial(service, 50)
    assert not await service.bind_provider_utterance(boundary_id=999, utterance_idx=50)
    assert await service.bind_provider_utterance(boundary_id=first, utterance_idx=50)
    assert not await service.bind_provider_utterance(boundary_id=second, utterance_idx=50)
    await final(service, 50)
    assert outcomes[0].candidate_turn_id == 20
    assert [failure.reason for failure in failures] == [
        "unobserved_utterance_idx",
        "unknown_boundary",
        "ambiguous_utterance_binding",
    ]


@pytest.mark.asyncio
async def test_retention_overflow_is_visible_and_does_not_drop_raw_observations(service_factory):
    """Overflow preserves raw visibility and emits a recovery diagnostic."""
    service = service_factory(max_unbound_finals=1)
    boundary_id = await start_boundary(service, 20)
    await end_boundary(service)
    failures, observations, outcomes = [], [], []
    listen(service, "on_correlation_failure", failures)
    listen(service, "on_final_observed", observations)
    listen(service, "on_segment_final", outcomes)
    await final(service, 1, "retained")
    await final(service, 2, "overflow")
    assert len(observations) == 2
    assert failures[-1].reason == "unbound_final_retention_exhausted"
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=1)
    assert [item.text for item in outcomes] == ["retained"]
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=2)
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_boundary_flushes_real_audio_without_waiting_for_missing_final(service_factory):
    """The unchanged wire order flushes the tail before closing a candidate segment."""
    service = service_factory()
    events = []
    listen(service, "on_manual_boundary", events)
    boundary_id = await start_boundary(service, 8)
    audio = b"\x01" * 400
    async for _ in service.run_stt(audio):
        pass
    # No STT final is ever supplied; closing the boundary must still return.
    await asyncio.wait_for(end_boundary(service), timeout=1.0)
    assert service._websocket.sent == [
        {"event": "speech_start"},
        {"event": "audio_input", "audio": base64.b64encode(audio).decode()},
        {"event": "speech_end"},
    ]
    assert service._audio_buffer == bytearray()
    assert [(event.boundary_id, event.candidate_turn_id, event.sent) for event in events] == [
        (boundary_id, 8, True),
        (boundary_id, 8, True),
    ]


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.asyncio
async def test_bound_segment_with_failed_stop_cannot_emit_owned_final(service_factory, raises):
    """A failed close invalidates an already established provider-ID association."""
    service = service_factory()
    boundary_id = await start_boundary(service, 8)
    await observe_partial(service, 3)
    assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=3)
    boundaries, outcomes, failures = [], [], []
    listen(service, "on_manual_boundary", boundaries)
    listen(service, "on_segment_final", outcomes)
    listen(service, "on_correlation_failure", failures)
    if raises:
        service._websocket.fail_on = "speech_end"
        with pytest.raises(RuntimeError, match="send failure"):
            await end_boundary(service)
    else:
        service._websocket.state = State.CLOSED
        await end_boundary(service)
    assert boundaries[-1].sent is False
    assert boundaries[-1].event is ManualBoundaryEvent.SPEECH_END
    await final(service, 3)
    assert outcomes == []
    assert failures[-1].reason == "boundary_send_failed"


@pytest.mark.asyncio
async def test_failed_start_and_ambiguous_duplicate_start_cannot_bind(service_factory):
    """Neither lost starts nor repeated starts can establish trusted ownership."""
    for duplicate in (False, True):
        service = service_factory()
        if not duplicate:
            service._websocket.state = State.CLOSED
        boundary_id = await start_boundary(service, 8)
        if duplicate:
            await service.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await observe_partial(service, 3)
        assert not await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=3)


@pytest.mark.asyncio
async def test_connection_generations_distinguish_obsolete_final_events(service_factory):
    """Identical provider IDs on replacement services never identify the same segment."""
    outcomes = []
    for generation in (5, 4):
        service = service_factory(generation)
        boundary_id = await start_boundary(service, 8)
        await end_boundary(service)
        listen(service, "on_segment_final", outcomes)
        await final(service, 0)
        assert await service.bind_provider_utterance(boundary_id=boundary_id, utterance_idx=0)
    assert [item.segment_id for item in outcomes] == [SegmentId(5, 0), SegmentId(4, 0)]
    assert len({item.segment_id for item in outcomes}) == 2


@pytest.mark.parametrize(
    ("incoming", "kind"),
    [
        ([], ConnectionEventKind.RECEIVE_ENDED),
        ([RuntimeError("receive failed")], ConnectionEventKind.RECEIVE_FAILED),
        ([ConnectionClosedError(Close(1006, "lost"), None)], ConnectionEventKind.RECEIVE_FAILED),
        (
            [ConnectionClosedOK(Close(1000, "done"), Close(1000, "done"), True)],
            ConnectionEventKind.RECEIVE_ENDED,
        ),
    ],
)
@pytest.mark.asyncio
async def test_actual_receive_loop_classifies_exit_and_keeps_base_usability(
    service_factory, incoming, kind
):
    """Normal exhaustion and exceptions are classified without replacing base error handling."""
    service = service_factory()
    service._websocket = FakeWebsocket(incoming)
    events = []
    listen(service, "on_connection_event", events)
    report = AsyncMock()
    await service._receive_task_handler(report)
    assert [event.kind for event in events] == [kind]
    assert events[0].connection_generation == 4
    assert service.is_usable is False


@pytest.mark.asyncio
async def test_receive_failure_still_forwards_final_events_from_socket(service_factory):
    """A provider final remains observable when the connection then drops."""
    service = service_factory()
    service._websocket = FakeWebsocket(
        [
            {"event": "transcript.final", "utterance_idx": 0, "text": ""},
            RuntimeError("drop"),
        ]
    )
    observations = []
    listen(service, "on_final_observed", observations)
    await service._receive_task_handler(AsyncMock())
    assert len(observations) == 1
    assert observations[0].outcome is SegmentFinalOutcome.EMPTY


@pytest.mark.asyncio
async def test_intentional_shutdown_and_cancellation_are_not_connection_failures(service_factory):
    """Application shutdown and receive-task cancellation do not report a provider drop."""
    service = service_factory()
    events = []
    listen(service, "on_connection_event", events)
    await service._disconnect()
    await service._disconnect()
    assert [event.kind for event in events] == [ConnectionEventKind.INTENTIONAL_SHUTDOWN]
    await service._receive_task_handler(AsyncMock())
    assert len(events) == 1
    assert service.is_usable is True
    cancelled = service_factory()
    listen(cancelled, "on_connection_event", events)
    cancelled._websocket = FakeWebsocket([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await cancelled._receive_task_handler(AsyncMock())
    assert len(events) == 1


@pytest.mark.asyncio
async def test_failed_connect_and_provider_error_are_visible(service_factory, monkeypatch):
    """Connection setup failures and server error messages preserve base reporting."""
    service = service_factory()
    events = []
    listen(service, "on_connection_event", events)
    service._websocket = None
    monkeypatch.setattr(
        service, "_websocket_connect", AsyncMock(side_effect=RuntimeError("offline"))
    )
    monkeypatch.setattr(service, "push_error", AsyncMock())
    await service._connect_websocket()
    assert events[-1].kind is ConnectionEventKind.CONNECT_FAILED
    await service._handle_message({"event": "error", "code": "invalid_config", "is_fatal": True})
    assert events[-1].kind is ConnectionEventKind.PROVIDER_ERROR
    assert service.push_error.await_count == 2


@pytest.mark.asyncio
async def test_partial_frames_preserve_base_behavior_without_terminal_events(service_factory):
    """Caption frames remain provisional and carry the original provider ID."""
    service = service_factory()
    observations = []
    listen(service, "on_final_observed", observations)
    await observe_partial(service, 8)
    frame = service.push_frame.call_args.args[0]
    assert isinstance(frame, InterimTranscriptionFrame)
    assert frame.result["utterance_idx"] == 8
    assert observations == []


def test_extension_enforces_manual_mode_and_fixed_generation(service_factory):
    """The public connection token cannot be reassigned and manual mode is required."""
    service = service_factory()
    assert service._endpointing == "manual"
    with pytest.raises(AttributeError):
        service.connection_generation = 6
    with pytest.raises(ValueError, match="manual"):
        service_factory(endpointing="vad")


@pytest.mark.parametrize("failure", [None, RuntimeError("receive failed")])
@pytest.mark.asyncio
async def test_real_service_error_callback_emits_one_connection_event(service_factory, failure):
    """The callback used by an actual connection does not duplicate lifecycle events."""
    service = service_factory()
    service._websocket = FakeWebsocket([] if failure is None else [failure])
    events = []
    listen(service, "on_connection_event", events)
    await service._receive_task_handler(service._report_error)
    assert [event.kind for event in events] == [
        ConnectionEventKind.RECEIVE_ENDED if failure is None else ConnectionEventKind.RECEIVE_FAILED
    ]
    assert service.is_usable is False


@pytest.mark.asyncio
async def test_attached_ledger_arms_owner_and_resolves_retained_final(service_factory):
    """Actual STT event callbacks populate the gate before raw transcript frames."""
    from demo.interview.ledger import TranscriptLedger
    from demo.interview.transcript_gate import TranscriptLedgerProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext

    service = service_factory()
    ledger = TranscriptLedger(connection_generation=4, transcript_final_timeout=2)
    ledger.begin_candidate_turn(candidate_turn_id=7, question_id="q1")
    gate = TranscriptLedgerProcessor(ledger=ledger, context=LLMContext())
    gate.attach_sarvam(service)
    boundaries = []
    listen(service, "on_manual_boundary", boundaries)
    token = ledger.response_token
    await service.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert ledger.response_token > token
    assert boundaries[-1].candidate_turn_id == 7
    await end_boundary(service)
    await final(service, 23, "complete answer")
    assert not ledger.readiness
    await service.bind_provider_utterance(boundary_id=boundaries[0].boundary_id, utterance_idx=23)
    assert ledger.readiness
    assert ledger.snapshot.text == "complete answer"
    await final(service, 23, "complete answer")
    assert len(ledger.snapshot.segments) == 1
