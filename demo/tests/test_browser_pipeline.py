"""Browser pipeline frame bridges and public event boundaries."""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from demo.interview.browser_contract import BrowserInterviewSetup, InterviewBrowserEvent
from demo.interview.config import Difficulty
from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.languages import INTERVIEW_LANGUAGES
from demo.interview.pipeline import (
    BrowserEventEmitter,
    _canonical_final_caption,
    _CaptionBridge,
    _config_for_setup,
    _emit_reply_rejection,
    _emit_status,
    _emit_transcript_recovery,
    _ErrorBridge,
    _eval_rtvi_observer_params,
    _PlaybackBridge,
    _ReplyBridge,
    default_live_config,
)
from demo.interview.rumik import RumikHTTPResponse
from demo.interview.session import InterviewSession
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner


@pytest.mark.asyncio
async def test_emitter_overwrites_untrusted_scope_and_sequences_only_public_fields():
    """A browser sink receives monotonic server-owned IDs and no provider settings."""
    sent = []

    async def send(payload):
        sent.append(payload)

    emitter = BrowserEventEmitter(session_id="server-session", connection_generation=4, send=send)
    await emitter.emit(
        InterviewBrowserEvent(
            session_id="client-spoof",
            connection_generation=99,
            seq=500,
            type="interview.reply",
            text="Approved text",
            dispatch_id=7,
        )
    )
    await emitter.emit(InterviewBrowserEvent(type="interview.interruption", playback_epoch=3))

    assert sent == [
        {
            "v": 1,
            "session_id": "server-session",
            "connection_generation": 4,
            "seq": 1,
            "type": "interview.reply",
            "text": "Approved text",
            "dispatch_id": 7,
        },
        {
            "v": 1,
            "session_id": "server-session",
            "connection_generation": 4,
            "seq": 2,
            "type": "interview.interruption",
            "playback_epoch": 3,
        },
    ]
    assert all("api_key" not in str(payload) and "endpoint" not in payload for payload in sent)


@pytest.mark.asyncio
async def test_caption_bridge_exposes_interim_text_but_never_raw_final_transcript():
    """Only identified provisional captions traverse the raw STT bridge."""
    events = []

    async def emit(event):
        events.append(event)

    bridge = _CaptionBridge(emit)
    bridge.push_frame = AsyncMock()
    interim = InterimTranscriptionFrame(
        "draft answer", "candidate", "now", result={"utterance_idx": 7}
    )
    unidentified = InterimTranscriptionFrame("unidentified", "candidate", "now")
    final = TranscriptionFrame("unbound final", "candidate", "now", finalized=True)
    await bridge.process_frame(interim, FrameDirection.DOWNSTREAM)
    await bridge.process_frame(unidentified, FrameDirection.DOWNSTREAM)
    await bridge.process_frame(final, FrameDirection.DOWNSTREAM)

    assert [(event.type, event.text, event.final, event.segment_id) for event in events] == [
        ("interview.caption", "draft answer", False, "7")
    ]
    assert [call.args[0] for call in bridge.push_frame.call_args_list] == [
        interim,
        unidentified,
        final,
    ]


@pytest.mark.asyncio
async def test_output_bridges_report_real_playback_and_safe_text_continuation():
    """Playback frame status and output errors are public without provider diagnostics."""
    events = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=2), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(), object(), object())),
        playback_epoch=4,
        _thinking_at=None,
    )
    playback = _PlaybackBridge(session, emit)
    tts = object()
    errors = _ErrorBridge(emit, output_processors=(tts,))
    playback.push_frame = AsyncMock()
    errors.push_frame = AsyncMock()
    playback.queue_output(4)
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    session._thinking_at = 1.0
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await errors.process_frame(
        ErrorFrame(error="provider endpoint token=secret", processor=tts), FrameDirection.UPSTREAM
    )

    assert [(event.type, event.status) for event in events[:2]] == [
        ("interview.status", "Speaking"),
        ("interview.status", "Giving you time"),
    ]
    fallback = events[-1]
    assert fallback.type == "interview.error"
    assert fallback.recoverable is True
    assert fallback.message == (
        "Speech output was unavailable. Read the question here or repeat it. Your accepted answers are saved."
    )
    assert "secret" not in fallback.message


@pytest.mark.asyncio
async def test_status_uses_one_based_question_progress_and_playback_activity_callback():
    """Public progress starts at one and output activity can suppress stale policy status."""
    events = []
    activity = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=0), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(),)),
        playback_epoch=2,
        _thinking_at=None,
    )
    playback = _PlaybackBridge(session, emit, on_output_activity=activity.append)
    playback.push_frame = AsyncMock()

    await _emit_status(session, emit, "Listening")
    playback.queue_output(2)
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert events[0].question_index == 1
    assert activity == [True, False]


