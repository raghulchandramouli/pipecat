"""Deterministic interview-question progression and voice-control state."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from .config import InterviewConfig, QuestionRubric
from .contracts import AcceptedAnswer, ReplyKind
from .interaction_copy import get_copy
from .prompt_languages import LANGUAGES, closing, introduction, question


class InterviewPhase(StrEnum):
    """Question-progression phase visible to the interview application."""

    INTRODUCTION = "introduction"
    QUESTION = "question"
    FOLLOW_UP = "follow_up"
    CLOSING = "closing"
    ENDED = "ended"


class InterviewControl(StrEnum):
    """Voice controls recognized before candidate-answer acceptance."""

    REPEAT = "repeat"
    SKIP = "skip"
    THINKING = "thinking"
    END = "end"


@dataclass(frozen=True)
class InterviewQuestion:
    """One deterministic question derived from a configured rubric item.

    Parameters:
        question_id: Stable controller identity for the rubric item.
        index: Zero-based rubric position.
        competency: Skill area to discuss.
        guidance: Configured question guidance.
        text: Candidate-facing deterministic question text.
    """

    question_id: str
    index: int
    competency: str
    guidance: str
    text: str


@dataclass(frozen=True)
class ControllerPrompt:
    """One controller-owned spoken output held until downstream release.

    Parameters:
        response_id: Controller response identity used for release and invalidation.
        phase: Interview phase represented by this output.
        reply_kind: Allowed spoken-output category.
        text: Clean candidate-facing text.
        question_id: Active question identity, when applicable.
        question_index: Active rubric position, when applicable.
    """

    response_id: int
    phase: InterviewPhase
    reply_kind: ReplyKind
    text: str
    question_id: str | None
    question_index: int | None


class _ReleaseAction(StrEnum):
    """Internal mutation to perform only after the matching output is released."""

    NONE = "none"
    INTRODUCTION_RELEASED = "introduction_released"
    REISSUE_INTRODUCTION = "reissue_introduction"
    FOLLOW_UP_RELEASED = "follow_up_released"
    ADVANCE_AFTER_FOLLOW_UP = "advance_after_follow_up"
    CLOSE = "close"


@dataclass
class _PendingOutput:
    """Output and optional answer mutation retained until release.

    Parameters:
        prompt: Candidate-facing pending output.
        action: State transition performed after delivery.
        answer: Validated answer that becomes accepted after delivery.
        evidence: Evidence excerpts stored with the answer after delivery.
        coaching: Optional internal coaching note retained separately from speech.
    """

    prompt: ControllerPrompt
    action: _ReleaseAction
    answer: AcceptedAnswer | None = None
    evidence: tuple[str, ...] = ()
    coaching: str | None = None


class InterviewController:
    """Advance configured interview questions only after released validated outputs.

    The controller owns deterministic introduction, question, repeat, and closing
    text. A reasoning integration supplies a grounded follow-up or coaching reply
    through :meth:`stage_answer`; its candidate answer remains uncommitted until
    the matching output reaches the downstream release boundary.

    Args:
        config: Validated interview role, difficulty, duration, and rubric.
        session_id: Non-blank application session identity.
        clock: Monotonic clock used to enforce the configured duration.
    """

    def __init__(
        self,
        config: InterviewConfig,
        session_id: str,
        clock: Callable[[], float] = monotonic,
        language: str = "english",
    ) -> None:
        """Initialize an unstarted controller with no accepted answer state.

        Args:
            config: Validated interview configuration.
            session_id: Non-blank application session identity.
            clock: Monotonic clock used for deterministic duration tests.
            language: Candidate-facing prompt language selection.
        """
        if not session_id.strip():
            raise ValueError("session_id must not be blank")
        if language not in LANGUAGES:
            raise ValueError(f"language must be one of: {', '.join(sorted(LANGUAGES))}")
        self.config = config
        self.session_id = session_id
        self._language = language
        self._clock = clock
        self._started_at: float | None = None
        self._phase = InterviewPhase.INTRODUCTION
        self._question_index = 0
        self._expected_candidate_turn_id: int | None = None
        self._accepted_answers: list[AcceptedAnswer] = []
        self._rubric_evidence: dict[str, list[str]] = {}
        self._coaching_notes: list[str] = []
        self._waiting_reason: str | None = None
        self._pending: _PendingOutput | None = None
        self._last_follow_up: str | None = None
        self._active_scenario: str | None = None
        self._scenario_questions: set[str] = set()
        self._prompt_revision = 0
        self._next_response_id = 1
        self._used_response_ids: set[int] = set()

    @property
    def phase(self) -> InterviewPhase:
        """Return the current question-progression phase."""
        return self._phase

    @property
    def current_question(self) -> InterviewQuestion | None:
        """Return the active rubric question, including while output is pending."""
        if self._question_index >= len(self.config.question_rubric):
            return None
        rubric = self.config.question_rubric[self._question_index]
        return InterviewQuestion(
            question_id=f"question-{self._question_index + 1}",
            index=self._question_index,
            competency=rubric.competency,
            guidance=rubric.guidance,
            text=self._question_text(rubric, self._question_index),
        )

    @property
    def current_prompt(self) -> ControllerPrompt | None:
        """Return output that has not yet reached its downstream release boundary."""
        return self._pending.prompt if self._pending is not None else None

    @property
    def prompt_revision(self) -> int:
        """Return the revision of the active candidate-facing prompt."""
        return self._prompt_revision

    @property
    def active_prompt_text(self) -> str | None:
        """Return the question, scenario, or follow-up the candidate should answer."""
        if self._phase is InterviewPhase.FOLLOW_UP:
            return self._last_follow_up
        if self._active_scenario is not None:
            return self._active_scenario
        question = self.current_question
        return question.text if question is not None else None

    @property
    def active_scenario(self) -> str | None:
        """Return the one allowed practice scenario for the current base question."""
        return self._active_scenario

    def offer_scenario(self, text: str) -> ControllerPrompt | None:
        """Offer one practice scenario without accepting an answer or changing identity."""
        question = self.current_question
        if self._phase is not InterviewPhase.QUESTION or question is None:
            return None
        if question.question_id in self._scenario_questions:
            return None
        scenario = text.strip()
        if not scenario:
            raise ValueError("scenario must not be blank")
        self.invalidate_response()
        self._scenario_questions.add(question.question_id)
        self._active_scenario = scenario
        return self._issue(text=scenario, reply_kind=ReplyKind.CHECK_IN, action=_ReleaseAction.NONE)

    def can_offer_scenario(self) -> bool:
        """Return whether the current base question can receive its one scenario."""
        question = self.current_question
        return (
            self._phase is InterviewPhase.QUESTION
            and question is not None
            and question.question_id not in self._scenario_questions
        )

    def offer_help(self, text: str) -> ControllerPrompt | None:
        """Issue a non-answer explanation while retaining the active candidate turn."""
        if self._phase not in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            return None
        message = text.strip()
        if not message:
            raise ValueError("help text must not be blank")
        self.invalidate_response()
        return self._issue(text=message, reply_kind=ReplyKind.CHECK_IN, action=_ReleaseAction.NONE)

    @property
    def accepted_answers(self) -> tuple[AcceptedAnswer, ...]:
        """Return committed accepted answers in interview order."""
        return tuple(self._accepted_answers)

    @property
    def expected_candidate_turn_id(self) -> int | None:
        """Return the candidate owner bound to the currently released question."""
        return self._expected_candidate_turn_id

    @property
    def rubric_evidence(self) -> dict[str, tuple[str, ...]]:
        """Return accepted transcript excerpts grouped by configured competency."""
        return {
            competency: tuple(excerpts) for competency, excerpts in self._rubric_evidence.items()
        }

    @property
    def coaching_notes(self) -> tuple[str, ...]:
        """Return accepted internal coaching annotations without exposing them as spoken text."""
        return tuple(self._coaching_notes)

    @property
    def waiting_reason(self) -> str | None:
        """Return the application-visible wait reason, such as explicit thinking time."""
        return self._waiting_reason

    @property
    def deadline(self) -> float | None:
        """Return the absolute duration deadline after the interview starts."""
        if self._started_at is None:
            return None
        return self._started_at + self.config.duration_minutes * 60

    def start(self) -> ControllerPrompt | None:
        """Start once and return the introduction output awaiting explicit release."""
        if self._started_at is not None:
            return self.current_prompt
        self._started_at = self._clock()
        self._phase = InterviewPhase.INTRODUCTION
        return self._issue(
            text=self._introduction_text(),
            reply_kind=ReplyKind.INTRODUCTION,
            action=_ReleaseAction.INTRODUCTION_RELEASED,
        )

    def expect_candidate_turn(self, candidate_turn_id: int) -> None:
        """Bind the next staged answer to the current candidate-turn snapshot.

        Args:
            candidate_turn_id: Non-negative candidate turn that owns the next answer.
        """
        self._require_started()
        if candidate_turn_id < 0:
            raise ValueError("candidate_turn_id must not be negative")
        if self._phase not in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            raise RuntimeError("candidate turns are not accepted in the current interview phase")
        if self._pending is not None:
            raise RuntimeError("cannot replace the expected candidate while output is pending")
        self._expected_candidate_turn_id = candidate_turn_id
        self._waiting_reason = None

    def stage_answer(
        self,
        answer: AcceptedAnswer,
        response_id: int,
        speak_text: str,
        *,
        evidence: Iterable[str] = (),
        coaching: str | None = None,
    ) -> ControllerPrompt:
        """Stage a validated answer and reply without committing question progression.

        Args:
            answer: Accepted full transcript from the current candidate snapshot.
            response_id: Unique positive response identity provided by the reply guard.
            speak_text: Clean grounded follow-up or coaching text approved for speech.
            evidence: Accepted transcript excerpts supporting later coaching.
            coaching: Optional non-spoken coaching annotation; never used for scoring.

        Returns:
            The pending output that must be passed to :meth:`release` after delivery.
        """
        self._require_started()
        if self._expired():
            self.tick()
            raise RuntimeError("interview duration has expired")
        question = self.current_question
        if (
            self._phase not in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}
            or question is None
        ):
            raise RuntimeError("answers are not accepted in the current interview phase")
        if self._pending is not None:
            raise RuntimeError("cannot stage an answer while output is pending")
        if self._expected_candidate_turn_id is None:
            raise RuntimeError("candidate turn has not been bound to the current question")
        if answer.candidate_turn_id != self._expected_candidate_turn_id:
            raise ValueError("accepted answer belongs to a different candidate turn")
        if answer.question_id != question.question_id:
            raise ValueError("accepted answer belongs to a different question")
        if response_id <= 0:
            raise ValueError("response_id must be positive")
        if response_id in self._used_response_ids:
            raise ValueError("response_id has already been used")
        text = speak_text.strip()
        if not text:
            raise ValueError("speak_text must not be blank")
        excerpts = tuple(excerpt.strip() for excerpt in evidence if excerpt.strip())
        if any(excerpt not in answer.transcript for excerpt in excerpts):
            raise ValueError("evidence excerpts must come from the accepted answer transcript")
        coaching = coaching.strip() if coaching is not None else None
        if coaching == "":
            coaching = None
        self._used_response_ids.add(response_id)
        self._next_response_id = max(self._next_response_id, response_id + 1)
        self._waiting_reason = None
        action = (
            _ReleaseAction.FOLLOW_UP_RELEASED
            if self._phase is InterviewPhase.QUESTION
            else _ReleaseAction.ADVANCE_AFTER_FOLLOW_UP
        )
        return self._issue(
            text=text,
            reply_kind=ReplyKind.FOLLOW_UP,
            action=action,
            response_id=response_id,
            answer=answer,
            evidence=excerpts,
            coaching=coaching,
        )

    def release(self, response_id: int) -> ControllerPrompt | None:
        """Commit exactly one matching pending output and return any next local prompt.

        Args:
            response_id: Output identity acknowledged by the downstream release boundary.

        Returns:
            A deterministic next prompt, if this release advances to one; otherwise None.
        """
        pending = self._pending
        if pending is None or pending.prompt.response_id != response_id:
            return None
        if self._phase is not InterviewPhase.CLOSING and self._expired():
            self._pending = None
            return self._begin_closing()
        self._pending = None
        if pending.answer is not None:
            self._commit_answer(pending.answer, pending.evidence, pending.coaching)
        if pending.action is _ReleaseAction.INTRODUCTION_RELEASED:
            self._phase = InterviewPhase.QUESTION
            return self._issue_question()
        if pending.action is _ReleaseAction.REISSUE_INTRODUCTION:
            return self._issue_introduction()
        if pending.action is _ReleaseAction.FOLLOW_UP_RELEASED:
            self._phase = InterviewPhase.FOLLOW_UP
            self._last_follow_up = pending.prompt.text
            self._expected_candidate_turn_id = None
            return None
        if pending.action is _ReleaseAction.ADVANCE_AFTER_FOLLOW_UP:
            self._expected_candidate_turn_id = None
            self._question_index += 1
            self._active_scenario = None
            if self._question_index < len(self.config.question_rubric):
                self._phase = InterviewPhase.QUESTION
                return self._issue_question()
            self._phase = InterviewPhase.CLOSING
            return self._issue(
                text=self._closing_text(),
                reply_kind=ReplyKind.CLOSING,
                action=_ReleaseAction.CLOSE,
            )
        if pending.action is _ReleaseAction.CLOSE:
            self._phase = InterviewPhase.ENDED
        return None

    def invalidate_response(self, response_id: int | None = None) -> bool:
        """Discard an uncommitted output, optionally only when its identity matches.

        Args:
            response_id: Specific output to invalidate. Omit to discard any pending output.

        Returns:
            Whether an output was discarded.
        """
        if self._pending is None:
            return False
        if response_id is not None and self._pending.prompt.response_id != response_id:
            return False
        self._pending = None
        return True

    def handle_control(self, control: InterviewControl | str) -> ControllerPrompt | None:
        """Apply a control-only voice command without accepting candidate evidence.

        Args:
            control: Repeat, skip, thinking-time, or end command.

        Returns:
            The local output to speak, if the command needs acknowledgement.
        """
        self._require_started()
        control = InterviewControl(control)
        if self._phase is InterviewPhase.ENDED:
            return None
        if self._phase is InterviewPhase.CLOSING:
            return self.current_prompt if control is InterviewControl.END else None
        if control is InterviewControl.END:
            return self._begin_closing()
        if self._expired():
            return self.tick()
        if control is InterviewControl.SKIP:
            return self._skip_question()
        if control is InterviewControl.THINKING:
            self.invalidate_response()
            self._waiting_reason = "thinking"
            return self._issue(
                text=get_copy(self._language, "thinking"),
                reply_kind=ReplyKind.CHECK_IN,
                action=(
                    _ReleaseAction.REISSUE_INTRODUCTION
                    if self._phase is InterviewPhase.INTRODUCTION
                    else _ReleaseAction.NONE
                ),
            )
        return self._repeat_question()

    def tick(self) -> ControllerPrompt | None:
        """Latch duration expiry and prevent all further question prompts.

        Returns:
            The closing prompt when duration has just expired; otherwise None.
        """
        if self._started_at is None or self._phase is InterviewPhase.ENDED:
            return None
        if not self._expired():
            return None
        return self._begin_closing()

    def _issue_question(self) -> ControllerPrompt:
        question = self.current_question
        if question is None:
            raise RuntimeError("cannot issue a question after the rubric is exhausted")
        self._waiting_reason = None
        return self._issue(
            text=question.text,
            reply_kind=ReplyKind.QUESTION,
            action=_ReleaseAction.NONE,
        )

    def _issue_introduction(self) -> ControllerPrompt:
        return self._issue(
            text=self._introduction_text(),
            reply_kind=ReplyKind.INTRODUCTION,
            action=_ReleaseAction.INTRODUCTION_RELEASED,
        )

    def _repeat_question(self) -> ControllerPrompt | None:
        if self._phase is InterviewPhase.CLOSING:
            return self.current_prompt
        if self._phase is InterviewPhase.INTRODUCTION:
            self.invalidate_response()
            return self._issue_introduction()
        question = self.current_question
        if question is None:
            return self._begin_closing()
        self.invalidate_response()
        self._waiting_reason = None
        text = (
            self._last_follow_up
            if self._phase is InterviewPhase.FOLLOW_UP
            else self._active_scenario or question.text
        )
        return self._issue(
            text=text or question.text,
            reply_kind=ReplyKind.REPEAT,
            action=_ReleaseAction.NONE,
        )

    def _skip_question(self) -> ControllerPrompt | None:
        if self._phase not in {InterviewPhase.QUESTION, InterviewPhase.FOLLOW_UP}:
            return None
        self.invalidate_response()
        self._expected_candidate_turn_id = None
        self._waiting_reason = None
        self._question_index += 1
        self._last_follow_up = None
        self._active_scenario = None
        if self._question_index >= len(self.config.question_rubric):
            return self._begin_closing()
        self._phase = InterviewPhase.QUESTION
        return self._issue_question()

    def _begin_closing(self) -> ControllerPrompt | None:
        if self._phase is InterviewPhase.ENDED:
            return None
        if self._phase is InterviewPhase.CLOSING:
            return self.current_prompt
        self.invalidate_response()
        self._expected_candidate_turn_id = None
        self._waiting_reason = None
        self._phase = InterviewPhase.CLOSING
        return self._issue(
            text=self._closing_text(),
            reply_kind=ReplyKind.CLOSING,
            action=_ReleaseAction.CLOSE,
        )

    def _issue(
        self,
        *,
        text: str,
        reply_kind: ReplyKind,
        action: _ReleaseAction,
        response_id: int | None = None,
        answer: AcceptedAnswer | None = None,
        evidence: tuple[str, ...] = (),
        coaching: str | None = None,
    ) -> ControllerPrompt:
        if self._pending is not None:
            raise RuntimeError("only one controller output may be pending")
        response_id = self._allocate_response_id() if response_id is None else response_id
        question = self.current_question
        prompt = ControllerPrompt(
            response_id=response_id,
            phase=self._phase,
            reply_kind=reply_kind,
            text=text,
            question_id=question.question_id if question is not None else None,
            question_index=question.index if question is not None else None,
        )
        self._prompt_revision += 1
        self._pending = _PendingOutput(prompt, action, answer, evidence, coaching)
        return prompt

    def _allocate_response_id(self) -> int:
        while self._next_response_id in self._used_response_ids:
            self._next_response_id += 1
        response_id = self._next_response_id
        self._used_response_ids.add(response_id)
        self._next_response_id += 1
        return response_id

    def _commit_answer(
        self,
        answer: AcceptedAnswer,
        evidence: tuple[str, ...],
        coaching: str | None,
    ) -> None:
        self._accepted_answers.append(answer)
        if evidence:
            competency = self.config.question_rubric[self._question_index].competency
            self._rubric_evidence.setdefault(competency, []).extend(evidence)
        if coaching is not None:
            self._coaching_notes.append(coaching)

    def _introduction_text(self) -> str:
        text = introduction(self._language)
        if self._language == "english":
            focus = f" focused on {self.config.role.focus}" if self.config.role.focus else ""
            return (
                f"{text} This is a {self.config.duration_minutes}-minute {self.config.difficulty.value} "
                f"practice interview for {self.config.role.title}{focus}."
            )
        return text

    def _question_text(self, rubric: QuestionRubric, index: int) -> str:
        if self._language == "hinglish":
            questions = {
                "teamwork": "क्या आपने कभी किसी का काम पूरा करने में help की है? उसके बारे में बताइए।",
                "communication": "अगर किसी को आपकी बात समझ नहीं आए, तो आप कैसे समझाएँगे?",
                "conflict resolution": "अगर कोई आपसे गुस्से में बात करे, तो आप क्या करेंगे?",
                "ownership": "अगर काम करते समय आपसे गलती हो जाए, तो आप क्या करेंगे?",
                "resilience": "अगर कोई काम मुश्किल लगे, तो आप क्या करेंगे?",
            }
            if rubric.competency.casefold() in questions:
                return questions[rubric.competency.casefold()]
        prompt = question(self._language, rubric.competency)
        if self._language == "english" and rubric.competency.casefold() not in {
            "teamwork",
            "communication",
            "conflict resolution",
            "ownership",
            "resilience",
        }:
            return (
                f"Question {index + 1} of {len(self.config.question_rubric)} for this "
                f"{self.config.difficulty.value} {self.config.role.title} interview. {prompt}"
            )
        return prompt

    def _closing_text(self) -> str:
        return closing(self._language)

    def _expired(self) -> bool:
        deadline = self.deadline
        return deadline is not None and self._clock() >= deadline

    def _require_started(self) -> None:
        if self._started_at is None:
            raise RuntimeError("start the interview before changing controller state")
