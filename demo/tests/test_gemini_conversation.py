"""Native Gemini Live browser-session contract coverage."""

from __future__ import annotations

import asyncio

import pytest

from demo.interview.browser_contract import BrowserInterviewSetup
from demo.interview.gemini_conversation import (
    GeminiConversationPipelineFactory,
    NativeConversationSession,
    _native_config_for_setup,
    _native_instruction,
    _NativeInputActivityBridge,
    _NativeOutputGate,
    _NativePlaybackBridge,
    _NativeTranscriptBridge,
)
from demo.interview.pipeline import default_live_config
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    InputTextRawFrame,
    InterruptionFrame,
    LLMRunFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.tests.utils import run_test


class _Service:
    """Minimal managed-task seam used by synchronous browser controls."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[object]] = []

    def create_task(self, coroutine, *, name: str):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task


class _Worker:
    """Capture queued native control input without a provider connection."""

    def __init__(self) -> None:
        self.frames = []

    async def queue_frame(self, frame) -> None:
        self.frames.append(frame)

    async def queue_frames(self, frames) -> None:
        self.frames.extend(frames)


def _cancel_deadlines(service: _Service) -> None:
    for task in service.tasks:
        if task.get_name() == "gemini-live-deadline":
            task.cancel()


@pytest.mark.asyncio
async def test_native_controls_seed_context_interrupt_before_prompt_and_end():
    """Controls change snapshots immediately and queue ordered native frames."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    service, worker = _Service(), _Worker()
    session.bind(service, worker)  # type: ignore[arg-type]
    session.client_ready()
    await service.tasks[0]

    assert isinstance(worker.frames[0], LLMRunFrame)
    assert session.interaction_snapshot()["question_text"] is None

    session.apply_interaction_action("thinking")
    assert session.interaction_snapshot()["interaction_state"] == "thinking"
    await service.tasks[-1]
    assert isinstance(worker.frames[-1], InterruptionFrame)
    assert not any(isinstance(frame, InputTextRawFrame) for frame in worker.frames[-1:])

    session.apply_interaction_action("skip")
    assert session.controller.current_question is None
    assert session.suppress_output is False
    await service.tasks[-1]
    assert isinstance(worker.frames[-2], InterruptionFrame)
    assert isinstance(worker.frames[-1], InputTextRawFrame)
    assert "different topic" in worker.frames[-1].text

    session.apply_interaction_action("end")
    assert session.interaction_snapshot()["permitted_actions"] == []
    await service.tasks[-1]
    assert isinstance(worker.frames[-1], EndFrame)
    assert any(event.type == "interview.ended" for event in events)
    _cancel_deadlines(service)


@pytest.mark.asyncio
async def test_native_audio_bridges_emit_full_reply_caption_and_audible_status():
    """Browser events reflect finalized input, complete output, and real playback frames."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    service, worker = _Service(), _Worker()
    session.bind(service, worker)  # type: ignore[arg-type]
    session.apply_interaction_action("thinking")

    input_bridge = _NativeInputActivityBridge(session)
    await input_bridge.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert session.interaction_snapshot()["interaction_state"] == "listening"
    await session.publish_candidate_caption("I handled a customer issue.", "native-input-1")

    transcript_bridge = _NativeTranscriptBridge(emit, session)
    await transcript_bridge.process_frame(
        TTSTextFrame(text="First part. ", aggregated_by="sentence"), FrameDirection.DOWNSTREAM
    )
    await transcript_bridge.process_frame(
        TTSTextFrame(text="Second part.", aggregated_by="sentence"), FrameDirection.DOWNSTREAM
    )
    await transcript_bridge.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)

    playback_bridge = _NativePlaybackBridge(session)
    await playback_bridge.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await playback_bridge.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.gather(*(task for task in service.tasks if task.get_name() == "gemini-status"))

    captions = [event for event in events if event.type == "interview.caption"]
    replies = [event for event in events if event.type == "interview.reply"]
    assert captions[0].segment_id == "native-input-1"
    assert replies[0].text == "First part. Second part."
    assert replies[0].dispatch_id == 1
    assert any(event.status == "Speaking" for event in events if event.type == "interview.status")
    assert any(event.status == "Listening" for event in events if event.type == "interview.status")
    assert all(
        isinstance(event.playback_epoch, int)
        for event in events
        if event.type == "interview.status"
    )


@pytest.mark.asyncio
async def test_thinking_gate_suppresses_late_native_audio_and_emits_interruption():
    """A thinking pause prevents late provider audio from reaching browser playback."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    service, worker = _Service(), _Worker()
    session.bind(service, worker)  # type: ignore[arg-type]
    session.apply_interaction_action("thinking")
    gate = _NativeOutputGate(session)

    down, _ = await run_test(
        gate,
        frames_to_send=[
            InterruptionFrame(),
            TTSAudioRawFrame(audio=b"\x01\x00" * 480, sample_rate=24_000, num_channels=1),
        ],
    )
    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in down)

    assert session.suppress_output is True
    assert any(event.type == "interview.interruption" for event in events)
    session.candidate_started_speaking()
    assert session.suppress_output is False
    await asyncio.gather(*(task for task in service.tasks if task.get_name() == "gemini-status"))


