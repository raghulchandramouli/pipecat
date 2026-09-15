"""Deterministic timing tests for interview turn policy."""

from demo.interview.config import InterviewDeadlines
from demo.interview.contracts import ReplyKind
from demo.interview.turn_policy import PolicyActionKind, TurnPolicy


class Clock:
    """Controllable monotonic clock for policy tests."""

    def __init__(self) -> None:
        """Start at the zero point used by each test."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the currently selected monotonic time."""
        return self.now


def policy(clock: Clock) -> TurnPolicy:
    """Create a policy using prototype timing defaults."""
    return TurnPolicy(deadlines=InterviewDeadlines(), clock=clock)


def test_pause_floor_ignores_early_final_and_short_hesitations() -> None:
    """No timer action occurs during repeated 0.5 to 2-second candidate pauses."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    for pause in (0.5, 1.0, 2.0):
        clock.now = pause
        assert turns.poll() is None
        assert not turns.can_dispatch()
    clock.now = 2.5
    action = turns.poll()
    assert action and action.kind is PolicyActionKind.SEMANTIC_PROBE
    assert action.reply_kind is ReplyKind.FOLLOW_UP
    assert turns.poll() is None


def test_incomplete_retry_suppresses_idle_and_watchdog_until_probe() -> None:
    """An incomplete verdict owns the next action even when other timers are overdue."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    turns.incomplete()
    assert turns.next_deadline == 10.5
    clock.now = 45.0
    action = turns.poll()
    assert action and action.reason == "incomplete_short_retry"
    assert action.kind is PolicyActionKind.CHECK_IN
    assert action.reply_kind is ReplyKind.CHECK_IN
    assert turns.poll() is None


def test_thinking_suppresses_collisions_then_emits_one_check_in() -> None:
    """Thinking grace is anchored to the request and clears stale competing timers."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    turns.incomplete(long=True)
    clock.now = 3.0
    turns.request_thinking()
    turns.request_thinking()
    assert turns.next_deadline == 33.0
    clock.now = 32.9
    assert turns.poll() is None
    clock.now = 33.0
    action = turns.poll()
    assert action and action.reason == "thinking_grace_expired"
    assert action.reply_kind is ReplyKind.CHECK_IN
    assert turns.poll() is None


def test_idle_and_watchdog_are_consumable_check_ins() -> None:
    """Idle and watchdog timers never request a substantive follow-up."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    clock.now = 15.0
    idle = turns.poll()
    assert idle and idle.reason == "user_idle_timeout" and idle.reply_kind is ReplyKind.CHECK_IN
    clock.now = 45.0
    watchdog = turns.poll()
    assert watchdog and watchdog.reason == "user_turn_stop_timeout"
    assert watchdog.reply_kind is ReplyKind.CHECK_IN
    assert turns.poll() is None


def test_speech_resume_cancels_thinking_and_retry_deadlines() -> None:
    """A resumed candidate turn invalidates stale policy work and starts fresh timing."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    turns.incomplete()
    turns.request_thinking(requested_at=3.0)
    turns.speech_started()
    assert turns.next_deadline is None
    clock.now = 40.0
    assert turns.poll() is None
    turns.speech_stopped()
    assert turns.next_deadline == 42.5


def test_incomplete_after_dispatched_keeps_original_pause_baseline() -> None:
    """An incomplete verdict can replace an in-flight generation with its retry timer."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    turns.dispatched()
    assert turns.can_dispatch()
    turns.incomplete()
    assert turns.next_deadline == 10.5


def test_completed_response_cancels_quiet_timers_until_new_speech() -> None:
    """A valid completed response cannot trigger another prompt in the same silence."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.speech_stopped()
    clock.now = 2.5
    assert turns.poll() is not None
    turns.dispatched()
    turns.response_completed()
    assert turns.next_deadline is None


def test_thinking_expiry_waits_for_a_late_vad_stop_pause_floor() -> None:
    """An already expired thinking request cannot bypass a later VAD stop grace."""
    clock = Clock()
    turns = policy(clock)
    turns.speech_started()
    turns.request_thinking(requested_at=0.0)
    clock.now = 40.0
    turns.speech_stopped()
    assert turns.next_deadline == 42.5
    assert turns.poll() is None
    assert not turns.can_dispatch()
    clock.now = 42.5
    action = turns.poll()
    assert action and action.reason == "thinking_grace_expired"
    assert turns.can_dispatch()
