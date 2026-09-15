"""Google text-LLM integration with transcript admission at the provider boundary."""

import asyncio
from collections.abc import AsyncIterator

from google.genai.types import GenerateContentResponse

from pipecat.frames.frames import LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService
from pipecat.turns.user_turn_completion_mixin import USER_TURN_COMPLETION_INSTRUCTIONS

from .transcript_gate import TranscriptGatedLLMMixin, TranscriptLedgerProcessor


class InterviewGoogleLLMService(TranscriptGatedLLMMixin, GoogleLLMService):
    """Route all pipeline context runs through the interview transcript coordinator."""

    def __init__(self, *, coordinator: TranscriptLedgerProcessor, **kwargs):
        """Initialize gated Gemini with the design's explicit text-model settings.

        Args:
            coordinator: Shared transcript ledger and dispatch owner.
            **kwargs: Google service options, including backend API credentials.
        """
        kwargs.setdefault(
            "settings",
            GoogleLLMService.Settings(
                model="gemini-3.8-flash",
                thinking=GoogleLLMService.ThinkingConfig(thinking_level="low"),
            ),
        )
        super().__init__(coordinator=coordinator, **kwargs)
        self._strict_interview_replies = getattr(coordinator, "strict_replies", False)
        if self._strict_interview_replies:
            self.append_system_instruction(USER_TURN_COMPLETION_INSTRUCTIONS)

    async def _push_llm_text(self, text: str) -> None:
        if self._strict_interview_replies:
            # The application guard validates the entire response before any semantic event.
            if self.reports_ttfat:
                await self.stop_ttfat_metrics()
            await self.push_frame(LLMTextFrame(text))
        else:
            await super()._push_llm_text(text)

    async def _broadcast_turn_completion(self) -> None:
        if not self._strict_interview_replies:
            await super()._broadcast_turn_completion()

    async def run_function_calls(self, function_calls) -> None:
        """Keep model-requested tools from mutating interview state before reply validation."""
        if self._strict_interview_replies:
            if function_calls:
                await self.push_error("Interview model tools require controller authorization")
            return
        await super().run_function_calls(function_calls)

    async def _process_context(self, context: LLMContext) -> None:
        if not self._response_is_current():
            raise asyncio.CancelledError
        await super()._process_context(context)

    async def _stream_content(self, context: LLMContext) -> AsyncIterator[GenerateContentResponse]:
        # Every retry reaches this boundary before the SDK starts a provider request.
        if not self._response_is_current():
            raise asyncio.CancelledError
        return await super()._stream_content(context)

    async def run_inference(
        self,
        context: LLMContext,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> str | None:
        """Reject out-of-pipeline inference that would bypass transcript admission."""
        raise RuntimeError("Use a gated LLMContextFrame; direct interview inference is disabled")
