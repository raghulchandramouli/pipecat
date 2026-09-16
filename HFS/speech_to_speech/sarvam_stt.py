"""Native Sarvam manual-endpointing STT handler for the isolated HF experiment.

Each local VAD turn owns one Sarvam websocket.  Sarvam does not provide a
client correlation ID for manual boundaries, so a final is accepted only when
its timestamps exactly identify that operation's successfully sent PCM range.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic, perf_counter
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import numpy as np

from speech_to_speech.pipeline.handler_types import STTIn, STTOut
from speech_to_speech.pipeline.messages import (
    PartialTranscription,
    Transcription,
    TranscriptionFailure,
    VADAudio,
)
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
from speech_to_speech.STT.base_stt_handler import BaseSTTHandler

logger = logging.getLogger(__name__)

SARVAM_REALTIME_ENDPOINT = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
PCM_SAMPLE_RATE = 16_000
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2
FINAL_SETTLE_S = 0.02


class _WebSocket(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


ConnectFactory = Callable[..., _WebSocket]


def _pcm16le(audio: bytes | np.ndarray) -> bytes:
    """Validate mono source audio and produce one little-endian PCM16 representation."""
    if isinstance(audio, bytes):
        if len(audio) % 2:
            raise ValueError("Sarvam STT requires little-endian mono PCM16 audio")
        return audio
    if not isinstance(audio, np.ndarray) or audio.ndim != 1:
        raise ValueError("Sarvam STT audio must be one-dimensional mono PCM")
    if np.issubdtype(audio.dtype, np.floating):
        if not np.isfinite(audio).all() or (np.abs(audio) > 1).any():
            raise ValueError("Sarvam STT float audio must be finite samples in [-1, 1]")
        return np.rint(audio * 32767).astype("<i2").tobytes()
    if audio.dtype != np.dtype(np.int16):
        raise ValueError("Sarvam STT audio must be float samples or PCM16")
    return audio.astype("<i2", copy=False).tobytes()


@dataclass(frozen=True)
class SarvamSTTHandlerArguments:
    """Configuration exposed by the experiment's ``sarvam`` STT selector."""

    sarvam_stt_api_key: str | None = None
    sarvam_stt_language_code: str = "auto"
    sarvam_stt_mode: str = "codemix"
    sarvam_stt_timeout: float = 15.0
    sarvam_stt_endpoint_url: str = SARVAM_REALTIME_ENDPOINT
    sarvam_stt_max_pending_audio_bytes: int = 512 * 1024


@dataclass(frozen=True)
class _Audio:
    data: bytes


@dataclass(frozen=True)
class _Commit:
    source: VADAudio


class _Stop:
    pass