def test_native_config_uses_public_choices_and_live_settings_omit_language_code():
    """Native setup accepts Gemini language choices without Sarvam provider remapping."""
    base = default_live_config()
    setup = BrowserInterviewSetup(
        role="Retail associate",
        language="spanish",
        stt_language="es",
        speech_pace=0.7,
    )
    config = _native_config_for_setup(base, setup)
    instruction = _native_instruction(setup, config)
    settings = GeminiLiveLLMService.Settings(model="gemini-3.8-live", language=None)

    assert config.role.title == "Retail associate"
    assert config.providers is base.providers
    assert "Spanish" in instruction
    assert "very slowly" in instruction
    assert "never wait for a Skip command" in instruction
    assert settings.language is None


def test_native_factory_requires_only_the_gemini_server_credential():
    """Native factory construction does not require Sarvam provider configuration."""
    factory = GeminiConversationPipelineFactory(
        base_config=default_live_config(), google_api_key="test-key"
    )
    assert factory is not None
    with pytest.raises(ValueError):
        GeminiConversationPipelineFactory(base_config=default_live_config(), google_api_key=" ")


@pytest.mark.asyncio
async def test_native_provider_failure_stops_output_and_limits_controls():
    """Failed native sessions expose a restart path without provider diagnostics."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    await session.provider_failed()
    assert session.suppress_output
    assert session.interaction_snapshot()["permitted_actions"] == ["end"]
    assert [event.type for event in events] == ["interview.error", "interview.status"]
    assert events[0].recoverable is False


@pytest.mark.asyncio
async def test_casual_conversation_controls_follow_the_spoken_question():
    """Displayed wording and repeat follow the conversation, not a rubric entry."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    service, worker = _Service(), _Worker()
    session.bind(service, worker)
    question = "You mentioned helping at the shop. What were you working on?"
    await session.record_interviewer_turn(question)
    assert session.interaction_snapshot()["question_text"] == question
    assert events[-1].question_index is None
    revision = session.interaction_snapshot()["prompt_revision"]
    session.apply_interaction_action("repeat")
    await service.tasks[-1]
    assert question in worker.frames[-1].text
    await session.record_interviewer_turn("How do you unwind after work?")
    assert session.interaction_snapshot()["prompt_revision"] > revision
    assert "shop" not in session.interaction_snapshot()["question_text"]


def test_simple_tamil_instruction_avoids_english_mixing_and_handles_interruptions():
    """Use everyday Tamil and follow the speaker after an interruption."""
    setup = BrowserInterviewSetup(role="Gentle conversation", language="tamil")
    config = _native_config_for_setup(default_live_config(), setup)
    instruction = _native_instruction(setup, config)
    assert "simple, everyday spoken Tamil" in instruction
    assert "Do not mix in English words" in instruction
    assert "two or three everyday" not in instruction
    assert "If the person interrupts, stop and listen" in instruction