@pytest.mark.asyncio
async def test_status_carries_the_core_interaction_snapshot_without_changing_v1_status():
    """The browser can render controls from state revisions while legacy clients retain status text."""
    events = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=0), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(),)),
        playback_epoch=2,
        _thinking_at=None,
        interaction_snapshot=lambda: {
            "prompt_revision": 5,
            "state_revision": 8,
            "question_text": "Tell me about teamwork.",
            "interaction_state": "listening",
            "permitted_actions": ["repeat", "explain", "end"],
            "recovery_token": None,
        },
    )

    await _emit_status(session, emit, "Listening")

    event = events[0]
    assert event.status == "Listening"
    assert event.prompt_revision == 5
    assert event.state_revision == 8
    assert event.question_text == "Tell me about teamwork."
    assert event.permitted_actions == ["repeat", "explain", "end"]


@pytest.mark.asyncio
async def test_playback_bridge_only_starts_idle_timing_after_current_output_stops():
    """A delayed stop from an invalidated response cannot restart an old candidate idle timer."""
    events = []
    calls = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=0), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(),)),
        playback_epoch=4,
        _thinking_at=None,
        playback_started=lambda epoch: calls.append(("started", epoch)),
        playback_stopped=lambda epoch: calls.append(("stopped", epoch)),
    )
    bridge = _PlaybackBridge(session, emit)
    bridge.push_frame = AsyncMock()
    bridge.queue_output(4)
    await bridge.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    session.playback_epoch = 5
    await bridge.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert calls == [("started", 4)]
    assert [(event.type, event.status) for event in events] == [("interview.status", "Speaking")]


@pytest.mark.asyncio
async def test_guard_rejection_reports_a_sanitized_recoverable_browser_error():
    """A withheld coaching payload leaves the current question intact for another answer."""
    events = []

    async def emit(event):
        events.append(event)

    await _emit_reply_rejection(emit)

    assert events == [
        InterviewBrowserEvent(
            type="interview.error",
            message="I couldn't prepare a safe follow-up. Please continue or repeat that part.",
            recoverable=True,
        )
    ]


@pytest.mark.asyncio
async def test_transcript_recovery_is_actionable_without_claiming_connection_loss():
    """A missing final is a repairable transcript gap, not a WebRTC reconnect status."""
    events = []

    async def emit(event):
        events.append(event)

    await _emit_transcript_recovery(emit)

    assert events[0].type == "interview.error"
    assert events[0].recoverable is True
    assert "missed part" in events[0].message
    assert "connection" not in events[0].message.lower()


@pytest.mark.asyncio
async def test_output_error_releases_the_current_playback_wait_for_text_fallback():
    """A TTS failure must not leave the candidate idle timer paused behind absent audio."""
    events = []
    failed = []
    activity = []

    async def emit(event):
        events.append(event)

    tts = object()
    session = SimpleNamespace(
        playback_epoch=6,
        playback_failed=lambda epoch: failed.append(epoch),
    )
    playback = _PlaybackBridge(session, emit, on_output_activity=activity.append)
    playback.queue_output(6, 41)
    bridge = _ErrorBridge(
        emit,
        output_processors=(tts,),
        on_output_failure=playback.output_failed,
    )
    bridge.push_frame = AsyncMock()
    error = ErrorFrame(error="provider detail", processor=tts)
    error.metadata.update(interview_playback_epoch=6, interview_dispatch_id=41)
    await bridge.process_frame(error, FrameDirection.UPSTREAM)

    assert failed == [6]
    assert activity == [False]
    assert events[0].recoverable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("epoch,dispatch", [(5, 41), (6, 40), (None, None), (True, 41)])
async def test_stale_or_unowned_output_errors_cannot_release_current_playback(epoch, dispatch):
    """Only the current response can fail its queued output and release its wait."""
    events, failures, activity = [], [], []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(playback_epoch=6, playback_failed=failures.append)
    playback = _PlaybackBridge(session, emit, on_output_activity=activity.append)
    playback.queue_output(6, 41)
    tts = object()
    bridge = _ErrorBridge(emit, output_processors=(tts,), on_output_failure=playback.output_failed)
    bridge.push_frame = AsyncMock()
    error = ErrorFrame(error="private provider details", processor=tts)
    error.metadata.update(interview_playback_epoch=epoch, interview_dispatch_id=dispatch)
    await bridge.process_frame(error, FrameDirection.UPSTREAM)

    assert failures == []
    assert activity == []
    assert list(playback._queued_epochs) == [6]
    if epoch in (5, 6) and not isinstance(epoch, bool):
        assert events == []
    assert all("private" not in event.message for event in events)
    bridge.push_frame.assert_awaited_once_with(error, FrameDirection.UPSTREAM)


