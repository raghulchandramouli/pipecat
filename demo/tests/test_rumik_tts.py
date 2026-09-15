"""Frame and ownership coverage for the private Rumik TTS adapter."""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import Awaitable, Callable

import pytest

from demo.interview.contracts import ReplyKind
from demo.interview.reply_guard import ReplyAuthorization, ReplyGuardProcessor
from demo.interview.rumik import RumikHTTPResponse, RumikTTSService
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
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
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import run_test
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


def _wav(
    pcm: bytes = b"\x01\x00" * 240,
    *,
    sample_rate: int = 24_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Build one complete PCM WAV response for the fake private server."""
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return output.getvalue()


def _metadata(epoch: int = 4, *, token: int = 17, dispatch: int = 23) -> dict[str, int]:
    """Return the full controller-owned authorization required by Rumik."""
    return {
        "interview_response_token": token,
        "interview_dispatch_id": dispatch,
        "interview_playback_epoch": epoch,
    }


def _text(text: str, epoch: int = 4, **kwargs: int) -> LLMTextFrame:
    """Make an already guard-approved text frame with authorization metadata."""
    frame = LLMTextFrame(text)
    frame.metadata.update(_metadata(epoch, **kwargs))
    return frame


def _envelope(text: str, epoch: int = 4, **kwargs: int) -> list[Frame]:
    """Make one complete, guard-shaped response envelope for the adapter."""
    metadata = _metadata(epoch, **kwargs)
    start = LLMFullResponseStartFrame()
    start.metadata.update(metadata)
    content = LLMTextFrame(text)
    content.metadata.update(metadata)
    end = LLMFullResponseEndFrame()
    end.metadata.update(metadata)
    return [start, content, end]


async def _submit(service: RumikTTSService, text: str, epoch: int = 4, **kwargs: int) -> None:
    """Submit a complete authorized response directly to a running adapter."""
    for frame in _envelope(text, epoch, **kwargs):
        await service.process_frame(frame, FrameDirection.DOWNSTREAM)


async def _yield_tasks() -> None:
    """Let a managed adapter worker consume explicitly completed fake I/O."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _direct_service(
    post: Callable[[str, dict[str, object], float], Awaitable[RumikHTTPResponse]],
    *,
    epoch: list[int] | None = None,
    queue_capacity: int = 2,
    timeout: float = 600.0,
) -> tuple[RumikTTSService, list[tuple[Frame, FrameDirection]], list[int]]:
    """Set up a real adapter with a capture sink for controlled late responses."""
    playback_epoch = epoch if epoch is not None else [4]
    service = RumikTTSService(
        current_playback_epoch=lambda: playback_epoch[0],
        http_post=post,
        queue_capacity=queue_capacity,
        timeout=timeout,
    )
    captured: list[tuple[Frame, FrameDirection]] = []

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        captured.append((frame, direction))

    service.push_frame = capture
    await service.setup(frame_processor_setup(TaskManager()))
    await service.start(StartFrame())
    return service, captured, playback_epoch


async def _cleanup(service: RumikTTSService) -> None:
    """Close a direct adapter test and give cancellation a chance to settle."""
    await service.cleanup()
    await _yield_tasks()
    assert not service._task_manager.current_tasks()


