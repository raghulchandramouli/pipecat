"""Transcript normalization and shared LLM admission for the interview pipeline."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMContextSummaryRequestFrame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.utils.time import time_now_iso8601

from .contracts import SegmentFinal
from .ledger import TranscriptLedger, TranscriptSnapshot
from .sarvam_events import InterviewSarvamRealtimeSTTService, SarvamCorrelationFailure

_RESPONSE: ContextVar[tuple[int, int, int] | None] = ContextVar("interview_response", default=None)
ProviderDispatch = Callable[[LLMContextFrame, FrameDirection], Awaitable[None]]


@dataclass
class TranscriptSnapshotFrame(Frame):
    """Canonical finalized pending answer supplied to user-turn strategies.

    Parameters:
        snapshot: Deduplicated ledger view in local speech order.
    """

    snapshot: TranscriptSnapshot


@dataclass(frozen=True)
class _DispatchIntent:
    """A request to reason about the latest ready transcript.

    Parameters:
        token: Speech generation at request time.
        direction: Direction from which the request reached the LLM.
    """

    token: int
    direction: FrameDirection


class TranscriptLedgerProcessor(FrameProcessor):
    """Normalize STT events upstream of aggregation and coordinate LLM admission.

    Place this processor after Sarvam and before :class:`InterviewUserAggregator`.
    Both user and LLM-side dispatch paths submit intent here. The shared context
    contains accepted/history messages; each provider snapshot adds the current
    pending answer once, without committing it as an accepted interview answer.
    """

    def __init__(
        self,
        *,
        ledger: TranscriptLedger,
        context: LLMContext,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        """Initialize the coordinator without starting provider or timer work.

        Args:
            ledger: Controller-owned transcript state.
            context: Shared conversation history used at provider admission.
            clock: Monotonic time source for deadline tests and scheduling.
            **kwargs: Frame processor options.
        """
        super().__init__(**kwargs)
        self.ledger = ledger
        self.context = context
        self._clock = clock
        self._changed = asyncio.Event()
        self._watch_task: asyncio.Task | None = None
        self._provider_task: asyncio.Task | None = None
        self._provider: ProviderDispatch | None = None
        self._pending: _DispatchIntent | None = None
        self._running = False
        self._closed = False
        self._serial = 0
        self._last_snapshot_revision = -1
        self._last_recovery: str | None = None
        self._last_token = ledger.response_token
        self._cancel_requested = False
        self._register_event_handler("on_transcript_recovery", sync=True)
        self._register_event_handler("on_dispatch", sync=True)

    def set_provider(self, provider: ProviderDispatch) -> None:
        """Register the sole provider admission callback for this pipeline."""
        if self._provider is not None:
            raise RuntimeError("the transcript coordinator already has a provider")
        self._provider = provider

    def changed(self) -> None:
        """Wake deadline/dispatch work after a synchronous ledger update."""
        self._changed.set()

    def speech_started(self) -> None:
        """Invalidate pending output promptly, without discarding valid transcripts."""
        self.ledger.speech_started()
        self.changed()

    def request_dispatch(
        self, frame: LLMContextFrame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        """Coalesce a context request as intent; never retain provisional transcript text.

        Args:
            frame: Triggering context frame. Shared canonical history and the ledger
                replace its possibly stale/provisional context at admission.
            direction: Original pipeline direction, including assistant upstream runs.
        """
        if self._closed:
            return
        self._pending = _DispatchIntent(self.ledger.response_token, direction)
        self.changed()

    def response_is_current(self, token: int, serial: int) -> bool:
        """Return whether a provider attempt remains eligible to emit output."""
        return (
            not self._closed
            and self._running
            and token == self.ledger.response_token
            and serial == self._serial
            and self.ledger.readiness
        )

    def attach_sarvam(self, service: InterviewSarvamRealtimeSTTService) -> None:
        """Bind service events to the ledger before audio or final frames flow.

        Args:
            service: Application Sarvam extension using the ledger's generation.
        """
        if service.connection_generation != self.ledger.connection_generation:
            raise ValueError("Sarvam and transcript ledger generations must match")

        @service.event_handler("on_speech_start_requested")
        def requested(stt, generation):
            if generation != self.ledger.connection_generation or self._closed:
                return
            self.speech_started()
            owner = self.ledger.snapshot.candidate_turn_id
            if owner is None:
                self.ledger.correlation_failure(
                    SarvamCorrelationFailure(generation, "speech_without_candidate_turn")
                )
            elif stt.candidate_turn_for_next_boundary is None:
                stt.set_candidate_turn(owner)
            elif stt.candidate_turn_for_next_boundary != owner:
                self.ledger.correlation_failure(
                    SarvamCorrelationFailure(generation, "armed_candidate_mismatch")
                )
            self.changed()

        @service.event_handler("on_manual_boundary")
        def boundary(_service, event):
            self.ledger.record_boundary(event, now=self._clock())
            self.changed()

        @service.event_handler("on_utterance_bound")
        def bound(_service, event):
            self.ledger.bind_boundary(event.boundary_id, event.segment_id)
            self.changed()

        @service.event_handler("on_unbound_final")
        def unbound(_service, event):
            self.ledger.observe_unbound(event, now=self._clock())
            self.changed()

        @service.event_handler("on_segment_final")
        def final(_service, event):
            self.record_final(event)
            self.changed()

        @service.event_handler("on_correlation_failure")
        def correlation(_service, event):
            self.ledger.correlation_failure(event)
            self.changed()

        @service.event_handler("on_connection_event")
        def connection(_service, event):
            self.ledger.connection_event(event)
            self.changed()

    async def setup(self, setup: FrameProcessorSetup) -> None:
        """Start one managed coordinator task for all deadlines and deferred runs."""
        await super().setup(setup)
        self._watch_task = self.create_task(self._watch(), name="interview-transcript-gate")

    def record_final(self, final: SegmentFinal) -> bool:
        """Record a bound final through the application event boundary."""
        return self.ledger.record_final(final)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Keep audio responsive and remove raw finals before user aggregation."""
        if isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            await self.close()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.speech_started()
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._running = True
            self.changed()
        if isinstance(frame, TranscriptionFrame):
            return
        await self.push_frame(frame, direction)

    async def close(self) -> None:
        """Stop deferred runs and cancel managed provider work on teardown."""
        self._closed = True
        self._running = False
        self._pending = None
        self._serial += 1
        if self._watch_task:
            await self.cancel_task(self._watch_task)
            self._watch_task = None
        if self._provider_task:
            await self.cancel_task(self._provider_task)
            self._provider_task = None

    async def cleanup(self) -> None:
        """Release coordinator tasks before releasing framework resources."""
        await self.close()
        await super().cleanup()

    async def _watch(self) -> None:
        while not self._closed:
            deadline = self._next_wake_deadline()
            timeout = None if deadline is None else max(0.0, deadline - self._clock())
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
            self._changed.clear()
            self.ledger.check_timeouts(self._clock())
            self._on_wake()
            if (
                self.ledger.response_token != self._last_token
                or self.ledger.recovery_error
                or self._cancel_requested
            ):
                self._cancel_requested = False
                self._last_token = self.ledger.response_token
                if self._pending and self._pending.token != self.ledger.response_token:
                    self._pending = None
                if self._provider_task and not self._provider_task.done():
                    await self.cancel_task(self._provider_task)
                    self._provider_task = None
            error = self.ledger.recovery_error
            if error:
                self._pending = None
                if error != self._last_recovery:
                    self._last_recovery = error
                    await self._call_event_handler("on_transcript_recovery", error)
                    await self.push_error(f"Interview transcript recovery required: {error}")
                continue
            self._last_recovery = None
            if not self._running:
                continue
            snapshot = self.ledger.snapshot
            if not snapshot.ready:
                continue
            if snapshot.revision != self._last_snapshot_revision:
                self._last_snapshot_revision = snapshot.revision
                await self.push_frame(TranscriptSnapshotFrame(snapshot))
            if self._pending is None or self._provider is None:
                continue
            if not self._dispatch_is_ready():
                continue
            if self._provider_task and not self._provider_task.done():
                continue
            intent = self._pending
            self._pending = None
            self._provider_task = self.create_task(
                self._dispatch(intent), name="interview-llm-dispatch"
            )

    def _next_wake_deadline(self) -> float | None:
        """Return the next transcript deadline for managed scheduling."""
        return self.ledger.next_deadline

    def _on_wake(self) -> None:
        """Allow application policy to update deferred admission intent."""

    def _dispatch_is_ready(self) -> bool:
        """Return whether application conditions permit a provider run."""
        return self.ledger.readiness

    def _prepare_dispatch(self, frame: LLMContextFrame) -> None:
        """Attach application authorization before an admitted context is exposed."""

    def _dispatch_failed(self) -> None:
        """Release application policy after a provider call fails."""

    async def _dispatch(self, intent: _DispatchIntent) -> None:
        try:
            snapshot = self.ledger.snapshot
            if (
                self._closed
                or not self._dispatch_is_ready()
                or intent.token != self.ledger.response_token
                or self._provider is None
            ):
                return
            self._serial += 1
            serial = self._serial
            messages = copy.deepcopy(self.context.messages)
            if snapshot.text:
                messages.append({"role": "user", "content": snapshot.text})
            # An empty terminal resolves readiness but cannot fabricate a candidate answer.
            context = LLMContext(
                messages=messages,
                tools=copy.deepcopy(self.context.tools),
                tool_choice=copy.deepcopy(self.context.tool_choice),
            )
            frame = LLMContextFrame(context=context)
            frame.metadata.update(
                interview_response_token=intent.token,
                interview_dispatch_id=serial,
                interview_candidate_turn_id=snapshot.candidate_turn_id,
                interview_transcript_revision=snapshot.revision,
            )
            self._prepare_dispatch(frame)
            marker = _RESPONSE.set((id(self), intent.token, serial))
            try:
                await self._call_event_handler("on_dispatch", frame)
                if self.response_is_current(intent.token, serial):
                    await self._provider(frame, intent.direction)
            finally:
                _RESPONSE.reset(marker)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._dispatch_failed()
            await self.push_error("Interview LLM dispatch failed", exception=error)
        finally:
            if self._provider_task is asyncio.current_task():
                self._provider_task = None
            self.changed()


