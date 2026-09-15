"""Strict streamed-reply validation before text reaches a TTS service."""

from __future__ import annotations

import pytest

from demo.interview.contracts import CompletionStatus, ReplyKind
from demo.interview.reply_guard import (
    ReplyAuthorization,
    ReplyGuardProcessor,
    parse_completion,
)
from pipecat.frames.frames import (
    ErrorFrame,
    FunctionCallsStartedFrame,
    HeartbeatFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StopFrame,
    TTSSpeakFrame,
    UserSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager
from tests.frame_processor_helpers import frame_processor_setup


def _authorization(
    kind: ReplyKind = ReplyKind.FOLLOW_UP, *, answer_allowed: bool = True
) -> ReplyAuthorization:
    return ReplyAuthorization(4, 9, 7, kind, 12, answer_allowed)


def _guard(
    *,
    current=lambda _auth: True,
    kind: ReplyKind = ReplyKind.FOLLOW_UP,
    answer_allowed: bool = True,
):
    authorization = _authorization(kind, answer_allowed=answer_allowed)
    guard = ReplyGuardProcessor(
        lookup_authorization=lambda metadata: (
            authorization if metadata.get("dispatch") == 9 else None
        ),
        is_current=current,
    )
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    guard.push_frame = capture
    return guard, pushed


async def _response(guard, *chunks: str, metadata=None):
    frame_metadata = metadata or {"dispatch": 9, "interview_response_token": 4}
    start = LLMFullResponseStartFrame()
    start.metadata.update(frame_metadata)
    await guard.process_frame(start, FrameDirection.DOWNSTREAM)
    for chunk in chunks:
        text = LLMTextFrame(chunk)
        text.metadata.update(frame_metadata)
        await guard.process_frame(text, FrameDirection.DOWNSTREAM)
    end = LLMFullResponseEndFrame()
    end.metadata.update(frame_metadata)
    await guard.process_frame(end, FrameDirection.DOWNSTREAM)


@pytest.mark.parametrize(
    ("text", "status", "spoken", "long_wait"),
    [
        ("● Explain the tradeoff", CompletionStatus.COMPLETE, "Explain the tradeoff", False),
        (" ◐ ", CompletionStatus.INCOMPLETE, "", False),
        ("○", CompletionStatus.INCOMPLETE, "", True),
        ("", CompletionStatus.MISSING, "", False),
        ("●", CompletionStatus.MALFORMED, "", False),
        ("●no boundary", CompletionStatus.MALFORMED, "", False),
        ("● answer ◐", CompletionStatus.MALFORMED, "", False),
    ],
)
def test_parse_completion_is_strict_at_the_marker_boundary(text, status, spoken, long_wait):
    """Only the three documented marker shapes create controller decisions."""
    parsed = parse_completion(text)
    assert (parsed.status, parsed.text, parsed.long_wait, parsed.is_valid) == (
        status,
        spoken,
        long_wait,
        status not in {CompletionStatus.MISSING, CompletionStatus.MALFORMED},
    )


@pytest.mark.asyncio
async def test_chunked_complete_reply_releases_clean_frames_and_authorized_decision():
    """A valid buffered completion emits only sanitized speech frames."""
    guard, pushed = _guard()
    decisions = []

    @guard.event_handler("on_decision")
    def decided(_guard, decision):
        decisions.append(decision)

    await _response(guard, "● A", " complete answer")
    assert [type(frame) for frame, _ in pushed] == [
        LLMFullResponseStartFrame,
        LLMTextFrame,
        LLMFullResponseEndFrame,
    ]
    assert pushed[1][0].text == "A complete answer"
    assert all(frame.metadata["dispatch"] == 9 for frame, _ in pushed)
    assert decisions[0].accept_answer is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["plain text", "●", "◐ extra", "● good ● mixed"])
