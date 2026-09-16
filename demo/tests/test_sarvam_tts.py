"""Streaming Sarvam transport and generation-ownership regression coverage."""

import asyncio
import json

import httpx
import pytest

from demo.interview.languages import TTS_LANGUAGES
from demo.interview.sarvam_tts import InterviewSarvamTTSService
from demo.tests.test_rumik_tts import _envelope, _submit, _yield_tasks
from pipecat.frames.frames import (
    ErrorFrame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


class Chunks(httpx.AsyncByteStream):
    """A controllable streaming response that records closure."""

    def __init__(self, chunks, gate=None):
        """Store chunks and an optional gate before subsequent bytes."""
        self.chunks, self.gate, self.closed = chunks, gate, False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index and self.gate:
                await self.gate.wait()
            yield chunk

    async def aclose(self):
        """Record that the response released its underlying stream."""
        self.closed = True


@pytest.mark.asyncio
async def test_opening_and_follow_up_keep_the_selected_speech_pace():
    """Successive replies retain the session's speed and PCM clock."""
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=b"\x01\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    tts = InterviewSarvamTTSService(
        api_key="test-key",
        language_code="ta-IN",
        pace=0.85,
        current_playback_epoch=lambda: 4,
        http_transport=httpx.MockTransport(handler),
    )
    down, _ = await run_test(
        tts,
        frames_to_send=_envelope("வணக்கம். நாம பேசலாம்.")
        + _envelope('"team" சொன்னீங்க. எப்படி help பண்ணீங்க?', dispatch=24),
    )
    assert len(requests) == 2
    assert [request["pace"] for request in requests] == [0.85, 0.85]
    assert all(request["speech_sample_rate"] == 24000 for request in requests)
    pcm = [frame for frame in down if isinstance(frame, TTSAudioRawFrame)]
    assert len(pcm) == 2
    assert all(frame.sample_rate == 24000 for frame in pcm)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", TTS_LANGUAGES)
async def test_sarvam_streams_pcm_and_recovers_from_http_error(language):
    """Explicit model/auth/codec settings reach HTTP, with incremental PCM output."""
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["api-subscription-key"] == "test-key"
        if len(requests) == 1:
            return httpx.Response(401, json={"error": "private provider diagnostic"})
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            stream=Chunks([b"\x01", b"\x00\x02\x00", b"\x03\x00"]),
        )

    tts = InterviewSarvamTTSService(
        api_key="test-key",
        language_code=language,
        pace=1.2,
        current_playback_epoch=lambda: 4,
        http_transport=httpx.MockTransport(handler),
    )
    down, up = await run_test(
        tts, frames_to_send=_envelope("First") + _envelope("Second", dispatch=24)
    )
    pcm = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert b"".join(f.audio for f in pcm) == b"\x01\x00\x02\x00\x03\x00"
    assert len(pcm) == 2
    assert all(f.metadata["interview_dispatch_id"] == 24 for f in pcm)
    assert any(isinstance(f, ErrorFrame) and not f.fatal for f in up)
    assert requests[1]["language_code"] == language
    assert requests[1]["model"] == "bulbul:v3"
    assert requests[1]["output_audio_codec"] == "linear16"
    assert requests[1]["speech_sample_rate"] == 24000
    assert requests[1]["pace"] == 1.2
    assert tts.last_metrics.first_audio_seconds >= 0
    assert tts._client.is_closed
    assert not tts._task_manager.current_tasks()


@pytest.mark.asyncio
async def test_interruption_closes_stalled_stream_and_later_speech_progresses():
    """An unfinished network stream cannot hold up a replacement utterance."""
    gate = asyncio.Event()
    first = Chunks([b"\x01\x00" * 4, b"\x09\x00" * 4], gate)
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            stream=first if len(calls) == 1 else Chunks([b"\x02\x00" * 4]),
        )

    epoch = [4]
    tts = InterviewSarvamTTSService(
        api_key="test-key",
        current_playback_epoch=lambda: epoch[0],
        http_transport=httpx.MockTransport(handler),
    )
    frames = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        frames.append(frame)

    tts.push_frame = capture
    await tts.setup(frame_processor_setup(TaskManager()))
    try:
        await tts.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await _submit(tts, "Old")
        await _yield_tasks()
        assert any(isinstance(f, TTSAudioRawFrame) for f in frames)
        epoch[0] += 1
        await tts.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await _submit(tts, "New", epoch=5, dispatch=24)
        await _yield_tasks()
        assert first.closed
        assert len(calls) == 2
        assert all(f.audio != b"\x09\x00" * 4 for f in frames if isinstance(f, TTSAudioRawFrame))
        assert any(
            isinstance(f, TTSStoppedFrame) and f.metadata["interview_dispatch_id"] == 24
            for f in frames
        )
    finally:
        gate.set()
        await tts.cleanup()
    assert not tts._task_manager.current_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,content_type",
    [
        (b"", "audio/pcm"),
        (b"RIFFoops", "audio/pcm"),
        (b"{}", "application/json"),
        (b"\x01", "audio/pcm"),
    ],
)
async def test_invalid_pcm_never_becomes_spoken_text(payload, content_type):
    """Empty, container, JSON, and incomplete samples fail closed."""
    tts = InterviewSarvamTTSService(
        api_key="test-key",
        current_playback_epoch=lambda: 4,
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=payload, headers={"content-type": content_type})
        ),
    )
    down, up = await run_test(tts, frames_to_send=_envelope("Hello"))
    assert not any(isinstance(f, TTSTextFrame) for f in down)
    assert any(isinstance(f, ErrorFrame) for f in up)


def test_session_defaults_to_sarvam():
    """The standard session factory selects the requested hosted provider."""
    from demo.interview.session import InterviewSession
    from demo.tests.test_playback_epoch import config

    session = InterviewSession(config=config(), session_id="sarvam")
    tts = session.create_tts(api_key="test-key")
    assert isinstance(tts, InterviewSarvamTTSService)
    assert session.create_tts() is tts
    assert session.config.providers.tts.model == "bulbul:v3"
    asyncio.run(tts._client.aclose())
