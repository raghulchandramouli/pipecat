"""Run the browser interview pipeline behind Pipecat's eval RTVI transport.

The eval bot deliberately constructs the same :class:`LiveInterviewPipelineFactory`
used by the browser server.  Only its transport changes: an ``EvalTransport`` lets
the repository eval harness act as the client while the interview's Sarvam, Gemini,
and Bulbul pipeline remains unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

from pipecat.evals.serializer import EvalSerializer
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from .browser_contract import BrowserInterviewSetup, InterviewBrowserEvent
from .pipeline import (
    InterviewPipelineFactory,
    LiveInterviewPipelineFactory,
    ManagedInterview,
    default_live_config,
    new_session_id,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7861


async def _discard_browser_event(_event: InterviewBrowserEvent) -> None:
    """Keep browser-only data-channel events out of the eval RTVI protocol."""


class EvalInterviewBot:
    """Own one bounded eval transport and its shared interview pipeline.

    Args:
        factory: The browser pipeline factory. Tests may inject a factory without
            provider credentials; production uses ``LiveInterviewPipelineFactory``.
        host: Interface on which the eval WebSocket server listens.
        port: TCP port on which the eval WebSocket server listens.
        setup: Server-owned interview setup for the eval session.
    """

    def __init__(
        self,
        *,
        factory: InterviewPipelineFactory,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        setup: BrowserInterviewSetup | None = None,
    ) -> None:
        """Initialize the eval host before its worker is started."""
        self._factory = factory
        self._host = host
        self._port = port
        self._setup = setup or BrowserInterviewSetup()
        self._runner: WorkerRunner | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._managed: ManagedInterview | None = None
        self._transport: EvalTransport | None = None
        self._listening = asyncio.Event()
        self._closed = False

    @property
    def transport(self) -> EvalTransport:
        """Return the active eval transport after :meth:`start`."""
        if self._transport is None:
            raise RuntimeError("eval bot has not started")
        return self._transport

    @property
    def managed(self) -> ManagedInterview:
        """Return the factory-owned session resources after :meth:`start`."""
        if self._managed is None:
            raise RuntimeError("eval bot has not started")
        return self._managed

    async def start(self, *, timeout: float = 5.0) -> None:
        """Start the WorkerRunner and wait until the WebSocket is listening.

        Audio input remains gated until an RTVI client sends ``client-ready``.
        That event also starts the shared interview session, matching the browser
        client's explicit readiness gate.

        Args:
            timeout: Maximum seconds to wait for the server socket to bind.
        """
        if self._runner_task is not None:
            raise RuntimeError("eval bot is already started")

        transport = EvalTransport(
            host=self._host,
            port=self._port,
            params=EvalTransportParams(
                serializer=EvalSerializer(),
                audio_in_enabled=True,
                audio_in_sample_rate=16_000,
                audio_in_stream_on_start=False,
                audio_out_enabled=True,
                audio_out_sample_rate=24_000,
                audio_out_auto_silence=True,
            ),
        )
        self._transport = transport
        self._managed = await self._factory.create(
            session_id=new_session_id(),
            setup=self._setup,
            transport=transport,
            emit=_discard_browser_event,
        )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        self._runner = runner

        @transport.event_handler("on_websocket_ready")
        async def websocket_ready(_transport: BaseTransport) -> None:
            self._listening.set()

        @self._managed.worker.rtvi.event_handler("on_client_ready")
        async def client_ready(_rtvi) -> None:
            await self.managed.ready()

        @transport.event_handler("on_client_disconnected")
        async def client_disconnected(_transport: BaseTransport, _client) -> None:
            # EvalTransport emits this only when the harness requested
            # trigger_disconnect, so ordinary multi-scenario sessions stay alive.
            await self.close()

        await runner.add_workers(self._managed.worker)
        self._runner_task = asyncio.create_task(runner.run(), name="interview-eval-runner")
        try:
            await asyncio.wait_for(self._listening.wait(), timeout=timeout)
        except BaseException:
            await self.close()
            raise

    async def wait_closed(self) -> None:
        """Wait until the worker runner has finished."""
        if self._runner_task is not None:
            await self._runner_task

    async def close(self) -> None:
        """Cancel the shared pipeline and release the eval listener once."""
        if self._closed:
            return
        self._closed = True
        if self._managed is not None:
            await self._managed.close()
        if self._runner is not None:
            await self._runner.cancel(reason="eval bot shutdown")
        if self._runner_task is not None:
            await self._runner_task


def load_demo_environment() -> None:
    """Load backend credentials from ``demo/.env`` without exposing their values."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the interview eval RTVI bot")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO"), default="INFO")
    return parser.parse_args()


async def run_live_eval_bot(*, host: str, port: int) -> None:
    """Run the live factory until the eval harness cancels or disconnects."""
    load_demo_environment()
    bot = EvalInterviewBot(
        factory=LiveInterviewPipelineFactory.from_environment(default_live_config()),
        host=host,
        port=port,
    )
    await bot.start()
    try:
        await bot.wait_closed()
    finally:
        await bot.close()


def main() -> NoReturn:
    """Run the command-line eval entrypoint."""
    import sys

    from loguru import logger

    args = _parse_args()
    logger.remove()
    logger.add(sys.stderr, level=args.log_level)
    asyncio.run(run_live_eval_bot(host=args.host, port=args.port))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
