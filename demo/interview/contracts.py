"""Internal state contracts for the conversational interview prototype.

These dataclasses deliberately model transcript ownership separately from reply
generation. Future controller and provider code can discard an interrupted reply
without losing late final transcript events for the candidate's pending answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True, order=True)
class SegmentId:
    """Identity assigned to one provider utterance on one STT connection.

    Parameters:
        connection_generation: Monotonic STT connection generation.
        utterance_idx: Provider utterance index within the connection.
    """

    connection_generation: int
    utterance_idx: int

    def __post_init__(self) -> None:
        if self.connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if self.utterance_idx < 0:
            raise ValueError("utterance_idx must not be negative")


class SegmentFinalOutcome(StrEnum):
    """Terminal outcome of a closed Sarvam speech segment."""

    TEXT = "text"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True)
class SegmentFinal:
    """The terminal result for a segment, including an explicit empty result.

    Parameters:
        segment_id: Provider segment that reached a terminal outcome.
        candidate_turn_id: Candidate turn that owns the segment.
        outcome: Terminal provider result category.
        text: Final transcript when ``outcome`` is ``TEXT``.
        error: Failure description when ``outcome`` is ``FAILED``.
    """

    segment_id: SegmentId
    candidate_turn_id: int
    outcome: SegmentFinalOutcome
    text: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        text = self.text.strip()
        if self.outcome is SegmentFinalOutcome.TEXT and not text:
            raise ValueError("text final outcomes require transcript text")
        if self.outcome is not SegmentFinalOutcome.TEXT and text:
            raise ValueError("only text final outcomes may carry transcript text")
        if self.outcome is SegmentFinalOutcome.FAILED and not self.error:
            raise ValueError("failed final outcomes require an error description")
        if self.outcome is not SegmentFinalOutcome.FAILED and self.error is not None:
            raise ValueError("only failed final outcomes may carry an error description")
        object.__setattr__(self, "text", text)


class CompletionStatus(StrEnum):
    """Completeness signalled by a response or completion probe."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING = "missing"
    MALFORMED = "malformed"


class CompletionValidity(StrEnum):
    """Whether the controller may act on the completion status."""

    VALID = "valid"
    STALE = "stale"


@dataclass(frozen=True)
class CompletionDecision:
    """Parsed completion status paired with its response-generation validity.

    Parameters:
        status: Completion signal extracted from a response.
        validity: Whether the controller may act on that signal.
    """

    status: CompletionStatus
    validity: CompletionValidity


class ReplyKind(StrEnum):
    """Controller-approved kinds of text that may be sent to speech output."""

    INTRODUCTION = "introduction"
    QUESTION = "question"
    FOLLOW_UP = "follow_up"
    CHECK_IN = "check_in"
    REPEAT = "repeat"
    CLOSING = "closing"


class AnswerBasis(StrEnum):
    """Origin of accepted interview material."""

    REPORTED_EXPERIENCE = "reported_experience"
    HYPOTHETICAL = "hypothetical"


@dataclass(frozen=True, order=True)
class ResponseGeneration:
    """Monotonic identifier for one Gemini/Rumik response attempt.

    Parameters:
        value: Non-negative response attempt number.
    """

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("response generation must not be negative")


@dataclass
class PendingAnswer:
    """Candidate-owned answer being assembled before validated acceptance.

    ``candidate_turn_id`` and ``segment_ids`` persist when a bot reply is
    invalidated. The controller owns mutation and records each segment's terminal
    result in ``finals``.

    Parameters:
        candidate_turn_id: Candidate turn that owns this answer.
        question_id: Interview question being answered.
        segment_ids: Segments attributed to the candidate turn.
        finals: Terminal segment outcomes recorded by the controller.
    """

    candidate_turn_id: int
    question_id: str
    segment_ids: list[SegmentId] = field(default_factory=list)
    finals: dict[SegmentId, SegmentFinal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if not self.question_id.strip():
            raise ValueError("question_id must not be blank")


@dataclass(frozen=True)
class AcceptedAnswer:
    """A validated complete answer eligible for interview progression.

    Parameters:
        candidate_turn_id: Candidate turn that produced the answer.
        question_id: Interview question answered.
        segment_ids: Final segments that compose the answer.
        transcript: Validated final transcript text.
        basis: Whether the answer reports experience or answers a practice scenario.
    """

    candidate_turn_id: int
    question_id: str
    segment_ids: tuple[SegmentId, ...]
    transcript: str
    basis: AnswerBasis = AnswerBasis.REPORTED_EXPERIENCE

    def __post_init__(self) -> None:
        if self.candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if not self.question_id.strip():
            raise ValueError("question_id must not be blank")
        if not self.segment_ids:
            raise ValueError("accepted answers require at least one segment")
        if not self.transcript.strip():
            raise ValueError("accepted answers require transcript text")


class SessionPhase(StrEnum):
    """Externally meaningful lifecycle state of an interview session."""

    CONNECTING = "connecting"
    LISTENING = "listening"
    THINKING = "thinking"
    GENERATING = "generating"
    SPEAKING = "speaking"
    RECOVERING = "recovering"
    CLOSING = "closing"
    ENDED = "ended"


@dataclass
class InterviewSessionState:
    """Controller-owned state for a single candidate interview session.

    Parameters:
        session_id: Stable application identifier for the interview.
        connection_generation: Current STT connection generation.
        question_id: Current interview question, if one is active.
        question_index: Zero-based position of the current question.
        phase: Current session lifecycle phase.
        pending_answer: Candidate answer awaiting validated acceptance.
        accepted_answers: Accepted answers in interview order.
        active_response_generation: Reply generation eligible to reach output.
        active_deadlines: Named monotonic-clock deadlines owned by the controller.
        rubric_evidence: Transcript excerpts grouped by rubric competency.
        waiting_reason: Reason the controller is waiting, if applicable.
    """

    session_id: str
    connection_generation: int = 0
    question_id: str | None = None
    question_index: int = 0
    phase: SessionPhase = SessionPhase.CONNECTING
    pending_answer: PendingAnswer | None = None
    accepted_answers: list[AcceptedAnswer] = field(default_factory=list)
    active_response_generation: ResponseGeneration | None = None
    active_deadlines: dict[str, float] = field(default_factory=dict)
    rubric_evidence: dict[str, list[str]] = field(default_factory=dict)
    waiting_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be blank")
        if self.connection_generation < 0:
            raise ValueError("connection_generation must not be negative")
        if self.question_index < 0:
            raise ValueError("question_index must not be negative")
