"""Native Sarvam Bulbul TTS adapter for the isolated speech-to-speech experiment.

The adapter retains the upstream handler's response identity, cancellation, and
terminal-event lifecycle.  Only the provider request and raw PCM validation
change: Sarvam accepts a subscription key and returns 24 kHz little-endian
PCM16, while the upstream pipeline consumes 16 kHz blocks.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import perf_counter
from typing import Any, cast

import numpy as np
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
from speech_to_speech.TTS.openai_compatible_handler import (
    _SPEECH_STREAM_DONE,
    _SPEECH_STREAM_POLL_INTERVAL_S,
    _SPEECH_STREAM_QUEUE_MAXSIZE,
    PIPELINE_SAMPLE_RATE,
    HttpSpeechOperation,
    OpenAICompatibleTTSHandler,
    SpeechRequestError,
    _StreamingFIRResampler,
)

SARVAM_TTS_ENDPOINT = "https://api.sarvam.ai/text-to-speech/stream"
SARVAM_MODEL = "bulbul:v3"
SARVAM_SAMPLE_RATE = 24000
SARVAM_LANGUAGES = frozenset({"en-IN", "hi-IN", "ta-IN"})
SARVAM_RAW_PCM_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "audio/linear16",
        "audio/pcm",
        "audio/raw",
        "audio/x-pcm",
    }
)
SARVAM_MAX_TEXT_CHARS = 3500
SARVAM_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_CONTAINER_PREFIXES = (b"RIFF", b"RIFX", b"RF64", b"OggS", b"fLaC", b"ID3")


@dataclass
class SarvamTTSHandlerArguments:
    """Settings accepted by :class:`SarvamTTSHandler`.

    ``sarvam_api_key`` is intentionally separate from OpenAI credentials.  If
    omitted, the handler reads ``SARVAM_API_KEY`` from its own process.
    """

    sarvam_tts_api_key: str | None = None
    sarvam_tts_language_code: str = "en-IN"
    sarvam_tts_speaker: str = "shubh"
    sarvam_tts_timeout: float = 30.0
    sarvam_tts_blocksize: int = 512
    sarvam_tts_warmup: bool = False


class _SarvamHttpSpeechOperation(HttpSpeechOperation):
    """One subscription-key-authenticated Sarvam streaming request."""

    def __init__(self, *, subscription_key: str, **kwargs: Any) -> None:
        super().__init__(api_key=None, response_format="pcm", **kwargs)
        self.subscription_key = subscription_key

    def iter_bytes(self, cancel_check: Callable[[], bool]) -> Iterator[bytes]:
        """Yield response bytes while enforcing a finite deadline and cancellation."""
        deadline_at_s = perf_counter() + self.timeout_s
        self._raise_if_stopped(cancel_check)
        results: Queue[tuple[bool, object]] = Queue(maxsize=_SPEECH_STREAM_QUEUE_MAXSIZE)
        headers = {"api-subscription-key": self.subscription_key}
        worker = Thread(
            target=self._read_stream,
            args=(headers, results, cancel_check),
            name="sarvam-tts-http-reader",
            daemon=True,
        )
        worker.start()

        completed = False
        try:
            while True:
                self._raise_if_stopped(cancel_check)
                remaining_s = deadline_at_s - perf_counter()
                if remaining_s <= 0:
                    self._deadline_exceeded.set()
                    self.cancel()
                    raise SpeechRequestError("speech request timed out")
                try:
                    succeeded, value = results.get(
                        timeout=min(_SPEECH_STREAM_POLL_INTERVAL_S, remaining_s)
                    )
                except Empty:
                    continue
                self._raise_if_stopped(cancel_check)
                if not succeeded:
                    raise cast(BaseException, value)
                if value is _SPEECH_STREAM_DONE:
                    completed = True
                    return
                yield cast(bytes, value)
        finally:
            if not completed:
                self.cancel()
            worker.join()

    def _validate_content_type(self, response: Any) -> None:
        headers = getattr(response, "headers", None)
        media_type = (
            ""
            if headers is None
            else headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if media_type not in SARVAM_RAW_PCM_MEDIA_TYPES:
            raise SpeechRequestError("Sarvam returned a non-PCM audio response")


class SarvamTTSHandler(OpenAICompatibleTTSHandler):
    """Stream native Sarvam Bulbul PCM into the upstream 16 kHz TTS lifecycle."""

    def setup(
        self,
        should_listen: Event,
        sarvam_api_key: str | None = None,
        language_code: str = "en-IN",
        speaker: str = "shubh",
        timeout: float = 30.0,
        blocksize: int = 512,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        warmup: bool = False,
    ) -> None:
        """Configure the fixed Sarvam endpoint and upstream lifecycle controls.

        Args:
            should_listen: Shared upstream listen-state event.
            sarvam_api_key: Sarvam subscription key, or ``SARVAM_API_KEY`` when omitted.
            language_code: One of ``en-IN``, ``hi-IN``, or ``ta-IN``.
            speaker: Sarvam Bulbul speaker name.
            timeout: Whole-request timeout in seconds.
            blocksize: Output block size in 16 kHz PCM samples.
            cancel_scope: Upstream generation cancellation scope.
            speculative_turns: Upstream response-identity tracker.
            warmup: Send a bounded provider preflight request during construction.
        """
        api_key = sarvam_api_key if sarvam_api_key is not None else os.getenv("SARVAM_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Sarvam TTS requires sarvam_api_key or SARVAM_API_KEY")
        if language_code not in SARVAM_LANGUAGES:
            raise ValueError("Sarvam TTS language_code must be en-IN, hi-IN, or ta-IN")
        if not isinstance(speaker, str) or not speaker.strip():
            raise ValueError("Sarvam TTS speaker must be a non-empty string")
        if timeout <= 0:
            raise ValueError("Sarvam TTS timeout must be > 0")
        if blocksize < 1:
            raise ValueError("Sarvam TTS blocksize must be >= 1")

        # These attributes are intentionally the upstream handler's lifecycle
        # seam. Its process(), cancellation, response failure, and terminal
        # handling operate unchanged over the native request operation below.
        self.should_listen = should_listen
        self.endpoint_url = SARVAM_TTS_ENDPOINT
        self.api_key = api_key.strip()
        self.language_code = language_code
        self.speaker = speaker.strip()
        self.timeout = float(timeout)
        self.blocksize = blocksize
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self._operation_lock = Lock()
        self._active_operation: HttpSpeechOperation | None = None
        self._failed_responses: set[tuple[int | None, str | None, str | None, int | None]] = set()

        # Compatibility fields used only by inherited warmup/process helpers.
        self.base_url = SARVAM_TTS_ENDPOINT.rsplit("/text-to-speech/stream", 1)[0]
        self.model = SARVAM_MODEL
        self.voice = self.speaker
        self.language = self.language_code
        self.response_format = "pcm"
        self.sample_rate = SARVAM_SAMPLE_RATE
        self.speed = 1.0
        self.stream = False
        self.task_type = None
        self.instructions = None
        self.gen_kwargs: dict[str, Any] = {}
        if warmup:
            self.warmup()

    def _make_operation(self, *, text: str, voice: str | dict[str, str]) -> HttpSpeechOperation:
        if isinstance(voice, dict):
            voice = voice.get("id", "")
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("Sarvam TTS speaker override must be a non-empty string")
        return _SarvamHttpSpeechOperation(
            endpoint_url=SARVAM_TTS_ENDPOINT,
            subscription_key=self.api_key,
            payload=self._request_payload(text=text, voice=voice),
            timeout_s=self.timeout,
        )

    def _request_payload(self, *, text: str, voice: str | dict[str, str]) -> dict[str, Any]:
        """Build Sarvam's native streaming request without OpenAI fields."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Sarvam TTS text must be non-empty")
        if len(text) > SARVAM_MAX_TEXT_CHARS:
            raise ValueError("Sarvam TTS text exceeds request limit")
        if isinstance(voice, dict):
            voice = voice.get("id", "")
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("Sarvam TTS speaker override must be a non-empty string")
        return {
            "text": text,
            "model": SARVAM_MODEL,
            "speaker": voice,
            "language_code": self.language_code,
            "speech_sample_rate": SARVAM_SAMPLE_RATE,
            "output_audio_codec": "linear16",
        }

    def _decode_pcm_stream(self, encoded_chunks: Iterator[bytes]) -> Iterator[np.ndarray]:
        """Validate raw PCM16 framing and resample one response at a time."""
        byte_remainder = b""
        total_bytes = 0
        prefix = bytearray()
        saw_audio = False

        def sample_chunks() -> Iterator[np.ndarray]:
            nonlocal byte_remainder, total_bytes, saw_audio
            for encoded in encoded_chunks:
                if not isinstance(encoded, bytes):
                    raise SpeechRequestError("Sarvam returned an invalid PCM chunk")
                total_bytes += len(encoded)
                if total_bytes > SARVAM_MAX_RESPONSE_BYTES:
                    raise SpeechRequestError("Sarvam audio exceeds response limit")
                if len(prefix) < 4:
                    prefix.extend(encoded[: 4 - len(prefix)])
                    if len(prefix) == 4 and bytes(prefix).startswith(_CONTAINER_PREFIXES):
                        raise SpeechRequestError(
                            "Sarvam returned an audio container instead of raw PCM"
                        )
                encoded = byte_remainder + encoded
                usable = len(encoded) - (len(encoded) % 2)
                byte_remainder = encoded[usable:]
                if usable:
                    saw_audio = True
                    yield np.frombuffer(encoded[:usable], dtype="<i2")
            if byte_remainder:
                raise SpeechRequestError("Sarvam returned an incomplete PCM16 sample")
            if not saw_audio:
                raise SpeechRequestError("speech endpoint returned no audio")

        yield from self._resample_to_blocks(sample_chunks(), SARVAM_SAMPLE_RATE)

    def _resample_to_blocks(
        self,
        sample_chunks: Iterator[np.ndarray],
        source_rate: int,
    ) -> Iterator[np.ndarray]:
        """Convert this response only, keeping its filter and padding isolated."""
        resampler = _StreamingFIRResampler(source_rate, PIPELINE_SAMPLE_RATE)
        sample_remainder = np.empty(0, dtype=np.int16)
        for samples in sample_chunks:
            converted = resampler.push(samples)
            sample_remainder = np.concatenate((sample_remainder, converted))
            while sample_remainder.size >= self.blocksize:
                yield sample_remainder[: self.blocksize].copy()
                sample_remainder = sample_remainder[self.blocksize :]

        converted = resampler.push(np.empty(0, dtype=np.float64), final=True)
        sample_remainder = np.concatenate((sample_remainder, converted))
        while sample_remainder.size >= self.blocksize:
            yield sample_remainder[: self.blocksize].copy()
            sample_remainder = sample_remainder[self.blocksize :]
        if sample_remainder.size:
            yield np.pad(sample_remainder, (0, self.blocksize - sample_remainder.size))
