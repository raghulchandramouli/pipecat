"""Bounded Rumik text-to-speech for authorized interview replies."""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import urlparse

import httpx

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    StopFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TextAggregationMode, TTSService
from pipecat.utils.text.base_text_aggregator import AggregationType


@dataclass(frozen=True)
class RumikHTTPResponse:
    """Complete response returned by the private Rumik HTTP service.

    Parameters:
        status_code: HTTP status code returned by the local server.
        body: Complete, bounded response body.
        content_type: Response media type, if supplied by the server.
    """

    status_code: int
    body: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class RumikTTSMetrics:
    """Measured local timings for one completed Rumik request.

    Parameters:
        queue_wait_seconds: Time waiting behind an earlier local request.
        request_seconds: HTTP request duration through the complete WAV body.
        audio_seconds: Duration represented by the validated PCM payload.
    """

    queue_wait_seconds: float
    request_seconds: float
    audio_seconds: float


@dataclass(frozen=True)
class _Request:
    """One authorized sentence waiting for local synthesis.

    Parameters:
        text: Clean speech text released by the reply guard.
        context_id: TTS context emitted with the validated audio.
        response_token: Controller generation that owns the reply.
        dispatch_id: Controller dispatch that owns the reply.
        playback_epoch: Playback generation captured at guard release.
        local_epoch: Adapter generation invalidated on interruption or shutdown.
        enqueued_at: Monotonic local admission timestamp.
        start: Guard-released LLM response start frame.
        end: Matching guard-released LLM response end frame.
        direction: Original frame direction for the response envelope.
    """

    text: str
    context_id: str
    response_token: int
    dispatch_id: int
    playback_epoch: int
    local_epoch: int
    enqueued_at: float
    start: LLMFullResponseStartFrame
    end: LLMFullResponseEndFrame
    direction: FrameDirection


@dataclass
class _Assembly:
    """One bounded guarded reply envelope awaiting its terminal frame.

    Parameters:
        ownership: Guard and playback ownership tuple.
        start: First frame of the authorized response.
        direction: Original response direction.
        text: One clean reply text, once supplied.
    """

    ownership: tuple[int, int, int]
    start: LLMFullResponseStartFrame
    direction: FrameDirection
    text: str | None = None


