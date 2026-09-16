"""Focused tests for multilingual interview controls and copy."""

from demo.interview.interaction import InteractionAction
from demo.interview.interaction_copy import get_copy, get_explanation, get_scenario
from demo.interview.voice_controls import VoiceControl, parse_voice_control


def test_voice_control_is_compatible_alias_for_interaction_action() -> None:
    """Keep legacy imports pointed at the shared action enum."""
    assert VoiceControl is InteractionAction
    assert parse_voice_control("Please, repeat the question!") is InteractionAction.REPEAT


def test_controls_accept_tanglish_tamil_and_hinglish_hindi() -> None:
    """Recognize curated commands in the supported language modes."""
    assert parse_voice_control("கொஞ்சம் நேரம் வேண்டும்") is InteractionAction.THINKING
    assert parse_voice_control("இந்த கேள்வியை தவிர்க்கவும்") is InteractionAction.SKIP
    assert parse_voice_control("कृपया सवाल फिर से बोलिए") is InteractionAction.REPEAT
    assert parse_voice_control("interview khatam kijiye") is InteractionAction.END


def test_matching_is_whole_utterance_and_normalizes_nfc() -> None:
    """Reject embedded commands while accepting normalized punctuation."""
    assert parse_voice_control("I will skip this question because it is difficult") is None
    assert parse_voice_control("I do not want to skip") is None
    assert parse_voice_control("repeat the question — please") is InteractionAction.REPEAT
    assert parse_voice_control("கொஞ்சம் நேரம் வேண்டும்") is InteractionAction.THINKING


def test_recovery_commands_are_explicit() -> None:
    """Require explicit recovery phrases."""
    assert parse_voice_control("retry response") is InteractionAction.RETRY_RESPONSE
    assert parse_voice_control("retry transcription") is InteractionAction.RETRY_TRANSCRIPTION
    assert parse_voice_control("restart my answer") is InteractionAction.RESTART_ANSWER
    assert parse_voice_control("please retry my response") is None


def test_copy_localization_fallback_and_placeholders() -> None:
    """Render localized copy and English fallback with placeholders."""
    assert get_copy("unknown", "thinking") == get_copy("en", "thinking")
    assert "Current question" in get_copy("en", "explain", question="Current question")
    assert get_copy("ta", "checkin")
    assert get_copy("hi", "recovery")


def test_known_competencies_have_distinct_localized_scenario_copy() -> None:
    """Keep experience explanations distinct from hypothetical scenarios."""
    for language in ("en", "ta", "hi"):
        for competency in (
            "Teamwork",
            "Communication",
            "Conflict resolution",
            "Ownership",
            "Resilience",
        ):
            explanation = get_explanation(language, competency)
            scenario = get_scenario(language, competency)
            assert explanation and scenario and explanation != scenario
            assert len(explanation) < 200 and len(scenario) < 200
    assert get_explanation("en", "Unknown") is None
    assert get_scenario("ta", "Unknown") is None
