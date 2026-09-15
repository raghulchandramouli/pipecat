"""Timestamp-bound correlation coverage for browser Sarvam STT."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from websockets.protocol import State

from demo.interview.browser_stt import BrowserSarvamSTTService
from demo.interview.contracts import SegmentFinalOutcome
from demo.interview.sarvam_events import ManualBoundaryEvent
from pipecat.frames.frames import (
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class FakeWebsocket:
    """Capture the manual wire protocol without contacting Sarvam."""

    def __init__(self) -> None:
        """Initialize an open socket with no scripted send failure."""
        self.state = State.OPEN
        self.sent: list[dict[str, object]] = []
        self.fail_on: str | None = None

    async def send(self, message: str) -> None:
        """Record a JSON wire event or raise for the selected event kind."""
        payload = json.loads(message)
        if payload["event"] == self.fail_on:
            raise RuntimeError("injected wire failure")
        self.sent.append(payload)


@pytest.fixture
def service(monkeypatch):
    """Create the actual correlator with provider I/O and frame sinks isolated."""
    stt = BrowserSarvamSTTService(api_key="test-key", connection_generation=9)
    stt._websocket = FakeWebsocket()
    stt._sample_rate = 16_000
    monkeypatch.setattr(stt, "push_frame", AsyncMock())
    monkeypatch.setattr(stt, "emit_stt_usage_metrics", AsyncMock())
    monkeypatch.setattr(stt, "_trace_transcription", AsyncMock())
    return stt


def collect(service, name):
    """Register a synchronous event collector on the real service."""
    values = []

    @service.event_handler(name)
    def capture(_service, value):
        values.append(value)

    return values


async def send_pcm(service, byte_count: int = 1600) -> None:
    """Send exactly one 50 ms, 16 kHz mono PCM interval through the service."""
    async for _ in service.run_stt(b"\0" * byte_count):
        pass


async def open_boundary(service, candidate: int) -> int:
    """Create a real VAD-owned manual interval and return its local identity."""
    service.set_candidate_turn(candidate)
    await service.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    return service._active_boundary_id


async def close_boundary(service) -> None:
    """Close the currently active manual interval through the provider path."""
    await service.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_pcm_timestamps_bind_final_to_the_interval_that_was_sent(service):
    """A final binds only after matching its exact sent PCM interval."""
    boundaries = collect(service, "on_manual_boundary")
    finals = collect(service, "on_segment_final")
    boundary = await open_boundary(service, candidate=31)
    await send_pcm(service)
    await close_boundary(service)

    assert boundary == 0
    assert [(item.event, item.sent) for item in boundaries] == [
        (ManualBoundaryEvent.SPEECH_START, True),
        (ManualBoundaryEvent.SPEECH_END, True),
    ]
    events = [item["event"] for item in service._websocket.sent]
    assert events[0] == "speech_start"
    assert events[-1] == "speech_end"
    assert set(events[1:-1]) == {"audio_input"}

    await service._handle_final_transcript(
        {"utterance_idx": 7, "start_s": 0.0, "end_s": 0.05, "text": "  concrete answer "}
    )

    assert [
        (item.candidate_turn_id, item.segment_id.utterance_idx, item.text) for item in finals
    ] == [(31, 7, "concrete answer")]


@pytest.mark.asyncio
async def test_vad_start_sends_buffered_onset_only_after_the_manual_boundary(service):
    """Pre-roll preserves onset PCM without sending an unbounded pre-speech timeline."""
    finals = collect(service, "on_segment_final")
    await send_pcm(service)
    assert service._websocket.sent == []

    await open_boundary(service, candidate=31)
    await send_pcm(service)
    await close_boundary(service)

    events = [item["event"] for item in service._websocket.sent]
    assert events[0] == "speech_start"
    assert events[-1] == "speech_end"
    assert set(events[1:-1]) == {"audio_input"}
    await service._handle_final_transcript(
        {"utterance_idx": 8, "start_s": 0.0, "end_s": 0.1, "text": "full onset"}
    )
    assert [(final.candidate_turn_id, final.text) for final in finals] == [(31, "full onset")]


@pytest.mark.asyncio
async def test_pre_roll_streams_idle_audio_and_sends_the_bounded_onset_tail():
    """Long pre-speech input retains only the newest PCM before the manual boundary."""
    limited = BrowserSarvamSTTService(
        api_key="test-key", connection_generation=11, pre_roll_secs=0.1
    )
    limited._websocket = FakeWebsocket()
    limited._sample_rate = 16_000

    for byte in (b"a", b"b", b"c"):
        async for _ in limited.run_stt(byte * 1600):
            pass
    assert bytes(limited._pre_roll_pcm) == b"b" * 1600 + b"c" * 1600

    await open_boundary(limited, candidate=5)
    chunks = [
        base64.b64decode(item["audio"])
        for item in limited._websocket.sent
        if item["event"] == "audio_input"
    ]
    assert chunks == [b"a" * 1600, b"b" * 1600, b"c" * 1600]
    assert [item["event"] for item in limited._websocket.sent] == [
        "audio_input",
        "speech_start",
        "audio_input",
        "audio_input",
    ]
    assert limited._audio_intervals[0][0] == 0.05


@pytest.mark.asyncio
async def test_leading_and_gap_audio_make_reverse_finals_keep_their_original_owner(service):
    """Correlation follows sent audio time rather than boundary or response arrival order."""
    finals = collect(service, "on_segment_final")
    await send_pcm(service)  # Leading audio is outside both manual boundaries.
    await open_boundary(service, candidate=101)
    await send_pcm(service)
    await close_boundary(service)
    await send_pcm(service)  # A gap must remain on the same provider audio timeline.
    await open_boundary(service, candidate=202)
    await send_pcm(service)
    await close_boundary(service)

    await service._handle_final_transcript(
        {"utterance_idx": 22, "start_s": 0.10, "end_s": 0.20, "text": "second"}
    )
    await service._handle_final_transcript(
        {"utterance_idx": 11, "start_s": 0.0, "end_s": 0.10, "text": "first"}
    )

    assert [
        (item.segment_id.utterance_idx, item.candidate_turn_id, item.text) for item in finals
    ] == [
        (22, 202, "second"),
        (11, 101, "first"),
    ]


@pytest.mark.asyncio
async def test_timestamp_bound_empty_final_is_terminal_without_raw_transcript_admission(service):
    """An empty final resolves the recorded segment but never fabricates text evidence."""
    finals = collect(service, "on_segment_final")
    await open_boundary(service, candidate=6)
    await send_pcm(service)
    await close_boundary(service)

    await service._handle_final_transcript(
        {"utterance_idx": 4, "start_s": 0.0, "end_s": 0.05, "text": " \t "}
    )

    assert [(item.candidate_turn_id, item.outcome, item.text) for item in finals] == [
        (6, SegmentFinalOutcome.EMPTY, "")
    ]
    assert not any(
        isinstance(call.args[0], TranscriptionFrame) for call in service.push_frame.call_args_list
    )


@pytest.mark.asyncio
async def test_conflicting_duplicate_provider_id_cannot_reassign_a_previous_boundary(service):
    """A provider ID cannot become evidence for two audio intervals."""
    finals = collect(service, "on_segment_final")
    failures = collect(service, "on_correlation_failure")
    await open_boundary(service, candidate=1)
    await send_pcm(service)
    await close_boundary(service)
    await open_boundary(service, candidate=2)
    await send_pcm(service)
    await close_boundary(service)

    await service._handle_final_transcript(
        {"utterance_idx": 3, "start_s": 0.0, "end_s": 0.05, "text": "first"}
    )
    await service._handle_final_transcript(
        {"utterance_idx": 3, "start_s": 0.05, "end_s": 0.10, "text": "conflict"}
    )

    assert [(item.candidate_turn_id, item.text) for item in finals] == [(1, "first")]
    assert failures[-1].reason == "timestamp_identity_conflict"


@pytest.mark.parametrize(
    "start,end",
    [
        (None, 0.05),
        (0.0, None),
        (float("nan"), 0.05),
        (0.05, float("nan")),
        (0.06, 0.05),
        (True, 0.05),
    ],
)
@pytest.mark.asyncio
async def test_missing_nonfinite_or_reversed_timestamps_never_admit_a_final(service, start, end):
    """Malformed provider timestamps leave the ledger unready rather than guessing."""
    finals = collect(service, "on_segment_final")
    failures = collect(service, "on_correlation_failure")
    await open_boundary(service, candidate=9)
    await send_pcm(service)
    await close_boundary(service)

    await service._handle_final_transcript(
        {"utterance_idx": 18, "start_s": start, "end_s": end, "text": "unsafe"}
    )

    assert finals == []
    assert failures[-1].reason == "unmatched_audio_timestamps"


@pytest.mark.asyncio
async def test_ambiguous_equal_intervals_and_failed_wire_send_do_not_accept_final(service):
    """Ambiguous timestamps or any boundary/audio send failure fail closed."""
    finals = collect(service, "on_segment_final")
    failures = collect(service, "on_correlation_failure")
    await open_boundary(service, candidate=1)
    await close_boundary(service)
    await open_boundary(service, candidate=2)
    await close_boundary(service)
    await service._handle_final_transcript(
        {"utterance_idx": 1, "start_s": 0.0, "end_s": 0.0, "text": "ambiguous"}
    )
    assert finals == []
    assert failures[-1].reason == "unmatched_audio_timestamps"

    failing = BrowserSarvamSTTService(api_key="test-key", connection_generation=10)
    failing._websocket = FakeWebsocket()
    failing._sample_rate = 16_000
    failing._websocket.fail_on = "audio_input"
    failing_finals = collect(failing, "on_segment_final")
    failing_failures = collect(failing, "on_correlation_failure")
    await open_boundary(failing, candidate=3)
    await send_pcm(failing)
    await close_boundary(failing)
    await failing._handle_final_transcript(
        {"utterance_idx": 2, "start_s": 0.0, "end_s": 0.05, "text": "must not bind"}
    )
    assert failing_finals == []
    assert failing_failures[-1].reason == "unmatched_audio_timestamps"
