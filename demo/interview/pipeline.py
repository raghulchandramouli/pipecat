"""Live Pipecat assembly for one isolated browser interview session."""

from __future__ import annotations

import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.evals.transport import EvalTransport
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMTextFrame,
    OutputTransportMessageFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserverParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from .browser_contract import BrowserInterviewSetup, InterviewBrowserEvent
from .browser_stt import BrowserSarvamSTTService
from .config import (
    Difficulty,
    InterviewConfig,
    RoleConfig,
)
from .contracts import SegmentFinal, SegmentId
from .languages import INTERVIEW_LANGUAGES
from .session import InterviewSession

HINGLISH_INSTRUCTION = (
    "Write interviewer wording in natural, respectful Hinglish, using Hindi and English as appropriate. "
    "Follow the requested coaching JSON format after the completion marker. "
    "For candidate quotations, copy a short substring "
    "character-for-character from the finalized transcript into evidence.quote and include "
    "that identical substring in speak. Preserve its original Devanagari or Latin letters, "
    "numbers, spacing, and punctuation. Never translate, transliterate, correct, or normalize "
    "a quotation. Understand voice controls in English or Hindi."
)

TANGLISH_INSTRUCTION = (
    "Write interviewer wording in natural, respectful Tanglish: conversational Tamil mixed with "
    "two or three everyday English words such as team, deadline, or handle when natural. "
    "Tamil must carry the sentences and most of the wording; do not produce whole English "
    "questions or paragraphs followed by a Tamil sentence. Use natural spoken Tamil, not "
    "formal literary Tamil. Use Tamil script for Tamil wording and English for English words. "
    "Example: 'அந்த நேரத்துல நீங்க என்ன பண்ணீங்க? Team எப்படி react பண்ணாங்க?' "
    "Treat English rubric guidance as private evaluation notes: phrase the question naturally "
    "in Tanglish and never read out 'Listen for' notes or evaluation criteria. "
    "Follow the requested coaching JSON format after the completion marker. "
    "Copy candidate quotations character-for-character from the finalized transcript into "
    "evidence.quote and include that identical substring in speak. Preserve the original script, "
    "spelling, numbers, spacing, and punctuation of quotations. Never translate, transliterate, "
    "correct, or normalize a quotation. Understand Tamil and English answers. "
    "The exact voice commands are repeat, skip, give me a moment, and end the interview."
)

EventSink = Callable[[InterviewBrowserEvent], Awaitable[None]]


class InterviewPipelineFactory(Protocol):
    """Create independent pipeline ownership for a browser or eval connection."""

    async def create(
        self,
        *,
        session_id: str,
        setup: BrowserInterviewSetup,
        transport: BaseTransport,
        emit: EventSink,
    ) -> ManagedInterview:
        """Return an unstarted session with its own services and context."""
        ...


@dataclass
class ManagedInterview:
    """Session resources that are torn down together when its client leaves.

    Parameters:
        session: Controller-owned interview state for one connection.
        worker: Pipeline worker that owns the session processors.
        transport: Browser or eval transport carrying this session's audio.
        ready: Activates the greeting after client media is ready.
        close: Releases all session resources.
    """

    session: InterviewSession
    worker: PipelineWorker
    transport: BaseTransport
    ready: Callable[[], Awaitable[None]]
    close: Callable[[], Awaitable[None]]


