"""Foundation validation, identity isolation, and deterministic provider seams."""

import asyncio
from dataclasses import fields

import pytest
from pydantic import ValidationError

from demo.interview.config import (
    InterviewConfig,
    InterviewDeadlines,
    ProviderCredentials,
    RumikTTSConfig,
    SarvamSTTConfig,
)
from demo.interview.contracts import (
    CompletionDecision,
    CompletionStatus,
    CompletionValidity,
    InterviewSessionState,
    PendingAnswer,
    ReplyKind,
    ResponseGeneration,
    SegmentFinal,
    SegmentFinalOutcome,
    SegmentId,
)
from demo.interview.providers import (
    FakeReasoningProvider,
    FakeSpeechProvider,
    FakeTranscriptSource,
    ReasoningReply,
    ReasoningRequest,
    SpeechRequest,
    SpeechResult,
)


def sample_config() -> dict:
    """Return minimal external configuration without credentials."""
    return {
        "role": {"title": "Backend engineer"},
        "difficulty": "mid",
        "duration_minutes": 30,
        "question_rubric": [{"competency": "Debugging", "guidance": "Explain the evidence."}],
    }


def test_offline_configuration_and_explicit_provider_choices() -> None:
    """Foundation config requires no credentials and preserves design settings."""
    config = InterviewConfig.model_validate(sample_config())
    assert config.providers.sarvam.endpointing == "manual"
    assert config.providers.sarvam.language_code == "en-IN"
    assert config.providers.sarvam.sample_rate == 16000
    assert config.providers.gemini.model == "gemini-3.8-flash"
    assert config.providers.gemini.thinking_level == "low"
    assert config.providers.rumik.provisional is True
    assert config.deadlines.candidate_pause == 2.5
    assert config.deadlines.thinking_grace == 30.0


@pytest.mark.parametrize("field", ["role", "difficulty", "duration_minutes", "question_rubric"])
def test_required_configuration(field: str) -> None:
    """Incomplete user configuration cannot construct a session."""
    values = sample_config()
    del values[field]
    with pytest.raises(ValidationError):
        InterviewConfig.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", {"title": "  "}), ("question_rubric", []), ("difficulty", "unknown")],
)
def test_invalid_configuration(field: str, value: object) -> None:
    """Blank interview content and unsupported difficulty fail validation."""
    with pytest.raises(ValidationError):
        InterviewConfig.model_validate(sample_config() | {field: value})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_deadlines_are_finite_positive(value: float) -> None:
    """No timer can be disabled accidentally by a non-finite or negative value."""
    for field in InterviewDeadlines.model_fields:
        with pytest.raises(ValidationError):
            InterviewDeadlines.model_validate({field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"incomplete_short_retry": 31},
        {"user_turn_stop_timeout": 30},
        {"candidate_pause": 46},
    ],
)
def test_incoherent_deadline_order(values: dict) -> None:
    """Reject timer ordering that conflicts with the declared pause policy."""
    with pytest.raises(ValidationError):
        InterviewDeadlines.model_validate(values)


@pytest.mark.parametrize("key", [None, "", " \t"])
def test_live_credentials_require_nonblank_keys(key: str | None) -> None:
    """Live mode is an explicit credential validation boundary."""
    with pytest.raises(ValidationError):
        ProviderCredentials(mode="live", api_key=key)