async def test_missing_malformed_or_mixed_markers_never_emit_speech_or_decision(reply):
    """Ambiguous marker streams cannot reach speech or mutate controller policy."""
    guard, pushed = _guard()
    decisions = []

    @guard.event_handler("on_decision")
    def decided(_guard, decision):
        decisions.append(decision)

    await _response(guard, reply)
    assert pushed == []
    assert decisions == []


@pytest.mark.asyncio
async def test_incomplete_emits_retry_decision_without_speech_and_checkin_never_accepts_answer():
    """Incomplete and policy-disallowed replies remain visible only to the controller."""
    guard, pushed = _guard()
    decisions = []

    @guard.event_handler("on_decision")
    def decided(_guard, decision):
        decisions.append(decision)

    await _response(guard, "○")
    assert pushed == []
    assert decisions[0].completion.long_wait is True
    assert decisions[0].accept_answer is False

    checkin, spoken = _guard(kind=ReplyKind.CHECK_IN)
    checkin_decisions = []

    @checkin.event_handler("on_decision")
    def checkin_decided(_guard, decision):
        checkin_decisions.append(decision)

    await _response(checkin, "● Take your time.")
    assert [frame.text for frame, _ in spoken if isinstance(frame, LLMTextFrame)] == [
        "Take your time."
    ]
    assert checkin_decisions[0].accept_answer is False

    disallowed, _spoken = _guard(answer_allowed=False)
    disallowed_decisions = []

    @disallowed.event_handler("on_decision")
    def disallowed_decided(_guard, decision):
        disallowed_decisions.append(decision)

    await _response(disallowed, "● Valid but policy-disallowed")
    assert disallowed_decisions[0].accept_answer is False


@pytest.mark.asyncio
async def test_stale_missing_auth_interruption_and_direct_tts_are_fail_closed():
    """Unauthorized streams and direct speech bypass attempts are suppressed."""
    stale, stale_pushed = _guard(current=lambda _auth: False)
    await _response(stale, "● stale")
    assert stale_pushed == []

    missing, missing_pushed = _guard()
    await _response(missing, "● unauthenticated", metadata={"dispatch": 99})
    assert missing_pushed == []

    guard, pushed = _guard()
    await guard.setup(frame_processor_setup(TaskManager()))
    try:
        await guard.process_frame(TTSSpeakFrame("bypass"), FrameDirection.DOWNSTREAM)
        await guard.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await guard.process_frame(LLMTextFrame("● abandoned"), FrameDirection.DOWNSTREAM)
        await guard.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await guard.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        assert [type(frame) for frame, _ in pushed] == [InterruptionFrame]
    finally:
        await guard.cleanup()


@pytest.mark.asyncio
async def test_missing_start_and_response_limit_are_suppressed_without_decision():
    """Orphan chunks and oversized replies cannot consume unbounded memory or reach TTS."""
    guard, pushed = _guard()
    decisions = []

    @guard.event_handler("on_decision")
    def decided(_guard, decision):
        decisions.append(decision)

    await guard.process_frame(LLMTextFrame("● orphan"), FrameDirection.DOWNSTREAM)
    await guard.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    assert pushed == decisions == []

    authorization = _authorization()
    limited = ReplyGuardProcessor(
        lookup_authorization=lambda _metadata: authorization,
        is_current=lambda _auth: True,
        max_response_chars=4,
    )
    limited_pushed = []
    limited_decisions = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        limited_pushed.append((frame, direction))

    limited.push_frame = capture

    @limited.event_handler("on_decision")
    def limited_decided(_guard, decision):
        limited_decisions.append(decision)

    await _response(limited, "● too long")
    assert limited_pushed == limited_decisions == []


