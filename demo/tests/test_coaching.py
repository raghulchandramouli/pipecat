"""Validation tests for structured evidence-grounded coaching replies."""

import pytest

from demo.interview.coaching import (
    CoachingReply,
    build_coaching_prompt,
    parse_coaching_reply,
    validate_coaching_reply,
)
from demo.interview.config import Difficulty, QuestionRubric, RoleConfig
from demo.interview.contracts import AcceptedAnswer, SegmentId
from demo.interview.voice_controls import VoiceControl, parse_voice_control


def _answer(turn: int = 7, text: str = "I profiled the database query.") -> AcceptedAnswer:
    return AcceptedAnswer(turn, "q1", (SegmentId(1, turn),), text)


def _rubric() -> tuple[QuestionRubric, ...]:
    return (QuestionRubric(competency="Debugging", guidance="Use evidence."),)


def _reply(**overrides) -> CoachingReply:
    data = {
        "speak": "You profiled the database; what measurement would you add next?",
        "evidence": [
            {
                "candidate_turn_id": 7,
                "quote": "profiled the database",
                "competency": "Debugging",
                "observation": "You profiled the query.",
                "suggestion": "Describe the next measurement.",
                "uncertainty": "The excerpt omits the result.",
            }
        ],
    }
    data.update(overrides)
    return CoachingReply.model_validate(data)


def test_validated_evidence_is_grounded_without_exposing_internal_fields_to_speech():
    """Only the single follow-up question is candidate-facing after evidence validation."""
    result = validate_coaching_reply(
        _reply(), rubric=_rubric(), accepted_answers=[], current_answer=_answer()
    )
    assert result.speak == "You profiled the database; what measurement would you add next?"
    assert result.evidence[0].quote == "profiled the database"


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": [{**_reply().evidence[0].model_dump(), "competency": "Unknown"}]},
        {"evidence": [{**_reply().evidence[0].model_dump(), "quote": "fabricated"}]},
        {"evidence": [{**_reply().evidence[0].model_dump(), "candidate_turn_id": 99}]},
        {
            "evidence": [
                {**_reply().evidence[0].model_dump(), "observation": "Their accent was clear."}
            ]
        },
    ],
)
def test_invalid_or_forbidden_evidence_is_rejected(change):
    """Unknown answers, fabricated quotes, non-rubric claims, and forbidden grounds fail closed."""
    reply = _reply(**change)
    with pytest.raises(ValueError):
        validate_coaching_reply(
            reply, rubric=_rubric(), accepted_answers=[], current_answer=_answer()
        )


def test_current_finalized_answer_can_be_staged_but_not_unrelated_pending_text():
    """Only an explicitly finalized current answer supplements accepted history."""
    reply = _reply(
        speak="You said current final; what evidence would you add?",
        evidence=[
            {**_reply().evidence[0].model_dump(), "candidate_turn_id": 8, "quote": "current final"}
        ],
    )
    result = validate_coaching_reply(
        reply, rubric=_rubric(), accepted_answers=[], current_answer=_answer(8, "current final")
    )
    assert result.evidence[0].candidate_turn_id == 8


def test_post_followup_acknowledgement_cannot_ask_a_second_question():
    """The controller can release the next deterministic question after non-question coaching."""
    reply = _reply(speak="That was a clear use of profiling.")
    result = validate_coaching_reply(
        reply, rubric=_rubric(), accepted_answers=[_answer()], require_follow_up=False
    )
    assert result.speak == "That was a clear use of profiling."
    with pytest.raises(ValueError):
        validate_coaching_reply(reply, rubric=_rubric(), accepted_answers=[_answer()])


def test_followup_requires_a_current_quote_anchor_and_duplicate_json_keys_are_rejected():
    """Model JSON cannot replace a grounded question or silently overwrite evidence fields."""
    with pytest.raises(ValueError, match="anchor"):
        validate_coaching_reply(
            _reply(speak="What measurement would you add next?"),
            rubric=_rubric(),
            accepted_answers=[],
            current_answer=_answer(),
        )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_coaching_reply('{"speak":"A?","speak":"B?","evidence":[]}')


def test_prompt_delimits_untrusted_transcripts_and_forbids_scores_or_hiring_recommendations():
    """The model instruction preserves configuration while transcript text stays data."""
    prompt = build_coaching_prompt(
        role=RoleConfig(title="Backend Engineer"),
        difficulty=Difficulty.MID,
        duration_minutes=30,
        current_question="How would you debug this?",
        rubric=_rubric(),
        accepted_answers=[_answer()],
    )
    assert "<UNTRUSTED_TRANSCRIPTS>" in prompt and "</UNTRUSTED_TRANSCRIPTS>" in prompt
    assert "Do not score" in prompt and "recommend hiring" in prompt
    assert "candidate_turn_id" in prompt


@pytest.mark.parametrize(
    ("text", "control"),
    [
        ("please repeat the question thanks", VoiceControl.REPEAT),
        ("skip this question", VoiceControl.SKIP),
        ("I need time to think", VoiceControl.THINKING),
        ("give me a moment", VoiceControl.THINKING),
        ("let me think", VoiceControl.THINKING),
        ("give me a minute", VoiceControl.THINKING),
        ("could you end the interview please", VoiceControl.END),
        ("I would skip a cache layer in my answer", None),
        ("Can you repeat the question and explain it?", None),
        ("repeat résumé", None),
        ("skip नमस्ते", None),
    ],
)
def test_voice_controls_are_exact_whole_utterance_commands(text, control):
    """Substrings and mixed substantive speech cannot trigger interview controls."""
    assert parse_voice_control(text) is control


@pytest.mark.parametrize("ground", ["accent", "hesitation", "transcription", "score", "rating"])
def test_spoken_assessment_grounds_are_rejected(ground):
    """Forbidden grading cannot bypass evidence validation through the spoken field."""
    with pytest.raises(ValueError, match="spoken reply"):
        validate_coaching_reply(
            _reply(speak=f"Your {ground} stood out; why profiled the database?"),
            rubric=_rubric(),
            accepted_answers=[],
            current_answer=_answer(),
        )


def test_operating_is_not_mistaken_for_rating():
    """Ordinary technical vocabulary remains valid coaching content."""
    reply = _reply()
    reply.evidence[0].observation = "You were operating the database."
    assert (
        validate_coaching_reply(
            reply, rubric=_rubric(), accepted_answers=[], current_answer=_answer()
        )
        .evidence[0]
        .observation
        == "You were operating the database."
    )


def test_post_followup_prompt_has_one_unambiguous_response_shape():
    """Acknowledgements use the same evidence schema without requesting another question."""
    prompt = build_coaching_prompt(
        role=RoleConfig(title="Backend Engineer"),
        difficulty=Difficulty.MID,
        duration_minutes=30,
        current_question="How would you debug this?",
        rubric=_rubric(),
        accepted_answers=[_answer()],
        require_follow_up=False,
    )
    assert "without asking a question" in prompt
    assert "Ask exactly one" not in prompt
    assert '"uncertainty": string' in prompt
