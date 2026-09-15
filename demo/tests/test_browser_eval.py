"""Eval transport coverage for the browser interview's shared pipeline factory."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import pytest

from demo.interview.eval_bot import EvalInterviewBot
from demo.interview.pipeline import ManagedInterview
from pipecat.evals.serializer import EvalSerializer
from pipecat.evals.transport import EvalTransport
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.transports.websocket.rtvi_client import RTVIClientTransport
from pipecat.workers.runner import WorkerRunner


def _free_port() -> int:
    """Reserve an available local TCP port for one short-lived test server."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _eventually(predicate, *, timeout: float = 5.0) -> None:
    """Wait for an asynchronous condition without depending on implementation delays."""

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


@dataclass
class _Factory:
    """Factory seam that retains the real eval transport and RTVI worker behavior."""

    ready_calls: int = 0
    close_calls: int = 0
    transports: list[EvalTransport] = field(default_factory=list)

    async def create(self, *, session_id, setup, transport, emit) -> ManagedInterview:
        """Build a transport-only worker so the test requires no provider credentials."""
        assert session_id
        assert setup.language == "tanglish"
        self.transports.append(transport)
        worker = PipelineWorker(Pipeline([transport.input(), transport.output()]))

        async def ready() -> None:
            self.ready_calls += 1

        async def close() -> None:
            self.close_calls += 1
            await worker.cancel()

        return ManagedInterview(
            session=object(), worker=worker, transport=transport, ready=ready, close=close
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_eval_client_handshake_gates_session_ready_and_bot_teardown():
    """A real RTVI client receives ready only after the shared factory's ready gate."""
    port = _free_port()
    factory = _Factory()
    bot = EvalInterviewBot(factory=factory, host="127.0.0.1", port=port)
    client_runner: WorkerRunner | None = None
    client_task: asyncio.Task[None] | None = None
    try:
        await bot.start()
        assert factory.ready_calls == 0
        assert len(factory.transports) == 1
        transport = factory.transports[0]
        assert isinstance(transport, EvalTransport)
        assert isinstance(transport._params.serializer, EvalSerializer)
        assert transport._params.audio_in_sample_rate == 16_000
        assert transport._params.audio_out_sample_rate == 24_000
        assert not transport._params.audio_in_stream_on_start

        client = RTVIClientTransport(f"ws://127.0.0.1:{port}")
        client_ready = asyncio.Event()

        @client.event_handler("on_bot_ready")
        async def bot_ready(_transport) -> None:
            client_ready.set()

        client_worker = PipelineWorker(
            Pipeline([client.input(), client.output()]),
            enable_rtvi=False,
            cancel_on_idle_timeout=False,
        )
        client_runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await client_runner.add_workers(client_worker)
        client_task = asyncio.create_task(client_runner.run(), name="eval-test-client")

        await asyncio.wait_for(client_ready.wait(), timeout=5.0)
        await _eventually(lambda: factory.ready_calls == 1)
        assert client.bot_ready

        await bot.close()
        await asyncio.wait_for(bot.wait_closed(), timeout=5.0)
        assert factory.close_calls == 1
    finally:
        if client_runner is not None:
            await client_runner.cancel(reason="eval test cleanup")
        if client_task is not None:
            await client_task
        await bot.close()
