"""Pure coordinated pause and thinking-time policy for interview turns."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from .config import InterviewDeadlines
from .contracts import ReplyKind


class PolicyActionKind(StrEnum):
    """Action requested by the policy after a consumable timer expires."""

    SEMANTIC_PROBE = "semantic_probe"
    CHECK_IN = "check_in"


@dataclass(frozen=True)
class PolicyAction:
    """One controller action requested by the turn policy.

    Parameters:
        kind: Semantic probe or spoken check-in action category.
        reason: Stable timer reason that caused the action.
        reply_kind: Controller reply kind allowed for this action.
    """

    kind: PolicyActionKind
    reason: str
    reply_kind: ReplyKind


class TurnPolicy:
    """Single-owner timing policy for one candidate speech turn.

    The policy emits no provider calls and does not inspect transcript readiness.
    Its caller combines ``can_dispatch`` with the transcript ledger, then runs a
    managed wake-up task at ``next_deadline``. Each action is consumed by
    ``poll`` so a pending transcript cannot cause a hot loop.

    Args:
        deadlines: Validated interview deadline configuration.
        clock: Monotonic clock used for deterministic timer decisions.
    """

    def __init__(self, *, deadlines: InterviewDeadlines, clock: Callable[[], float] = monotonic):
        """Initialize a policy with no active candidate speech timing.

        Args:
            deadlines: Validated interview deadline configuration.
            clock: Monotonic clock used by event methods without explicit time.
        """
        self._deadlines = deadlines
        self._clock = clock
        self._speaking = False
        self._response_pending = False
        self._stopped_at: float | None = None
        self._initial_probe_consumed = False
        self._idle_consumed = False
        self._watchdog_consumed = False
        self._retry_at: float | None = None
        self._retry_reason: str | None = None
        self._thinking_requested_at: float | None = None
        self._thinking_deadline: float | None = None

    @property
    def next_deadline(self) -> float | None:
        """Return the next managed-task wake-up deadline, if any."""
        if self._speaking or self._response_pending or self._stopped_at is None:
            return None
        if self._thinking_deadline is not None:
            return max(
                self._thinking_deadline,
                self._stopped_at + self._deadlines.candidate_pause,
            )
        if self._retry_at is not None:
            return self._retry_at
        deadlines: list[float] = []
        if not self._initial_probe_consumed:
            deadlines.append(self._stopped_at + self._deadlines.candidate_pause)
        if not self._idle_consumed:
            deadlines.append(self._stopped_at + self._deadlines.user_idle_timeout)
        if not self._watchdog_consumed:
            deadlines.append(self._stopped_at + self._deadlines.user_turn_stop_timeout)
        return min(deadlines, default=None)

    def speech_started(self) -> None:
        """Invalidate all pending deadlines when the candidate resumes speech."""
        self._speaking = True
        self._response_pending = False
        self._stopped_at = None
        self._initial_probe_consumed = False
        self._idle_consumed = False
        self._watchdog_consumed = False
        self._retry_at = None
        self._retry_reason = None
        self._thinking_requested_at = None
        self._thinking_deadline = None

    def begin_waiting(self) -> None:
        """Arm idle notification after a question, without inventing a candidate answer."""
        if self._speaking:
            raise ValueError("cannot begin waiting while the candidate is speaking")
        self._response_pending = False
        self._stopped_at = self._clock()
        self._initial_probe_consumed = True
        self._idle_consumed = False
        self._watchdog_consumed = False
        self._retry_at = None
        self._retry_reason = None
        self._thinking_requested_at = None
        self._thinking_deadline = None

    def speech_stopped(self) -> None:
        """Start the pause floor and non-semantic quiet-time deadlines."""
        if not self._speaking and self._stopped_at is not None:
            return
        self._speaking = False
        self._response_pending = False
        self._stopped_at = self._clock()
        self._initial_probe_consumed = False
        self._idle_consumed = False
        self._watchdog_consumed = False
        self._retry_at = None
        self._retry_reason = None

    def request_thinking(self, requested_at: float | None = None) -> None:
        """Suppress competing timers until the explicit thinking grace expires.

        A repeated request without a newer explicit timestamp leaves the existing
        deadline intact. A distinct later request starts a new grace interval.

        Args:
            requested_at: Monotonic timestamp of the explicit request.
        """
        requested_at = self._clock() if requested_at is None else requested_at
        if self._thinking_requested_at is not None and requested_at <= self._thinking_requested_at:
            return
        self._thinking_requested_at = requested_at
        self._thinking_deadline = requested_at + self._deadlines.thinking_grace
        self._response_pending = False
        self._retry_at = None
        self._retry_reason = None

    def incomplete(self, *, long: bool = False) -> None:
        """Schedule a semantic retry from an incomplete completion verdict.

        Args:
            long: Whether to use the long retry interval.
        """
        if self._speaking or self._thinking_deadline is not None:
            return
        self._response_pending = False
        delay = (
            self._deadlines.incomplete_long_retry
            if long
            else self._deadlines.incomplete_short_retry
        )
        self._retry_at = self._clock() + delay
        self._retry_reason = "incomplete_long_retry" if long else "incomplete_short_retry"

    def poll(self) -> PolicyAction | None:
        """Consume and return one currently due policy action, if any."""
        now = self._clock()
        if self._speaking or self._response_pending or self._stopped_at is None:
            return None
        if self._thinking_deadline is not None:
            effective_deadline = max(
                self._thinking_deadline,
                self._stopped_at + self._deadlines.candidate_pause,
            )
            if now < effective_deadline:
                return None
            self._thinking_deadline = None
            self._thinking_requested_at = None
            self._initial_probe_consumed = True
            self._idle_consumed = True
            self._watchdog_consumed = True
            return PolicyAction(
                PolicyActionKind.CHECK_IN, "thinking_grace_expired", ReplyKind.CHECK_IN
            )
        if self._retry_at is not None:
            if now < self._retry_at:
                return None
            reason = self._retry_reason
            self._retry_at = None
            self._retry_reason = None
            self._consume_overdue_check_ins(now)
            return PolicyAction(
                PolicyActionKind.CHECK_IN, reason or "incomplete_retry", ReplyKind.CHECK_IN
            )
        if (
            not self._initial_probe_consumed
            and now >= self._stopped_at + self._deadlines.candidate_pause
        ):
            self._initial_probe_consumed = True
            self._consume_overdue_check_ins(now)
            return PolicyAction(
                PolicyActionKind.SEMANTIC_PROBE, "pause_grace_elapsed", ReplyKind.FOLLOW_UP
            )
        if not self._idle_consumed and now >= self._stopped_at + self._deadlines.user_idle_timeout:
            self._idle_consumed = True
            self._consume_overdue_check_ins(now)
            return PolicyAction(PolicyActionKind.CHECK_IN, "user_idle_timeout", ReplyKind.CHECK_IN)
        if (
            not self._watchdog_consumed
            and now >= self._stopped_at + self._deadlines.user_turn_stop_timeout
        ):
            self._watchdog_consumed = True
            return PolicyAction(
                PolicyActionKind.CHECK_IN, "user_turn_stop_timeout", ReplyKind.CHECK_IN
            )
        return None

    def can_dispatch(self, now: float | None = None) -> bool:
        """Return whether the pause floor and thinking grace permit a dispatch attempt.

        Transcript readiness remains a separate mandatory gate.

        Args:
            now: Optional monotonic timestamp; defaults to the configured clock.
        """
        now = self._clock() if now is None else now
        return (
            not self._speaking
            and self._stopped_at is not None
            and self._thinking_deadline is None
            and now >= self._stopped_at + self._deadlines.candidate_pause
        )

    def dispatched(self) -> None:
        """Suspend timers while a validated response is pending or being delivered."""
        self._response_pending = True
        self._retry_at = None
        self._retry_reason = None
        self._thinking_deadline = None
        self._thinking_requested_at = None

    def response_completed(self) -> None:
        """Finish a response without scheduling another reply for the same silence."""
        self._response_pending = False
        self._stopped_at = None
        self._initial_probe_consumed = False
        self._idle_consumed = False
        self._watchdog_consumed = False
        self._retry_at = None
        self._retry_reason = None
        self._thinking_deadline = None
        self._thinking_requested_at = None

    def _consume_overdue_check_ins(self, now: float) -> None:
        assert self._stopped_at is not None
        if now >= self._stopped_at + self._deadlines.user_idle_timeout:
            self._idle_consumed = True
        if now >= self._stopped_at + self._deadlines.user_turn_stop_timeout:
            self._watchdog_consumed = True