@pytest.mark.asyncio
async def test_reply_epoch_owns_playback_and_invalidation_retires_stale_transport_events():
    """A late old output event cannot revive speaking state after a newer interruption."""
    events = []
    activity = []
    calls = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=0), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(),)),
        playback_epoch=4,
        _thinking_at=None,
        playback_started=lambda epoch: calls.append(("started", epoch)),
        playback_stopped=lambda epoch: calls.append(("stopped", epoch)),
    )
    playback = _PlaybackBridge(session, emit, on_output_activity=activity.append)
    replies = _ReplyBridge(emit, on_output_queued=playback.queue_output)
    playback.push_frame = AsyncMock()
    replies.push_frame = AsyncMock()
    text = LLMTextFrame("approved")
    text.metadata["interview_playback_epoch"] = 4
    await replies.process_frame(text, FrameDirection.DOWNSTREAM)

    session.playback_epoch = 5
    playback.invalidate(5)
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert calls == []
    assert activity == [False, False]
    assert not [event for event in events if event.type == "interview.status"]


@pytest.mark.asyncio
async def test_guard_released_output_metadata_drives_the_matching_playback_lifecycle():
    """Local and provider replies use their guarded epoch instead of the session's later epoch."""
    events = []
    calls = []

    async def emit(event):
        events.append(event)

    session = SimpleNamespace(
        controller=SimpleNamespace(
            current_question=SimpleNamespace(index=0), phase=SimpleNamespace(value="question")
        ),
        config=SimpleNamespace(question_rubric=(object(),)),
        playback_epoch=7,
        _thinking_at=None,
        playback_started=lambda epoch: calls.append(("started", epoch)),
        playback_stopped=lambda epoch: calls.append(("stopped", epoch)),
    )
    playback = _PlaybackBridge(session, emit)
    replies = _ReplyBridge(emit, on_output_queued=playback.queue_output)
    playback.push_frame = AsyncMock()
    replies.push_frame = AsyncMock()
    text = LLMTextFrame("approved local output")
    text.metadata["interview_playback_epoch"] = 7
    await replies.process_frame(text, FrameDirection.DOWNSTREAM)
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert calls == [("started", 7), ("stopped", 7)]
    assert [(event.type, event.status) for event in events if event.type == "interview.status"] == [
        ("interview.status", "Speaking"),
        ("interview.status", "Listening"),
    ]
    reply = next(event for event in events if event.type == "interview.reply")
    assert reply.playback_epoch == 7