@pytest.mark.asyncio
async def test_interleaved_old_end_and_error_cannot_complete_another_response():
    """Every chunk and end is tied to its start authorization before it can affect a buffer."""
    first = _authorization()
    second = ReplyAuthorization(5, 10, 7, ReplyKind.FOLLOW_UP, 13, True)
    third = ReplyAuthorization(6, 11, 7, ReplyKind.FOLLOW_UP, 14, True)
    fourth = ReplyAuthorization(7, 12, 7, ReplyKind.FOLLOW_UP, 15, True)
    guard = ReplyGuardProcessor(
        lookup_authorization=lambda metadata: {9: first, 10: second, 11: third, 12: fourth}.get(
            metadata.get("dispatch")
        ),
        is_current=lambda _auth: True,
    )
    pushed, rejected = [], []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    guard.push_frame = capture

    @guard.event_handler("on_rejected")
    def rejected_event(_guard, rejection):
        rejected.append(rejection.reason)

    def frame(cls, dispatch, text=None):
        value = cls(text) if text is not None else cls()
        value.metadata["dispatch"] = dispatch
        return value

    await guard.process_frame(frame(LLMFullResponseStartFrame, 9), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMTextFrame, 9, "● old"), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMFullResponseStartFrame, 10), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMFullResponseEndFrame, 9), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMTextFrame, 10, "● new"), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMFullResponseEndFrame, 10), FrameDirection.DOWNSTREAM)
    assert [item.text for item, _ in pushed if isinstance(item, LLMTextFrame)] == ["new"]
    assert "mismatched_end" in rejected

    pushed.clear()
    await guard.process_frame(frame(LLMFullResponseStartFrame, 10), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMTextFrame, 10, "● truncated"), FrameDirection.DOWNSTREAM)
    await guard.process_frame(ErrorFrame(error="provider failure"), FrameDirection.DOWNSTREAM)
    await guard.process_frame(frame(LLMFullResponseEndFrame, 10), FrameDirection.DOWNSTREAM)
    assert [type(item) for item, _ in pushed] == [ErrorFrame]


@pytest.mark.asyncio
async def test_stale_start_does_not_discard_an_active_current_response():
    """A late stale start is ignored so its stream cannot erase current buffered text."""
    guard, pushed = _guard()
    current_start = LLMFullResponseStartFrame()
    current_start.metadata["dispatch"] = 9
    await guard.process_frame(current_start, FrameDirection.DOWNSTREAM)
    current_text = LLMTextFrame("● retained")
    current_text.metadata["dispatch"] = 9
    await guard.process_frame(current_text, FrameDirection.DOWNSTREAM)
    stale_start = LLMFullResponseStartFrame()
    stale_start.metadata["dispatch"] = 404
    await guard.process_frame(stale_start, FrameDirection.DOWNSTREAM)
    current_end = LLMFullResponseEndFrame()
    current_end.metadata["dispatch"] = 9
    await guard.process_frame(current_end, FrameDirection.DOWNSTREAM)
    assert [frame.text for frame, _ in pushed if isinstance(frame, LLMTextFrame)] == ["retained"]