@pytest.mark.asyncio
async def test_guarded_reply_stamps_epoch_and_emits_one_scoped_pcm_lifecycle():
    """Only a validated guard release reaches Rumik, with its current playback epoch."""
    epoch = [9]
    requests: list[dict[str, object]] = []
    pcm = b"\x01\x00\x02\x00" * 120

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        requests.append(payload)
        return RumikHTTPResponse(200, _wav(pcm), "audio/wav")

    authorization = ReplyAuthorization(17, 23, 3, ReplyKind.FOLLOW_UP, 5, True)
    guard = ReplyGuardProcessor(
        lookup_authorization=lambda metadata: (
            authorization if metadata.get("interview_dispatch_id") == 23 else None
        ),
        is_current=lambda candidate: candidate == authorization,
        output_metadata=lambda: {"interview_playback_epoch": epoch[0]},
    )
    tts = RumikTTSService(current_playback_epoch=lambda: epoch[0], http_post=post)
    start = LLMFullResponseStartFrame()
    start.metadata.update(_metadata(epoch=0))
    text = LLMTextFrame("● What signal changed first?")
    text.metadata.update(_metadata(epoch=0))
    end = LLMFullResponseEndFrame()
    end.metadata.update(_metadata(epoch=0))

    down, up = await run_test(Pipeline([guard, tts]), frames_to_send=[start, text, end])

    assert requests == [{"input": "What signal changed first?", "speaker": "Ira"}]
    ordered = [
        type(frame)
        for frame in down
        if isinstance(
            frame,
            (
                LLMFullResponseStartFrame,
                TTSStartedFrame,
                TTSAudioRawFrame,
                TTSTextFrame,
                TTSStoppedFrame,
                LLMFullResponseEndFrame,
            ),
        )
    ]
    assert ordered == [
        LLMFullResponseStartFrame,
        TTSStartedFrame,
        TTSAudioRawFrame,
        TTSTextFrame,
        TTSStoppedFrame,
        LLMFullResponseEndFrame,
    ]
    lifecycle = [
        frame
        for frame in down
        if isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSTextFrame, TTSStoppedFrame))
    ]
    assert [type(frame) for frame in lifecycle] == [
        TTSStartedFrame,
        TTSAudioRawFrame,
        TTSTextFrame,
        TTSStoppedFrame,
    ]
    started, audio, spoken, stopped = lifecycle
    assert started.context_id == audio.context_id == spoken.context_id == stopped.context_id
    assert audio.audio == pcm
    assert not audio.audio.startswith(b"RIFF")
    assert (audio.sample_rate, audio.num_channels) == (24_000, 1)
    assert spoken.text == "What signal changed first?"
    assert all(frame.metadata == _metadata(epoch=9) for frame in lifecycle)
    assert not [frame for frame in up if isinstance(frame, ErrorFrame)]


@pytest.mark.asyncio
async def test_invalid_wav_does_not_quarantine_a_later_valid_request():
    """A completed validation error is safe: the next approved sentence can progress."""
    responses = [
        RumikHTTPResponse(200, b"corrupt", "audio/wav"),
        RumikHTTPResponse(200, _wav(), "audio/wav"),
    ]

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        return responses.pop(0)

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post, queue_capacity=2)
    down, up = await run_test(
        tts, frames_to_send=[*_envelope("bad"), *_envelope("safe", dispatch=24)]
    )
    assert any(isinstance(frame, ErrorFrame) for frame in up)
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 1


@pytest.mark.asyncio
async def test_zero_queue_allows_one_active_request_but_no_waiting_work():
    """Zero capacity still permits the active request but rejects every waiting request."""
    calls = 0

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        nonlocal calls
        calls += 1
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post, queue_capacity=0)
    down, up = await run_test(
        tts, frames_to_send=[*_envelope("active"), *_envelope("never queued", dispatch=24)]
    )
    assert calls == 1
    assert any(isinstance(frame, ErrorFrame) for frame in up)
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 1


@pytest.mark.asyncio
async def test_bare_or_mixed_response_envelopes_never_reach_http():
    """Only one matching Start, clean text, and matching End can admit speech."""
    calls = 0

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        nonlocal calls
        calls += 1
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post)
    first = _envelope("one", dispatch=23)
    duplicate = _text("two", dispatch=23)
    mismatched_end = LLMFullResponseEndFrame()
    mismatched_end.metadata.update(_metadata(dispatch=99))
    down, up = await run_test(
        tts,
        frames_to_send=[
            _text("bare"),
            first[0],
            first[1],
            duplicate,
            mismatched_end,
        ],
    )
    assert calls == 0
    assert not any(
        isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame)) for frame in down
    )
    assert not any(isinstance(frame, ErrorFrame) for frame in up)