RumikTransport = Callable[[str, dict[str, Any], float], Awaitable[RumikHTTPResponse]]
PlaybackEpoch = Callable[[], int]
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class RumikTTSService(TTSService):
    """Synthesize trusted reply-guard text through one bounded local Rumik server.

    The service receives whole, guard-released ``LLMTextFrame`` objects. It does
    not synthesize arbitrary pipeline text, completion markers, or assessment
    metadata. Rumik provides a complete WAV rather than word timestamps, so the
    adapter can report decoded audio duration but cannot identify words heard at
    interruption time. Construct exactly one adapter for each private Rumik
    process: the adapter's bounded single-flight queue is process ownership, not
    a cross-instance lock.
    """

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:6006/v1/audio/speech",
        speaker: str = "Ira",
        current_playback_epoch: PlaybackEpoch,
        http_post: RumikTransport | None = None,
        queue_capacity: int = 2,
        timeout: float = 600.0,
        **kwargs: Any,
    ) -> None:
        """Initialize bounded synthesis without starting HTTP work.

        Args:
            endpoint: Credential-free private Rumik speech endpoint.
            speaker: Rumik speaker passed with every approved request.
            current_playback_epoch: Session callback invalidated by candidate speech
                and controls, but not ordinary reply-controller advancement.
            http_post: Injectable complete-response HTTP transport for tests.
            queue_capacity: Maximum pending work behind one active HTTP request.
            timeout: Positive complete-request deadline.
            **kwargs: Additional TTS service options. ``push_silence_after_stop``
                is unsupported because its unowned audio bypasses reply ownership.
        """
        try:
            parsed_endpoint = urlparse(endpoint)
            valid_endpoint = (
                parsed_endpoint.scheme in {"http", "https"}
                and parsed_endpoint.hostname is not None
                and parsed_endpoint.username is None
                and parsed_endpoint.password is None
                and not parsed_endpoint.query
                and not parsed_endpoint.fragment
            )
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            raise ValueError("endpoint must be a credential-free HTTP(S) URL")
        if not speaker.strip():
            raise ValueError("speaker must be non-empty")
        if (
            not isinstance(queue_capacity, int)
            or isinstance(queue_capacity, bool)
            or queue_capacity < 0
        ):
            raise ValueError("queue_capacity must be non-negative")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        if kwargs.pop("push_silence_after_stop", False):
            raise ValueError("push_silence_after_stop is unsupported")
        kwargs.setdefault("text_aggregation_mode", TextAggregationMode.TOKEN)
        kwargs.setdefault("settings", TTSSettings(model=None, voice=None, language=None))
        super().__init__(
            sample_rate=24_000, push_start_frame=False, push_stop_frames=False, **kwargs
        )
        self._endpoint = endpoint
        self._speaker = speaker
        self._current_playback_epoch = current_playback_epoch
        self._http_post = http_post or self._post
        self._queue_capacity = queue_capacity
        self._timeout_seconds = float(timeout)
        self._pending: deque[_Request] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._http_tasks: set[asyncio.Task[RumikHTTPResponse]] = set()
        self._active: _Request | None = None
        self._local_epoch = 0
        self._ending: EndFrame | None = None
        self._assembly: _Assembly | None = None
        self._retired_dispatch_id = -1
        self._closed = False
        self._quarantined = False
        self._last_metrics: RumikTTSMetrics | None = None
        self._register_event_handler("on_rumik_metrics", sync=True)

    @property
    def last_metrics(self) -> RumikTTSMetrics | None:
        """Return timings for the most recent validated WAV response."""
        return self._last_metrics

    def invalidate_playback(self) -> None:
        """Discard pending work and make active or late audio ineligible for emission."""
        self._local_epoch += 1
        self._pending.clear()
        self._retire_assembly()
        self._wake.set()

    def recover_after_server_restart(self) -> None:
        """Permit new requests after the operator verifies the private server recovered.

        Raises:
            RuntimeError: If an HTTP request is still active locally.
        """
        if self._active is not None or any(not task.done() for task in self._http_tasks):
            raise RuntimeError("cannot recover Rumik admission while an HTTP request is active")
        self._quarantined = False

    def _abort(self) -> None:
        """Close new admission immediately without allowing a later graceful-end leak."""
        self.invalidate_playback()
        self._closed = True
        self._ending = None
        self._retire_assembly()

    async def start(self, frame: StartFrame) -> None:
        """Start base TTS lifecycle and the single local synthesis worker."""
        await super().start(frame)
        self._closed = False
        self._ensure_worker()

    async def cancel(self, frame: CancelFrame) -> None:
        """Invalidate playback immediately while leaving the active HTTP completion bounded."""
        self._abort()
        await super().cancel(frame)

    async def stop(self, frame: EndFrame) -> None:
        """Schedule ordered end delivery after queued local audio without blocking frames."""
        self._ending = frame
        self._wake.set()

    async def cleanup(self) -> None:
        """Cancel local worker ownership during processor teardown."""
        self.invalidate_playback()
        if self._worker is not None and self._worker is not asyncio.current_task():
            await self.cancel_task(self._worker)
        self._worker = None
        for task in tuple(self._http_tasks):
            if task is not asyncio.current_task():
                await self.cancel_task(task)
        self._http_tasks.clear()
        await super().cleanup()

    async def run_tts(self, text: str, context_id: str):
        """Satisfy the TTS base contract; requests are scheduled by ``process_frame``.

        Args:
            text: Unused because only authorized frame handling can schedule work.
            context_id: Unused context identifier.

        Yields:
            No frames.
        """
        del text, context_id
        if False:
            yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Accept trusted text quickly and leave HTTP work to the managed worker."""
        await AIService.process_frame(self, frame, direction)
        if (
            isinstance(frame, (LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame))
            and direction != FrameDirection.DOWNSTREAM
        ):
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMTextFrame):
            self._assemble_text(frame)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._assemble_start(frame, direction)
            return
        if isinstance(frame, InterruptionFrame):
            self.invalidate_playback()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (CancelFrame, StopFrame)):
            self._abort()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            await self._assemble_end(frame, direction)
            return
        if isinstance(frame, EndFrame):
            if self._closed:
                await self.push_frame(frame, direction)
                return
            self._retire_assembly()
            self._ending = frame
            self._wake.set()
            return
        await self.push_frame(frame, direction)

    def _assemble_start(self, frame: LLMFullResponseStartFrame, direction: FrameDirection) -> None:
        """Begin one bounded reply envelope only for a fresh controller dispatch."""
        ownership = self._ownership(frame.metadata)
        if ownership is None or ownership[1] <= self._retired_dispatch_id:
            return
        if self._assembly is not None and ownership[1] < self._assembly.ownership[1]:
            self._retire(ownership[1])
            return
        self._retire_assembly()
        if ownership[1] <= self._retired_dispatch_id:
            return
        self._assembly = _Assembly(ownership, frame, direction)

    def _assemble_text(self, frame: LLMTextFrame) -> None:
        """Retain exactly one clean guarded text frame for the active envelope."""
        assembly = self._assembly
        ownership = self._ownership(frame.metadata)
        text = self._safe_text(frame.text)
        if (
            assembly is None
            or ownership != assembly.ownership
            or ownership[1] <= self._retired_dispatch_id
            or assembly.text is not None
            or text is None
            or len(frame.text) > 16_000
        ):
            if assembly is not None and ownership == assembly.ownership:
                self._retire_assembly()
            elif ownership is not None:
                self._retire(ownership[1])
            return
        assembly.text = text

    async def _assemble_end(
        self, frame: LLMFullResponseEndFrame, direction: FrameDirection
    ) -> None:
        """Commit one complete matching envelope to bounded synthesis admission."""
        assembly = self._assembly
        ownership = self._ownership(frame.metadata)
        if (
            assembly is None
            or ownership != assembly.ownership
            or ownership[1] <= self._retired_dispatch_id
        ):
            if ownership is not None:
                self._retire(ownership[1])
            return
        self._assembly = None
        if (
            assembly.text is None
            or direction != assembly.direction
            or self._closed
            or self._ending is not None
        ):
            self._retire(ownership[1])
            return
        self._retire(ownership[1])
        if self._quarantined:
            await self.push_error("Rumik TTS is quarantined pending private-server recovery")
            return
        if ownership[2] != self._current_playback_epoch():
            return
        self._pending = deque(request for request in self._pending if self._eligible(request))
        projected_waiting = len(self._pending) + (1 if self._active is not None else 0)
        if projected_waiting > self._queue_capacity:
            await self.push_error("Rumik TTS queue is full")
            return
        request = _Request(
            text=assembly.text,
            context_id=self.create_context_id(),
            response_token=ownership[0],
            dispatch_id=ownership[1],
            playback_epoch=ownership[2],
            local_epoch=self._local_epoch,
            enqueued_at=time.monotonic(),
            start=assembly.start,
            end=frame,
            direction=direction,
        )
        self._pending.append(request)
        self._ensure_worker()
        self._wake.set()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = self.create_task(self._run(), name=f"{self}::rumik_tts")

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                request = self._pending.popleft()
                if not self._eligible(request):
                    continue
                self._active = request
                await self._synthesize(request)
                self._active = None
                if self._quarantined:
                    self._pending.clear()
            if self._ending is not None:
                end = self._ending
                self._closed = True
                await super().stop(end)
                if self._ending is end:
                    self._ending = None
                    await self.push_frame(end)
                return

    async def _synthesize(self, request: _Request) -> None:
        queued_at = time.monotonic()
        try:
            await self.start_tts_usage_metrics(request.text)
            await self.start_ttfb_metrics()
            response = await self._request_http({"input": request.text, "speaker": self._speaker})
            request_seconds = time.monotonic() - queued_at
            if not 200 <= response.status_code < 300:
                await self._error(f"Rumik TTS returned HTTP {response.status_code}")
                return
            pcm, audio_seconds = self._decode_wav(response)
            await self.stop_ttfb_metrics()
            self._last_metrics = RumikTTSMetrics(
                queue_wait_seconds=queued_at - request.enqueued_at,
                request_seconds=request_seconds,
                audio_seconds=audio_seconds,
            )
            await self._call_event_handler("on_rumik_metrics", self._last_metrics)
            if not self._eligible(request):
                return
            await self._emit_response_frame(request.start, request)
            if not self._eligible(request):
                return
            started = TTSStartedFrame(context_id=request.context_id)
            self._stamp_ownership(started, request)
            await self.push_frame(started)
            if not self._eligible(request):
                return
            audio = TTSAudioRawFrame(
                audio=pcm,
                sample_rate=24_000,
                num_channels=1,
                context_id=request.context_id,
            )
            self._stamp_ownership(audio, request)
            await self.push_frame(audio)
            if self._eligible(request):
                spoken = TTSTextFrame(request.text, aggregated_by=AggregationType.SENTENCE)
                spoken.context_id = request.context_id
                self._stamp_ownership(spoken, request)
                await self.push_frame(spoken)
            if self._eligible(request):
                stopped = TTSStoppedFrame(context_id=request.context_id)
                self._stamp_ownership(stopped, request)
                await self.push_frame(stopped)
            if self._eligible(request):
                await self._emit_response_frame(request.end, request)
        except TimeoutError as error:
            self._quarantined = True
            await self._error("Rumik TTS timed out; remote completion is unknown", error)
        except (EOFError, ValueError, wave.Error) as error:
            await self._error("Rumik TTS returned an invalid WAV response", error)
        except Exception as error:
            self._quarantined = True
            await self._error("Rumik TTS request failed", error)
        finally:
            await self.stop_ttfb_metrics()

    async def _request_http(self, payload: dict[str, Any]) -> RumikHTTPResponse:
        """Bound every injected or default HTTP transport and retain ambiguous work."""
        task = self.create_task(
            self._http_post(self._endpoint, payload, self._timeout_seconds),
            name=f"{self}::rumik_http",
        )
        self._http_tasks.add(task)
        task.add_done_callback(self._observe_http_task)
        return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout_seconds)

    def _observe_http_task(self, task: asyncio.Task[RumikHTTPResponse]) -> None:
        """Retire a late HTTP task and retrieve any exception after a timeout."""
        self._http_tasks.discard(task)
        if not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    async def _post(
        self, endpoint: str, payload: dict[str, Any], timeout_seconds: float
    ) -> RumikHTTPResponse:
        """POST one bounded complete request to the private local server."""
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(trust_env=False, timeout=timeout_seconds) as client:
                async with client.stream(
                    "POST", endpoint, json=payload, headers={"Accept": "audio/wav"}
                ) as response:
                    declared = response.headers.get("Content-Length")
                    if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
                        raise ValueError("Rumik response exceeds audio limit")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise ValueError("Rumik response exceeds audio limit")
                return RumikHTTPResponse(
                    response.status_code, bytes(body), response.headers.get("Content-Type")
                )

    def _decode_wav(self, response: RumikHTTPResponse) -> tuple[bytes, float]:
        """Return PCM16 payload and duration after validating the pinned WAV contract."""
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds audio limit")
        if response.content_type and not response.content_type.lower().startswith("audio/wav"):
            raise ValueError("response content type is not audio/wav")
        if (
            len(response.body) < 12
            or response.body[:4] != b"RIFF"
            or response.body[8:12] != b"WAVE"
            or int.from_bytes(response.body[4:8], "little") + 8 != len(response.body)
        ):
            raise ValueError("response is not a complete RIFF/WAVE container")
        with wave.open(io.BytesIO(response.body), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 24_000
                or wav.getcomptype() != "NONE"
                or wav.getnframes() < 1
            ):
                raise ValueError("response must be mono 24000 Hz PCM16 WAV")
            frames = wav.getnframes()
            pcm = wav.readframes(frames)
        if len(pcm) != frames * 2:
            raise ValueError("response WAV PCM payload is truncated")
        return pcm, frames / 24_000

    def _eligible(self, request: _Request) -> bool:
        return (
            not self._closed
            and request.local_epoch == self._local_epoch
            and request.playback_epoch == self._current_playback_epoch()
        )

    async def _emit_response_frame(self, frame: Frame, request: _Request) -> None:
        """Emit one captured envelope boundary with immutable ownership metadata."""
        if not self._eligible(request):
            return
        self._stamp_ownership(frame, request)
        await self.push_frame(frame, request.direction)

    @staticmethod
    def _stamp_ownership(frame: Frame, request: _Request) -> None:
        """Attach the guard and playback ownership captured at request admission."""
        frame.metadata.update(
            {
                "interview_response_token": request.response_token,
                "interview_dispatch_id": request.dispatch_id,
                "interview_playback_epoch": request.playback_epoch,
            }
        )

    @staticmethod
    def _ownership(metadata: dict[str, Any]) -> tuple[int, int, int] | None:
        keys = (
            "interview_response_token",
            "interview_dispatch_id",
            "interview_playback_epoch",
        )
        values = tuple(metadata.get(key) for key in keys)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            return None
        return values  # type: ignore[return-value]

    def _retire(self, dispatch_id: int) -> None:
        """Advance the monotonic reply replay watermark."""
        self._retired_dispatch_id = max(self._retired_dispatch_id, dispatch_id)
        if self._assembly is not None and self._assembly.ownership[1] <= self._retired_dispatch_id:
            self._assembly = None

    def _retire_assembly(self) -> None:
        """Discard a partial envelope without retaining unbounded response state."""
        if self._assembly is not None:
            self._retire(self._assembly.ownership[1])

    @staticmethod
    def _safe_text(text: str) -> str | None:
        cleaned = text.strip()
        cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
        if (
            not cleaned
            or any(marker in cleaned for marker in "●◐○")
            or cleaned.startswith(("{", "["))
            or "<" in cleaned
            or ">" in cleaned
            or any(ord(character) < 32 and character not in "\n\t" for character in cleaned)
        ):
            return None
        return cleaned

    async def _error(self, message: str, error: Exception | None = None) -> None:
        """Report a recoverable service error without rendering it permanent."""
        del error
        await self.push_error(message)
