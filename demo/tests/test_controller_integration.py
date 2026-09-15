"""Composition coverage for the transcript gate, reply guard, and interview controller."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from demo.interview.config import InterviewConfig
from demo.interview.contracts import ReplyKind, SegmentFinal, SegmentFinalOutcome, SegmentId
from demo.interview.controller import InterviewPhase
from demo.interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from demo.interview.session import InterviewSession
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


@dataclass
class Clock:
    """A monotonic test clock that advances only when the test chooses."""

    now: float = 0.0

    def __call__(self) -> float:
        """Return the test-selected time."""
        return self.now


async def _drain() -> None:
    """Let managed processor tasks consume already-signalled work."""
    for _ in range(30):
        await asyncio.sleep(0)


def _config() -> InterviewConfig:
    """Create the smallest deterministic two-rubric interview."""
    return InterviewConfig.model_validate(
        {
            "role": {"title": "Backend engineer", "focus": "distributed systems"},
            "difficulty": "mid",
            "duration_minutes": 5,
            "question_rubric": [
                {"competency": "Debugging", "guidance": "Describe the incident."},
                {"competency": "Design", "guidance": "Explain the tradeoff."},
            ],
        }
    )


class ScriptedProvider:
    """Provider boundary that emits guarded response frames from a test script."""

    def __init__(self, session: InterviewSession, replies: list[str | None]) -> None:
        """Store a session boundary and responses to emit in admission order."""
        self.session = session
        self.replies = replies
        self.calls: list[tuple[LLMContextFrame, FrameDirection]] = []
        self.held: list[tuple[LLMContextFrame, FrameDirection]] = []

    async def __call__(self, frame: LLMContextFrame, direction: FrameDirection) -> None:
        """Capture one admitted call and either hold or emit its scripted result."""
        self.calls.append((frame, direction))
        reply = self.replies.pop(0)
        if reply is None:
            self.held.append((frame, direction))
            return
        await self.emit(frame, direction, reply)

    async def emit(self, frame: LLMContextFrame, direction: FrameDirection, response: str) -> None:
        """Emit one provider response through the actual reply guard."""
        for outgoing in (
            LLMFullResponseStartFrame(),
            LLMTextFrame(response),
            LLMFullResponseEndFrame(),
        ):
            outgoing.metadata.update(frame.metadata)
            await self.session.guard.process_frame(outgoing, direction)


async def _session(
    replies: tuple[str | None, ...] = (), *, drain: bool = True, language: str = "english"
):
    """Start a real session through its managed setup and capture released frames."""
    clock = Clock()
    session = InterviewSession(
        config=_config(), session_id="integration", clock=clock, language=language
    )
    provider = ScriptedProvider(session, list(replies))
    captured: list[tuple[Frame, FrameDirection]] = []

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        captured.append((frame, direction))

    session.guard.push_frame = capture
    session.push_frame = capture
    session.set_provider(provider)
    await session.setup(frame_processor_setup(TaskManager()))
    await session.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    if drain:
        await _drain()
    return clock, session, provider, captured


async def _final_answer(session: InterviewSession, clock: Clock, text: str) -> int:
    """Bind and finalize one substantive candidate segment for the active question."""
    snapshot = session.ledger.snapshot
    assert snapshot.candidate_turn_id is not None
    candidate_turn_id = snapshot.candidate_turn_id
    boundary_id = candidate_turn_id
    segment = SegmentId(session.ledger.connection_generation, candidate_turn_id)
    session.speech_started()
    assert session.ledger.record_boundary(
        SarvamManualBoundary(
            session.ledger.connection_generation,
            boundary_id,
            candidate_turn_id,
            ManualBoundaryEvent.SPEECH_START,
            True,
        ),
        now=clock.now,
    )
    assert session.ledger.record_boundary(
        SarvamManualBoundary(
            session.ledger.connection_generation,
            boundary_id,
            candidate_turn_id,
            ManualBoundaryEvent.SPEECH_END,
            True,
        ),
        now=clock.now,
    )
    session.speech_stopped()
    assert session.ledger.bind_boundary(boundary_id, segment)
    assert session.record_final(
        SegmentFinal(segment, candidate_turn_id, SegmentFinalOutcome.TEXT, text)
    )
    session.changed()
    return candidate_turn_id


async def _control_final(session: InterviewSession, clock: Clock, text: str) -> int:
    """Deliver one exact voice-control segment after an earlier substantive segment."""
    snapshot = session.ledger.snapshot
    assert snapshot.candidate_turn_id is not None
    candidate_turn_id = snapshot.candidate_turn_id
    boundary_id = 1_000 + candidate_turn_id
    segment = SegmentId(session.ledger.connection_generation, boundary_id)
    session.speech_started()
    assert session.ledger.record_boundary(
        SarvamManualBoundary(
            session.ledger.connection_generation,
            boundary_id,
            candidate_turn_id,
            ManualBoundaryEvent.SPEECH_START,
            True,
        ),
        now=clock.now,
    )
    assert session.ledger.record_boundary(
        SarvamManualBoundary(
            session.ledger.connection_generation,
            boundary_id,
            candidate_turn_id,
            ManualBoundaryEvent.SPEECH_END,
            True,
        ),
        now=clock.now,
    )
    session.speech_stopped()
    assert session.ledger.bind_boundary(boundary_id, segment)
    assert session.record_final(
        SegmentFinal(segment, candidate_turn_id, SegmentFinalOutcome.TEXT, text)
    )
    session.changed()
    return candidate_turn_id


def _reply(speak: str, candidate_turn_id: int, quote: str, competency: str) -> str:
    """Build a strict completion-marked model payload with one grounded item."""
    return "● " + json.dumps(
        {
            "speak": speak,
            "evidence": [
                {
                    "candidate_turn_id": candidate_turn_id,
                    "quote": quote,
                    "competency": competency,
                    "observation": "The answer gives a concrete technical detail.",
                    "suggestion": "Name the next signal to inspect.",
                    "uncertainty": "The excerpt does not show the complete incident timeline.",
                }
            ],
        }
    )


def _spoken(captured: list[tuple[Frame, FrameDirection]]) -> list[str]:
    """Return only clean candidate-facing guarded text."""
    return [frame.text for frame, _ in captured if isinstance(frame, LLMTextFrame)]


@pytest.mark.asyncio
async def test_two_rubric_session_accepts_only_grounded_released_answers_and_closes():
    """A full session advances after each released grounded coaching response."""
    clock, session, provider, captured = await _session()
    try:
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.ledger.snapshot.question_id == "question-1"
        first_turn = await _final_answer(
            session, clock, "I traced the connection leak in production."
        )
        provider.replies.append(
            _reply(
                "You mentioned connection leak. Which metric changed first?",
                first_turn,
                "connection leak",
                "Debugging",
            )
        )
        clock.now = 2.5
        session.changed()
        await _drain()

        assert len(provider.calls) == 1
        first_call = provider.calls[0][0]
        assert first_call.metadata["interview_reply_kind"] == ReplyKind.FOLLOW_UP.value
        assert first_call.context.messages[-2] == {
            "role": "user",
            "content": "I traced the connection leak in production.",
        }
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
        assert [answer.transcript for answer in session.controller.accepted_answers] == [
            "I traced the connection leak in production."
        ]
        assert _spoken(captured)[-1] == "You mentioned connection leak. Which metric changed first?"
        assert not any("observation" in text or "suggestion" in text for text in _spoken(captured))

        second_turn = await _final_answer(
            session, clock, "The saturation metric climbed before errors."
        )
        provider.replies.append(
            _reply(
                "Thank you for explaining the saturation metric.",
                second_turn,
                "saturation metric",
                "Debugging",
            )
        )
        clock.now = 5.0
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-2"
        assert len(session.controller.accepted_answers) == 2

        third_turn = await _final_answer(
            session, clock, "I would separate writes from reads at peak load."
        )
        provider.replies.append(
            _reply(
                "You mentioned separate writes from reads. What tradeoff would you measure?",
                third_turn,
                "separate writes from reads",
                "Design",
            )
        )
        clock.now = 7.5
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.FOLLOW_UP

        fourth_turn = await _final_answer(
            session, clock, "I would watch replication lag and recovery time."
        )
        provider.replies.append(
            _reply(
                "Thank you for tying replication lag to recovery time.",
                fourth_turn,
                "replication lag",
                "Design",
            )
        )
        clock.now = 10.0
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.ENDED
        assert len(session.controller.accepted_answers) == 4
        assert any(isinstance(frame, EndFrame) for frame, _ in captured)
        assert all("uncertainty" not in text for text in _spoken(captured))
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_interrupted_stream_between_start_and_end_cannot_accept_or_speak() -> None:
    """Candidate speech invalidates a provider stream after its start frame but before release."""
    clock, session, provider, captured = await _session((None,))
    try:
        turn = await _final_answer(session, clock, "I traced a queue backlog.")
        clock.now = 2.5
        session.changed()
        await _drain()
        frame, direction = provider.held.pop()
        before = list(_spoken(captured))

        start = LLMFullResponseStartFrame()
        start.metadata.update(frame.metadata)
        await session.guard.process_frame(start, direction)
        session.speech_started()
        for outgoing in (
            LLMTextFrame(
                _reply(
                    "You mentioned queue backlog. What signal rose first?",
                    turn,
                    "queue backlog",
                    "Debugging",
                )
            ),
            LLMFullResponseEndFrame(),
        ):
            outgoing.metadata.update(frame.metadata)
            await session.guard.process_frame(outgoing, direction)
        await _drain()

        assert _spoken(captured) == before
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == "I traced a queue backlog."
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_repeat_and_thinking_controls_cancel_provider_but_keep_earlier_evidence() -> None:
    """Exact control segments never become evidence and invalidate output already admitted."""
    for control in ("repeat the question", "let me think"):
        clock, session, provider, captured = await _session((None,))
        try:
            await _final_answer(session, clock, "I found the failing cache key.")
            clock.now = 2.5
            session.changed()
            await _drain()
            assert len(provider.calls) == 1

            await _control_final(session, clock, control)
            await _drain()
            assert session.ledger.snapshot.text == "I found the failing cache key."
            assert control not in session.ledger.snapshot.text
            assert session.controller.current_prompt is None
            assert session.controller.accepted_answers == ()
            assert session.lookup_authorization(provider.calls[0][0].metadata) is None
        finally:
            await session.cleanup()


@pytest.mark.asyncio
async def test_skip_end_and_control_only_final_do_not_accept_a_candidate_answer() -> None:
    """Skip advances without evidence, while end closes a pending output without accepting it."""
    clock, session, _provider, _captured = await _session()
    try:
        await _control_final(session, clock, "skip this question")
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-2"
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == ""
    finally:
        await session.cleanup()

    clock, session, provider, _captured = await _session((None,))
    try:
        await _final_answer(session, clock, "I found an answer that skip must discard.")
        clock.now = 2.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 1
        await _control_final(session, clock, "skip this question")
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-2"
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == ""
        assert session.lookup_authorization(provider.calls[0][0].metadata) is None
    finally:
        await session.cleanup()

    clock, session, provider, captured = await _session((None,))
    try:
        await _final_answer(session, clock, "I retained an answer that must not be accepted.")
        clock.now = 2.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 1
        await _control_final(session, clock, "end the interview")
        await _drain()
        assert session.controller.phase is InterviewPhase.ENDED
        assert session.controller.accepted_answers == ()
        assert any(isinstance(frame, EndFrame) for frame, _ in captured)
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_bound_intro_control_is_excluded_before_the_first_question_is_released() -> None:
    """A control received while the local introduction is pending never seeds candidate evidence."""
    clock, session, _provider, _captured = await _session(drain=False)
    try:
        assert session.controller.phase is InterviewPhase.INTRODUCTION
        await _control_final(session, clock, "repeat")
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-1"
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == ""
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_ignored_intro_skip_keeps_the_pending_introduction() -> None:
    """A skip command cannot erase the only introduction transition before question one."""
    clock, session, _provider, _captured = await _session(drain=False)
    try:
        introduction = session._local_prompt
        assert introduction is not None
        response_token = session.ledger.response_token
        await _control_final(session, clock, "skip this question")
        assert session._local_prompt == introduction
        assert session.ledger.response_token > response_token
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-1"
    finally:
        await session.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ("repeat", "skip", "thinking"))
async def test_non_end_control_in_closing_keeps_the_pending_closing_prompt(control: str) -> None:
    """Closing accepts no new control transition except another end request."""
    _clock, session, _provider, captured = await _session()
    try:
        session.handle_control("end")
        closing = session._local_prompt
        assert closing is not None and closing.reply_kind is ReplyKind.CLOSING
        serial = session._serial
        response_token = session.ledger.response_token
        deadline = session._next_wake_deadline()
        session.handle_control(control)
        assert session._local_prompt == closing
        assert session._serial == serial
        assert session.ledger.response_token == response_token
        assert session._next_wake_deadline() == deadline
        await _drain()
        assert session.controller.phase is InterviewPhase.ENDED
        assert any(isinstance(frame, EndFrame) for frame, _ in captured)
    finally:
        await session.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "● {not JSON}",
        _reply(
            "You mentioned fabricated evidence. What happened next?",
            2,
            "fabricated evidence",
            "Debugging",
        ),
        _reply(
            "You mentioned cache logs. What signal changed next?",
            2,
            "cache logs",
            "Design",
        ),
    ],
)
async def test_malformed_or_fabricated_model_payload_keeps_question_and_answer_pending(
    response: str,
) -> None:
    """Schema and quote validation fail closed before speech or progression."""
    clock, session, provider, captured = await _session((response,))
    try:
        await _final_answer(session, clock, "I inspected the cache logs.")
        clock.now = 2.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 1
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == "I inspected the cache logs."
        assert not any("fabricated" in text for text in _spoken(captured))
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_timed_checkin_replaces_model_json_or_grading_with_neutral_controller_text() -> None:
    """A check-in can preserve liveness without exposing model coaching or accepting an answer."""
    unsafe_checkin = '● {"speak":"You earned 10/10; I recommend hiring you?","evidence":[]}'
    clock, session, provider, captured = await _session(("◐", unsafe_checkin))
    try:
        await _final_answer(session, clock, "I explained the incident timeline.")
        clock.now = 2.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 1
        clock.now = 10.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 2
        assert provider.calls[-1][0].metadata["interview_reply_kind"] == ReplyKind.CHECK_IN.value
        assert _spoken(captured)[-1] == "Take your time. Is there anything you would like to add?"
        assert "hiring" not in _spoken(captured)[-1]
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == "I explained the incident timeline."
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_timeout_and_replayed_provider_response_do_not_advance_or_speak_twice() -> None:
    """The watchdog and guard tombstone preserve a pending turn under delayed or replayed I/O."""
    clock, session, provider, captured = await _session((None,))
    try:
        await _final_answer(session, clock, "I measured queue depth.")
        clock.now = 2.5
        session.changed()
        await _drain()
        assert len(provider.calls) == 1
        clock.now = 47.5
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.accepted_answers == ()
        assert session.ledger.snapshot.text == "I measured queue depth."
        assert _spoken(captured)[-1] != "I measured queue depth."
    finally:
        await session.cleanup()

    reply = _reply(
        "You mentioned queue depth. Which worker was saturated?",
        2,
        "queue depth",
        "Debugging",
    )
    clock, session, provider, captured = await _session((reply,))
    try:
        await _final_answer(session, clock, "I measured queue depth.")
        clock.now = 2.5
        session.changed()
        await _drain()
        frame, direction = provider.calls[0]
        spoken_count = len(_spoken(captured))
        await provider.emit(frame, direction, reply)
        await _drain()
        assert len(_spoken(captured)) == spoken_count
        assert len(session.controller.accepted_answers) == 1
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_duration_expiry_blocks_dispatch_and_release_races() -> None:
    """The duration deadline closes before dispatch and before a delayed response can release."""
    clock, session, provider, captured = await _session()
    try:
        clock.now = 300.0
        session.changed()
        await _drain()
        assert provider.calls == []
        assert session.controller.phase is InterviewPhase.ENDED
        assert any(isinstance(frame, EndFrame) for frame, _ in captured)
    finally:
        await session.cleanup()

    clock, session, provider, captured = await _session((None,))
    try:
        turn = await _final_answer(session, clock, "I traced a duration race.")
        clock.now = 2.5
        session.changed()
        await _drain()
        frame, direction = provider.held.pop()
        start = LLMFullResponseStartFrame()
        start.metadata.update(frame.metadata)
        await session.guard.process_frame(start, direction)
        text = LLMTextFrame(
            _reply(
                "You mentioned duration race. Which deadline won?",
                turn,
                "duration race",
                "Debugging",
            )
        )
        text.metadata.update(frame.metadata)
        await session.guard.process_frame(text, direction)
        spoken_count = len(_spoken(captured))
        clock.now = 300.0
        end = LLMFullResponseEndFrame()
        end.metadata.update(frame.metadata)
        await session.guard.process_frame(end, direction)
        session.changed()
        await _drain()
        assert len(_spoken(captured)) == spoken_count + 1
        assert "duration race" not in _spoken(captured)[-1]
        assert session.controller.accepted_answers == ()
        assert session.controller.phase is InterviewPhase.ENDED
    finally:
        await session.cleanup()


@pytest.mark.asyncio
async def test_interruption_after_validation_clears_staged_answer_and_replays_local_prompt() -> (
    None
):
    """A standalone interruption between guarded output and release cannot strand a transition."""
    clock, session, provider, captured = await _session()
    try:
        original_push = session.guard.push_frame
        interrupt_on_end = False

        async def capture_and_interrupt(
            frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
        ) -> None:
            nonlocal interrupt_on_end
            await original_push(frame, direction)
            if interrupt_on_end and isinstance(frame, LLMFullResponseEndFrame):
                interrupt_on_end = False
                await session.process_frame(InterruptionFrame(), direction)

        session.guard.push_frame = capture_and_interrupt
        turn = await _final_answer(session, clock, "I traced the saturation alert.")
        provider.replies.append(
            _reply(
                "You mentioned saturation alert. Which queue filled first?",
                turn,
                "saturation alert",
                "Debugging",
            )
        )
        interrupt_on_end = True
        clock.now = 2.5
        session.changed()
        await _drain()

        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_prompt is None
        assert session.controller.accepted_answers == ()
        assert session._staged_coaching == {}
        assert session.ledger.snapshot.text == "I traced the saturation alert."

        question_text = session.controller.current_question.text
        interrupt_on_end = True
        session.handle_control("repeat")
        await _drain()
        assert _spoken(captured).count(question_text) >= 3
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_prompt is None

        await _control_final(session, clock, "I verified the worker queue.")
        provider.replies.append(
            _reply(
                "You mentioned worker queue. What threshold would you alert on?",
                turn,
                "worker queue",
                "Debugging",
            )
        )
        clock.now = 5.0
        session.changed()
        await _drain()
        assert len(session.controller.accepted_answers) == 1
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
    finally:
        await session.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ("repeat", "let me think"))
async def test_bound_control_replaces_a_pending_new_question_without_losing_the_next_turn(
    control: str,
) -> None:
    """Repeat and thinking controls can replace an unreleased next-question prompt safely."""
    clock, session, provider, _captured = await _session()
    try:
        first = await _final_answer(session, clock, "I traced a connection leak.")
        provider.replies.append(
            _reply(
                "You mentioned connection leak. Which metric rose first?",
                first,
                "connection leak",
                "Debugging",
            )
        )
        clock.now = 2.5
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.FOLLOW_UP

        second = await _final_answer(session, clock, "The saturation metric rose before errors.")
        provider.replies.append(None)
        clock.now = 5.0
        session.changed()
        await _drain()
        frame, direction = provider.held.pop()
        await provider.emit(
            frame,
            direction,
            _reply(
                "Thank you for explaining the saturation metric.",
                second,
                "saturation metric",
                "Debugging",
            ),
        )
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session._local_prompt is not None
        assert session._local_prompt.question_id == "question-2"

        await _control_final(session, clock, control)
        await _drain()
        assert session.controller.phase is InterviewPhase.QUESTION
        assert session.controller.current_prompt is None
        assert session.controller.current_question is not None
        assert session.controller.current_question.question_id == "question-2"
        assert session.ledger.snapshot.question_id == "question-2"
        assert session.ledger.snapshot.text == ""
        assert len(session.controller.accepted_answers) == 2

        third = await _final_answer(session, clock, "I would partition writes by tenant.")
        provider.replies.append(
            _reply(
                "You mentioned partition writes. Which skew would you monitor?",
                third,
                "partition writes",
                "Design",
            )
        )
        clock.now = 7.5
        session.changed()
        await _drain()
        assert len(session.controller.accepted_answers) == 3
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
    finally:
        await session.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "இல்ல நான் இப்ப வரைக்கும் வாழ்க்கையில ஒண்ணுமே பண்ணதில்ல",
        "எனக்கு அனுபவம் இல்லை",
        "எனக்கு  அனுபவம் இல்லை.",
    ],
)
async def test_no_experience_gets_guarded_tamil_follow_up_without_provider(answer):
    """Lack of experience receives an everyday question without a model dependency."""
    clock, session, provider, captured = await _session(language="tanglish")
    try:
        await _final_answer(session, clock, answer)
        clock.now = 2.5
        session.changed()
        await _drain()
        assert not provider.calls
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
        assert "வீட்டுல ஒருத்தர் உதவி கேட்டா" in _spoken(captured)[-1]
        assert answer in _spoken(captured)[-1]
        assert session.controller.accepted_answers[-1].transcript == answer
        assert session.coaching[-1].evidence[0].quote == answer
    finally:
        await session.cleanup()
