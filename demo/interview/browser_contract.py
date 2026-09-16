"""Public setup and event payloads for the browser interview.

Provider settings belong to server configuration and cannot be supplied through
these models. Event text is rendered as text content by the browser.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from .config import Difficulty, InterviewModel, QuestionRubric
from .gemini_languages import GEMINI_INPUT_LANGUAGES, GEMINI_LANGUAGES
from .languages import INTERVIEW_LANGUAGES, STT_LANGUAGES

BrowserStatus = Literal["Listening", "Giving you time", "Thinking", "Speaking", "Reconnecting"]
BrowserInteractionAction = Literal[
    "repeat",
    "explain",
    "thinking",
    "skip",
    "end",
    "retry_response",
    "retry_transcription",
    "restart_answer",
]
BrowserCommandOutcome = Literal["applied", "rejected", "failed"]


def _strict_int(value: object) -> int:
    """Accept JSON integers while rejecting booleans and coercions."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("must be an integer")
    return value


StrictInt = Annotated[int, BeforeValidator(_strict_int)]


class BrowserInterviewSetup(InterviewModel):
    """Candidate-selected practice settings with a Tanglish default."""

    role: str = Field(default="Behavioural interview", min_length=1, max_length=120)
    difficulty: Difficulty = Difficulty.JUNIOR
    duration_minutes: int = Field(default=5, ge=5, le=180)
    rubric: list[QuestionRubric] = Field(
        default_factory=lambda: [
            QuestionRubric(
                competency="Teamwork",
                guidance="Tell me about a time you worked with others to achieve a shared goal.",
            ),
            QuestionRubric(
                competency="Communication",
                guidance="Tell me about a time you explained something difficult to another person.",
            ),
            QuestionRubric(
                competency="Conflict resolution",
                guidance="Describe a disagreement you handled and how you resolved it.",
            ),
            QuestionRubric(
                competency="Ownership",
                guidance="Tell me about a time you took responsibility for a mistake.",
            ),
            QuestionRubric(
                competency="Resilience",
                guidance="Describe a setback, how you responded, and what you learned.",
            ),
        ],
        min_length=1,
        max_length=12,
    )
    language: str = "tanglish"
    stt_language: str = "auto"
    speech_pace: float = Field(default=0.85, ge=0.5, le=2.0)

    @field_validator("role")
    @classmethod
    def _strip_role(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("role must not be blank")
        return value

    @field_validator("language", "stt_language")
    @classmethod
    def _validate_language_choice(cls, value: str, info) -> str:
        value = value.strip()
        choices = (
            {*INTERVIEW_LANGUAGES, *GEMINI_LANGUAGES}
            if info.field_name == "language"
            else {*STT_LANGUAGES, *GEMINI_INPUT_LANGUAGES, "auto"}
        )
        if value not in choices:
            raise ValueError("unsupported interview language")
        return value


class InterviewBrowserEvent(InterviewModel):
    """Allowlisted application events carried as plain data-channel JSON."""

    v: Literal[1] = 1
    session_id: str = ""
    connection_generation: int = Field(default=1, ge=1)
    seq: int = Field(default=0, ge=0)
    type: Literal[
        "interview.status",
        "interview.caption",
        "interview.reply",
        "interview.interruption",
        "interview.error",
        "interview.ended",
        "interview.command_result",
    ]
    status: BrowserStatus | None = None
    phase: str | None = None
    question_index: int | None = Field(default=None, ge=0)
    question_count: int | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, max_length=16000)
    final: bool | None = None
    playback_epoch: int | None = Field(default=None, ge=0)
    message: str | None = Field(default=None, max_length=500)
    recoverable: bool | None = None
    segment_id: str | None = None
    candidate_turn_id: int | None = None
    dispatch_id: int | None = None
    capabilities: list[Literal["interaction_controls_v1"]] | None = Field(
        default=None, max_length=1
    )
    prompt_revision: StrictInt | None = Field(default=None, ge=0)
    state_revision: StrictInt | None = Field(default=None, ge=0)
    question_text: str | None = Field(default=None, max_length=4_000)
    interaction_state: str | None = Field(default=None, max_length=80)
    question_delivery: Literal["pending", "complete", "unconfirmed"] | None = None
    permitted_actions: list[BrowserInteractionAction] | None = Field(default=None, max_length=8)
    recovery_token: str | None = Field(default=None, min_length=1, max_length=512)
    command_id: StrictInt | None = Field(default=None, ge=1, le=2**53 - 1)
    outcome: BrowserCommandOutcome | None = None
    code: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def _validate_command_result(self) -> InterviewBrowserEvent:
        required = (self.command_id, self.outcome, self.code, self.prompt_revision)
        if self.type == "interview.command_result" and any(value is None for value in required):
            raise ValueError("command results require an ID, outcome, code, and prompt revision")
        return self


class BrowserReady(InterviewModel):
    """The bounded capability advertisement sent on the established data channel."""

    v: Literal[1] = 1
    type: Literal["interview.ready"]
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    capabilities: list[str] | None = Field(default=None, max_length=16)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _capabilities_are_short_strings(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list) or not all(
            isinstance(capability, str) for capability in value
        ):
            raise ValueError("capabilities must be a list of strings")
        if any(not capability or len(capability) > 64 for capability in value):
            raise ValueError("capabilities must be non-empty short strings")
        return value


class BrowserInterviewCommand(InterviewModel):
    """A validated browser control request on its already-bound data channel."""

    v: Literal[1] = 1
    type: Literal["interview.command"]
    session_id: str = Field(min_length=1, max_length=128)
    connection_generation: StrictInt = Field(ge=1)
    command_id: StrictInt = Field(ge=1, le=2**53 - 1)
    prompt_revision: StrictInt = Field(ge=0)
    action: BrowserInteractionAction
    recovery_token: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _validate_recovery_token(self) -> BrowserInterviewCommand:
        recovery_action = self.action in {
            "retry_response",
            "retry_transcription",
            "restart_answer",
        }
        if recovery_action != (self.recovery_token is not None):
            raise ValueError("recovery token is required only for recovery actions")
        return self
