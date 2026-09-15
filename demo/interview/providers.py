"""Asynchronous provider boundaries and deterministic, network-free test doubles."""

from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Protocol

from demo.interview.contracts import (
    CompletionDecision,
    ReplyKind,
    ResponseGeneration,
    SegmentFinal,
)


@dataclass(frozen=True)
class ReasoningRequest:
    """Snapshot dispatched after transcript readiness has been established.

    Parameters:
        candidate_turn_id: Owner of the pending candidate answer.
        question_id: Current interview question.
        transcript: Finalized text supplied to reasoning.
        generation: Bot response attempt; independent of transcript ownership.
    """

    candidate_turn_id: int
    question_id: str
    transcript: str
    generation: ResponseGeneration


@dataclass(frozen=True)
class ReasoningReply:
    """Parsed provider response awaiting the application reply guard.

    Parameters:
        generation: Response attempt that produced this result.
        kind: Intended conversational purpose, subject to controller validation.
        completion: Parsed completeness and validity metadata.
        text: Proposed spoken text, not yet authorized for synthesis.
    """

    generation: ResponseGeneration
    kind: ReplyKind
    completion: CompletionDecision
    text: str


@dataclass(frozen=True)
class SpeechRequest:
    """Approved text and output ownership supplied to synthesis.

    Parameters:
        generation: Response attempt owning the requested audio.
        context_id: Pipecat synthesis context identifier.
        text: Text already approved by the application reply guard.
    """

    generation: ResponseGeneration
    context_id: str
    text: str


@dataclass(frozen=True)
class SpeechResult:
    """Mono PCM audio result with its original synthesis ownership.

    Parameters:
        generation: Response attempt that requested this audio.
        context_id: Original synthesis context identifier.
        pcm: Signed 16-bit little-endian mono samples.
        sample_rate: Audio sample rate in Hz.
    """

    generation: ResponseGeneration
    context_id: str
    pcm: bytes
    sample_rate: int = 24_000


class TranscriptSource(Protocol):
    """Expose normalized terminal segment events, including empty outcomes."""

    def events(self) -> AsyncIterator[SegmentFinal]:
        """Yield terminal results with connection and candidate ownership."""
        ...


class ReasoningProvider(Protocol):
    """Produce a response without owning interview progression."""

    async def respond(self, request: ReasoningRequest) -> ReasoningReply:
        """Return a parsed response retaining its original generation."""
        ...


class SpeechProvider(Protocol):
    """Synthesize approved text without owning output acceptance."""

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        """Return audio retaining its original generation and context."""
        ...


class FakeTranscriptSource:
    """Replay supplied final events verbatim, including duplicates and stale IDs."""

    def __init__(self, events: Iterable[SegmentFinal]) -> None:
        """Store the finite event script.

        Args:
            events: Outcomes in the exact order each subscriber should observe.
        """
        self._events = tuple(events)

    async def events(self) -> AsyncIterator[SegmentFinal]:
        """Replay the script without sorting, deduplicating, or network access."""
        for event in self._events:
            yield event


class FakeReasoningProvider:
    """Consume scripted replies verbatim so tests can exercise stale responses."""

    def __init__(self, replies: Iterable[ReasoningReply]) -> None:
        """Initialize a finite reply script and captured requests.

        Args:
            replies: Responses returned in call order without rewriting metadata.
        """
        self._replies = deque(replies)
        self.requests: list[ReasoningRequest] = []

    async def respond(self, request: ReasoningRequest) -> ReasoningReply:
        """Capture the request and return the next scripted response."""
        self.requests.append(request)
        if not self._replies:
            raise RuntimeError("fake reasoning reply script exhausted")
        return self._replies.popleft()


class FakeSpeechProvider:
    """Consume scripted audio without implying that speech synthesis occurred."""

    def __init__(self, results: Iterable[SpeechResult]) -> None:
        """Initialize a finite audio script and captured requests.

        Args:
            results: Results returned verbatim, including intentionally stale IDs.
        """
        self._results = deque(results)
        self.requests: list[SpeechRequest] = []

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        """Capture the request and return the next scripted audio result."""
        self.requests.append(request)
        if not self._results:
            raise RuntimeError("fake speech result script exhausted")
        return self._results.popleft()
