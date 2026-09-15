"""Deterministic progression and control coverage for the interview controller."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from demo.interview.config import InterviewConfig
from demo.interview.contracts import AcceptedAnswer, ReplyKind, SegmentId
from demo.interview.controller import InterviewControl, InterviewController, InterviewPhase


@dataclass
class Clock:
    """Controllable monotonic clock for controller state tests.

    Parameters:
        now: Current monotonic time in seconds.
    """

    now: float = 0.0

    def __call__(self) -> float:
        """Return the selected time."""
        return self.now


def _config() -> InterviewConfig:
    """Create a two-question configuration with every prompt value represented."""
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


def _start(controller: InterviewController):
    """Release introduction and first question so a candidate answer can be staged."""
    intro = controller.start()
    assert intro is not None and intro.reply_kind is ReplyKind.INTRODUCTION
    question = controller.release(intro.response_id)
    assert question is not None and question.reply_kind is ReplyKind.QUESTION
    assert controller.release(question.response_id) is None
    return intro, question


def _answer(
    controller: InterviewController, candidate_turn_id: int, transcript: str
) -> AcceptedAnswer:
    """Build an answer exactly matching the controller's active question snapshot."""
    question = controller.current_question
    assert question is not None
    return AcceptedAnswer(
        candidate_turn_id=candidate_turn_id,
        question_id=question.question_id,
        segment_ids=(SegmentId(1, candidate_turn_id),),
        transcript=transcript,
    )


def test_question_follow_up_and_next_question_commit_only_after_release() -> None:
    """Each rubric question gets one grounded follow-up before deterministic progression."""
    controller = InterviewController(_config(), "session-1", Clock())
    intro, first = _start(controller)
    assert controller.phase is InterviewPhase.QUESTION
    assert "5-minute" in intro.text
    assert "mid" in first.text and "Backend engineer" in first.text

    controller.expect_candidate_turn(10)
    staged = controller.stage_answer(
        _answer(controller, 10, "I traced the connection leak."),
        20,
        "What signal first pointed you to the connection leak?",
        evidence=("traced the connection leak",),
        coaching="Name the metric that changed.",
    )
    assert controller.accepted_answers == ()
    assert staged.reply_kind is ReplyKind.FOLLOW_UP
    assert controller.release(staged.response_id) is None
    assert controller.phase is InterviewPhase.FOLLOW_UP
    assert [answer.transcript for answer in controller.accepted_answers] == [
        "I traced the connection leak."
    ]
    assert controller.rubric_evidence == {"Debugging": ("traced the connection leak",)}
    assert controller.coaching_notes == ("Name the metric that changed.",)

    controller.expect_candidate_turn(11)
    follow_up_answer = controller.stage_answer(
        _answer(controller, 11, "The saturation metric climbed before errors."),
        21,
        "Thank you. Let us move to the next topic.",
    )
    next_question = controller.release(follow_up_answer.response_id)
    assert next_question is not None and next_question.reply_kind is ReplyKind.QUESTION
    assert controller.phase is InterviewPhase.QUESTION
    assert controller.current_question is not None
    assert controller.current_question.index == 1
    assert len(controller.accepted_answers) == 2
    assert controller.current_prompt == next_question


def test_intro_controls_keep_the_introduction_transition_before_questioning() -> None:
    """Repeat and thinking acknowledgement reissue the introduction with its release action."""
    repeated = InterviewController(_config(), "session-intro-repeat", Clock())
    initial = repeated.start()
    assert initial is not None
    repeated_intro = repeated.handle_control(InterviewControl.REPEAT)
    assert repeated_intro is not None and repeated_intro.reply_kind is ReplyKind.INTRODUCTION
    question = repeated.release(repeated_intro.response_id)
    assert question is not None and question.reply_kind is ReplyKind.QUESTION
    assert repeated.phase is InterviewPhase.QUESTION

    thinking = InterviewController(_config(), "session-intro-thinking", Clock())
    assert thinking.start() is not None
    acknowledgement = thinking.handle_control(InterviewControl.THINKING)
    assert acknowledgement is not None and acknowledgement.reply_kind is ReplyKind.CHECK_IN
    intro = thinking.release(acknowledgement.response_id)
    assert intro is not None and intro.reply_kind is ReplyKind.INTRODUCTION
    question = thinking.release(intro.response_id)
    assert question is not None and question.reply_kind is ReplyKind.QUESTION
    assert thinking.phase is InterviewPhase.QUESTION