def test_credentials_are_redacted_in_repr_serialization_and_errors() -> None:
    """Nested model errors cannot print plaintext credentials."""
    secret = "foundation-secret-must-not-be-printed"
    creds = ProviderCredentials.for_live(secret)
    assert secret not in repr(creds)
    assert secret not in creds.model_dump_json()
    assert secret not in str(creds.model_dump())
    bad = sample_config() | {
        "providers": {
            "sarvam": {
                "credentials": {"mode": "live", "api_key": secret},
                "endpointing": "automatic",
            }
        }
    }
    with pytest.raises(ValidationError) as error:
        InterviewConfig.model_validate(bad)
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    with pytest.raises(ValidationError) as error:
        ProviderCredentials(mode="invalid", api_key=secret)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("model", "values"),
    [(SarvamSTTConfig, {"model": " "}), (RumikTTSConfig, {"checkpoint": " "})],
)
def test_provider_identifiers_cannot_be_blank(model: type, values: dict) -> None:
    """An explicit blank provider setting cannot bypass the intended default."""
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_candidate_ownership_survives_response_invalidation() -> None:
    """Reply generation changes leave pending transcript ownership untouched."""
    segment_id = SegmentId(connection_generation=2, utterance_idx=3)
    pending = PendingAnswer(candidate_turn_id=7, question_id="q1", segment_ids=[segment_id])
    state = InterviewSessionState(
        session_id="session-a",
        connection_generation=2,
        pending_answer=pending,
        active_response_generation=ResponseGeneration(8),
    )
    state.active_response_generation = ResponseGeneration(9)
    assert state.pending_answer is pending
    assert pending.segment_ids == [segment_id]
    assert pending.candidate_turn_id == 7
    assert SegmentId(3, 3) != segment_id
    assert not any("response_generation" in field.name for field in fields(PendingAnswer))
    assert not any("response_generation" in field.name for field in fields(SegmentFinal))


def test_session_mutable_state_is_isolated() -> None:
    """Separate sessions never share answer lists or pending-final maps."""
    first = InterviewSessionState(session_id="one")
    second = InterviewSessionState(session_id="two")
    assert first.accepted_answers is not second.accepted_answers
    assert PendingAnswer(1, "q").finals is not PendingAnswer(1, "q").finals


def test_empty_final_is_distinct_from_missing_or_failed() -> None:
    """An empty terminal event is observable without invented transcript text."""
    event = SegmentFinal(
        segment_id=SegmentId(0, 0), candidate_turn_id=1, outcome=SegmentFinalOutcome.EMPTY
    )
    assert event.text == ""
    with pytest.raises(ValueError):
        SegmentFinal(
            segment_id=event.segment_id,
            candidate_turn_id=1,
            outcome=SegmentFinalOutcome.TEXT,
        )


def test_fake_provider_scripts_preserve_order_ownership_and_exhaustion() -> None:
    """Fakes expose duplicates and stale replies for future gate tests to reject."""

    async def exercise() -> None:
        final = SegmentFinal(
            segment_id=SegmentId(2, 1),
            candidate_turn_id=7,
            outcome=SegmentFinalOutcome.TEXT,
            text="Evidence",
        )
        empty = SegmentFinal(
            segment_id=SegmentId(2, 0), candidate_turn_id=7, outcome=SegmentFinalOutcome.EMPTY
        )
        source = FakeTranscriptSource([final, empty, final])
        assert [event async for event in source.events()] == [final, empty, final]
        stale = ResponseGeneration(3)
        active = ResponseGeneration(4)
        reply = ReasoningReply(
            generation=stale,
            kind=ReplyKind.CHECK_IN,
            completion=CompletionDecision(CompletionStatus.COMPLETE, CompletionValidity.VALID),
            text="Take your time.",
        )
        llm = FakeReasoningProvider([reply])
        request = ReasoningRequest(7, "q1", "Evidence", active)
        assert await llm.respond(request) is reply
        assert llm.requests == [request]
        with pytest.raises(RuntimeError, match="exhausted"):
            await llm.respond(request)
        audio = SpeechResult(stale, "old-context", b"\x00\x00")
        tts = FakeSpeechProvider([audio])
        speech = SpeechRequest(active, "new-context", "Take your time.")
        assert await tts.synthesize(speech) is audio
        assert tts.requests == [speech]
        with pytest.raises(RuntimeError, match="exhausted"):
            await tts.synthesize(speech)

    asyncio.run(exercise())
