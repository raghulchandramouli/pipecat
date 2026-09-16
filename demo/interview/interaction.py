"""Typed conversational-interaction contracts and source provenance.

The interview controller remains the authority for accepting an answer.  This
module only describes what an already-authorized reply may do with immutable
candidate text, so help can narrow an authorization but never broaden one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import AnswerBasis


class InteractionAction(StrEnum):
    """Actions accepted by the session ingress."""

    REPEAT = "repeat"
    EXPLAIN = "explain"
    THINKING = "thinking"
    SKIP = "skip"
    END = "end"
    RETRY_RESPONSE = "retry_response"
    RETRY_TRANSCRIPTION = "retry_transcription"
    RESTART_ANSWER = "restart_answer"


class InteractionKind(StrEnum):
    """A bounded disposition for one complete candidate turn."""

    ANSWER = "answer"
    EXPLAIN = "explain"
    NO_EXAMPLE = "no_example"
    CLARIFY = "clarify"
    MIXED_HELP = "mixed_help"


class SourceDisposition(StrEnum):
    """One non-overlapping interpretation of a source range."""

    ANSWER = "answer"
    HELP = "help"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class RecoveryGrant:
    """Server-issued recovery capability bound to one immutable failure view."""

    token: str
    connection_generation: int
    candidate_turn_id: int | None
    source_revision: int
    purpose: str
    failed_dispatch_id: int | None = None


class SourceSpan(BaseModel):
    """A half-open range in the immutable serialized candidate source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    disposition: SourceDisposition

    @model_validator(mode="after")
    def _ordered(self) -> SourceSpan:
        if self.end <= self.start:
            raise ValueError("source span end must be after start")
        return self


class InteractionReply(BaseModel):
    """Validated model-shaped interaction reply before controller mutation.

    ``spans`` use Python character offsets into the exact current source.  The
    application validates their coverage and text before staging evidence.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: InteractionKind = InteractionKind.ANSWER
    speak: str = Field(min_length=1, max_length=1_000)
    spans: tuple[SourceSpan, ...] = ()
    scenario: str | None = Field(default=None, max_length=1_000)

    @field_validator("scenario")
    @classmethod
    def _scenario_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("scenario must not be blank")
        return value


def validate_source_spans(source: str, spans: tuple[SourceSpan, ...]) -> tuple[SourceSpan, ...]:
    """Validate linear complete source coverage without rewriting candidate text."""
    if len(source.encode("utf-8")) > 32 * 1024:
        raise ValueError("candidate source exceeds 32 KiB")
    if len(spans) > 512:
        raise ValueError("too many source disposition spans")
    if not spans:
        return ()
    previous = 0
    for span in spans:
        if span.start != previous or span.end > len(source):
            raise ValueError("source spans must be ordered, in bounds, and contiguous")
        previous = span.end
    if previous != len(source):
        raise ValueError("source spans must cover the complete candidate source")
    return spans


def answer_text_from_spans(source: str, spans: tuple[SourceSpan, ...]) -> str:
    """Return original-order answer material and reject uncertain classifications."""
    validate_source_spans(source, spans)
    if any(span.disposition is SourceDisposition.UNCERTAIN for span in spans):
        raise ValueError("uncertain source spans require clarification")
    return " ".join(
        source[span.start : span.end].strip()
        for span in spans
        if span.disposition is SourceDisposition.ANSWER
    ).strip()