@pytest.mark.asyncio
async def test_replay_duplicate_start_and_provider_errors_cannot_finish_wrong_response():
    """Replays and stale errors cannot create duplicate speech or poison a current stream."""
    first = _authorization()
    second = ReplyAuthorization(5, 10, 7, ReplyKind.FOLLOW_UP, 13, True)
    third = ReplyAuthorization(6, 11, 7, ReplyKind.FOLLOW_UP, 14, True)
    fourth = ReplyAuthorization(7, 12, 7, ReplyKind.FOLLOW_UP, 15, True)
    guard = ReplyGuardProcessor(
        lookup_authorization=lambda metadata: {9: first, 10: second, 11: third, 12: fourth}.get(
            metadata.get("dispatch")
        ),
        is_current=lambda _auth: True,
    )
    pushed, rejected = [], []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    guard.push_frame = capture

    @guard.event_handler("on_rejected")
    def rejected_event(_guard, rejection):
        rejected.append((rejection.authorization, rejection.reason))

    async def stream(dispatch, text):
        for cls, value in (
            (LLMFullResponseStartFrame, None),
            (LLMTextFrame, text),
            (LLMFullResponseEndFrame, None),
        ):
            frame = cls(value) if value is not None else cls()
            frame.metadata["dispatch"] = dispatch
            await guard.process_frame(frame, FrameDirection.DOWNSTREAM)

    await stream(9, "● once")
    await stream(9, "● replay")
    assert [frame.text for frame, _ in pushed if isinstance(frame, LLMTextFrame)] == ["once"]
    assert (first, "replayed_response") in rejected

    pushed.clear()
    start = LLMFullResponseStartFrame()
    start.metadata["dispatch"] = 10
    await guard.process_frame(start, FrameDirection.DOWNSTREAM)
    duplicate = LLMFullResponseStartFrame()
    duplicate.metadata["dispatch"] = 10
    await guard.process_frame(duplicate, FrameDirection.DOWNSTREAM)
    end = LLMFullResponseEndFrame()
    end.metadata["dispatch"] = 10
    await guard.process_frame(end, FrameDirection.DOWNSTREAM)
    assert pushed == []
    assert (second, "duplicate_start") in rejected

    pushed.clear()
    start = LLMFullResponseStartFrame()
    start.metadata["dispatch"] = 11
    await guard.process_frame(start, FrameDirection.DOWNSTREAM)
    text = LLMTextFrame("● current")
    text.metadata["dispatch"] = 11
    await guard.process_frame(text, FrameDirection.DOWNSTREAM)
    stale_error = ErrorFrame(error="old error")
    stale_error.metadata["dispatch"] = 9
    await guard.process_frame(stale_error, FrameDirection.DOWNSTREAM)
    end = LLMFullResponseEndFrame()
    end.metadata["dispatch"] = 11
    await guard.process_frame(end, FrameDirection.DOWNSTREAM)
    assert [frame.text for frame, _ in pushed if isinstance(frame, LLMTextFrame)] == ["current"]

    pushed.clear()
    start = LLMFullResponseStartFrame()
    start.metadata["dispatch"] = 12
    await guard.process_frame(start, FrameDirection.DOWNSTREAM)
    active_error = ErrorFrame(error="current error")
    active_error.metadata["dispatch"] = 12
    await guard.process_frame(active_error, FrameDirection.DOWNSTREAM)
    assert (fourth, "provider_error") in rejected


@pytest.mark.asyncio
async def test_infrastructure_passthrough_and_model_side_effect_blocking_are_explicit():
    """Heartbeats, stop, and speaking state survive while model tools remain fail-closed."""
    guard, pushed = _guard()
    await guard.setup(frame_processor_setup(TaskManager()))
    try:
        for frame in (HeartbeatFrame(timestamp=0), UserSpeakingFrame(), StopFrame()):
            await guard.process_frame(frame, FrameDirection.UPSTREAM)
        await guard.process_frame(
            FunctionCallsStartedFrame(function_calls=[]), FrameDirection.DOWNSTREAM
        )
        assert [type(frame) for frame, _ in pushed] == [
            HeartbeatFrame,
            UserSpeakingFrame,
            StopFrame,
        ]
        assert all(direction is FrameDirection.UPSTREAM for _frame, direction in pushed)
    finally:
        await guard.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_start", [False, True])
async def test_rejected_dispatch_cannot_restart_with_valid_text(bad_start):
    """Malformed and duplicate-start streams permanently retire their dispatch ID."""
    guard, pushed = _guard()

    async def send(frame):
        frame.metadata["dispatch"] = 9
        await guard.process_frame(frame, FrameDirection.DOWNSTREAM)

    await send(LLMFullResponseStartFrame())
    if bad_start:
        await send(LLMFullResponseStartFrame())
    else:
        await send(LLMTextFrame("malformed"))
        await send(LLMFullResponseEndFrame())
    await send(LLMFullResponseStartFrame())
    await send(LLMTextFrame("● Must not speak"))
    await send(LLMFullResponseEndFrame())
    assert pushed == []
