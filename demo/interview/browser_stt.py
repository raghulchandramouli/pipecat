"""Match manual Sarvam finals to successfully sent PCM intervals."""

import asyncio
import base64
import math
from collections.abc import AsyncGenerator
from typing import Any

from pipecat.frames.frames import Frame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from .sarvam_events import InterviewSarvamRealtimeSTTService


class BrowserSarvamSTTService(InterviewSarvamRealtimeSTTService):
    """Bind provider IDs by exact audio-time boundaries, never response arrival order.

    Sarvam timestamps begin at each successfully sent manual boundary. A bounded
    local pre-roll preserves speech onset that reached the service before VAD
    confirmed the boundary. Millisecond rounding permits a one-millisecond
    tolerance. Missing, ambiguous, or inconsistent timestamps leave the ledger
    unready.
    """

    def __init__(self, *, pre_roll_secs: float = 0.4, **kwargs: Any) -> None:
        """Enable required timestamps and initialize connection-local PCM accounting.

        Args:
            pre_roll_secs: Bounded PCM retained before a local VAD speech start.
            **kwargs: Manual Sarvam service settings and backend credentials.
        """
        if not math.isfinite(pre_roll_secs) or pre_roll_secs <= 0:
            raise ValueError("browser speech pre-roll must be a positive finite duration")
        kwargs["return_timestamps"] = True
        if kwargs.get("sample_rate", 16000) != 16000:
            raise ValueError("browser speech correlation requires 16000 Hz PCM")
        kwargs["sample_rate"] = 16000
        super().__init__(**kwargs)
        self._wire_lock = asyncio.Lock()
        self._sent_pcm_bytes = 0
        self._audio_intervals: dict[int, tuple[float, float | None]] = {}
        self._timestamp_failed = False
        self._retired_boundary_ids: set[int] = set()
        self._retired_utterance_idxs: set[int] = set()
        self._retired_final_count = 0
        self._pre_roll_limit_bytes = int(pre_roll_secs * 16_000 * 2)
        self._pre_roll_pcm = bytearray()

    @property
    def timestamp_correlation_trusted(self) -> bool:
        """Return whether this connection has avoided timestamp correlation failure."""
        return not self._timestamp_failed

    @property
    def retired_final_count(self) -> int:
        """Return the number of identifiable late finals dropped after retirement."""
        return self._retired_final_count

    def can_repair_boundary(self, boundary_id: int) -> bool:
        """Return whether one closed interval can be selectively repaired safely.

        This remains false after any timestamp send/correlation failure. Repair
        cannot make a connection trustworthy again.
        """
        interval = self._audio_intervals.get(boundary_id)
        websocket = self._websocket
        return (
            not self._timestamp_failed
            and boundary_id not in self._retired_boundary_ids
            and boundary_id in self._boundaries
            and boundary_id not in self._failed_boundary_ids
            and interval is not None
            and interval[1] is not None
            and websocket is not None
            and getattr(getattr(websocket, "state", None), "name", None) == "OPEN"
        )

    def retire_boundary(self, boundary_id: int) -> bool:
        """Retire one known interval so its late final cannot bind fresh speech."""
        return self.retire_boundaries((boundary_id,))

    def retire_boundaries(self, boundary_ids: tuple[int, ...]) -> bool:
        """Atomically retire trustworthy closed intervals before a ledger repair."""
        if not boundary_ids or len(set(boundary_ids)) != len(boundary_ids):
            return False
        if not all(self.can_repair_boundary(boundary_id) for boundary_id in boundary_ids):
            return False
        self._retired_boundary_ids.update(boundary_ids)
        self._retired_utterance_idxs.update(
            utterance_idx
            for utterance_idx, bound_boundary in self._boundary_by_utterance_idx.items()
            if bound_boundary in self._retired_boundary_ids
        )
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Open Sarvam's manual interval before sending the local VAD pre-roll."""
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._flush_pre_roll()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Stream older idle audio while retaining recent onset PCM for the next boundary."""
        if self._active_boundary_id is None:
            self._pre_roll_pcm.extend(audio)
            excess = len(self._pre_roll_pcm) - self._pre_roll_limit_bytes
            if excess > 0:
                older_audio = bytes(self._pre_roll_pcm[:excess])
                del self._pre_roll_pcm[:excess]
                async for frame in super().run_stt(older_audio):
                    yield frame
            yield None
            return
        async for frame in super().run_stt(audio):
            yield frame

    async def _flush_pre_roll(self) -> None:
        """Send retained onset PCM after a successful manual speech-start boundary."""
        if not self._pre_roll_pcm:
            return
        boundary_id = self._active_boundary_id
        if boundary_id is None or boundary_id in self._failed_boundary_ids:
            self._pre_roll_pcm.clear()
            return
        audio = bytes(self._pre_roll_pcm)
        self._pre_roll_pcm.clear()
        async for _ in super().run_stt(audio):
            pass

    async def _send_json(self, payload: dict[str, Any]) -> bool:
        async with self._wire_lock:
            event = payload.get("event")
            boundary = self._active_boundary_id
            try:
                sent = await super()._send_json(payload)
            except Exception:
                self._timestamp_failed = True
                raise
            if not sent:
                if event in {"audio_input", "speech_start", "speech_end"}:
                    self._timestamp_failed = True
                return False
            if event == "audio_input":
                self._sent_pcm_bytes += len(base64.b64decode(payload["audio"], validate=True))
            elif event == "speech_start" and boundary is not None:
                self._audio_intervals[boundary] = (self._sent_pcm_bytes / 32000, None)
            elif event == "speech_end" and boundary in self._audio_intervals:
                start, _ = self._audio_intervals[boundary]
                self._audio_intervals[boundary] = (start, self._sent_pcm_bytes / 32000)
            return True

    async def _handle_final_transcript(self, message: dict[str, Any]) -> None:
        async with self._wire_lock:
            idx = message.get("utterance_idx")
            if self._is_utterance_idx(idx) and (
                idx in self._retired_utterance_idxs
                or self._boundary_by_utterance_idx.get(idx) in self._retired_boundary_ids
            ):
                self._retired_utterance_idxs.add(idx)
                self._retired_final_count += 1
                return
            start, end = message.get("start_s"), message.get("end_s")
            valid = all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
                for value in (start, end)
            )
            matches = []
            if valid and end >= start and not self._timestamp_failed:
                matches = [
                    boundary
                    for boundary, (left, right) in self._audio_intervals.items()
                    if right is not None
                    and abs(start - left) <= 0.001
                    and abs(end - right) <= 0.001
                ]
            if len(matches) == 1 and self._is_utterance_idx(idx):
                if matches[0] in self._retired_boundary_ids:
                    self._retired_utterance_idxs.add(idx)
                    self._retired_final_count += 1
                    return
                self._observed_utterance_idxs.add(idx)
                bound = self._boundary_by_utterance_idx.get(idx)
                if bound is not None and bound != matches[0]:
                    self._timestamp_failed = True
                    await self._emit_correlation_failure("timestamp_identity_conflict")
                    return
                # A boundary may resolve to only one provider utterance.
                if any(
                    b == matches[0] and i != idx for i, b in self._boundary_by_utterance_idx.items()
                ):
                    self._timestamp_failed = True
                    await self._emit_correlation_failure("timestamp_boundary_conflict")
                    return
                await self.bind_provider_utterance(boundary_id=matches[0], utterance_idx=idx)
            else:
                self._timestamp_failed = True
                await self._emit_correlation_failure("unmatched_audio_timestamps")
                return
            await super()._handle_final_transcript(message)