@pytest.mark.asyncio
async def test_browser_control_reply_reaches_tts_with_its_current_playback_epoch():
    """A delayed queued interruption clears old output before local control speech enters TTS."""
    first_audio = asyncio.Event()
    current_audio = asyncio.Event()
    delayed_interruption = asyncio.Event()
    action_issued = asyncio.Event()
    audio_epochs: list[int] = []
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x01\x00" * 240)

    async def post(
        _endpoint: str, payload: dict[str, object], _timeout: float
    ) -> RumikHTTPResponse:
        return RumikHTTPResponse(200, wav.getvalue(), "audio/wav")

    async def emit(event):
        del event

    base_config = default_live_config()
    config = base_config.model_copy(
        update={
            "question_rubric": (
                base_config.question_rubric[0].model_copy(update={"competency": "Teamwork"}),
            )
        }
    )
    session = InterviewSession(config=config, session_id="browser-control")
    playback = _PlaybackBridge(session, emit)
    replies = _ReplyBridge(emit, on_output_queued=playback.queue_output)
    tts = session.create_rumik_tts(http_post=post)

    class DelayInterruption(FrameProcessor):
        """Force the replacement-output race at the queued pipeline boundary."""

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if (
                isinstance(frame, InterruptionFrame)
                and "interview_internal_output_epoch" in frame.metadata
            ):
                await asyncio.sleep(0.05)
                delayed_interruption.set()
            await self.push_frame(frame, direction)

    class CaptureAudio(FrameProcessor):
        """Record generated PCM after the acknowledgement point."""

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSAudioRawFrame):
                epoch = frame.metadata.get("interview_playback_epoch")
                if isinstance(epoch, int):
                    audio_epochs.append(epoch)
                    first_audio.set()
                    if action_issued.is_set() and epoch == session.playback_epoch:
                        current_audio.set()
            await self.push_frame(frame, direction)

    worker = PipelineWorker(
        Pipeline(
            [session, DelayInterruption(), session.guard, replies, tts, playback, CaptureAudio()]
        ),
        cancel_on_idle_timeout=False,
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    runner_task = asyncio.create_task(runner.run(), name="browser-control-race")
    try:
        await worker.queue_frame(StartFrame())
        await asyncio.wait_for(first_audio.wait(), timeout=2)
        session.apply_interaction_action("explain")
        action_issued.set()
        await asyncio.wait_for(delayed_interruption.wait(), timeout=2)
        await asyncio.wait_for(current_audio.wait(), timeout=2)

        assert audio_epochs[-1] == session.playback_epoch
    finally:
        await worker.cancel()
        await runner_task


@pytest.mark.asyncio
async def test_non_output_errors_do_not_release_playback_or_claim_speech_failed():
    """STT and model errors must not be presented as missing browser audio output."""
    events = []
    releases = []

    async def emit(event):
        events.append(event)

    tts = object()
    bridge = _ErrorBridge(
        emit,
        output_processors=(tts,),
        on_output_failure=lambda: releases.append("tts"),
    )
    bridge.push_frame = AsyncMock()
    await bridge.process_frame(
        ErrorFrame(error="STT failed", processor=object()), FrameDirection.UPSTREAM
    )

    assert releases == []
    assert events == []


def test_final_caption_uses_only_the_accepted_ledger_value():
    """A stale or conflicting final cannot replace the browser's canonical caption."""
    canonical = SegmentFinal(SegmentId(4, 9), 12, SegmentFinalOutcome.TEXT, "accepted transcript")
    session = SimpleNamespace(
        ledger=SimpleNamespace(snapshot=SimpleNamespace(segments=(canonical,)))
    )

    event = _canonical_final_caption(session, canonical)
    conflicting = SegmentFinal(
        SegmentId(4, 9), 12, SegmentFinalOutcome.TEXT, "conflicting transcript"
    )

    assert event is not None
    assert event.text == "accepted transcript"
    assert event.segment_id == "9"
    assert _canonical_final_caption(session, conflicting) is None


def test_only_eval_enables_restricted_rtvi_observation():
    """Browser transports expose only application events, never raw model/context frames."""
    assert _eval_rtvi_observer_params(object()) is None  # type: ignore[arg-type]

    params = _eval_rtvi_observer_params(
        EvalTransport(host="127.0.0.1", port=0, params=EvalTransportParams())
    )

    assert params is not None
    assert not params.bot_output_enabled
    assert not params.bot_llm_enabled
    assert not params.user_llm_enabled
    assert not params.user_transcription_enabled


def test_setup_changes_only_public_interview_policy_and_keeps_backend_provider_selection():
    """Language and rubric choices do not let the browser replace provider policy or credentials."""
    base = default_live_config()
    configured = _config_for_setup(
        base,
        BrowserInterviewSetup(
            role="Platform engineer",
            difficulty=Difficulty.SENIOR,
            duration_minutes=20,
            language="english",
            rubric=base.question_rubric,
        ),
    )

    assert configured.role.title == "Platform engineer"
    assert configured.difficulty is Difficulty.SENIOR
    assert configured.duration_minutes == 20
    assert configured.providers.sarvam.model == base.providers.sarvam.model
    assert configured.providers.sarvam.language_code == "auto"
    assert configured.providers.sarvam.mode == "transcribe"
    assert configured.providers.tts.language_code == "en-IN"
    assert configured.providers.tts.pace == 0.85
    assert configured.providers.gemini == base.providers.gemini


def test_tanglish_setup_uses_codemixed_recognition_and_tamil_speech():
    """Tanglish defaults recognize mixed speech and synthesize with Tamil normalization."""
    base = default_live_config()
    configured = _config_for_setup(base, BrowserInterviewSetup(rubric=base.question_rubric))
    assert configured.providers.sarvam.language_code == "auto"
    assert configured.providers.sarvam.mode == "codemix"
    assert configured.providers.tts.language_code == "ta-IN"
    type(configured.providers.tts).model_validate(configured.providers.tts.model_dump())


@pytest.mark.parametrize("language,tts_language", INTERVIEW_LANGUAGES.items())
def test_every_interview_language_selects_its_tts_code_and_requested_stt(language, tts_language):
    """Setup language style and recognition language remain independently selectable."""
    base = default_live_config()
    configured = _config_for_setup(
        base,
        BrowserInterviewSetup(
            language=language,
            stt_language="gu-IN",
            speech_pace=1.2,
            rubric=base.question_rubric,
        ),
    )
    assert configured.providers.sarvam.language_code == "gu-IN"
    assert configured.providers.sarvam.mode == (
        "codemix" if language in {"tanglish", "hinglish"} else "transcribe"
    )
    assert configured.providers.tts.language_code == tts_language
    assert configured.providers.tts.pace == 1.2
