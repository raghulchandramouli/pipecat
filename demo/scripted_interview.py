"""Run an offline two-question interview through the real session state machine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from .interview.config import InterviewConfig
from .interview.contracts import SegmentFinal, SegmentFinalOutcome, SegmentId
from .interview.controller import InterviewPhase
from .interview.sarvam_events import ManualBoundaryEvent, SarvamManualBoundary
from .interview.session import InterviewSession


@dataclass
class Clock:
    """A deterministic monotonic clock for the scripted session.

    Parameters:
        now: Current monotonic timestamp in seconds.
    """

    now: float = 0.0

    def __call__(self) -> float:
        """Return the selected monotonic timestamp."""
        return self.now


class ScriptedProvider:
    """Emit four grounded coaching responses without contacting a model service."""

    def __init__(self, session: InterviewSession) -> None:
        """Store the active session and deterministic response sequence.

        Args:
            session: Session whose reply guard receives scripted provider frames.
        """
        self._session = session
        self._replies = iter(
            (
                (
                    "You mentioned the connection leak. Which metric changed first?",
                    "connection leak",
                    "Debugging",
                ),
                (
                    "Thank you for explaining the saturation metric.",
                    "saturation metric",
                    "Debugging",
                ),
                (
                    "You said separate writes from reads. Which tradeoff would you measure?",
                    "separate writes from reads",
                    "Design",
                ),
                (
                    "Thank you for tying replication lag to recovery time.",
                    "replication lag",
                    "Design",
                ),
            )
        )

    async def __call__(self, frame: LLMContextFrame, direction: FrameDirection) -> None:
        """Send one strict completion-marked coaching response through the real guard.

        Args:
            frame: Admitted model context carrying trusted candidate metadata.
            direction: Pipeline direction for the provider response frames.
        """
        speak, quote, competency = next(self._replies)
        candidate_turn_id = frame.metadata["interview_candidate_turn_id"]
        reply = "● " + json.dumps(
            {
                "speak": speak,
                "evidence": [
                    {
                        "candidate_turn_id": candidate_turn_id,
                        "quote": quote,
                        "competency": competency,
                        "observation": "The answer includes a concrete technical detail.",
                        "suggestion": "Name the next signal to inspect.",
                        "uncertainty": "The excerpt does not show the complete timeline.",
                    }
                ],
            }
        )
        for outgoing in (
            LLMFullResponseStartFrame(),
            LLMTextFrame(reply),
            LLMFullResponseEndFrame(),
        ):
            outgoing.metadata.update(frame.metadata)
            await self._session.guard.process_frame(outgoing, direction)


def _config() -> InterviewConfig:
    """Build the deterministic offline configuration used by the demonstration."""
    return InterviewConfig.model_validate(
        {
            "role": {"title": "Backend engineer", "focus": "distributed systems"},
            "difficulty": "mid",
            "duration_minutes": 5,
            "question_rubric": [
                {"competency": "Debugging", "guidance": "Describe a production incident."},
                {"competency": "Design", "guidance": "Explain a scaling tradeoff."},
            ],
        }
    )


async def _drain_until(predicate, *, rounds: int = 160, state=None) -> None:
    """Yield to managed tasks until a deterministic session condition becomes true.

    Args:
        predicate: Synchronous condition that marks the desired session state.
        rounds: Maximum event-loop yields before the scripted flow is considered stuck.
        state: Optional synchronous summary included when draining times out.

    Raises:
        RuntimeError: If the bounded managed-task drain does not reach the condition.
    """
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    detail = "" if state is None else f": {state()}"
    raise RuntimeError(f"scripted interview did not reach its expected state{detail}")


async def _finalize_answer(session: InterviewSession, clock: Clock, text: str) -> None:
    """Deliver one local manual boundary pair and its finalized substantive transcript.

    Args:
        session: Live offline interview session.
        clock: Shared deterministic monotonic clock.
        text: Final candidate transcript for the active rubric question.
    """
    snapshot = session.ledger.snapshot
    candidate_turn_id = snapshot.candidate_turn_id
    if candidate_turn_id is None:
        raise RuntimeError("session has no active candidate turn")
    boundary_id = candidate_turn_id
    segment_id = SegmentId(session.ledger.connection_generation, candidate_turn_id)
    session.speech_started()
    for event in (ManualBoundaryEvent.SPEECH_START, ManualBoundaryEvent.SPEECH_END):
        if not session.ledger.record_boundary(
            SarvamManualBoundary(
                session.ledger.connection_generation,
                boundary_id,
                candidate_turn_id,
                event,
                True,
            ),
            now=clock.now,
        ):
            raise RuntimeError("scripted local boundary was not accepted")
    session.speech_stopped()
    if not session.ledger.bind_boundary(boundary_id, segment_id):
        raise RuntimeError("scripted provider segment was not bound")
    if not session.record_final(
        SegmentFinal(segment_id, candidate_turn_id, SegmentFinalOutcome.TEXT, text)
    ):
        raise RuntimeError("scripted transcript final was not accepted")
    clock.now += session.config.deadlines.candidate_pause
    session.changed()


async def run() -> None:
    """Run the complete local interview and print only guarded spoken prompts."""
    clock = Clock()
    session = InterviewSession(config=_config(), session_id="offline-script", clock=clock)
    provider = ScriptedProvider(session)
    emitted: list[Frame] = []

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        del direction
        emitted.append(frame)

    session.guard.push_frame = capture
    session.push_frame = capture
    session.set_provider(provider)
    setup = FrameProcessorSetup(
        task_manager=TaskManager(), clock=SystemClock(), pipeline_worker=None
    )
    await session.setup(setup)
    try:
        await session.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await _drain_until(
            lambda: (
                session.controller.phase is InterviewPhase.QUESTION
                and session.controller.current_prompt is None
                and session.ledger.snapshot.candidate_turn_id is not None
            ),
            state=lambda: _state_summary(session),
        )

        for expected_answers, answer in enumerate(
            (
                "I traced a connection leak in production.",
                "The saturation metric climbed before errors.",
                "I would separate writes from reads at peak load.",
                "I would watch replication lag and recovery time.",
            ),
            start=1,
        ):
            await _finalize_answer(session, clock, answer)
            target = expected_answers
            if expected_answers in {1, 3}:
                await _drain_until(
                    lambda target=target: (
                        len(session.controller.accepted_answers) == target
                        and session.controller.phase is InterviewPhase.FOLLOW_UP
                    ),
                    state=lambda: _state_summary(session),
                )
            elif expected_answers < 4:
                await _drain_until(
                    lambda target=target: (
                        len(session.controller.accepted_answers) == target
                        and session.controller.phase is InterviewPhase.QUESTION
                        and session.controller.current_prompt is None
                    ),
                    state=lambda: _state_summary(session),
                )
            else:
                await _drain_until(
                    lambda: session.controller.phase is InterviewPhase.ENDED,
                    state=lambda: _state_summary(session),
                )

        if len(session.controller.accepted_answers) != 4:
            raise AssertionError("scripted interview did not accept four answers")
        if session.controller.phase is not InterviewPhase.ENDED:
            raise AssertionError("scripted interview did not end")
        if not any(isinstance(frame, EndFrame) for frame in emitted):
            raise AssertionError("scripted interview did not emit an end frame")
        for frame in emitted:
            if isinstance(frame, LLMTextFrame):
                print(frame.text)
        print(f"Accepted answers: {len(session.controller.accepted_answers)}")
        print(f"Interview phase: {session.controller.phase.value}")
    finally:
        await session.cleanup()


def _state_summary(session: InterviewSession) -> str:
    """Return a compact diagnostic for a bounded scripted-drain timeout."""
    prompt = session.controller.current_prompt
    snapshot = session.ledger.snapshot
    return (
        f"phase={session.controller.phase.value}, answers={len(session.controller.accepted_answers)}, "
        f"prompt={None if prompt is None else prompt.reply_kind.value}, "
        f"candidate={snapshot.candidate_turn_id}, ready={snapshot.ready}, text={snapshot.text!r}, "
        f"recovery={snapshot.recovery_error}"
    )


def main() -> None:
    """Run the offline scripted interview from ``python -m demo.scripted_interview``."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
