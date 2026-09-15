"""Live Pipecat assembly for one isolated browser interview session."""

from __future__ import annotations

import os
import uuid
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
    "occasional everyday English words such as team, deadline, or handle. "
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

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Report actual output activity and safe pipeline errors."""
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            if self._on_output_activity is not None:
                self._on_output_activity(True)
            await _emit_status(self._session, self._emit, "Speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
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

    def __init__(self, emit: EventSink, language: str = "english") -> None:
        """Initialize output-error reporting.

        Args:
            emit: Session event sink.
            language: Candidate-facing message language.
        """
        super().__init__()
        self._emit = emit
        self._language = language

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Forward all frames and expose a safe, recoverable output failure."""
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame):
            await self._emit(
                InterviewBrowserEvent(
                    type="interview.error",
                    message=(
                        "இப்போ குரல் வரலை. பதிலை இங்கே படிக்கலாம். நீங்க தொடர்ந்து பேசலாம்."
                        if self._language == "tanglish"
                        else "Speech output was unavailable. Continue speaking and the interview will continue in text."
                    ),
                    recoverable=True,
                )
            )
        await self.push_frame(frame, direction)


class _ReplyBridge(FrameProcessor):
    """Expose guard-released text before it reaches speech synthesis."""

    def __init__(self, emit: EventSink) -> None:
        """Initialize clean reply reporting.

        Args:
            emit: Session event sink.
        """
        super().__init__()
        self._emit = emit

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Emit only text that has already crossed the reply guard."""
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            dispatch_id = frame.metadata.get("interview_dispatch_id")
            await self._emit(
                InterviewBrowserEvent(
                    type="interview.reply",
                    text=frame.text,
                    dispatch_id=dispatch_id if isinstance(dispatch_id, int) else None,
                )
            )
        await self.push_frame(frame, direction)


def _waiting_status(session: InterviewSession) -> str:
    if getattr(session, "_thinking_at", None) is not None:
        return "Giving you time"
    return "Listening"


async def _emit_status(session: InterviewSession, emit: EventSink, status: str) -> None:
    question = session.controller.current_question
    await emit(
        InterviewBrowserEvent(
            type="interview.status",
            status=status,  # type: ignore[arg-type]
            phase=session.controller.phase.value,
            question_index=question.index + 1 if question is not None else None,
            question_count=len(session.config.question_rubric),
            playback_epoch=session.playback_epoch,
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
            llm.append_system_instruction("Conduct the interview in simple English.")
        llm.append_system_instruction(
            "Conduct a non-technical behavioural interview. Ask about real experiences with "
            "teamwork, communication, conflict, ownership, and setbacks. Accept examples from "
            "work, education, volunteering, or everyday life. Ask one focused follow-up at a time "
            "about the situation, the candidate's own action, outcome, or learning. "
            "Do not ask coding, backend engineering, system design, or technical trivia questions. "
            "Assess behavioural examples, not technical knowledge. "
            "The candidate may have studied only through class 10 or may have little or no formal "
            "schooling. Use short, familiar sentences in the selected interview language. "
            "For Tanglish use mostly Tamil; for Hinglish use mostly Hindi with occasional common "
            "English words; for English use simple everyday English. Never require literacy, "
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

        pipeline = Pipeline(
            [
                transport.input(),
                _ErrorBridge(emit, setup.language),
                stt,
                _CaptionBridge(emit),
                session,
                user,
                llm,
                guard,
                _ReplyBridge(emit),
                tts,
                transport.output(),
                _PlaybackBridge(session, emit, on_output_activity=set_output_active),
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

        @session.event_handler("on_transcript_recovery")
        async def transcript_recovery(_session, _error) -> None:
            await _emit_status(session, emit, "Reconnecting")

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
                    "language_code": "auto" if codemix else "en-IN",
                    "mode": "codemix" if codemix else "transcribe",
                }
            ),
            "tts": base.providers.tts.model_copy(
                update={
                    "language_code": {"hinglish": "hi-IN", "tanglish": "ta-IN"}.get(
                        setup.language, "en-IN"
                    )
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