def test_invalid_or_replayed_output_never_commits_an_answer() -> None:
    """Only the expected candidate and matching single release can advance the rubric."""
    controller = InterviewController(_config(), "session-2", Clock())
    _start(controller)
    controller.expect_candidate_turn(2)
    answer = _answer(controller, 2, "I inspected the logs.")
    staged = controller.stage_answer(answer, 50, "Which log line was decisive?")

    assert controller.release(404) is None
    assert controller.accepted_answers == ()
    assert controller.invalidate_response(49) is False
    assert controller.invalidate_response(50) is True
    assert controller.accepted_answers == ()
    with pytest.raises(ValueError, match="already been used"):
        controller.stage_answer(answer, 50, "A replacement reply")

    controller.expect_candidate_turn(2)
    staged = controller.stage_answer(answer, 51, "Which log line was decisive?")
    assert controller.release(staged.response_id) is None
    assert len(controller.accepted_answers) == 1
    assert controller.release(staged.response_id) is None
    assert len(controller.accepted_answers) == 1

    controller.expect_candidate_turn(3)
    wrong_question = AcceptedAnswer(3, "question-99", (SegmentId(1, 3),), "Wrong snapshot")
    with pytest.raises(ValueError, match="different question"):
        controller.stage_answer(wrong_question, 52, "This must not speak")
    valid_question = _answer(controller, 3, "The error rate rose.")
    with pytest.raises(ValueError, match="evidence excerpts"):
        controller.stage_answer(valid_question, 53, "This must not speak", evidence=("invented",))


def test_controls_preserve_or_discard_pending_state_as_declared() -> None:
    """Repeat retains a question, skip discards staged evidence, and thinking stays non-substantive."""
    controller = InterviewController(_config(), "session-3", Clock())
    _intro, first = _start(controller)
    controller.expect_candidate_turn(4)
    controller.stage_answer(_answer(controller, 4, "A pending answer."), 60, "Follow up?")

    repeated = controller.handle_control(InterviewControl.REPEAT)
    assert repeated is not None and repeated.reply_kind is ReplyKind.REPEAT
    assert repeated.question_id == first.question_id
    assert controller.accepted_answers == ()
    assert controller.current_prompt == repeated

    skipped = controller.handle_control(InterviewControl.SKIP)
    assert skipped is not None and skipped.reply_kind is ReplyKind.QUESTION
    assert controller.accepted_answers == ()
    assert skipped.question_index == 1
    assert controller.phase is InterviewPhase.QUESTION

    assert controller.release(skipped.response_id) is None
    controller.expect_candidate_turn(5)
    thinking = controller.handle_control(InterviewControl.THINKING)
    assert thinking is not None and thinking.reply_kind is ReplyKind.CHECK_IN
    assert controller.waiting_reason == "thinking"
    assert controller.accepted_answers == ()
    assert controller.release(thinking.response_id) is None
    assert controller.phase is InterviewPhase.QUESTION


def test_end_and_duration_expiry_latch_closing_without_further_questions() -> None:
    """Stop and expiry discard uncommitted answers and allow only one closing output."""
    clock = Clock()
    controller = InterviewController(_config(), "session-4", clock)
    _start(controller)
    controller.expect_candidate_turn(6)
    staged = controller.stage_answer(_answer(controller, 6, "Unreleased."), 70, "Follow up?")

    closing = controller.handle_control(InterviewControl.END)
    assert closing is not None and closing.reply_kind is ReplyKind.CLOSING
    assert controller.phase is InterviewPhase.CLOSING
    assert controller.accepted_answers == ()
    assert controller.release(staged.response_id) is None
    assert controller.handle_control(InterviewControl.SKIP) is None
    assert controller.handle_control(InterviewControl.THINKING) is None
    assert controller.release(closing.response_id) is None
    assert controller.phase is InterviewPhase.ENDED

    expired = InterviewController(_config(), "session-5", clock)
    _start(expired)
    current = expired.current_question
    clock.now = 300.0
    close_on_timeout = expired.tick()
    assert close_on_timeout is not None and close_on_timeout.reply_kind is ReplyKind.CLOSING
    assert expired.phase is InterviewPhase.CLOSING
    assert expired.current_question == current
    assert expired.release(close_on_timeout.response_id) is None
    assert expired.phase is InterviewPhase.ENDED
    assert expired.tick() is None


def test_release_checks_duration_before_committing_a_staged_answer() -> None:
    """A duration race discards an unreleased answer and cannot yield another question."""
    clock = Clock()
    controller = InterviewController(_config(), "session-6", clock)
    _start(controller)
    controller.expect_candidate_turn(8)
    staged = controller.stage_answer(_answer(controller, 8, "Almost committed."), 80, "Follow up?")

    clock.now = 300.0
    closing = controller.release(staged.response_id)
    assert closing is not None and closing.reply_kind is ReplyKind.CLOSING
    assert controller.accepted_answers == ()
    assert controller.phase is InterviewPhase.CLOSING
    assert controller.release(closing.response_id) is None
    assert controller.phase is InterviewPhase.ENDED


def test_tanglish_behavioural_questions_do_not_read_english_rubric_notes() -> None:
    """Speak Tamil-led questions while retaining the rubric as evaluation context."""
    from demo.interview.pipeline import default_live_config

    config = default_live_config()
    controller = InterviewController(config, "tanglish-test", language="tanglish")
    for index, rubric in enumerate(config.question_rubric):
        text = controller._question_text(rubric, index)
        assert "Listen for" not in text
        assert rubric.guidance not in text
        assert any("\u0b80" <= char <= "\u0bff" for char in text)
    assert "உதவி செஞ்சிருக்கீங்களா?" in controller._question_text(config.question_rubric[0], 0)