@pytest.mark.asyncio
async def test_barge_in_drops_old_audio_until_user_finishes_and_new_response_starts():
    """Discard late audio across the speech window and accept a fresh response."""
    from pipecat.frames.frames import (
        TTSStartedFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.tests.utils import SleepFrame

    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit)
    gate = _NativeOutputGate(session)
    stale = TTSAudioRawFrame(audio=b"\x01\x00" * 480, sample_rate=24000, num_channels=1)
    fresh = TTSAudioRawFrame(audio=b"\x02\x00" * 480, sample_rate=24000, num_channels=1)
    down, _ = await run_test(
        gate,
        frames_to_send=[
            InterruptionFrame(),
            UserStartedSpeakingFrame(),
            TTSStartedFrame(),
            stale,
            SleepFrame(sleep=0.05),
            UserStoppedSpeakingFrame(),
            stale,
            SleepFrame(sleep=0.05),
            TTSStartedFrame(),
            fresh,
        ],
    )
    assert [frame.audio for frame in down if isinstance(frame, TTSAudioRawFrame)] == [fresh.audio]
    assert events[0].playback_epoch == 1
    assert events[0].interaction_state == "listening"


@pytest.mark.asyncio
async def test_closing_waits_for_spoken_note_playback_before_ending():
    """Keep audio connected until the closing transcript and playback finish."""
    events = []

    async def emit(event):
        events.append(event)

    session = NativeConversationSession(default_live_config(), emit, language="tamil")
    service, worker = _Service(), _Worker()
    session.bind(service, worker)
    session.start_closing("behaviour", send_prompt=False)
    assert session.interaction_snapshot()["permitted_actions"] == ["end"]
    session.mark_speaking()
    await session.record_interviewer_turn(session.closing_note())
    assert not session._closed
    session.mark_listening()
    assert session._closed
    await asyncio.gather(*(t for t in service.tasks if t.get_name() != "gemini-closing-timeout"))
    ended = next(e for e in events if e.type == "interview.ended")
    assert "தொடர முடியாது" in ended.message
    assert isinstance(worker.frames[-1], EndFrame)
    for task in service.tasks:
        task.cancel()
    await asyncio.gather(*service.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_time_limit_closing_preserves_deadline_and_repeat_does_not_count():
    """A time-limit note leaves the deadline in charge and rephrases consume no slot."""

    async def emit(event):
        pass

    session = NativeConversationSession(default_live_config(), emit)
    service, worker = _Service(), _Worker()
    session.bind(service, worker)
    await session.record_interviewer_turn("What do you build?")
    session.apply_interaction_action("repeat")
    await session.record_interviewer_turn("What do you build?")
    assert session._question_turns == 1
    session.start_closing("time_limit", send_prompt=False)
    session.mark_speaking()
    await session.record_interviewer_turn(session.closing_note())
    session.mark_listening()
    assert not session._closed
    assert session._question_turns == 1
    for task in service.tasks:
        task.cancel()
    await asyncio.gather(*service.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_completion_tool_requires_question_round_and_returns_closing_note():
    """Reject premature completion and allow the finished question round to close."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    session = NativeConversationSession(default_live_config(), AsyncMock(), language="tamil")
    service, worker = _Service(), _Worker()
    session.bind(service, worker)
    callback = AsyncMock()
    params = SimpleNamespace(arguments={"reason": "completed"}, result_callback=callback)
    await session.finish_interview_tool(params)
    assert callback.call_args.args[0]["continue"] is True
    assert session._closing_reason is None
    for index in range(12):
        await session.record_interviewer_turn(f"Question {index + 1}?")
    await session.finish_interview_tool(params)
    assert callback.call_args.args[0]["ending"] is True
    assert "நன்றி" in callback.call_args.args[0]["closing_note"]
    assert session._closing_reason == "completed"
    assert not session._closed
    for task in service.tasks:
        task.cancel()
    await asyncio.gather(*service.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_question_guide_reserves_life_choices_and_problem_solving():
    """The server assigns bounded themes without relying on model question counting."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    session = NativeConversationSession(default_live_config(), AsyncMock())
    callback = AsyncMock()
    params = SimpleNamespace(result_callback=callback)
    for completed, expected in [
        (0, "occupation"),
        (3, "profession"),
        (5, "life-choice"),
        (8, "problem"),
    ]:
        session._question_turns = completed
        await session.next_question_tool(params)
        result = callback.call_args.args[0]
        assert result["question_number"] == completed + 1
        assert expected in result["focus"]
    session._question_turns = 12
    await session.next_question_tool(params)
    assert callback.call_args.args[0]["complete"] is True