class BrowserEventEmitter:
    """Stamp allowlisted events before sending plain JSON over the data channel.

    Parameters:
        session_id: Server-generated session identity.
        connection_generation: Immutable generation assigned to this connection.
        send: Data-channel send function accepting a JSON-compatible mapping.
    """

    def __init__(
        self,
        *,
        session_id: str,
        connection_generation: int,
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Initialize data-channel event sequencing.

        Args:
            session_id: Server-generated session identity.
            connection_generation: Immutable connection generation.
            send: Data-channel send function accepting a JSON-compatible mapping.
        """
        self._session_id = session_id
        self._connection_generation = connection_generation
        self._send = send
        self._seq = 0

    async def emit(self, event: InterviewBrowserEvent) -> None:
        """Send one sequenced public event without exposing provider configuration."""
        self._seq += 1
        event = event.model_copy(
            update={
                "session_id": self._session_id,
                "connection_generation": self._connection_generation,
                "seq": self._seq,
            }
        )
        await self._send(event.model_dump(mode="json", exclude_none=True))


class _CaptionBridge(FrameProcessor):
    """Mirror provisional and provider-final captions without admitting raw finals."""

    def __init__(self, emit: EventSink) -> None:
        """Initialize caption output.

        Args:
            emit: Session event sink.
        """
        super().__init__()
        self._emit = emit

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Forward the frame after emitting its browser-only caption representation."""
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame) and frame.text:
            result = frame.result if isinstance(frame.result, dict) else {}
            utterance_idx = result.get("utterance_idx")
            if (
                isinstance(utterance_idx, int)
                and not isinstance(utterance_idx, bool)
                and utterance_idx >= 0
            ):
                # Sarvam retains this provider utterance ID from partial through final.
                # The final event carries the same ID only after the timestamp-bound
                # correlation has accepted it, so the client replaces this caption
                # instead of treating a provisional transcript as evidence.
                await self._emit(
                    InterviewBrowserEvent(
                        type="interview.caption",
                        text=frame.text,
                        final=False,
                        segment_id=str(utterance_idx),
                    )
                )
        await self.push_frame(frame, direction)


class _PlaybackBridge(FrameProcessor):
    """Publish audible playback status after transport output, then retain playback history."""

    def __init__(
        self,
        session: InterviewSession,
        emit: EventSink,
        *,
        on_output_activity: Callable[[bool], None] | None = None,
    ) -> None:
        """Initialize browser playback status reporting.

        Args:
            session: Isolated interview controller.
            emit: Session event sink.
            on_output_activity: Receives whether the output transport is actively
                playing interviewer speech.
        """
        super().__init__()
        self._session = session
        self._emit = emit
        self._on_output_activity = on_output_activity
        self._queued_epochs: deque[int] = deque()
        self._playing_epoch: int | None = None
        enable_acknowledgments = getattr(
            session, "enable_output_interruption_acknowledgments", None
        )
        if callable(enable_acknowledgments):
            enable_acknowledgments()
        self._latest_dispatch_id: int | None = None

    def queue_output(self, epoch: int, dispatch_id: int | None = None) -> None:
        """Associate one guard-released reply with its epoch before TTS queues audio."""
        if epoch >= 0:
            self._queued_epochs.append(epoch)
            self._latest_dispatch_id = dispatch_id

    def invalidate(self, epoch: int) -> None:
        """Retire queued audio and visual playback activity after an interruption."""
        self._queued_epochs.clear()
        self._playing_epoch = None
        self._latest_dispatch_id = None
        if self._on_output_activity is not None:
            self._on_output_activity(False)

    def output_failed(self, epoch: int, dispatch_id: int) -> bool:
        """Release an owned output wait when the current TTS context cannot play."""
        if epoch != self._session.playback_epoch or dispatch_id != self._latest_dispatch_id:
            return False
        self._queued_epochs = deque(item for item in self._queued_epochs if item != epoch)
        if self._playing_epoch == epoch:
            self._playing_epoch = None
        if self._on_output_activity is not None:
            self._on_output_activity(False)
        playback_failed = getattr(self._session, "playback_failed", None)
        if callable(playback_failed):
            playback_failed(epoch)
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Report actual output activity and safe pipeline errors."""
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and direction is FrameDirection.DOWNSTREAM:
            acknowledge = getattr(self._session, "output_interruption_processed", None)
            if callable(acknowledge):
                acknowledge(frame)
        elif isinstance(frame, BotStartedSpeakingFrame):
            current_epoch = self._session.playback_epoch
            while self._queued_epochs and self._queued_epochs[0] != current_epoch:
                self._queued_epochs.popleft()
            if not self._queued_epochs:
                await self.push_frame(frame, direction)
                return
            epoch = self._queued_epochs.popleft()
            self._playing_epoch = epoch
            playback_started = getattr(self._session, "playback_started", None)
            if callable(playback_started):
                playback_started(epoch)
            if self._on_output_activity is not None:
                self._on_output_activity(True)
            await _emit_status(self._session, self._emit, "Speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            epoch = self._playing_epoch
            self._playing_epoch = None
            if epoch is None or epoch != self._session.playback_epoch:
                if self._on_output_activity is not None:
                    self._on_output_activity(False)
                await self.push_frame(frame, direction)
                return
            playback_stopped = getattr(self._session, "playback_stopped", None)
            if callable(playback_stopped):
                playback_stopped(epoch)
            if self._on_output_activity is not None:
                self._on_output_activity(False)
            await _emit_status(self._session, self._emit, _waiting_status(self._session))
        elif isinstance(frame, EndFrame):
            if self._on_output_activity is not None:
                self._on_output_activity(False)
            await self._emit(InterviewBrowserEvent(type="interview.ended"))
        await self.push_frame(frame, direction)


class _ErrorBridge(FrameProcessor):
    """Publish a text-continuation notice for upstream output failures."""

    def __init__(
        self,
        emit: EventSink,
        language: str = "english",
        *,
        output_processors: tuple[object, ...] = (),
        on_output_failure: Callable[[int, int], bool] | None = None,
    ) -> None:
        """Initialize output-error reporting.

        Args:
            emit: Session event sink.
            language: Candidate-facing message language.
            output_processors: TTS processors whose failures can prevent browser playback.
            on_output_failure: Releases only a failed output's playback wait.
        """
        super().__init__()
        self._emit = emit
        self._language = language
        self._output_processors = output_processors
        self._on_output_failure = on_output_failure

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Forward all frames and expose a safe, recoverable output failure."""
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and frame.processor in self._output_processors:
            epoch = frame.metadata.get("interview_playback_epoch")
            dispatch_id = frame.metadata.get("interview_dispatch_id")
            owned = all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (epoch, dispatch_id)
            )
            if owned and self._on_output_failure is not None:
                if not self._on_output_failure(epoch, dispatch_id):
                    await self.push_frame(frame, direction)
                    return
            await self._emit(
                InterviewBrowserEvent(
                    type="interview.error",
                    message=(
                        "இப்போ குரல் வரலை. பதிலை இங்கே படிக்கலாம். நீங்க தொடர்ந்து பேசலாம்."
                        if self._language == "tanglish"
                        else "Speech output was unavailable. Read the question here or repeat it. Your accepted answers are saved."
                    ),
                    recoverable=True,
                )
            )
        await self.push_frame(frame, direction)


