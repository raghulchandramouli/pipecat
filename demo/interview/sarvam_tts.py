"""Streaming Bulbul speech for complete, generation-owned interview replies."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from pipecat.frames.frames import (
    ErrorFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.utils.text.base_text_aggregator import AggregationType

from .languages import TTS_LANGUAGES
from .rumik import RumikTTSService, _Request


@dataclass(frozen=True)
class SarvamSpeechMetrics:
    """Local request timing and streamed audio duration, excluding client playback."""

    queue_wait_seconds: float
    first_audio_seconds: float
    request_seconds: float
    audio_seconds: float


class InterviewSarvamTTSService(RumikTTSService):
    """Reuse guarded admission with Sarvam's streaming HTTP synthesis transport.

    Each approved reply owns a separate HTTP stream. PCM chunks are released as
    they arrive, with generation checks at every boundary. No Rumik inference is
    performed. Closing an obsolete stream releases local work; Sarvam owns remote
    scheduling, so a failed request does not quarantine subsequent requests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        current_playback_epoch,
        speaker: str = "shubh",
        model: str = "bulbul:v3",
        language_code: str = "en-IN",
        pace: float = 0.85,
        timeout: float = 30.0,
        http_transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> None:
        """Configure streamed PCM24k speech and an optional mock HTTP transport."""
        if not api_key.strip():
            raise ValueError("Sarvam TTS requires an API key")
        if model != "bulbul:v3" or language_code not in TTS_LANGUAGES:
            raise ValueError("the demo requires bulbul:v3 with a supported language")
        if not isinstance(pace, (int, float)) or isinstance(pace, bool) or not 0.5 <= pace <= 2.0:
            raise ValueError("Sarvam TTS pace must be between 0.5 and 2.0")
        super().__init__(
            endpoint="https://api.sarvam.ai/text-to-speech/stream",
            current_playback_epoch=current_playback_epoch,
            speaker=speaker,
            timeout=timeout,
            **kwargs,
        )
        self._stream_task = None
        self._cancel_stream_task = None
        self._api_key = api_key
        self._model = model
        self._language = language_code
        self._pace = float(pace)
        self._client = httpx.AsyncClient(
            transport=http_transport, trust_env=False, timeout=timeout, follow_redirects=False
        )
        self._register_event_handler("on_sarvam_metrics", sync=True)

    def invalidate_playback(self) -> None:
        """Cancel the owned HTTP stream when speech is no longer current."""
        super().invalidate_playback()
        if self._stream_task is not None and not self._stream_task.done():
            if self._cancel_stream_task is None or self._cancel_stream_task.done():
                self._cancel_stream_task = self.create_task(
                    self.cancel_task(self._stream_task), name="sarvam-cancel-stream"
                )

    async def _synthesize(self, request: _Request) -> None:
        self._stream_task = self.create_task(self._stream(request), name="sarvam-stream")
        try:
            await self._stream_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._stream_task = None

    async def cleanup(self) -> None:
        """Release managed synthesis tasks and the reusable HTTP connection pool."""
        try:
            await super().cleanup()
            if self._cancel_stream_task is not None:
                await self.cancel_task(self._cancel_stream_task)
                self._cancel_stream_task = None
        finally:
            await self._client.aclose()

    async def push_error(self, error_msg: str, **kwargs) -> None:
        """Label inherited admission errors with the active provider."""
        await super().push_error(error_msg.replace("Rumik TTS", "Sarvam TTS"), **kwargs)

    async def _request_failed(self, request: _Request, message: str) -> None:
        """Report a synthesis failure only to the reply that still owns it."""
        if not self._eligible(request):
            return
        error = ErrorFrame(error=message)
        error.metadata.update(
            interview_playback_epoch=request.playback_epoch,
            interview_dispatch_id=request.dispatch_id,
            interview_response_token=request.response_token,
        )
        await self.push_error_frame(error)

    async def _stream(self, request: _Request) -> None:
        start = time.monotonic()
        started = False
        total = 0
        first_audio = None
        pending = b""
        try:
            if len(request.text) > 3500:
                raise ValueError("text exceeds Sarvam request limit")
            await self.start_tts_usage_metrics(request.text)
            await self.start_ttfb_metrics()
            async with asyncio.timeout(self._timeout_seconds):
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    headers={"api-subscription-key": self._api_key},
                    json={
                        "text": request.text,
                        "model": self._model,
                        "speaker": self._speaker,
                        "language_code": self._language,
                        "pace": self._pace,
                        "speech_sample_rate": 24000,
                        "output_audio_codec": "linear16",
                    },
                ) as response:
                    if response.status_code != 200:
                        await self._request_failed(
                            request, f"Sarvam TTS returned HTTP {response.status_code}"
                        )
                        return
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type not in {
                        "application/octet-stream",
                        "audio/pcm",
                        "audio/x-pcm",
                        "audio/linear16",
                        "audio/raw",
                    }:
                        raise ValueError("unexpected Sarvam audio format")
                    async for chunk in response.aiter_bytes():
                        if not self._eligible(request):
                            return
                        total += len(chunk)
                        if total > 16 * 1024 * 1024:
                            raise ValueError("Sarvam audio exceeds response limit")
                        pending += chunk
                        if first_audio is None and len(pending) < 4:
                            continue
                        if first_audio is None and pending.startswith((b"RIFF", b"OggS", b"fLaC")):
                            raise ValueError("expected raw PCM, received container")
                        aligned = len(pending) // 2 * 2
                        pcm, pending = pending[:aligned], pending[aligned:]
                        if not pcm:
                            continue
                        if not started:
                            await self._emit_response_frame(request.start, request)
                            if not self._eligible(request):
                                return
                            await self._emit_response_frame(
                                TTSStartedFrame(context_id=request.context_id), request
                            )
                            started = True
                            first_audio = time.monotonic() - start
                            await self.stop_ttfb_metrics()
                        await self._emit_response_frame(
                            TTSAudioRawFrame(pcm, 24000, 1, context_id=request.context_id), request
                        )
            if not total or pending:
                raise ValueError("empty or unaligned PCM response")
            if self._eligible(request):
                spoken = TTSTextFrame(request.text, aggregated_by=AggregationType.SENTENCE)
                spoken.context_id = request.context_id
                await self._emit_response_frame(spoken, request)
            self._last_metrics = SarvamSpeechMetrics(
                start - request.enqueued_at,
                first_audio or 0.0,
                time.monotonic() - start,
                total / 48000,
            )
            await self._call_event_handler("on_sarvam_metrics", self._last_metrics)
        except (httpx.HTTPError, TimeoutError, ValueError):
            await self._request_failed(request, "Sarvam TTS stream failed; repeat the question")
        finally:
            await self.stop_ttfb_metrics()
            if started and self._eligible(request):
                await self._emit_response_frame(
                    TTSStoppedFrame(context_id=request.context_id), request
                )
                await self._emit_response_frame(request.end, request)
