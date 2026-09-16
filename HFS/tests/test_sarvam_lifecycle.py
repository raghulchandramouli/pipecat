"""Loopback websocket cancellation and next-turn recovery coverage for Sarvam STT."""

from __future__ import annotations

import json
from queue import Queue
from threading import Event, Thread

import numpy as np
from speech_to_speech.pipeline.messages import (
    PartialTranscription,
    Transcription,
    TranscriptionFailure,
    VADAudio,
)
from websockets.sync.server import serve

from HFS.speech_to_speech.sarvam_stt import SarvamSTTHandler


def _source(turn: str) -> VADAudio:
    return VADAudio(
        audio=np.empty(0, dtype=np.float32), mode="final", turn_id=turn, turn_revision=0
    )


def test_cancelled_operation_closes_its_socket_and_late_final_cannot_finish_next_turn() -> None:
    """A delayed A final is fenced before fresh B websocket evidence is accepted."""
    first_boundary = Event()
    first_closed = Event()
    release_late_final = Event()
    connections = 0

    def handler(socket):
        nonlocal connections
        connections += 1
        connection = connections
        pcm_bytes = 0
        try:
            for raw in socket:
                event = json.loads(raw)
                if event["event"] == "audio_input":
                    import base64

                    pcm_bytes += len(base64.b64decode(event["audio"], validate=True))
                if event["event"] != "speech_end":
                    continue
                if connection == 1:
                    first_boundary.set()
                    # Wait for cancellation before attempting a deliberately late final.
                    release_late_final.wait(1)
                    socket.send('{"event":"transcript.partial","text":"old"}')
                    socket.send(
                        '{"event":"transcript.final","utterance_idx":1,"start_s":0.0,'
                        f'"end_s":{pcm_bytes / 32000},"text":"old final"}}'
                    )
                    return
                socket.send('{"event":"transcript.partial","text":"new"}')
                socket.send(
                    '{"event":"transcript.final","utterance_idx":2,"start_s":0.0,'
                    f'"end_s":{pcm_bytes / 32000},"text":"new final"}}'
                )
                return
        finally:
            if connection == 1:
                first_closed.set()

    server = serve(handler, "127.0.0.1", 0)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/manual"
    stt = SarvamSTTHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_kwargs={"sarvam_api_key": "test", "endpoint_url": url, "timeout": 1.0},
    )
    old_outputs: list[object] = []
    old_thread = Thread(target=lambda: old_outputs.extend(stt.process(_source("old"))), daemon=True)
    try:
        stt.start_turn("old", 0)
        stt.append_audio(b"\0" * 1600)
        old_thread.start()
        assert first_boundary.wait(1)
        stt.cancel_session()
        release_late_final.set()
        assert first_closed.wait(1)
        old_thread.join(timeout=1)
        assert old_outputs == []

        stt.start_turn("new", 0)
        stt.append_audio(b"\0" * 1600)
        fresh = list(stt.process(_source("new")))
        assert [item.text for item in fresh if isinstance(item, Transcription)] == ["new final"]
        partials = []
        while not stt.queue_out.empty():
            item = stt.queue_out.get_nowait()
            if isinstance(item, PartialTranscription):
                partials.append(item.text)
        assert partials == ["new"]
    finally:
        stt.cleanup()
        server.shutdown()
        server_thread.join(timeout=2)


def test_provider_error_closes_the_operation_and_a_fresh_socket_can_succeed() -> None:
    """A terminal provider error is turn-local and cannot poison the next operation."""
    connections = 0

    def handler(socket):
        nonlocal connections
        connections += 1
        connection = connections
        pcm_bytes = 0
        for raw in socket:
            event = json.loads(raw)
            if event["event"] == "audio_input":
                import base64

                pcm_bytes += len(base64.b64decode(event["audio"], validate=True))
            if event["event"] != "speech_end":
                continue
            if connection == 1:
                socket.send('{"event":"error","message":"provider rejected turn"}')
            else:
                socket.send(
                    '{"event":"transcript.final","utterance_idx":2,"start_s":0.0,'
                    f'"end_s":{pcm_bytes / 32000},"text":"recovered"}}'
                )
            return

    server = serve(handler, "127.0.0.1", 0)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/manual"
    stt = SarvamSTTHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_kwargs={"sarvam_api_key": "test", "endpoint_url": url, "timeout": 1.0},
    )
    try:
        stt.start_turn("failed", 0)
        stt.append_audio(b"\0" * 1600)
        failed = list(stt.process(_source("failed")))
        assert len(failed) == 1 and isinstance(failed[0], TranscriptionFailure)
        assert failed[0].message == "remote streaming transcription failed"

        stt.start_turn("recovered", 0)
        stt.append_audio(b"\0" * 1600)
        recovered = list(stt.process(_source("recovered")))
        assert [item.text for item in recovered if isinstance(item, Transcription)] == ["recovered"]
        assert connections == 2
    finally:
        stt.cleanup()
        server.shutdown()
        server_thread.join(timeout=2)
