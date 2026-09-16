"""Sarvam manual websocket request and timestamp ownership contracts."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread

import numpy as np
import pytest
from speech_to_speech.pipeline.messages import (
    PartialTranscription,
    Transcription,
    TranscriptionFailure,
    VADAudio,
)
from websockets.sync.server import serve

from HFS.speech_to_speech.sarvam_stt import SarvamSTTHandler, SarvamSTTHandlerArguments, _pcm16le


def _source(turn: str = "turn-1", revision: int = 0) -> VADAudio:
    return VADAudio(
        audio=np.empty(0, dtype=np.float32), mode="final", turn_id=turn, turn_revision=revision
    )


@pytest.fixture
def sarvam_server():
    """Run a loopback Sarvam-shaped websocket and expose its wire observations."""
    received: Queue[dict[str, object]] = Queue()
    headers: Queue[object] = Queue()

    def handler(socket):
        headers.put(socket.request.headers)
        pcm_bytes = 0
        for raw in socket:
            event = __import__("json").loads(raw)
            received.put(event)
            if event["event"] == "audio_input":
                pcm_bytes += len(__import__("base64").b64decode(event["audio"], validate=True))
            elif event["event"] == "speech_end":
                socket.send('{"event":"transcript.partial","text":"hello"}')
                socket.send(
                    '{"event":"transcript.final","utterance_idx":7,"start_s":0.0,'
                    f'"end_s":{pcm_bytes / 32000},"text":"hello world","language_code":"en-IN"}}'
                )
                return

    server = serve(handler, "127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}/manual", received, headers
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _handler(url: str, **kwargs) -> SarvamSTTHandler:
    return SarvamSTTHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_kwargs={"sarvam_api_key": "test-key", "endpoint_url": url, "timeout": 1.0, **kwargs},
    )


def test_arguments_do_not_collide_with_the_tts_selector() -> None:
    """The combined HF parser can distinguish Sarvam STT configuration fields."""
    assert set(SarvamSTTHandlerArguments.__dataclass_fields__) == {
        "sarvam_stt_api_key",
        "sarvam_stt_language_code",
        "sarvam_stt_mode",
        "sarvam_stt_timeout",
        "sarvam_stt_endpoint_url",
        "sarvam_stt_max_pending_audio_bytes",
    }


def test_audio_conversion_rejects_unsafe_shapes_and_preserves_pcm16() -> None:
    """Float input has one defined conversion while invalid audio never reaches a socket."""
    assert _pcm16le(np.array([-1.0, 0.0, 1.0], dtype=np.float32)) == b"\x01\x80\0\0\xff\x7f"
    assert _pcm16le(np.array([1, -2], dtype=np.int16)) == b"\x01\0\xfe\xff"
    for invalid in (
        np.array([[0.0]], dtype=np.float32),
        np.array([float("nan")], dtype=np.float32),
        np.array([1.1], dtype=np.float32),
        np.array([1], dtype=np.int32),
        b"\0",
    ):
        with pytest.raises(ValueError):
            _pcm16le(invalid)


@pytest.mark.parametrize("commit_from_vad", [False, True])
def test_manual_pcm_and_timestamp_bound_final_are_owned_by_one_turn(sarvam_server, commit_from_vad) -> None:
    """Only a timestamp-matched final completes the VAD turn; partials stay captions."""
    url, received, headers = sarvam_server
    handler = _handler(url)
    try:
        pcm = b"\x01\x00" * 800
        handler.append_audio(pcm)
        handler.start_turn("turn-1", 3)
        handler.append_audio(pcm)
        if commit_from_vad:
            handler.commit_boundary("turn-1", 3)
        outputs = list(handler.process(_source(revision=3)))

        assert outputs == [
            Transcription(
                text="hello world",
                language_code="en-IN",
                turn_id="turn-1",
                turn_revision=3,
                speech_stopped_at_s=outputs[0].speech_stopped_at_s,
            )
        ]
        partial = handler.queue_out.get(timeout=1)
        assert partial == PartialTranscription(text="hello", turn_id="turn-1", turn_revision=3)
        assert [received.get(timeout=1)["event"] for _ in range(4)] == [
            "speech_start",
            "audio_input",
            "audio_input",
            "speech_end",
        ]
        request_headers = headers.get(timeout=1)
        assert request_headers["api-subscription-key"] == "test-key"
        assert "authorization" not in request_headers
    finally:
        handler.cleanup()


def test_invalid_timestamp_fails_closed_without_a_transcription() -> None:
    """A final with unowned timing is a typed failure, never guessed evidence."""

    class Socket:
        def __init__(self) -> None:
            self.events = Queue()
            self.closed = Event()

        def send(self, message: str) -> None:
            event = __import__("json").loads(message)
            if event["event"] == "speech_end":
                self.events.put(
                    '{"event":"transcript.final","utterance_idx":1,"start_s":0.01,'
                    '"end_s":0.05,"text":"unsafe"}'
                )

        def recv(self, timeout=None):
            from queue import Empty

            try:
                return self.events.get(timeout=timeout)
            except Empty:
                raise TimeoutError

        def close(self) -> None:
            self.closed.set()

    socket = Socket()
    handler = _handler("ws://loopback.invalid", connect_factory=lambda *args, **kwargs: socket)
    try:
        handler.start_turn("turn-1", 0)
        handler.append_audio(b"\0" * 1600)
        outputs = list(handler.process(_source()))
        assert len(outputs) == 1 and isinstance(outputs[0], TranscriptionFailure)
        assert outputs[0].message == "Sarvam transcription timestamps could not be correlated"
        assert handler.queue_out.empty()
        assert socket.closed.wait(1)
    finally:
        handler.cleanup()


def test_conflicting_duplicate_final_fails_closed() -> None:
    """Two distinct provider finals for one manual interval cannot select an arbitrary result."""

    class Socket:
        def __init__(self) -> None:
            self.events = Queue()

        def send(self, message: str) -> None:
            event = __import__("json").loads(message)
            if event["event"] == "speech_end":
                self.events.put(
                    '{"event":"transcript.final","utterance_idx":1,"start_s":0.0,'
                    '"end_s":0.05,"text":"first"}'
                )
                self.events.put(
                    '{"event":"transcript.final","utterance_idx":2,"start_s":0.0,'
                    '"end_s":0.05,"text":"conflict"}'
                )

        def recv(self, timeout=None):
            try:
                return self.events.get(timeout=timeout)
            except Empty:
                raise TimeoutError

        def close(self) -> None:
            pass

    handler = _handler("ws://loopback.invalid", connect_factory=lambda *args, **kwargs: Socket())
    try:
        handler.start_turn("turn-1", 0)
        handler.append_audio(b"\0" * 1600)
        outputs = list(handler.process(_source()))
        assert len(outputs) == 1 and isinstance(outputs[0], TranscriptionFailure)
        assert outputs[0].message == "Sarvam transcription final conflicted with an owned boundary"
    finally:
        handler.cleanup()
