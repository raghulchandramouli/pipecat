"""FastAPI application that owns isolated SmallWebRTC interview sessions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pipecat.frames.frames import OutputTransportMessageFrame
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.workers.runner import WorkerRunner

from .browser_contract import BrowserInterviewSetup, InterviewBrowserEvent
from .pipeline import (
    BrowserEventEmitter,
    InterviewPipelineFactory,
    LiveInterviewPipelineFactory,
    ManagedInterview,
    create_smallwebrtc_transport,
    default_live_config,
    new_session_id,
)


@dataclass
class _SessionRecord:
    """Server-only browser session record.

    Parameters:
        setup: Validated public interview choices.
        managed: Pipeline resources created after SDP offer acceptance.
        pc_id: Peer connection identity bound to this session.
        status: Last public status for REST reconnect state.
        caption: Last public caption for REST reconnect state.
    """

    setup: BrowserInterviewSetup
    managed: ManagedInterview | None = None
    pc_id: str | None = None
    status: str = "Reconnecting"
    caption: str | None = None
    negotiating: bool = False
    connection: Any | None = None
    ready: bool = False
    closed: bool = False


@dataclass
class _ServerState:
    """Long-lived FastAPI state shared only by session lifecycle handlers.

    Parameters:
        factory: Application factory for live or injected test pipelines.
        sessions: Opaque session records keyed by server-generated IDs.
        handler: Shared SmallWebRTC SDP and ICE handler.
        runner: Worker runner installed during app startup.
        runner_task: Background runner lifetime task.
    """

    factory: InterviewPipelineFactory
    sessions: dict[str, _SessionRecord] = field(default_factory=dict)
    handler: SmallWebRTCRequestHandler = field(default_factory=SmallWebRTCRequestHandler)
    runner: WorkerRunner | None = None
    runner_task: asyncio.Task[None] | None = None


def create_interview_app(*, factory: InterviewPipelineFactory | None = None) -> FastAPI:
    """Create the browser interview API with no module-global session state.

    Args:
        factory: Optional injected factory for deterministic tests. Omitting it
            constructs the real Sarvam, Gemini, and Bulbul factory from backend
            environment variables.

    Returns:
        A FastAPI application serving public setup, SDP, ICE, and teardown routes.
    """
    factory = factory or LiveInterviewPipelineFactory.from_environment(default_live_config())
    state = _ServerState(factory=factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        state.runner = runner
        state.runner_task = asyncio.create_task(runner.run(auto_end=False), name="interview-runner")
        try:
            yield
        finally:
            await asyncio.gather(
                *(_close_record(record, disconnect=False) for record in state.sessions.values()),
                return_exceptions=True,
            )
            await state.handler.close()
            await runner.cancel(reason="browser server shutdown")
            if state.runner_task is not None:
                await state.runner_task

    app = FastAPI(title="Interview practice", lifespan=lifespan)
    app.state.interview = state

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        """Return validation failures without reflecting request content or provider secrets."""
        return JSONResponse(status_code=422, content={"detail": "invalid interview request"})

    @app.post("/api/sessions")
    async def create_session(setup: BrowserInterviewSetup) -> dict[str, str]:
        session_id = new_session_id()
        state.sessions[session_id] = _SessionRecord(setup=setup)
        return {"session_id": session_id, "offer_url": f"/api/sessions/{session_id}/offer"}

    @app.post("/api/sessions/{session_id}/offer")
    async def offer(session_id: str, request: Request) -> dict[str, str]:
        record = _record_or_404(state, session_id)
        payload = await _json_object(request)
        if set(payload) - {"sdp", "type", "pc_id", "restart_pc"}:
            raise HTTPException(status_code=422, detail="unsupported offer fields")
        try:
            offer_request = SmallWebRTCRequest.from_dict(dict(payload))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="invalid WebRTC offer") from error
        if record.pc_id is None and offer_request.pc_id is not None:
            raise HTTPException(
                status_code=409, detail="unbound session cannot reuse a peer connection"
            )
        if record.pc_id is not None and offer_request.pc_id != record.pc_id:
            raise HTTPException(status_code=409, detail="peer connection does not match session")
        if record.closed or record.managed is not None:
            raise HTTPException(status_code=409, detail="interview session is already connected")
        if record.negotiating:
            raise HTTPException(status_code=409, detail="WebRTC negotiation is already in progress")

        initialization_error: Exception | None = None

        async def connected(connection) -> None:
            nonlocal initialization_error
            if state.runner is None:
                raise RuntimeError("interview worker runner is not started")
            if record.closed or state.sessions.get(session_id) is not record:
                raise RuntimeError("interview session closed during WebRTC negotiation")
            record.connection = connection
            transport = create_smallwebrtc_transport(connection)

            async def send(message: dict[str, Any]) -> None:
                output = transport.output()
                await output.send_message(OutputTransportMessageFrame(message))

            emitter = BrowserEventEmitter(session_id=session_id, connection_generation=1, send=send)

            async def emit(event: InterviewBrowserEvent) -> None:
                if event.status is not None:
                    record.status = event.status
                if event.type == "interview.caption" and event.text is not None:
                    record.caption = event.text
                await emitter.emit(event)

            managed: ManagedInterview | None = None
            try:
                managed = await state.factory.create(
                    session_id=session_id,
                    setup=record.setup,
                    transport=transport,
                    emit=emit,
                )
                if record.closed or state.sessions.get(session_id) is not record:
                    await managed.close()
                    raise RuntimeError("interview session closed during WebRTC negotiation")
                record.managed = managed

                @transport.event_handler("on_client_connected")
                async def client_connected(_transport, _client) -> None:
                    await emit(
                        InterviewBrowserEvent(type="interview.status", status="Reconnecting")
                    )

                @transport.event_handler("on_app_message")
                async def app_message(_transport, message, _client) -> None:
                    if (
                        isinstance(message, dict)
                        and message.get("type") == "interview.ready"
                        and not record.ready
                        and not record.closed
                    ):
                        record.ready = True
                        await managed.ready()

                @transport.event_handler("on_client_disconnected")
                async def client_disconnected(_transport, _client) -> None:
                    await _remove_session(state, session_id, record, disconnect=False)

                await state.runner.add_workers(managed.worker)
            except Exception as error:
                initialization_error = error
                if record.managed is managed:
                    record.managed = None
                if managed is not None:
                    await managed.close()
                raise

        record.negotiating = True
        try:
            answer = await state.handler.handle_web_request(offer_request, connected)
        except HTTPException:
            raise
        except Exception as error:
            if initialization_error is not None:
                await _remove_session(state, session_id, record, disconnect=True)
            raise HTTPException(status_code=400, detail="WebRTC negotiation failed") from error
        finally:
            record.negotiating = False
        if initialization_error is not None:
            await _remove_session(state, session_id, record, disconnect=True)
            raise HTTPException(
                status_code=400, detail="WebRTC session initialization failed"
            ) from initialization_error
        if answer is None:
            raise HTTPException(status_code=500, detail="WebRTC negotiation produced no answer")
        if record.managed is None:
            if record.connection is not None:
                await record.connection.disconnect()
                record.connection = None
            raise HTTPException(status_code=400, detail="WebRTC session initialization failed")
        answer_pc_id = answer.get("pc_id")
        if not isinstance(answer_pc_id, str):
            raise HTTPException(status_code=500, detail="WebRTC answer missing peer identity")
        record.pc_id = answer_pc_id
        return answer

    @app.patch("/api/sessions/{session_id}/ice", status_code=204)
    async def ice(session_id: str, request: Request) -> Response:
        record = _record_or_404(state, session_id)
        payload = await _json_object(request)
        if payload.get("pc_id") != record.pc_id:
            raise HTTPException(status_code=409, detail="peer connection does not match session")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise HTTPException(status_code=422, detail="candidates must be a list")
        try:
            patch = SmallWebRTCPatchRequest(
                pc_id=record.pc_id or "",
                candidates=[IceCandidate(**candidate) for candidate in candidates],
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="invalid ICE candidates") from error
        await state.handler.handle_patch_request(patch)
        return Response(status_code=204)

    @app.get("/api/sessions/{session_id}")
    async def session_status(session_id: str) -> dict[str, Any]:
        record = _record_or_404(state, session_id)
        session = record.managed.session if record.managed is not None else None
        question = session.controller.current_question if session is not None else None
        return {
            "session_id": session_id,
            "status": record.status,
            "phase": session.controller.phase.value if session is not None else "connecting",
            "question_index": question.index + 1 if question is not None else 0,
            "question_count": len(session.config.question_rubric)
            if session is not None
            else len(record.setup.rubric),
            "provisional_caption": record.caption,
        }

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> Response:
        record = _record_or_404(state, session_id)
        await _remove_session(state, session_id, record, disconnect=True)
        return Response(status_code=204)

    static_root = Path(__file__).with_name("web")
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="interview-browser")
    return app


def _record_or_404(state: _ServerState, session_id: str) -> _SessionRecord:
    record = state.sessions.get(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="interview session not found")
    return record


async def _remove_session(
    state: _ServerState, session_id: str, record: _SessionRecord, *, disconnect: bool
) -> None:
    """Detach one browser session before releasing its pipeline and peer connection."""
    if state.sessions.get(session_id) is not record:
        return
    state.sessions.pop(session_id, None)
    await _close_record(record, disconnect=disconnect)


async def _close_record(record: _SessionRecord, *, disconnect: bool) -> None:
    """Release a session's managed pipeline and, when requested, its peer connection once."""
    if record.closed:
        return
    record.closed = True
    managed, record.managed = record.managed, None
    connection, record.connection = record.connection, None
    if managed is not None:
        await managed.close()
    if disconnect and connection is not None:
        await connection.disconnect()


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="request body must be JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be an object")
    return payload


def main() -> None:
    """Run the local browser interview server with backend credentials from ``demo/.env``."""
    import argparse
    import sys

    import uvicorn
    from dotenv import load_dotenv
    from loguru import logger

    parser = argparse.ArgumentParser(description="Run the browser interview practice server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO"), default="INFO")
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level=args.log_level)
    load_dotenv(Path(__file__).parents[1] / ".env", override=False)
    uvicorn.run(create_interview_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