class _ReplyBridge(FrameProcessor):
    """Expose guard-released text before it reaches speech synthesis."""

    def __init__(
        self, emit: EventSink, *, on_output_queued: Callable[[int, int | None], None] | None = None
    ) -> None:
        """Initialize clean reply reporting.

        Args:
            emit: Session event sink.
            on_output_queued: Associates guarded output with an owned playback epoch.
        """
        super().__init__()
        self._emit = emit
        self._on_output_queued = on_output_queued

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Emit only text that has already crossed the reply guard."""
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            dispatch_id = frame.metadata.get("interview_dispatch_id")
            epoch = frame.metadata.get("interview_playback_epoch")
            if (
                self._on_output_queued is not None
                and isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
            ):
                self._on_output_queued(epoch, dispatch_id if isinstance(dispatch_id, int) else None)
            await self._emit(
                InterviewBrowserEvent(
                    type="interview.reply",
                    text=frame.text,
                    dispatch_id=dispatch_id if isinstance(dispatch_id, int) else None,
                    playback_epoch=(
                        epoch
                        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0
                        else None
                    ),
                )
            )
        await self.push_frame(frame, direction)


def _waiting_status(session: InterviewSession) -> str:
    if getattr(session, "_thinking_at", None) is not None:
        return "Giving you time"
    return "Listening"


async def _emit_status(
    session: InterviewSession,
    emit: EventSink,
    status: str,
    *,
    interaction_snapshot: dict[str, object] | None = None,
) -> None:
    """Publish legacy progress plus the current additive interaction snapshot."""
    question = session.controller.current_question
    snapshot = interaction_snapshot
    if snapshot is None:
        snapshot_getter = getattr(session, "interaction_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else {}
    await emit(
        InterviewBrowserEvent(
            type="interview.status",
            status=status,  # type: ignore[arg-type]
            phase=session.controller.phase.value,
            question_index=question.index + 1 if question is not None else None,
            question_count=len(session.config.question_rubric),
            playback_epoch=session.playback_epoch,
            prompt_revision=snapshot.get("prompt_revision"),  # type: ignore[arg-type]
            state_revision=snapshot.get("state_revision"),  # type: ignore[arg-type]
            question_text=snapshot.get("question_text"),  # type: ignore[arg-type]
            interaction_state=snapshot.get("interaction_state"),  # type: ignore[arg-type]
            permitted_actions=snapshot.get("permitted_actions"),  # type: ignore[arg-type]
            recovery_token=snapshot.get("recovery_token"),  # type: ignore[arg-type]
            question_delivery=snapshot.get("question_delivery"),  # type: ignore[arg-type]
        )
    )


async def _emit_reply_rejection(emit: EventSink, language: str = "english") -> None:
    """Tell the browser that a model response was withheld by the reply guard."""
    await emit(
        InterviewBrowserEvent(
            type="interview.error",
            message=(
                "மன்னிக்கவும், பதில் சொல்ல முடியலை. நீங்க சொன்னதை இன்னொரு முறை சொல்ல முடியுமா?"
                if language == "tanglish"
                else "I couldn't prepare a safe follow-up. Please continue or repeat that part."
            ),
            recoverable=True,
        )
    )


async def _emit_transcript_recovery(emit: EventSink, language: str = "english") -> None:
    """Explain that a transcription gap is recoverable without claiming the connection failed."""
    await emit(
        InterviewBrowserEvent(
            type="interview.error",
            message=(
                "நான் ஒரு பகுதியை கேட்க முடியலை. அந்த பகுதியை மீண்டும் சொல்லுங்க அல்லது Retry அழுத்துங்க."
                if language == "tanglish"
                else "I missed part of that. Please say that part again or use Retry."
            ),
            recoverable=True,
        )
    )


def _canonical_final_caption(
    session: InterviewSession, final: SegmentFinal
) -> InterviewBrowserEvent | None:
    """Return the ledger-owned final caption when this provider final was accepted."""
    canonical = next(
        (
            segment
            for segment in session.ledger.snapshot.segments
            if segment.segment_id == final.segment_id
        ),
        None,
    )
    if canonical != final:
        return None
    return InterviewBrowserEvent(
        type="interview.caption",
        text=canonical.text,
        final=True,
        segment_id=str(canonical.segment_id.utterance_idx),
        candidate_turn_id=canonical.candidate_turn_id,
    )


class LiveInterviewPipelineFactory:
    """Build the real Sarvam, Gemini, and Bulbul pipeline for each connection.

    Args:
        base_config: Server-owned default interview policy and provider choices.
        sarvam_api_key: Backend-only Sarvam credential.
        google_api_key: Backend-only Gemini credential.
    """

    def __init__(
        self,
        *,
        base_config: InterviewConfig,
        sarvam_api_key: str,
        google_api_key: str,
    ) -> None:
        """Store backend-only provider credentials and base policy.

        Args:
            base_config: Server-owned default interview configuration.
            sarvam_api_key: Backend-only Sarvam API key.
            google_api_key: Backend-only Gemini API key.
        """
        if not sarvam_api_key.strip() or not google_api_key.strip():
            raise ValueError("live interview providers require configured backend credentials")
        self._base_config = base_config
        self._sarvam_api_key = sarvam_api_key
        self._google_api_key = google_api_key

    @classmethod
    def from_environment(cls, base_config: InterviewConfig) -> LiveInterviewPipelineFactory:
        """Load required backend credentials without returning or logging their values."""
        sarvam = os.environ.get("SARVAM_API_KEY", "")
        google = os.environ.get("GOOGLE_API_KEY", "")
        return cls(base_config=base_config, sarvam_api_key=sarvam, google_api_key=google)

    async def create(
        self,
        *,
        session_id: str,
        setup: BrowserInterviewSetup,
        transport: BaseTransport,
        emit: EventSink,
    ) -> ManagedInterview:
        """Create a complete live pipeline with no mutable state shared across sessions."""
        config = _config_for_setup(self._base_config, setup)
        session = InterviewSession(
            config=config,
            session_id=session_id,
            defer_start=True,
            language=setup.language,
        )
        stt = BrowserSarvamSTTService(
            connection_generation=session.ledger.connection_generation,
            api_key=self._sarvam_api_key,
            endpointing="manual",
            sample_rate=16_000,
            settings=BrowserSarvamSTTService.Settings(
                model=config.providers.sarvam.model,
                language_code=config.providers.sarvam.language_code,
                mode=config.providers.sarvam.mode,
                stream_type="balanced",
            ),
        )
        session.attach_sarvam(stt)
        user = session.create_user_aggregator(vad_analyzer=SileroVADAnalyzer(sample_rate=16_000))
        llm = session.create_llm(api_key=self._google_api_key)
        if setup.language == "hinglish":
            llm.append_system_instruction(HINGLISH_INSTRUCTION)
        elif setup.language == "tanglish":
            llm.append_system_instruction(TANGLISH_INSTRUCTION)
        else:
            llm.append_system_instruction(
                f"Conduct the interview in short, simple, natural {setup.language} sentences."
            )
        llm.append_system_instruction(
            "Conduct a non-technical behavioural interview. Ask about real experiences with "
            "teamwork, communication, conflict, ownership, and setbacks. Accept examples from "
            "work, education, volunteering, or everyday life. Ask one focused follow-up at a time "
            "about the situation, the candidate's own action, outcome, or learning. "
            "Do not ask coding, backend engineering, system design, or technical trivia questions. "
            "Assess behavioural examples, not technical knowledge. "
            "The candidate may have studied only through class 10 or may have little or no formal "
            "schooling. Use short, familiar sentences in the selected interview language. "
            "For Tanglish use mostly Tamil with two or three everyday English words when natural; "
            "for Hinglish use mostly Hindi with occasional common English words; for every other "
            "selected language use simple everyday wording in that language. Never require literacy, "
            "qualifications, office work, or college "
            "experience. Ask one simple question at a time. Accept examples from home, helping "
            "others, shops, daily-wage work, or everyday life. If they have no example, offer a "
            "simple everyday situation. Do not use STAR, competency, ownership, resilience, "
            "tradeoff, or other interview jargon. Never judge intelligence by education or accent. "
            "Do not label the candidate as uneducated. Keep replies to one or two short sentences."
        )
        guard = session.create_reply_guard()
        tts = session.create_tts(api_key=self._sarvam_api_key)
        playback_context = LLMContext()
        assistant = LLMContextAggregatorPair(playback_context).assistant()
        output_active = False
        emitted_final_captions: set[SegmentId] = set()

        def set_output_active(value: bool) -> None:
            nonlocal output_active
            output_active = value

        playback = _PlaybackBridge(session, emit, on_output_activity=set_output_active)
        errors = _ErrorBridge(
            emit,
            setup.language,
            output_processors=(tts,),
            on_output_failure=playback.output_failed,
        )
        replies = _ReplyBridge(emit, on_output_queued=playback.queue_output)
        pipeline = Pipeline(
            [
                transport.input(),
                errors,
                stt,
                _CaptionBridge(emit),
                session,
                user,
                llm,
                guard,
                replies,
                tts,
                transport.output(),
                playback,
                assistant,
            ]
        )
        rtvi_observer_params = _eval_rtvi_observer_params(transport)
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=16_000,
                audio_out_sample_rate=24_000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            processor_unusable_policy=ProcessorUnusablePolicy.CONTINUE,
            # Browser clients receive only the allowlisted application events
            # emitted above. Eval retains RTVI lifecycle/audio support, but never
            # mirrors model or canonical user-context frames that precede the guard.
            enable_rtvi=rtvi_observer_params is not None,
            rtvi_observer_params=rtvi_observer_params,
        )
        ready_once = False
        closed = False

        async def ready() -> None:
            nonlocal ready_once
            if ready_once:
                return
            ready_once = True
            session.client_ready()
            await _emit_status(session, emit, "Listening")

        async def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            await worker.cancel()

        @session.event_handler("on_playback_invalidated")
        async def playback_invalidated(_session, epoch: int) -> None:
            playback.invalidate(epoch)
            await emit(InterviewBrowserEvent(type="interview.interruption", playback_epoch=epoch))

        @session.event_handler("on_policy_action")
        async def policy_action(_session, action) -> None:
            if output_active:
                return
            await _emit_status(
                session,
                emit,
                "Thinking" if action.kind.value == "semantic_probe" else "Giving you time",
            )

        @session.event_handler("on_dispatch")
        async def dispatch_started(_session, _frame) -> None:
            if output_active:
                return
            await _emit_status(session, emit, "Thinking")

        @session.event_handler("on_interaction_changed")
        async def interaction_changed(_session, snapshot: dict[str, object]) -> None:
            await _emit_status(
                session,
                emit,
                "Speaking" if output_active else _waiting_status(session),
                interaction_snapshot=snapshot,
            )

        @session.event_handler("on_transcript_recovery")
        async def transcript_recovery(_session, _error) -> None:
            await _emit_transcript_recovery(emit, setup.language)
            await _emit_status(session, emit, "Listening")

        @session.event_handler("on_reply_rejected")
        async def reply_rejected(_session, _rejection) -> None:
            session.create_task(
                _emit_reply_rejection(emit, setup.language), "interview-reply-rejection"
            )

        @stt.event_handler("on_segment_final")
        def final_caption(service, final: SegmentFinal) -> None:
            event = _canonical_final_caption(session, final)
            if event is None or final.segment_id in emitted_final_captions:
                return
            emitted_final_captions.add(final.segment_id)
            service.create_task(emit(event), name="interview-final-caption")

        return ManagedInterview(session, worker, transport, ready, close)


def create_smallwebrtc_transport(connection: SmallWebRTCConnection) -> SmallWebRTCTransport:
    """Create the supported browser transport with continuous microphone input.

    Args:
        connection: SmallWebRTC peer connection already initialized from SDP.
    """
    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16_000,
            audio_out_enabled=True,
            audio_out_sample_rate=24_000,
            audio_out_auto_silence=True,
        ),
    )


def _eval_rtvi_observer_params(transport: BaseTransport) -> RTVIObserverParams | None:
    """Return the constrained RTVI policy used by the eval transport only."""
    if not isinstance(transport, EvalTransport):
        return None
    return RTVIObserverParams(
        bot_output_enabled=False,
        bot_llm_enabled=False,
        user_llm_enabled=False,
        user_transcription_enabled=False,
    )


def new_session_id() -> str:
    """Return an opaque browser-safe session identity."""
    return uuid.uuid4().hex


def _config_for_setup(base: InterviewConfig, setup: BrowserInterviewSetup) -> InterviewConfig:
    """Apply public interview choices while retaining server-only live provider settings."""
    codemix = setup.language in {"hinglish", "tanglish"}
    providers = base.providers.model_copy(
        update={
            "sarvam": base.providers.sarvam.model_copy(
                update={
                    "language_code": setup.stt_language,
                    "mode": "codemix" if codemix else "transcribe",
                }
            ),
            "tts": base.providers.tts.model_copy(
                update={
                    "language_code": INTERVIEW_LANGUAGES[setup.language],
                    "pace": setup.speech_pace,
                }
            ),
        }
    )
    return base.model_copy(
        update={
            "role": RoleConfig(title=setup.role),
            "difficulty": Difficulty(setup.difficulty),
            "duration_minutes": setup.duration_minutes,
            "question_rubric": tuple(setup.rubric),
            "providers": providers,
        }
    )


def default_live_config() -> InterviewConfig:
    """Return the minimal server-side policy used when no deployment config is supplied."""
    return InterviewConfig(
        role=RoleConfig(title="Behavioural interview"),
        difficulty=Difficulty.MID,
        duration_minutes=5,
        question_rubric=tuple(BrowserInterviewSetup().rubric),
    )
