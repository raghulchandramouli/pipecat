"""Public browser setup and event payload boundary coverage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from demo.interview.browser_contract import BrowserInterviewSetup, InterviewBrowserEvent


def test_setup_defaults_to_tanglish_and_contains_only_candidate_choices():
    """The browser may choose interview content but receives no provider configuration."""
    setup = BrowserInterviewSetup()
    payload = setup.model_dump(mode="json")
    assert payload["language"] == "tanglish"
    assert payload["rubric"]
    assert set(payload) == {"role", "difficulty", "duration_minutes", "rubric", "language"}


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