@pytest.mark.asyncio
async def test_duplicate_start_and_mismatched_higher_frames_retire_the_old_envelope():
    """Malformed envelope races cannot revive a retired dispatch or block a later one."""
    calls: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    duplicate = _envelope("stale", dispatch=23)
    higher_text = _text("mismatch", dispatch=24)
    old_end = LLMFullResponseEndFrame()
    old_end.metadata.update(_metadata(dispatch=23))
    fresh = _envelope("fresh", dispatch=25)
    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post, queue_capacity=2)
    down, _up = await run_test(
        tts,
        frames_to_send=[
            duplicate[0],
            duplicate[0],
            duplicate[1],
            duplicate[2],
            higher_text,
            old_end,
            *fresh,
        ],
    )
    assert calls == ["fresh"]
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 1


@pytest.mark.asyncio
async def test_http_admission_waits_for_the_matching_end_frame():
    """A start/text prefix cannot begin remote synthesis before its terminal guard boundary."""
    calls = 0

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        nonlocal calls
        calls += 1
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post)
    try:
        partial = _envelope("only after end", epoch[0])
        await tts.process_frame(partial[0], FrameDirection.DOWNSTREAM)
        await tts.process_frame(partial[1], FrameDirection.DOWNSTREAM)
        await _yield_tasks()
        assert calls == 0
        await tts.process_frame(partial[2], FrameDirection.DOWNSTREAM)
        await _yield_tasks()
        assert calls == 1
        assert len([frame for frame, _ in captured if isinstance(frame, TTSAudioRawFrame)]) == 1
    finally:
        await _cleanup(tts)


@pytest.mark.asyncio
async def test_lower_start_cannot_disturb_a_newer_incomplete_envelope():
    """A delayed lower dispatch is ignored while a newer response stays eligible."""
    calls: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    newer = _envelope("newest", dispatch=25)
    lower = _envelope("older", dispatch=24)
    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post)
    down, _up = await run_test(tts, frames_to_send=[newer[0], lower[0], newer[1], newer[2]])
    assert calls == ["newest"]
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 1


@pytest.mark.asyncio
async def test_upstream_envelopes_pass_without_synthesis():
    """Provider output control frames moving upstream never enter local speech admission."""
    calls = 0

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        nonlocal calls
        calls += 1
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post)
    down, up = await run_test(
        tts,
        frames_to_send=_envelope("upstream reply"),
        frames_to_send_direction=FrameDirection.UPSTREAM,
    )
    assert calls == 0
    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in down + up)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"endpoint": "http://user@127.0.0.1:6006/v1/audio/speech"}, "endpoint"),
        ({"endpoint": "http://127.0.0.1:6006/v1/audio/speech?token=x"}, "endpoint"),
        ({"push_silence_after_stop": True}, "push_silence_after_stop"),
    ],
)
def test_unsafe_adapter_configuration_is_rejected(kwargs, message):
    """The adapter refuses URL credential/query leakage and synthetic trailing audio."""
    with pytest.raises(ValueError, match=message):
        RumikTTSService(current_playback_epoch=lambda: 0, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        RumikHTTPResponse(200, b"not a wav", "audio/wav"),
        RumikHTTPResponse(200, b"", "audio/wav"),
        RumikHTTPResponse(200, _wav(sample_rate=16_000), "audio/wav"),
        RumikHTTPResponse(200, _wav(channels=2), "audio/wav"),
        RumikHTTPResponse(200, _wav(sample_width=1), "audio/wav"),
        RumikHTTPResponse(200, _wav(), "application/octet-stream"),
    ],
)
async def test_bad_empty_and_unsupported_wav_are_nonfatal_and_emit_no_lifecycle(response):
    """Container validation fails closed without emitting partial or mislabelled PCM."""

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        return response

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post)
    down, up = await run_test(tts, frames_to_send=_envelope("safe reply"))
    assert any(isinstance(frame, ErrorFrame) and not frame.fatal for frame in up)
    assert not any(
        isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame)) for frame in down
    )