class InterviewUserAggregator(LLMUserAggregator):
    """Use canonical ledger snapshots for turn detection without committing partial answers."""

    def __init__(self, context: LLMContext, *, coordinator: TranscriptLedgerProcessor, **kwargs):
        """Create the user aggregator sharing the coordinator's canonical history.

        Args:
            context: Shared conversation history.
            coordinator: Transcript ledger and LLM admission owner.
            **kwargs: Normal user aggregator parameters.
        """
        if context is not coordinator.context:
            raise ValueError("user aggregator and coordinator must share the same context")
        super().__init__(context, _realtime_service_mode=False, **kwargs)
        self._coordinator = coordinator

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Feed only canonical final snapshots to the normal turn controllers."""
        if isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            await self._coordinator.close()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._coordinator.speech_started()
        if isinstance(frame, TranscriptSnapshotFrame):
            current = self._coordinator.ledger.snapshot
            if not current.ready or current.revision != frame.snapshot.revision:
                return
            if current.text:
                frame = TranscriptionFrame(
                    text=current.text,
                    user_id=str(current.candidate_turn_id),
                    timestamp=time_now_iso8601(),
                    finalized=True,
                )
            else:
                return
        elif isinstance(frame, TranscriptionFrame):
            return
        await super().process_frame(frame, direction)

    async def _stop(self, frame: EndFrame) -> None:
        await self._coordinator.close()
        await super()._stop(frame)

    async def _cancel(self, frame: CancelFrame) -> None:
        await self._coordinator.close()
        await super()._cancel(frame)

    async def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        # The controller sees canonical transcript frames; the pending answer stays in the ledger.
        pass

    async def _push_aggregation(self, *, run_llm: bool = True) -> str:
        if run_llm:
            await self.push_context_frame()
        return ""

    async def _run_speculative_inference(self, speculation) -> None:
        # Eager provisional text cannot enter the provider snapshot.
        await self.push_context_frame()

    async def _on_vad_speech_started(self, controller) -> None:
        self._coordinator.speech_started()
        await super()._on_vad_speech_started(controller)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        """Route every user-context emission through shared admission."""
        if isinstance(frame, LLMContextFrame):
            self._coordinator.request_dispatch(frame, direction)
            return
        await super().push_frame(frame, direction)


class TranscriptGatedLLMMixin:
    """Admit both user-downstream and assistant-upstream context runs through one gate.

    Place this mixin before the concrete LLM service in the inheritance order.
    The provider's context processing runs in the coordinator's managed task;
    normal audio, speech-start, and interruption frames remain responsive.
    """

    def __init__(self, *, coordinator: TranscriptLedgerProcessor, **kwargs):
        """Register this LLM as the coordinator's sole provider.

        Args:
            coordinator: Shared transcript admission owner.
            **kwargs: Concrete LLM service constructor arguments.
        """
        super().__init__(**kwargs)
        self._transcript_coordinator = coordinator
        coordinator.set_provider(self._invoke_provider)

    async def _invoke_provider(self, frame: LLMContextFrame, direction: FrameDirection) -> None:
        if self._response_is_current():
            await super().process_frame(frame, direction)

    def _response_is_current(self) -> bool:
        marker = _RESPONSE.get()
        return (
            marker is not None
            and marker[0] == id(self._transcript_coordinator)
            and self._transcript_coordinator.response_is_current(marker[1], marker[2])
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Gate all context runs and reject automatic summary inference in this demo."""
        if isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            await self._transcript_coordinator.close()
        if isinstance(frame, LLMContextFrame):
            self._transcript_coordinator.request_dispatch(frame, direction)
            return
        if isinstance(frame, LLMContextSummaryRequestFrame):
            await self.push_error("Automatic context summarization is disabled for interview turns")
            return
        await super().process_frame(frame, direction)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        """Discard output from invalidated provider tasks, including delayed completions."""
        marker = _RESPONSE.get()
        if marker is not None and marker[0] == id(self._transcript_coordinator):
            if not self._response_is_current() and not isinstance(frame, ErrorFrame):
                return
            frame.metadata["interview_response_token"] = marker[1]
            frame.metadata["interview_dispatch_id"] = marker[2]
        await super().push_frame(frame, direction)
