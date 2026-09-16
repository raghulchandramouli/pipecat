"""Accepted evidence survives failed delivery and retry of the same question."""

import pytest

from demo.interview.controller import InterviewPhase
from demo.tests.test_controller_integration import _drain, _final_answer, _reply, _session, _spoken


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_follow_up_delivery_preserves_answer_and_repeat_does_not_advance(partial):
    """Before-audio and partial failures retain evidence while repeat replays the prompt."""
    clock, session, provider, captured = await _session()
    try:
        candidate = await _final_answer(
            session, clock, "I traced the connection leak in production."
        )
        follow_up = "You mentioned connection leak. Which metric changed first?"
        provider.replies.append(_reply(follow_up, candidate, "connection leak", "Debugging"))
        clock.now = 2.5
        session.changed()
        await _drain()
        assert session.controller.phase is InterviewPhase.FOLLOW_UP
        answers = tuple(session.controller.accepted_answers)
        assert len(answers) == 1
        question_id = session.controller.current_question.question_id
        candidate_id = session.ledger.snapshot.candidate_turn_id
        epoch = session.playback_epoch
        if partial:
            session.playback_started(epoch)
        session.playback_failed(epoch)
        assert session.interaction_snapshot()["question_delivery"] == "unconfirmed"
        assert tuple(session.controller.accepted_answers) == answers
        session.playback_stopped(epoch)
        assert session.interaction_snapshot()["question_delivery"] == "unconfirmed"

        for _ in range(2):
            session.apply_interaction_action("repeat")
            await _drain()
            assert session.controller.phase is InterviewPhase.FOLLOW_UP
            assert session.controller.current_question.question_id == question_id
            assert session.ledger.snapshot.candidate_turn_id == candidate_id
            assert tuple(session.controller.accepted_answers) == answers
            assert _spoken(captured)[-1] == follow_up
            assert len(provider.calls) == 1

        retry_epoch = session.playback_epoch
        assert retry_epoch != epoch
        session.playback_started(retry_epoch)
        session.playback_stopped(epoch)
        assert session.interaction_snapshot()["question_delivery"] == "pending"
        session.playback_stopped(retry_epoch)
        assert session.interaction_snapshot()["question_delivery"] == "complete"
        assert tuple(session.controller.accepted_answers) == answers
    finally:
        await session.cleanup()
