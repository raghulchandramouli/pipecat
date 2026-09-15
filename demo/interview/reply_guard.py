"""Fail-closed validation of streamed interview replies before speech output."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMarkerFrame,
    LLMTextFrame,
    StartFrame,
    StopFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserTurnInferenceCompletedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .contracts import CompletionStatus, CompletionValidity, ReplyKind


@dataclass(frozen=True)
class ParsedCompletion:
    """A strict completion marker parsed from one complete LLM response.

    Parameters:
        status: Completion state encoded by the response marker.
        text: Speakable response text after removing a valid complete marker.
        long_wait: Whether the incomplete marker requests the long retry delay.
        validity: Freshness field, left valid by this generation-independent parser.
            The guard separately rejects stale response authorization.
        is_valid: Whether the full response satisfies the completion-marker syntax.
    """

    status: CompletionStatus
    text: str
    long_wait: bool
    validity: CompletionValidity
    is_valid: bool


@dataclass(frozen=True)
class ReplyAuthorization:
    """Controller-owned authorization for one response stream.

    Parameters:
        response_token: Active response generation token.
        dispatch_id: Coordinator dispatch identifier.
        candidate_turn_id: Candidate answer this response addresses.
        reply_kind: Controller-selected purpose of the response.
        transcript_revision: Final transcript revision used for this response.
        answer_allowed: Whether controller policy permits this response to make
            the non-empty finalized answer eligible for acceptance.
    """

    response_token: int
    dispatch_id: int
    candidate_turn_id: int
    reply_kind: ReplyKind
    transcript_revision: int
    answer_allowed: bool


@dataclass(frozen=True)
class ReplyDecision:
    """Validated response result supplied to controller policy.

    Parameters:
        authorization: Controller-owned response authorization.
        completion: Strictly parsed completion result.
        accept_answer: Whether this response may make the pending answer eligible
            for acceptance. Controller policy still owns actual mutation.
    """

    authorization: ReplyAuthorization
    completion: ParsedCompletion
    accept_answer: bool


@dataclass(frozen=True)
class ReplyRejection:
    """A fail-closed response rejection reported without controller mutation."""

    authorization: ReplyAuthorization | None
    reason: str


LookupAuthorization = Callable[[dict[str, Any]], ReplyAuthorization | None]
IsCurrent = Callable[[ReplyAuthorization], bool]


def parse_completion(text: str) -> ParsedCompletion:
    """Parse exactly one terminal completion marker from a full response."""
    stripped = text.strip()
    if not stripped:
        return ParsedCompletion(
            CompletionStatus.MISSING, "", False, CompletionValidity.VALID, False
        )
    if stripped in {"◐", "○"}:
        return ParsedCompletion(
            CompletionStatus.INCOMPLETE,
            "",
            stripped == "○",
            CompletionValidity.VALID,
            True,
        )
    if stripped.startswith("●"):
        remainder = stripped[1:]
        if (
            remainder
            and remainder[0].isspace()
            and remainder.strip()
            and not any(marker in remainder for marker in "●◐○")
        ):
            return ParsedCompletion(
                CompletionStatus.COMPLETE,
                remainder.strip(),
                False,
                CompletionValidity.VALID,
                True,
            )
    return ParsedCompletion(CompletionStatus.MALFORMED, "", False, CompletionValidity.VALID, False)


class ReplyGuardProcessor(FrameProcessor):
    """Buffer a full model response and release only authorized, valid speech.

    Raw text and direct TTS frames are suppressed while a response is active.
    This is deliberately fail-closed: a response without a start frame,
    authorization, current generation, or exact marker is never forwarded.
    """

    def __init__(
        self,
        *,
        lookup_authorization: LookupAuthorization,
        is_current: IsCurrent,
        validate_completion: Callable[[ReplyAuthorization, ParsedCompletion], ParsedCompletion]
        | None = None,
        output_metadata: Callable[[], dict[str, Any]] | None = None,
        max_response_chars: int = 16_000,
        **kwargs: Any,
    ) -> None:
        """Initialize reply buffering and controller authorization callbacks."""
        if max_response_chars < 1:
            raise ValueError("max_response_chars must be positive")
        super().__init__(**kwargs)
        self._lookup_authorization = lookup_authorization
        self._is_current = is_current
        self._validate_completion = validate_completion
        self._output_metadata = output_metadata
        self._max_response_chars = max_response_chars
        self._authorization: ReplyAuthorization | None = None
        self._metadata: dict[str, Any] = {}
        self._chunks: list[str] = []
        self._size = 0
        self._overflowed = False
        self._retired_dispatch_id = -1
        self._register_event_handler("on_decision", sync=True)
        self._register_event_handler("on_rejected", sync=True)
        self._register_event_handler("on_released", sync=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Buffer model text and publish only a complete authorized response."""
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            authorization = self._lookup_authorization(dict(frame.metadata))
            if authorization is None:
                await self._reject(None, "missing_authorization")
                return
            if not self._is_current(authorization):
                await self._reject(authorization, "stale_authorization")
                return
            if authorization == self._authorization:
                self._retired_dispatch_id = max(
                    self._retired_dispatch_id, authorization.dispatch_id
                )
                self._reset()
                await self._reject(authorization, "duplicate_start")
                return
            if authorization.dispatch_id <= self._retired_dispatch_id:
                await self._reject(authorization, "replayed_response")
                return
            self._reset()
            self._authorization = authorization
            self._metadata = dict(frame.metadata)
            return
        if isinstance(frame, LLMTextFrame):
            if not self._matches_active(frame):
                await self._reject(self._incoming_authorization(frame), "mismatched_text")
                return
            self._append(frame.text)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            if not self._matches_active(frame):
                await self._reject(self._incoming_authorization(frame), "mismatched_end")
                return
            await self._finish(direction)
            return
        if isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame, StopFrame)):
            if self._authorization is not None:
                self._retired_dispatch_id = max(
                    self._retired_dispatch_id, self._authorization.dispatch_id
                )
            self._reset()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (TextFrame, TTSSpeakFrame)):
            return
        if isinstance(frame, ErrorFrame):
            authorization = (
                self._incoming_authorization(frame) if frame.metadata else self._authorization
            )
            if authorization is not None and authorization == self._authorization:
                self._retired_dispatch_id = max(
                    self._retired_dispatch_id, authorization.dispatch_id
                )
                self._reset()
                await self._reject(authorization, "provider_error")
            await self.push_frame(frame, direction)
            return
        # Model tools, completion control, and generated audio cannot bypass
        # the controller's validated reply decision. All other infrastructure
        # frames (heartbeats, metadata, speaking state) retain normal flow.
        if isinstance(
            frame,
            (
                FunctionCallsStartedFrame,
                FunctionCallInProgressFrame,
                FunctionCallResultFrame,
                FunctionCallCancelFrame,
                LLMMarkerFrame,
                UserTurnInferenceCompletedFrame,
                TTSAudioRawFrame,
            ),
        ):
            return
        await self.push_frame(frame, direction)

    def _append(self, text: str) -> None:
        if self._authorization is None or self._overflowed:
            return
        self._size += len(text)
        if self._size > self._max_response_chars:
            self._overflowed = True
            self._chunks.clear()
            return
        self._chunks.append(text)

    async def _finish(self, direction: FrameDirection) -> None:
        authorization = self._authorization
        text = "".join(self._chunks)
        overflowed = self._overflowed
        metadata = self._metadata
        self._reset()
        if authorization is None:
            return
        self._retired_dispatch_id = max(self._retired_dispatch_id, authorization.dispatch_id)
        if overflowed:
            await self._reject(authorization, "response_too_large")
            return
        if not self._is_current(authorization):
            await self._reject(authorization, "stale_authorization")
            return
        completion = parse_completion(text)
        if not completion.is_valid:
            await self._reject(authorization, completion.status.value)
            return
        if self._validate_completion is not None:
            try:
                completion = self._validate_completion(authorization, completion)
            except (ValueError, TypeError):
                await self._reject(authorization, "application_validation")
                return
            if not completion.is_valid or not self._is_current(authorization):
                await self._reject(authorization, "application_validation")
                return
        decision = ReplyDecision(
            authorization=authorization,
            completion=completion,
            accept_answer=(
                completion.status is CompletionStatus.COMPLETE
                and authorization.reply_kind is ReplyKind.FOLLOW_UP
                and authorization.answer_allowed
            ),
        )
        await self._call_event_handler("on_decision", decision)
        if completion.status is not CompletionStatus.COMPLETE or not completion.text:
            return
        if not self._is_current(authorization):
            return
        if self._output_metadata is not None:
            metadata.update(self._output_metadata())
        start = LLMFullResponseStartFrame()
        start.metadata.update(metadata)
        await self.push_frame(start, direction)
        if not self._is_current(authorization):
            return
        output = LLMTextFrame(completion.text)
        output.metadata.update(metadata)
        await self.push_frame(output, direction)
        if not self._is_current(authorization):
            return
        end = LLMFullResponseEndFrame()
        end.metadata.update(metadata)
        await self.push_frame(end, direction)
        if self._is_current(authorization):
            await self._call_event_handler("on_released", decision)

    def _reset(self) -> None:
        self._authorization = None
        self._metadata = {}
        self._chunks = []
        self._size = 0
        self._overflowed = False

    def _matches_active(self, frame: Frame) -> bool:
        authorization = self._authorization
        return (
            authorization is not None
            and self._incoming_authorization(frame) == authorization
            and self._is_current(authorization)
        )

    def _incoming_authorization(self, frame: Frame) -> ReplyAuthorization | None:
        return self._lookup_authorization(dict(frame.metadata))

    async def _reject(self, authorization: ReplyAuthorization | None, reason: str) -> None:
        await self._call_event_handler("on_rejected", ReplyRejection(authorization, reason))
