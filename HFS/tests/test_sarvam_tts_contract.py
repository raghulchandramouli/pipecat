"""Contract coverage for the isolated native Sarvam TTS handler."""

from __future__ import annotations

import asyncio
import json
from queue import Queue
from threading import Event, Thread

import httpx
import numpy as np
import pytest
from speech_to_speech.pipeline.events import ResponseFailedEvent
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse, TTSInput
from speech_to_speech.TTS import openai_compatible_handler as upstream_tts

import HFS.speech_to_speech.sarvam_tts as sarvam_tts
from HFS.speech_to_speech.sarvam_tts import (
    SARVAM_MAX_RESPONSE_BYTES,
    SARVAM_SAMPLE_RATE,
    SarvamTTSHandler,
    SpeechRequestError,
)

_REAL_OPERATION = sarvam_tts._SarvamHttpSpeechOperation


class _FakeOperation:
    """Provider seam that exercises the inherited response lifecycle."""

    instances: list[_FakeOperation] = []
    response_bytes = np.arange(2400, dtype="<i2").tobytes()
    chunks: list[bytes] | None = None
    startup_error: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.payload = kwargs["payload"]
        self.cancelled = False
        type(self).instances.append(self)

    def iter_bytes(self, cancel_check):
        if type(self).startup_error is not None:
            raise type(self).startup_error
        chunks = type(self).chunks
        if chunks is None:
            chunks = [type(self).response_bytes]
        for chunk in chunks:
            if self.cancelled or cancel_check():
                self.cancel()
                return
            yield chunk

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def _reset_fake_operation(monkeypatch):
    _FakeOperation.instances.clear()
    _FakeOperation.response_bytes = np.arange(2400, dtype="<i2").tobytes()
    _FakeOperation.chunks = None
    _FakeOperation.startup_error = None
    monkeypatch.setattr(sarvam_tts, "_SarvamHttpSpeechOperation", _FakeOperation)


def _handler(**setup_kwargs) -> SarvamTTSHandler:
    return SarvamTTSHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(Event(),),
        setup_kwargs={"sarvam_api_key": "subscription-key", **setup_kwargs},
    )


@pytest.mark.parametrize("language_code", ["en-IN", "hi-IN", "ta-IN"])
def test_native_request_uses_subscription_configuration(language_code):
    """Translate each supported language into Sarvam's native payload."""
    handler = _handler(language_code=language_code, speaker="shubh")

    chunks = list(handler.process(TTSInput(text="Hello")))

    assert chunks
    payload = _FakeOperation.instances[0].payload
    assert payload == {
        "text": "Hello",
        "model": "bulbul:v3",
        "speaker": "shubh",
        "language_code": language_code,
        "speech_sample_rate": 24000,
        "output_audio_codec": "linear16",
    }
    assert "input" not in payload
    assert "voice" not in payload


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sarvam_api_key": " "}, "requires sarvam_api_key"),
        ({"language_code": "fr-FR"}, "language_code"),
        ({"speaker": ""}, "speaker"),
        ({"timeout": 0}, "timeout"),
        ({"blocksize": 0}, "blocksize"),
    ],
)
def test_setup_rejects_invalid_native_configuration(kwargs, message):
    """Reject invalid Sarvam values before a provider operation is made."""
    with pytest.raises(ValueError, match=message):
        _handler(**kwargs)


def test_empty_or_oversized_text_fails_before_dispatch():
    """Do not submit blank or provider-invalid text."""
    handler = _handler()

    assert list(handler.process(TTSInput(text=" \n"))) == []
    assert not _FakeOperation.instances
    assert list(handler.process(TTSInput(text="a" * 3501, response_key="long"))) == []
    assert not _FakeOperation.instances
    failure = handler.queue_out.get_nowait()
    assert isinstance(failure, ResponseFailedEvent)
    assert failure.response_key == "long"


def test_uses_subscription_header_without_openai_bearer(monkeypatch):
    """Authenticate the native endpoint with its subscription-key header."""
    received: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        received["headers"] = dict(request.headers)
        received["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=np.arange(2400, dtype="<i2").tobytes(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(upstream_tts.httpx, "AsyncClient", lambda **_: client)
    # Use the actual native operation instead of the lifecycle fixture.
    monkeypatch.setattr(sarvam_tts, "_SarvamHttpSpeechOperation", _REAL_OPERATION)
    handler = _handler()

    assert list(handler.process(TTSInput(text="Hello")))
    headers = received["headers"]
    assert headers["api-subscription-key"] == "subscription-key"
    assert "authorization" not in headers


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [b"R", b"IFF", b"not raw PCM"],
        [b"Ogg", b"Snot raw PCM"],
        [b"\x01"],
        [b"\x00" * (SARVAM_MAX_RESPONSE_BYTES + 1)],
    ],
)
def test_invalid_raw_pcm_fails_and_never_completes_response(chunks):
    """Fail closed for empty, container, odd, and oversized PCM bodies."""
    handler = _handler()
    _FakeOperation.chunks = chunks

    assert list(handler.process(TTSInput(text="Hello", response_key="bad"))) == []
    failure = handler.queue_out.get_nowait()
    assert isinstance(failure, ResponseFailedEvent)
    assert failure.response_key == "bad"
    assert list(handler.process(EndOfResponse(response_key="bad"))) == [AUDIO_RESPONSE_DONE]


