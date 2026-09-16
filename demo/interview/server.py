"""FastAPI application that owns isolated SmallWebRTC interview sessions."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict, deque
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

from .browser_contract import (
    BrowserInterviewCommand,
    BrowserInterviewSetup,
    BrowserReady,
    InterviewBrowserEvent,
)
from .pipeline import (
    BrowserEventEmitter,
    InterviewPipelineFactory,
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
    connection_generation: int = 1
    negotiated_capabilities: frozenset[str] = field(default_factory=frozenset)
    command_cache: OrderedDict[int, tuple[str, dict[str, Any]]] = field(default_factory=OrderedDict)
    command_highwater: int = 0
    command_times: deque[float] = field(default_factory=deque)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_INTERACTION_CAPABILITY = "interaction_controls_v1"
_COMMAND_CACHE_SIZE = 128
_COMMAND_RATE_LIMIT = 5
_COMMAND_WINDOW_SECONDS = 1.0
_COMMAND_MAX_BYTES = 2_048


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
            constructs the Gemini native audio factory from backend environment
            variables.

    Returns:
        A FastAPI application serving public setup, SDP, ICE, and teardown routes.
    """
    if factory is None:
        from .gemini_conversation import GeminiConversationPipelineFactory

        factory = GeminiConversationPipelineFactory.from_environment(default_live_config())
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

            emitter = BrowserEventEmitter(
                session_id=session_id,
                connection_generation=record.connection_generation,
                send=send,
            )

            async def emit(event: InterviewBrowserEvent) -> None:
                if event.type == "interview.command_result" and not _controls_negotiated(record):
                    return
                event = _event_for_capabilities(record, event)
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
                    if not isinstance(message, dict) or record.closed:
                        return
                    message_type = message.get("type")
                    if message_type == "interview.ready" and not record.ready:
                        capability_fallback = False
                        try:
                            ready_message = BrowserReady.model_validate(message)
                        except ValueError:
                            try:
                                ready_message = BrowserReady.model_validate(
                                    {
                                        key: value
                                        for key, value in message.items()
                                        if key != "capabilities"
                                    }
                                )
                            except ValueError:
                                await emit(
                                    InterviewBrowserEvent(
                                        type="interview.error",
                                        message="Browser readiness could not be verified. Reload and try again.",
                                        recoverable=True,
                                    )
                                )
                                return
                            capability_fallback = True
                        if (
                            ready_message.session_id is not None
                            and ready_message.session_id != session_id
                        ):
                            await emit(
                                InterviewBrowserEvent(
                                    type="interview.error",
                                    message="Browser readiness belongs to another interview. Reload and try again.",
                                    recoverable=True,
                                )
                            )
                            return
                        record.negotiated_capabilities = frozenset(
                            capability
                            for capability in ready_message.capabilities or ()
                            if capability == _INTERACTION_CAPABILITY
                        )
                        if capability_fallback:
                            await emit(
                                InterviewBrowserEvent(
                                    type="interview.error",
                                    message="Browser controls are unavailable. The interview will continue by voice.",
                                    recoverable=True,
                                )
                            )
                        record.ready = True
                        await managed.ready()
                    elif message_type == "interview.command":
                        await _handle_browser_command(
                            record,
                            session_id=session_id,
                            message=message,
                            emit=emit,
                        )

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
        snapshot = _interaction_snapshot(record)
        return {
            "session_id": session_id,
            "connection_generation": record.connection_generation,
            "status": record.status,
            "phase": session.controller.phase.value if session is not None else "connecting",
            "question_index": question.index + 1 if question is not None else 0,
            "question_count": len(session.config.question_rubric)
            if session is not None
            else len(record.setup.rubric),
            "provisional_caption": record.caption,
            "capabilities": sorted(record.negotiated_capabilities),
            **snapshot,
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


def _controls_negotiated(record: _SessionRecord) -> bool:
    """Return whether this established data channel may use interaction controls."""
    return _INTERACTION_CAPABILITY in record.negotiated_capabilities


def _event_for_capabilities(
    record: _SessionRecord, event: InterviewBrowserEvent
) -> InterviewBrowserEvent:
    """Keep the v1 event surface unchanged until the client has negotiated controls."""
    if _controls_negotiated(record):
        if event.type == "interview.status":
            return event.model_copy(update={"capabilities": [_INTERACTION_CAPABILITY]})
        return event
    return event.model_copy(
        update={
            "capabilities": None,
            "prompt_revision": None,
            "state_revision": None,
            "question_text": None,
            "interaction_state": None,
            "permitted_actions": None,
            "recovery_token": None,
        }
    )


def _interaction_snapshot(record: _SessionRecord) -> dict[str, Any]:
    """Return a read-only interaction view without borrowing data-channel sequence numbers."""
    session = record.managed.session if record.managed is not None else None
    snapshot_getter = getattr(session, "interaction_snapshot", None)
    if not callable(snapshot_getter):
        return {
            "prompt_revision": 0,
            "state_revision": 0,
            "question_text": None,
            "interaction_state": "connecting",
            "permitted_actions": [],
            "recovery_token": None,
        }
    snapshot = snapshot_getter()
    return {
        "prompt_revision": snapshot["prompt_revision"],
        "state_revision": snapshot["state_revision"],
        "question_text": snapshot["question_text"],
        "interaction_state": snapshot["interaction_state"],
        "permitted_actions": snapshot["permitted_actions"],
        "recovery_token": snapshot["recovery_token"],
    }


async def _handle_browser_command(
    record: _SessionRecord,
    *,
    session_id: str,
    message: dict[str, Any],
    emit,
) -> None:
    """Validate, serialize, apply, and acknowledge one browser action.

    The cache stores semantic results rather than data-channel envelopes so a resend
    gets a fresh server sequence number and cannot be dropped as an old event.
    """
    if not _controls_negotiated(record) or record.closed or record.managed is None:
        return
    try:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return
    if len(encoded) > _COMMAND_MAX_BYTES:
        await _emit_rejected_command(message, emit, record, "payload_too_large")
        return
    try:
        command = BrowserInterviewCommand.model_validate(message)
    except ValueError:
        await _emit_rejected_command(message, emit, record, "invalid_command")
        return

    canonical = json.dumps(
        command.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    async with record.command_lock:
        cached = record.command_cache.get(command.command_id)
        if cached is not None:
            cached_payload, result = cached
            if canonical == cached_payload:
                await emit(InterviewBrowserEvent(**result))
            else:
                await _emit_command_result(
                    emit,
                    record,
                    command.command_id,
                    outcome="rejected",
                    code="command_id_conflict",
                )
            return
        if command.command_id <= record.command_highwater:
            await _emit_command_result(
                emit,
                record,
                command.command_id,
                outcome="rejected",
                code="command_id_expired",
            )
            return
        if command.session_id != session_id:
            await _emit_command_result(
                emit, record, command.command_id, outcome="rejected", code="foreign_session"
            )
            return
        if command.connection_generation != record.connection_generation:
            await _emit_command_result(
                emit, record, command.command_id, outcome="rejected", code="foreign_connection"
            )
            return

        record.command_highwater = command.command_id
        snapshot = _interaction_snapshot(record)
        if command.prompt_revision != snapshot["prompt_revision"]:
            result = _command_result(
                command.command_id,
                outcome="rejected",
                code="stale_prompt",
                prompt_revision=snapshot["prompt_revision"],
            )
        elif command.action not in snapshot["permitted_actions"]:
            result = _command_result(
                command.command_id,
                outcome="rejected",
                code="unavailable",
                prompt_revision=snapshot["prompt_revision"],
            )
        elif command.action != "end" and not _within_command_rate(record):
            result = _command_result(
                command.command_id,
                outcome="rejected",
                code="rate_limited",
                prompt_revision=snapshot["prompt_revision"],
            )
        else:
            try:
                record.managed.session.apply_interaction_action(
                    command.action, recovery_token=command.recovery_token
                )
            except ValueError:
                result = _command_result(
                    command.command_id,
                    outcome="rejected",
                    code="unavailable",
                    prompt_revision=snapshot["prompt_revision"],
                )
            except Exception:
                result = _command_result(
                    command.command_id,
                    outcome="failed",
                    code="action_failed",
                    prompt_revision=snapshot["prompt_revision"],
                )
            else:
                result = _command_result(
                    command.command_id,
                    outcome="applied",
                    code="ok",
                    prompt_revision=_interaction_snapshot(record)["prompt_revision"],
                )
        _cache_command_result(record, command.command_id, canonical, result)
        await emit(InterviewBrowserEvent(**result))


def _within_command_rate(record: _SessionRecord) -> bool:
    """Record at most five new non-end actions in each rolling one-second window."""
    now = time.monotonic()
    while record.command_times and now - record.command_times[0] >= _COMMAND_WINDOW_SECONDS:
        record.command_times.popleft()
    if len(record.command_times) >= _COMMAND_RATE_LIMIT:
        return False
    record.command_times.append(now)
    return True


def _command_result(
    command_id: int,
    *,
    outcome: str,
    code: str,
    prompt_revision: object,
) -> dict[str, Any]:
    """Build the cacheable semantic portion of a command result."""
    return {
        "type": "interview.command_result",
        "command_id": command_id,
        "outcome": outcome,
        "code": code,
        "prompt_revision": prompt_revision,
    }


def _cache_command_result(
    record: _SessionRecord, command_id: int, canonical: str, result: dict[str, Any]
) -> None:
    """Keep the latest bounded command outcomes while retaining their high-water mark."""
    record.command_cache[command_id] = (canonical, result)
    while len(record.command_cache) > _COMMAND_CACHE_SIZE:
        record.command_cache.popitem(last=False)


async def _emit_command_result(
    emit,
    record: _SessionRecord,
    command_id: int,
    *,
    outcome: str,
    code: str,
) -> None:
    """Emit a non-cacheable rejection for a request that never reached ingress."""
    await emit(
        InterviewBrowserEvent(
            **_command_result(
                command_id,
                outcome=outcome,
                code=code,
                prompt_revision=_interaction_snapshot(record)["prompt_revision"],
            )
        )
    )


async def _emit_rejected_command(
    message: dict[str, Any], emit, record: _SessionRecord, code: str
) -> None:
    """Acknowledge malformed commands only when their ID is itself a safe integer."""
    command_id = message.get("command_id")
    if isinstance(command_id, int) and not isinstance(command_id, bool) and command_id > 0:
        await _emit_command_result(emit, record, command_id, outcome="rejected", code=code)


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
