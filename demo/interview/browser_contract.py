"""Public setup and event payloads for the browser interview.

Provider settings belong to server configuration and cannot be supplied through
these models. Event text is rendered as text content by the browser.
"""

from typing import Literal

from pydantic import Field, field_validator

from .config import Difficulty, InterviewModel, QuestionRubric

BrowserStatus = Literal["Listening", "Giving you time", "Thinking", "Speaking", "Reconnecting"]


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
    language: Literal["tanglish", "hinglish", "english"] = "tanglish"

    @field_validator("role")
    @classmethod
    def _strip_role(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("role must not be blank")
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