class _SarvamOperation:
    """One cancellable websocket and exactly one local turn/revision."""

    def __init__(
        self,
        handler: SarvamSTTHandler,
        generation: int,
        turn_id: str | None,
        turn_revision: int | None,
    ) -> None:
        self.handler = handler
        self.generation = generation
        self.turn_id = turn_id
        self.turn_revision = turn_revision
        self.commands: Queue[_Audio | _Commit | _Stop] = Queue(maxsize=handler._max_commands)
        self.done = Event()
        self.cancelled = Event()
        self._connection_lock = Lock()
        self._audio_queue_lock = Lock()
        self._queued_audio_bytes = 0
        self._connection: _WebSocket | None = None
        self._result: str | None = None
        self._language: str | None = None
        self.error: str | None = None
        self._committed = False
        self._commit_queued = False
        self._pcm_bytes = 0
        self._final_fingerprint: tuple[int, float, float, str] | None = None
        self._final_settle_deadline: float | None = None
        self._thread = Thread(target=self._run, name="sarvam-stt-operation", daemon=True)
        self._thread.start()

    def append(self, audio: bytes) -> None:
        if not audio or self.done.is_set() or self.cancelled.is_set():
            return
        with self._audio_queue_lock:
            if self._queued_audio_bytes + len(audio) > self.handler._max_pending_audio_bytes:
                overflow = True
            else:
                try:
                    self.commands.put_nowait(_Audio(audio))
                except Full:
                    overflow = True
                else:
                    self._queued_audio_bytes += len(audio)
                    overflow = False
        if overflow:
            self._fail("streaming transcription audio queue is full")

    def _dequeue_audio(self, byte_count: int) -> None:
        with self._audio_queue_lock:
            self._queued_audio_bytes = max(0, self._queued_audio_bytes - byte_count)

    def commit(self, source: VADAudio) -> None:
        if self.done.is_set() or self.cancelled.is_set():
            return
        with self._audio_queue_lock:
            if self._commit_queued:
                return
            try:
                self.commands.put_nowait(_Commit(source))
                self._commit_queued = True
            except Full:
                self._fail("streaming transcription audio queue is full")

    def cancel(self) -> None:
        self.cancelled.set()
        self._close_connection()
        try:
            self.commands.put_nowait(_Stop())
        except Full:
            pass

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout=timeout)
        self._close_connection()

    def _publish_connection(self, connection: _WebSocket | None) -> None:
        with self._connection_lock:
            self._connection = connection
            cancelled = self.cancelled.is_set()
        if cancelled and connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _close_connection(self) -> None:
        with self._connection_lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.debug("Ignoring Sarvam STT socket close error", exc_info=True)

    def _finish(self, text: str, language: str | None) -> None:
        if self.cancelled.is_set():
            self.done.set()
            return
        self._result = text
        self._language = language
        self.done.set()
        self._close_connection()

    def _fail(self, message: str) -> None:
        if not self.done.is_set():
            self.error = message
            self.done.set()
        self._close_connection()

    def _send(self, connection: _WebSocket, payload: dict[str, Any]) -> None:
        connection.send(json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(value, dict):
            raise ValueError("Sarvam STT event must be an object")
        return value

    def _is_current(self) -> bool:
        return not self.cancelled.is_set() and self.handler._operation_is_current(self)

    def _publish_partial(self, message: dict[str, Any]) -> None:
        text = message.get("text")
        if (
            self._final_fingerprint is not None
            or not isinstance(text, str)
            or not text.strip()
            or not self._is_current()
        ):
            return
        tracker = self.handler.speculative_turns
        if tracker is not None and not tracker.is_latest(self.turn_id, self.turn_revision):
            return
        with self.handler._publication_lock:
            if self._is_current():
                self.handler.queue_out.put(
                    PartialTranscription(
                        text=text.strip(), turn_id=self.turn_id, turn_revision=self.turn_revision
                    )
                )

    def _accept_final(self, message: dict[str, Any]) -> None:
        utterance_idx = message.get("utterance_idx")
        start_s, end_s = message.get("start_s"), message.get("end_s")
        text = message.get("text")
        valid_times = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            for value in (start_s, end_s)
        )
        if (
            not isinstance(utterance_idx, int)
            or isinstance(utterance_idx, bool)
            or not valid_times
            or end_s < start_s
        ):
            self._fail("Sarvam transcription timestamps could not be correlated")
            return
        expected_end = self._pcm_bytes / PCM_BYTES_PER_SECOND
        if abs(start_s) > 0.001 or abs(end_s - expected_end) > 0.001:
            self._fail("Sarvam transcription timestamps could not be correlated")
            return
        if not isinstance(text, str):
            self._fail("Sarvam transcription final was malformed")
            return
        normalized = text.strip()
        fingerprint = (utterance_idx, float(start_s), float(end_s), normalized)
        if self._final_fingerprint is not None:
            if fingerprint != self._final_fingerprint:
                self._fail("Sarvam transcription final conflicted with an owned boundary")
            return
        self._final_fingerprint = fingerprint
        language = message.get("language_code")
        self._result = normalized
        self._language = language if isinstance(language, str) and language else None
        self._final_settle_deadline = monotonic() + FINAL_SETTLE_S

    def _handle_event(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "transcript.partial":
            self._publish_partial(message)
        elif event == "transcript.final":
            if not self._committed:
                self._fail("Sarvam transcription final arrived before the local boundary")
            else:
                self._accept_final(message)
        elif event in {"error", "session.error"}:
            self._fail("remote streaming transcription failed")
        elif event == "session.end" and not self.done.is_set():
            if self._final_fingerprint is None:
                self._fail("remote streaming transcription ended before a final")
            else:
                self._finish(self._result or "", self._language)

    def _run(self) -> None:
        connection: _WebSocket | None = None
        try:
            connection = self.handler._connect()
            self._publish_connection(connection)
            if self.cancelled.is_set():
                return
            self._send(connection, {"event": "speech_start"})
            deadline: float | None = None
            while not self.cancelled.is_set() and not self.done.is_set():
                if (
                    self._final_settle_deadline is not None
                    and monotonic() >= self._final_settle_deadline
                ):
                    self._finish(self._result or "", self._language)
                    break
                if deadline is not None and monotonic() >= deadline:
                    self._fail("streaming transcription timed out")
                    break
                try:
                    command = self.commands.get(timeout=0.01)
                except Empty:
                    command = None
                if isinstance(command, _Stop):
                    return
                if isinstance(command, _Audio):
                    self._dequeue_audio(len(command.data))
                    if self._committed:
                        self._fail("audio arrived after the Sarvam speech boundary")
                        break
                    self._send(
                        connection,
                        {
                            "event": "audio_input",
                            "audio": base64.b64encode(command.data).decode("ascii"),
                        },
                    )
                    self._pcm_bytes += len(command.data)
                elif isinstance(command, _Commit):
                    if self._committed:
                        self._fail("duplicate Sarvam speech boundary")
                        break
                    if not self._pcm_bytes:
                        self._finish("", None)
                        break
                    self._committed = True
                    self._send(connection, {"event": "speech_end"})
                    deadline = monotonic() + self.handler.timeout
                try:
                    raw = connection.recv(timeout=0.0 if deadline is None else 0.01)
                except (TimeoutError, Empty):
                    continue
                self._handle_event(self._decode(raw))
        except Exception as exc:
            if (
                self._final_fingerprint is not None
                and not self.cancelled.is_set()
                and not self.done.is_set()
            ):
                self._finish(self._result or "", self._language)
            elif not self.cancelled.is_set() and not self.done.is_set():
                logger.warning("Sarvam STT operation failed: %s", type(exc).__name__)
                self._fail("streaming transcription connection failed")
        finally:
            self._close_connection()
            self.done.set()


class SarvamSTTHandler(BaseSTTHandler):
    """Stream manual Sarvam STT while preserving HF turn and cancellation semantics."""

    def setup(
        self,
        sarvam_api_key: str | None = None,
        language_code: str = "auto",
        mode: str = "codemix",
        timeout: float = 15.0,
        endpoint_url: str = SARVAM_REALTIME_ENDPOINT,
        max_pending_audio_bytes: int = 512 * 1024,
        speculative_turns: SpeculativeTurnTracker | None = None,
        connect_factory: ConnectFactory | None = None,
        pipeline_index: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Configure the Sarvam realtime websocket.

        ``api_key`` is accepted as a compatibility alias for local callers;
        the registry selector exposes only ``sarvam_api_key``.
        """
        api_key = sarvam_api_key or kwargs.pop("api_key", None) or os.getenv("SARVAM_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Sarvam STT requires sarvam_api_key")
        if not isinstance(language_code, str) or not language_code.strip():
            raise ValueError("Sarvam STT language_code must not be blank")
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("Sarvam STT mode must not be blank")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Sarvam STT timeout must be a positive finite number")
        if (
            not isinstance(max_pending_audio_bytes, int)
            or isinstance(max_pending_audio_bytes, bool)
            or max_pending_audio_bytes < 320
        ):
            raise ValueError("Sarvam STT max_pending_audio_bytes must be at least 320")
        split = urlsplit(endpoint_url)
        if split.scheme not in {"ws", "wss"} or not split.netloc:
            raise ValueError("Sarvam STT endpoint_url must be a ws(s) URL")
        self.speculative_turns = speculative_turns
        self.final_revision_settle_s = 0.0
        self.timeout = float(timeout)
        self._api_key = api_key.strip()
        self._endpoint_url = self._endpoint_with_params(
            endpoint_url, language_code.strip(), mode.strip()
        )
        self._connect_factory = connect_factory or _default_connect
        self._max_pending_audio_bytes = max_pending_audio_bytes
        self._max_commands = max(2, max_pending_audio_bytes // 320 + 1)
        self._state_lock = Lock()
        self._publication_lock = Lock()
        self._generation = 0
        self._operation: _SarvamOperation | None = None
        self._pre_turn_audio = bytearray()
        self.pipeline_index = pipeline_index

    @staticmethod
    def _endpoint_with_params(endpoint_url: str, language_code: str, mode: str) -> str:
        split = urlsplit(endpoint_url)
        params = dict(
            language_code=language_code,
            model="saaras:v3-realtime",
            mode=mode,
            endpointing="manual",
            sample_rate="16000",
            encoding="linear16",
            return_timestamps="true",
        )
        return urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), ""))

    def _connect(self) -> _WebSocket:
        return self._connect_factory(
            self._endpoint_url,
            headers={"API-SUBSCRIPTION-KEY": self._api_key},
            open_timeout=self.timeout,
        )

    def _operation_is_current(self, operation: _SarvamOperation) -> bool:
        with self._state_lock:
            return self._generation == operation.generation and self._operation is operation

    def start_turn(self, turn_id: str | None, turn_revision: int | None) -> None:
        """Start an isolated provider operation for the announced VAD turn."""
        old: _SarvamOperation | None
        with self._state_lock:
            old = self._operation
            self._operation = _SarvamOperation(self, self._generation, turn_id, turn_revision)
            if self._pre_turn_audio:
                self._operation.append(bytes(self._pre_turn_audio))
                self._pre_turn_audio.clear()
        if old is not None:
            old.cancel()

    def append_audio(self, audio: bytes | np.ndarray) -> None:
        """Queue validated 16 kHz mono PCM for the active local operation."""
        pcm = _pcm16le(audio)
        with self._state_lock:
            operation = self._operation
            if operation is None or operation._commit_queued:
                if len(self._pre_turn_audio) + len(pcm) > self._max_pending_audio_bytes:
                    self._pre_turn_audio.clear()
                    raise ValueError("streaming transcription pre-turn audio queue is full")
                self._pre_turn_audio.extend(pcm)
                return
        if operation is not None:
            operation.append(pcm)

    def commit_boundary(self, turn_id: str | None, turn_revision: int | None) -> None:
        """Close the matching manual boundary without accepting later audio."""
        with self._state_lock:
            operation = self._operation
        if operation is not None and (operation.turn_id, operation.turn_revision) == (
            turn_id,
            turn_revision,
        ):
            operation.commit(
                VADAudio(
                    audio=np.empty(0, dtype=np.float32),
                    mode="final",
                    turn_id=turn_id,
                    turn_revision=turn_revision,
                )
            )

    def discard_utterance(self) -> None:
        """Close and retire an uncommitted local operation."""
        self._retire_operation(increment_generation=False)

    def _retire_operation(self, *, increment_generation: bool) -> None:
        with self._state_lock:
            if increment_generation:
                self._generation += 1
            operation, self._operation = self._operation, None
            self._pre_turn_audio.clear()
        # A partial already entering the output queue must finish before a
        # session boundary can be forwarded by BaseHandler.
        with self._publication_lock:
            pass
        if operation is not None:
            operation.cancel()

    def cancel_session(self) -> None:
        """Fence current publications and close the session's provider socket."""
        self._retire_operation(increment_generation=True)

    def process(self, vad_audio: STTIn):
        """Await one owned final and translate it to the HF transcription contract."""
        if vad_audio.mode == "progressive":
            return
        with self._state_lock:
            operation = self._operation
        if operation is None or (operation.turn_id, operation.turn_revision) != (
            vad_audio.turn_id,
            vad_audio.turn_revision,
        ):
            yield TranscriptionFailure(
                message="streaming transcription received no owned audio",
                turn_id=vad_audio.turn_id,
                turn_revision=vad_audio.turn_revision,
                speech_stopped_at_s=vad_audio.created_at_s,
            )
            return
        operation.commit(vad_audio)
        deadline = perf_counter() + self.timeout
        while not operation.done.wait(timeout=min(0.05, max(0.0, deadline - perf_counter()))):
            if not self._operation_is_current(operation):
                return
            if perf_counter() >= deadline:
                operation._fail("streaming transcription timed out")
                break
        if not self._operation_is_current(operation):
            return
        with self._state_lock:
            if self._operation is operation:
                self._operation = None
        if operation.error is not None:
            yield TranscriptionFailure(
                message=operation.error,
                turn_id=vad_audio.turn_id,
                turn_revision=vad_audio.turn_revision,
                speech_stopped_at_s=vad_audio.created_at_s,
            )
            return
        tracker = self.speculative_turns
        if tracker is not None and not tracker.is_latest(
            vad_audio.turn_id, vad_audio.turn_revision
        ):
            return
        yield Transcription(
            text=operation._result or "",
            language_code=operation._language,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
        )

    def on_session_end(self) -> None:
        """Cancel provider work before forwarding the upstream session boundary."""
        self.cancel_session()
        super().on_session_end()

    def cleanup(self) -> None:
        """Release the active websocket when the handler thread exits."""
        self.cancel_session()


def _default_connect(url: str, *, headers: dict[str, str], open_timeout: float) -> _WebSocket:
    from websockets.sync.client import connect

    return connect(
        url,
        additional_headers=headers,
        open_timeout=open_timeout,
        close_timeout=1.0,
        legacy=True,
    )