@pytest.mark.asyncio
async def test_non_success_and_sanitization_fail_without_poisoning_later_request():
    """A definitive HTTP failure and rejected marker text leave later work usable."""
    calls: list[str] = []

    async def post(_endpoint: str, payload: dict[str, object], timeout: float) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        if payload["input"] == "server error":
            return RumikHTTPResponse(503, b"busy", "text/plain")
        assert timeout == 12.0
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts = RumikTTSService(
        current_playback_epoch=lambda: 4,
        http_post=post,
        timeout=12.0,
        queue_capacity=2,
    )
    down, up = await run_test(
        tts,
        frames_to_send=[
            *_envelope("server error"),
            *_envelope("● never speak", dispatch=24),
            *_envelope("later valid reply", dispatch=25),
        ],
    )
    assert calls == ["server error", "later valid reply"]
    assert sum(isinstance(frame, ErrorFrame) for frame in up) == 1
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 1
    assert tts.last_metrics is not None
    assert tts.last_metrics.audio_seconds == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_timeout_is_nonfatal_and_emits_no_partial_lifecycle():
    """An indeterminate request deadline reports an error without any audio frames."""

    async def post(
        _endpoint: str, _payload: dict[str, object], timeout: float
    ) -> RumikHTTPResponse:
        assert timeout == 0.25
        raise TimeoutError()

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post, timeout=0.25)
    down, up = await run_test(tts, frames_to_send=_envelope("timed request"))
    assert any(isinstance(frame, ErrorFrame) and not frame.fatal for frame in up)
    assert not any(
        isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame)) for frame in down
    )


@pytest.mark.asyncio
async def test_timeout_quarantines_until_late_http_finishes_and_operator_recovers():
    """An unresponsive remote call blocks new audio until its late result is observed and verified."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        if payload["input"] == "late":
            started.set()
            await release.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post, timeout=0.01)
    try:
        await _submit(tts, "late", epoch[0])
        await started.wait()
        await asyncio.sleep(0.03)
        assert tts._quarantined is True
        assert any(isinstance(frame, ErrorFrame) for frame, _ in captured)
        await _submit(tts, "blocked", epoch[0], dispatch=24)
        assert calls == ["late"]
        with pytest.raises(RuntimeError, match="request is active"):
            tts.recover_after_server_restart()

        release.set()
        await _yield_tasks()
        tts.recover_after_server_restart()
        await _submit(tts, "recovered", epoch[0], dispatch=25)
        await _yield_tasks()
        assert calls == ["late", "recovered"]
        assert len([frame for frame, _ in captured if isinstance(frame, TTSAudioRawFrame)]) == 1
    finally:
        await _cleanup(tts)


@pytest.mark.asyncio
async def test_bounded_queue_overload_rejects_excess_without_dispatching_it():
    """One active-or-pending local request cannot grow an unbounded speech backlog."""
    requests: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        requests.append(str(payload["input"]))
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts = RumikTTSService(current_playback_epoch=lambda: 4, http_post=post, queue_capacity=1)
    down, up = await run_test(
        tts,
        frames_to_send=[
            *_envelope("one"),
            *_envelope("two", dispatch=24),
            *_envelope("three", dispatch=25),
        ],
    )
    assert requests == ["one", "two"]
    assert sum(isinstance(frame, ErrorFrame) for frame in up) == 1
    assert len([frame for frame in down if isinstance(frame, TTSAudioRawFrame)]) == 2


@pytest.mark.asyncio
async def test_epoch_change_without_interruption_discards_late_result_then_later_current_reply_progresses():
    """The session callback alone blocks a late remote WAV before its start frame exists."""
    started = asyncio.Event()
    release = asyncio.Event()
    requests: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        requests.append(str(payload["input"]))
        if payload["input"] == "old":
            started.set()
            await release.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post)
    try:
        await _submit(tts, "old", epoch[0])
        await started.wait()
        epoch[0] += 1  # Controller response advancement did not send an interruption frame.
        release.set()
        await _yield_tasks()
        assert not any(
            isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame))
            for frame, _ in captured
        )

        await _submit(tts, "new", epoch[0], dispatch=24)
        await _yield_tasks()
        assert requests == ["old", "new"]
        assert len([frame for frame, _ in captured if isinstance(frame, TTSAudioRawFrame)]) == 1
    finally:
        await _cleanup(tts)
    assert tts._worker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [InterruptionFrame, CancelFrame, StopFrame])
async def test_interrupt_cancel_and_stop_preempt_late_audio_and_cleanup_worker(control):
    """Playback controls invalidate in-flight remote work and leave no local worker behind."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        started.set()
        await release.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post)
    try:
        await _submit(tts, "pending", epoch[0])
        await started.wait()
        await tts.process_frame(control(), FrameDirection.DOWNSTREAM)
        release.set()
        await _yield_tasks()
        assert not any(
            isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame))
            for frame, _ in captured
        )
    finally:
        await _cleanup(tts)
    assert tts._worker is None


