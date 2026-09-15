"""Session ownership and transport interruption at the guarded speech boundary."""

import asyncio
import io
import wave

import pytest

from demo.interview.config import InterviewConfig
from demo.interview.rumik import RumikHTTPResponse
from demo.interview.session import InterviewSession
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    LLMTextFrame,
    StartFrame,
    StopFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


def config():
    """Build the smallest practice session with a question to advance into."""
    return InterviewConfig.model_validate(
        {
            "role": {"title": "Engineer", "focus": "Systems"},
            "difficulty": "mid",
            "duration_minutes": 5,
            "question_rubric": [{"competency": "Debugging", "guidance": "Describe an incident."}],
        }
    )


async def drain():
    """Let explicitly signalled managed tasks consume ready work."""
    for _ in range(60):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [None, "speech", "connection", "cancel", "stop"])
async def test_released_prompt_survives_candidate_advance_but_not_speech_or_reconnect(invalidate):
    """Captured speech survives normal commits but fails closed after ownership changes."""
    session = InterviewSession(config=config(), session_id="playback")
    started, finish = asyncio.Event(), asyncio.Event()
    wav = io.BytesIO()
    with wave.open(wav, "wb") as writer:
        writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x01\x00" * 240)

    async def post(*_args):
        started.set()
        await finish.wait()
        return RumikHTTPResponse(200, wav.getvalue(), "audio/wav")

    tts = session.create_rumik_tts(http_post=post)
    assert session.create_rumik_tts() is tts
    frames, approved = [], []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        frames.append(frame)

    async def guarded(frame, direction=FrameDirection.DOWNSTREAM):
        approved.append(frame)
        await tts.process_frame(frame, direction)

    tts.push_frame = capture
    session.push_frame = capture
    session.guard.push_frame = guarded
    manager = TaskManager()
    await tts.setup(frame_processor_setup(manager))
    await session.setup(frame_processor_setup(manager))
    try:
        await tts.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        initial_token = session.ledger.response_token
        await session.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.wait_for(started.wait(), 2)
        await drain()
        texts = [f for f in approved if isinstance(f, LLMTextFrame)]
        assert len(texts) >= 2  # Introduction and first question released before HTTP completes.
        assert session.ledger.response_token != initial_token
        assert all(f.metadata["interview_playback_epoch"] == session.playback_epoch for f in texts)
        captured_epoch = session.playback_epoch
        if invalidate == "speech":
            session.speech_started()
        elif invalidate == "connection":
            session.ledger.replace_connection(
                connection_generation=2,
                candidate_turn_id=50,
                question_id=session.controller.current_question.question_id,
            )
        elif invalidate in {"cancel", "stop"}:
            frame = CancelFrame() if invalidate == "cancel" else StopFrame()
            await session.process_frame(frame, FrameDirection.DOWNSTREAM)
        if invalidate is not None:
            assert session.playback_epoch > captured_epoch
        # Graceful coordinator close must preserve already authorized closing/queued speech.
        await session.close()
        assert session.playback_epoch == (
            captured_epoch if invalidate is None else captured_epoch + 1
        )
        finish.set()
        await drain()
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        assert bool(audio) is (invalidate is None)
        assert all(f.metadata["interview_playback_epoch"] == captured_epoch for f in audio)
    finally:
        finish.set()
        await session.cleanup()
        await tts.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["repeat", "deadline"])
async def test_replacement_prompt_broadcasts_interruption_before_guarded_text(trigger):
    """Replacement speech follows a transport queue-clearing interruption."""
    now = [0.0]
    session = InterviewSession(config=config(), session_id="controls", clock=lambda: now[0])
    events = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        events.append((frame, direction))

    session.push_frame = capture
    session.guard.push_frame = capture
    await session.setup(frame_processor_setup(TaskManager()))
    try:
        await session.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await drain()
        events.clear()
        if trigger == "repeat":
            session.handle_control("repeat")
        else:
            now[0] = session.controller.deadline + 1
            session.changed()
        await drain()
        downstream = [f for f, d in events if d is FrameDirection.DOWNSTREAM]
        first_text = next(i for i, f in enumerate(downstream) if isinstance(f, LLMTextFrame))
        assert any(isinstance(f, InterruptionFrame) for f in downstream[:first_text])
        if trigger == "deadline":
            assert any(isinstance(f, EndFrame) for f in downstream[first_text:])
    finally:
        await session.cleanup()
    assert session._output_interruption_task is None
