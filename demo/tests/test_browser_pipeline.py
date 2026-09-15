"""Browser pipeline frame bridges and public event boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from demo.interview.browser_contract import BrowserInterviewSetup, InterviewBrowserEvent
from demo.interview.config import Difficulty
from demo.interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.pipeline import (
    BrowserEventEmitter,
    _canonical_final_caption,
    _CaptionBridge,
    _config_for_setup,
    _emit_reply_rejection,
    _emit_status,
    _ErrorBridge,
    _eval_rtvi_observer_params,
    _PlaybackBridge,
    default_live_config,
)
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection


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
    errors = _ErrorBridge(emit)
    playback.push_frame = AsyncMock()
    errors.push_frame = AsyncMock()
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    session._thinking_at = 1.0
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await errors.process_frame(
        ErrorFrame(error="provider endpoint token=secret"), FrameDirection.UPSTREAM
    )

    assert [(event.type, event.status) for event in events[:2]] == [
        ("interview.status", "Speaking"),
        ("interview.status", "Giving you time"),
    ]
    fallback = events[-1]
    assert fallback.type == "interview.error"
    assert fallback.recoverable is True
    assert fallback.message == (
        "Speech output was unavailable. Continue speaking and the interview will continue in text."
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
    await playback.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert events[0].question_index == 1
    assert activity == [True, False]


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
    assert configured.providers.sarvam.language_code == "en-IN"
    assert configured.providers.sarvam.mode == "transcribe"
    assert configured.providers.tts.language_code == "en-IN"
    assert configured.providers.gemini == base.providers.gemini


def test_tanglish_setup_uses_codemixed_recognition_and_tamil_speech():
    """Tanglish defaults recognize mixed speech and synthesize with Tamil normalization."""
    base = default_live_config()
    configured = _config_for_setup(base, BrowserInterviewSetup(rubric=base.question_rubric))
    assert configured.providers.sarvam.language_code == "auto"
    assert configured.providers.sarvam.mode == "codemix"
    assert configured.providers.tts.language_code == "ta-IN"
    type(configured.providers.tts).model_validate(configured.providers.tts.model_dump())