def test_pcm_resampling_is_partition_invariant_and_response_local():
    """Keep filter state stable within one response and reset it afterward."""
    handler = _handler(blocksize=512)
    samples = np.round(
        16000 * np.sin(2 * np.pi * 1000 * np.arange(SARVAM_SAMPLE_RATE) / SARVAM_SAMPLE_RATE)
    ).astype("<i2")
    encoded = samples.tobytes()

    single = np.concatenate(list(handler._decode_pcm_stream(iter([encoded]))))
    cuts = [1, 301, 2, 717, 17, 4999]
    parts: list[bytes] = []
    offset = 0
    for size in cuts:
        parts.append(encoded[offset : offset + size])
        offset += size
    parts.append(encoded[offset:])
    partitioned = np.concatenate(list(handler._decode_pcm_stream(iter(parts))))
    next_response = np.concatenate(list(handler._decode_pcm_stream(iter([encoded]))))

    np.testing.assert_array_equal(partitioned, single)
    np.testing.assert_array_equal(next_response, single)
    assert 16000 <= single.size < 16000 + handler.blocksize
    assert not np.any(single[16000:])
    assert np.sqrt(np.mean(single.astype(np.float64) ** 2)) > 10000

    stopband = np.round(
        16000 * np.sin(2 * np.pi * 10000 * np.arange(SARVAM_SAMPLE_RATE) / SARVAM_SAMPLE_RATE)
    ).astype("<i2")
    rejected = np.concatenate(list(handler._decode_pcm_stream(iter([stopband.tobytes()]))))
    assert np.sqrt(np.mean(rejected.astype(np.float64) ** 2)) < 500


def test_http_failure_marks_only_its_response_and_next_response_recovers():
    """Preserve inherited failed-response suppression without cross-response leakage."""
    handler = _handler()
    _FakeOperation.startup_error = SpeechRequestError("speech server returned HTTP 503")
    failed = TTSInput(text="First", turn_id="turn", turn_revision=0, response_key="first")

    assert list(handler.process(failed)) == []
    assert list(handler.process(failed)) == []
    assert len(_FakeOperation.instances) == 1
    failure = handler.queue_out.get_nowait()
    assert isinstance(failure, ResponseFailedEvent)
    assert failure.message == "speech server returned HTTP 503"

    _FakeOperation.startup_error = None
    assert list(
        handler.process(
            TTSInput(text="Second", turn_id="turn", turn_revision=0, response_key="second")
        )
    )
    assert len(_FakeOperation.instances) == 2
    assert _FakeOperation.instances[-1].payload["text"] == "Second"


def test_end_of_response_releases_a_failed_response_identity():
    """Use the inherited terminal event to permit an explicit retry."""
    handler = _handler()
    request = TTSInput(text="Retry", response_key="response")
    _FakeOperation.startup_error = SpeechRequestError("speech server returned HTTP 500")

    assert list(handler.process(request)) == []
    assert list(handler.process(EndOfResponse(response_key="response"))) == [AUDIO_RESPONSE_DONE]
    _FakeOperation.startup_error = None
    assert list(handler.process(request))
    assert len(_FakeOperation.instances) == 2


def test_non_pcm_http_response_is_rejected_by_actual_operation(monkeypatch):
    """Reject a successful HTTP status with a JSON response body."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{}", request=request
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(upstream_tts.httpx, "AsyncClient", lambda **_: client)
    operation = _REAL_OPERATION(
        endpoint_url="http://speech.test/stream",
        subscription_key="subscription-key",
        payload={},
        timeout_s=1,
    )

    with pytest.raises(SpeechRequestError, match="non-PCM"):
        list(operation.iter_bytes(lambda: False))


def test_actual_operation_cancellation_closes_a_stalled_socket(monkeypatch):
    """Cancel a blocked HTTP stream without leaving its reader thread behind."""
    started = Event()
    closed = Event()

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            while True:
                await asyncio.sleep(0.01)
                yield b""

        async def aclose(self) -> None:
            closed.set()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=StalledStream(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(upstream_tts.httpx, "AsyncClient", lambda **_: client)
    operation = _REAL_OPERATION(
        endpoint_url="http://speech.test/stream",
        subscription_key="subscription-key",
        payload={},
        timeout_s=1,
    )
    errors: list[BaseException] = []

    def read() -> None:
        try:
            list(operation.iter_bytes(lambda: False))
        except BaseException as exc:
            errors.append(exc)

    reader = Thread(target=read)
    reader.start()
    assert started.wait(0.5)
    operation.cancel()
    reader.join(0.5)

    assert not reader.is_alive()
    assert closed.wait(0.5)
    assert errors and errors[0].__class__.__name__ == "SpeechRequestCancelled"