@pytest.mark.asyncio
async def test_end_drains_authorized_audio_but_cancel_drops_queued_work_before_http():
    """End preserves accepted closing speech, while cancellation removes queued unstarted calls."""
    active_started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        if payload["input"] == "closing":
            active_started.set()
            await release.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post)
    try:
        await _submit(tts, "closing", epoch[0])
        await active_started.wait()
        await tts.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        release.set()
        await _yield_tasks()
        assert [
            type(frame)
            for frame, _ in captured
            if isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame, EndFrame))
        ] == [
            TTSStartedFrame,
            TTSAudioRawFrame,
            TTSStoppedFrame,
            EndFrame,
        ]
    finally:
        await _cleanup(tts)


@pytest.mark.asyncio
async def test_stop_preempts_end_while_base_stop_is_still_draining(monkeypatch):
    """Stop and cancellation preempt delayed graceful End teardown without audio leaks."""
    entered_base_stop = asyncio.Event()
    release_base_stop = asyncio.Event()

    async def delayed_base_stop(_service, _frame):
        entered_base_stop.set()
        await release_base_stop.wait()

    monkeypatch.setattr(TTSService, "stop", delayed_base_stop)

    async def post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(post)
    try:
        await _submit(tts, "closing", epoch[0])
        await _yield_tasks()
        await tts.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        await entered_base_stop.wait()
        await tts.process_frame(StopFrame(), FrameDirection.DOWNSTREAM)
        release_base_stop.set()
        await _yield_tasks()
        assert not any(isinstance(frame, EndFrame) for frame, _ in captured)
    finally:
        release_base_stop.set()
        await _cleanup(tts)

    # A cancellation after End was queued preempts that graceful drain and its EndFrame.
    active_started = asyncio.Event()
    release = asyncio.Event()

    async def ending_post(
        _endpoint: str, _payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        active_started.set()
        await release.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(ending_post)
    try:
        await _submit(tts, "interrupted closing", epoch[0])
        await active_started.wait()
        await tts.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        await tts.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)
        release.set()
        await _yield_tasks()
        assert not any(isinstance(frame, EndFrame) for frame, _ in captured)
        assert not any(
            isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame))
            for frame, _ in captured
        )
    finally:
        await _cleanup(tts)

    # A separate live worker proves queued work never reaches HTTP after a cancellation.
    gate = asyncio.Event()
    calls: list[str] = []

    async def blocked_post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        calls.append(str(payload["input"]))
        await gate.wait()
        return RumikHTTPResponse(200, _wav(), "audio/wav")

    tts, captured, epoch = await _direct_service(blocked_post, queue_capacity=2)
    try:
        await _submit(tts, "active", epoch[0])
        await _yield_tasks()
        await _submit(tts, "queued", epoch[0], dispatch=24)
        await tts.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)
        gate.set()
        await _yield_tasks()
        assert calls == ["active"]
        assert not any(
            isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame))
            for frame, _ in captured
        )
    finally:
        await _cleanup(tts)
