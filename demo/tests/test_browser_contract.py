"""Public browser setup and event payload boundary coverage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from demo.interview.browser_contract import (
    BrowserInterviewCommand,
    BrowserInterviewSetup,
    BrowserReady,
    InterviewBrowserEvent,
)
from demo.interview.gemini_languages import GEMINI_INPUT_LANGUAGES, GEMINI_LANGUAGES
from demo.interview.languages import INTERVIEW_LANGUAGES, STT_LANGUAGES


def test_setup_defaults_to_tanglish_and_contains_only_candidate_choices():
    """The browser may choose interview content but receives no provider configuration."""
    setup = BrowserInterviewSetup()
    payload = setup.model_dump(mode="json")
    assert payload["language"] == "tanglish"
    assert payload["rubric"]
    assert set(payload) == {
        "role",
        "difficulty",
        "duration_minutes",
        "rubric",
        "language",
        "stt_language",
        "speech_pace",
    }
    assert payload["stt_language"] == "auto"
    assert payload["speech_pace"] == 0.85


@pytest.mark.parametrize("language", INTERVIEW_LANGUAGES)
@pytest.mark.parametrize("stt_language", ["auto", *STT_LANGUAGES])
def test_setup_accepts_supported_interview_and_recognition_languages(language, stt_language):
    """Every advertised language choice is accepted at the public setup boundary."""
    setup = BrowserInterviewSetup(language=language, stt_language=stt_language, speech_pace=1.2)
    assert setup.language == language
    assert setup.stt_language == stt_language
    assert setup.speech_pace == 1.2


def test_setup_accepts_gemini_native_language_choices():
    """The Gemini catalog extends the legacy Sarvam-compatible setup values."""
    for language in GEMINI_LANGUAGES:
        assert BrowserInterviewSetup(language=language).language == language
    for input_language in GEMINI_INPUT_LANGUAGES:
        assert BrowserInterviewSetup(stt_language=input_language).stt_language == input_language


@pytest.mark.parametrize("payload", [{"language": "unknown"}, {"stt_language": "fr-FR"}])
def test_setup_rejects_unsupported_language_choices(payload):
    """The browser cannot select an unverified provider language."""
    with pytest.raises(ValidationError):
        BrowserInterviewSetup(**payload)


@pytest.mark.parametrize("pace", [0.49, 2.01, float("inf"), float("nan")])
def test_setup_rejects_unsafe_speech_pace(pace):
    """Speech pace remains a finite, bounded setup preference."""
    with pytest.raises(ValidationError):
        BrowserInterviewSetup(speech_pace=pace)


@pytest.mark.parametrize(
    "unsafe_field",
    ["api_key", "SARVAM_API_KEY", "providers", "inference_url", "endpoint", "credentials"],
)
def test_setup_rejects_provider_credentials_and_endpoints(unsafe_field):
    """Client setup cannot select or disclose a backend provider boundary."""
    with pytest.raises(ValidationError):
        BrowserInterviewSetup.model_validate({unsafe_field: "sentinel-secret"})


@pytest.mark.parametrize("role", ["", " \t\n "])
def test_setup_rejects_blank_role(role):
    """A browser session cannot create an unusable whitespace-only interview role."""
    with pytest.raises(ValidationError):
        BrowserInterviewSetup(role=role)


def test_event_is_versioned_session_scoped_and_plain_data_only():
    """A browser event has stable ordering fields and no incidental backend model state."""
    event = InterviewBrowserEvent(
        session_id="session-1",
        connection_generation=2,
        seq=7,
        type="interview.caption",
        text="provisional answer",
        final=False,
        segment_id="2:11",
        candidate_turn_id=4,
    )
    payload = event.model_dump(mode="json", exclude_none=True)
    assert payload == {
        "v": 1,
        "session_id": "session-1",
        "connection_generation": 2,
        "seq": 7,
        "type": "interview.caption",
        "text": "provisional answer",
        "final": False,
        "segment_id": "2:11",
        "candidate_turn_id": 4,
    }
    assert "api_key" not in payload and "endpoint" not in payload


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unknown"},
        {"type": "interview.reply", "status": "Unknown"},
        {"type": "interview.status", "api_key": "sentinel-secret"},
        {"type": "interview.status", "credentials": {"api_key": "sentinel-secret"}},
        {"type": "interview.status", "seq": -1},
        {"type": "interview.status", "connection_generation": 0},
    ],
)
def test_event_rejects_unknown_or_sensitive_fields(payload):
    """Wire payload validation is fail-closed for schema drift and backend secrets."""
    with pytest.raises(ValidationError):
        InterviewBrowserEvent.model_validate(payload)


def test_ready_negotiates_only_a_bounded_capability_list():
    """Readiness accepts unknown capability names for forward-compatible intersection."""
    ready = BrowserReady.model_validate(
        {
            "v": 1,
            "type": "interview.ready",
            "session_id": "session-1",
            "capabilities": ["interaction_controls_v1", "future"],
        }
    )
    assert ready.session_id == "session-1"
    assert ready.capabilities == ["interaction_controls_v1", "future"]

    with pytest.raises(ValidationError):
        BrowserReady.model_validate({"type": "interview.ready", "capabilities": "not-a-list"})


def test_command_has_closed_actions_strict_ids_and_recovery_token_scope():
    """Browser commands cannot coerce IDs or use recovery credentials for ordinary actions."""
    command = BrowserInterviewCommand.model_validate(
        {
            "v": 1,
            "type": "interview.command",
            "session_id": "session-1",
            "connection_generation": 1,
            "command_id": 3,
            "prompt_revision": 2,
            "action": "retry_response",
            "recovery_token": "opaque-token",
        }
    )
    assert command.command_id == 3

    for payload in (
        {**command.model_dump(), "command_id": True},
        {**command.model_dump(), "command_id": "3"},
        {**command.model_dump(), "action": "unknown"},
        {**command.model_dump(), "action": "repeat", "recovery_token": "opaque-token"},
        {**command.model_dump(), "recovery_token": None},
        {**command.model_dump(), "extra": "not allowed"},
    ):
        with pytest.raises(ValidationError):
            BrowserInterviewCommand.model_validate(payload)


def test_command_results_require_a_complete_safe_outcome():
    """An acknowledgment cannot be mistaken for success without its terminal outcome fields."""
    result = InterviewBrowserEvent.model_validate(
        {
            "type": "interview.command_result",
            "command_id": 4,
            "outcome": "applied",
            "code": "ok",
            "prompt_revision": 2,
        }
    )
    assert result.outcome == "applied"
    with pytest.raises(ValidationError):
        InterviewBrowserEvent.model_validate(
            {"type": "interview.command_result", "command_id": 4, "outcome": "applied"}
        )
