"""FastAPI browser-session lifecycle coverage with an injected pipeline factory."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import demo.interview.server as browser_server
from demo.interview.pipeline import ManagedInterview
from demo.interview.server import create_interview_app


class FakeOutput:
    """Collect public data-channel frames produced by a fake browser transport."""

    def __init__(self) -> None:
        """Initialize an empty ordered public-message capture."""
        self.messages: list[dict[str, object]] = []

    async def send_message(self, frame) -> None:
        """Store the public JSON payload from an output transport message."""
        self.messages.append(frame.message)


class FakeTransport:
    """SmallWebRTC transport seam exposing app-message lifecycle callbacks."""

    def __init__(self) -> None:
        """Initialize callback registration and a per-connection output sink."""
        self.handlers = {}
        self._output = FakeOutput()

    def output(self) -> FakeOutput:
        """Return the isolated browser data-channel output sink."""
        return self._output

    def event_handler(self, name: str):
        """Register the callback shape used by the real transport."""

        def register(callback):
            self.handlers[name] = callback
            return callback

        return register

    async def fire(self, name: str, message=None) -> None:
        """Invoke a registered transport event as the SmallWebRTC transport would."""
        callback = self.handlers[name]
        if name == "on_app_message":
            await callback(self, message, object())
        else:
            await callback(self, object())


class FakeManaged:
    """Per-session managed resources with observable activation and teardown."""

    def __init__(self, session_id: str) -> None:
        """Create isolated session metadata and asynchronous lifecycle callbacks."""
        self.session = SimpleNamespace(
            controller=SimpleNamespace(
                current_question=SimpleNamespace(index=1), phase=SimpleNamespace(value="question")
            ),
            config=SimpleNamespace(question_rubric=(object(), object())),
        )
        self.worker = object()
        self.session_id = session_id
        self.ready_calls = 0
        self.close_calls = 0

    async def ready(self) -> None:
        """Record browser media/data-channel readiness."""
        self.ready_calls += 1

    async def close(self) -> None:
        """Record deterministic session resource teardown."""
        self.close_calls += 1


class FakeFactory:
    """Inject fresh managed resources for every accepted browser connection."""

    def __init__(self) -> None:
        """Initialize call and event ownership records."""
        self.created: list[FakeManaged] = []
        self.emits = []

    async def create(self, *, session_id, setup, transport, emit) -> ManagedInterview:
        """Create one isolated fake session and retain its real server event sink."""
        managed = FakeManaged(session_id)
        self.created.append(managed)
        self.emits.append(emit)
        return ManagedInterview(
            session=managed.session,
            worker=managed.worker,
            transport=transport,
            ready=managed.ready,
            close=managed.close,
        )


class FakeHandler:
    """Invoke the server connection callback without SDP or network activity."""

    def __init__(self) -> None:
        """Initialize per-offer transport and ICE-call captures."""
        self.transports: list[FakeTransport] = []
        self.patches = []

    async def handle_web_request(self, request, connected):
        """Create one fake transport connection and return a server-owned peer ID."""
        connection = SimpleNamespace(disconnect=AsyncMock())
        await connected(connection)
        return {"sdp": "answer", "type": "answer", "pc_id": f"pc-{len(self.transports)}"}

    async def handle_patch_request(self, patch) -> None:
        """Record validated ICE patches without invoking aiortc."""
        self.patches.append(patch)

    async def close(self) -> None:
        """Satisfy FastAPI lifespan cleanup."""


def make_client(monkeypatch):
    """Build a TestClient whose WebRTC edges are deterministic and provider-free."""
    factory = FakeFactory()
    handler = FakeHandler()
    made_transports: list[FakeTransport] = []

    def make_transport(_connection):
        transport = FakeTransport()
        made_transports.append(transport)
        handler.transports.append(transport)
        return transport

    monkeypatch.setattr(browser_server, "create_smallwebrtc_transport", make_transport)
    app = create_interview_app(factory=factory)
    app.state.interview.handler = handler
    return TestClient(app), app, factory, made_transports


def create_session(client: TestClient, **setup) -> str:
    """Create one public browser session and return its opaque identifier."""
    response = client.post("/api/sessions", json=setup or {"role": "Backend engineer"})
    assert response.status_code == 200
    assert set(response.json()) == {"session_id", "offer_url"}
    return response.json()["session_id"]


def offer(client: TestClient, session_id: str):
    """Negotiate a synthetic SDP offer through the real API route."""
    return client.post(f"/api/sessions/{session_id}/offer", json={"sdp": "v=0", "type": "offer"})


def test_setup_validation_and_status_payload_never_echo_provider_secrets(monkeypatch):
    """Browser REST bodies expose setup and progress only, never backend configuration."""
    client, _app, _factory, _transports = make_client(monkeypatch)
    with client:
        rejected = client.post("/api/sessions", json={"api_key": "sentinel-secret"})
        assert rejected.status_code == 422
        assert "sentinel-secret" not in rejected.text

        session_id = create_session(client)
        status = client.get(f"/api/sessions/{session_id}")
        assert status.status_code == 200
        assert status.json() == {
            "session_id": session_id,
            "status": "Reconnecting",
            "phase": "connecting",
            "question_index": 0,
            "question_count": 5,
            "provisional_caption": None,
        }
        assert "api_key" not in status.text and "endpoint" not in status.text


def test_offer_waits_for_explicit_ready_and_duplicate_ready_is_idempotent(monkeypatch):
    """The greeting is deferred until the browser declares media and data-channel readiness."""
    client, app, factory, transports = make_client(monkeypatch)
    with client:
        app.state.interview.runner.add_workers = AsyncMock()
        session_id = create_session(client)
        response = offer(client, session_id)
        assert response.status_code == 200
        assert response.json()["pc_id"] == "pc-1"
        assert len(factory.created) == len(transports) == 1
        managed = factory.created[0]
        assert managed.ready_calls == 0

        transport = transports[0]
        client.portal.call(transport.fire, "on_client_connected")
        client.portal.call(transport.fire, "on_app_message", {"type": "other"})
        assert managed.ready_calls == 0
        client.portal.call(transport.fire, "on_app_message", {"type": "interview.ready"})
        client.portal.call(transport.fire, "on_app_message", {"type": "interview.ready"})
        assert managed.ready_calls == 1
        assert transport.output().messages[0]["status"] == "Reconnecting"
        assert client.get(f"/api/sessions/{session_id}").json()["question_index"] == 2


def test_sessions_bind_distinct_resources_and_reject_peer_connection_reuse(monkeypatch):
    """Two clients never share contexts, while a peer ID cannot be attached to another session."""
    client, app, factory, transports = make_client(monkeypatch)
    with client:
        app.state.interview.runner.add_workers = AsyncMock()
        first, second = create_session(client), create_session(client)
        assert offer(client, first).status_code == 200
        assert offer(client, second).status_code == 200
        assert [managed.session_id for managed in factory.created] == [first, second]
        assert factory.created[0].session is not factory.created[1].session
        assert transports[0] is not transports[1]

        stolen = client.post(
            f"/api/sessions/{second}/offer",
            json={"sdp": "v=0", "type": "offer", "pc_id": "pc-1"},
        )
        assert stolen.status_code == 409
        assert len(factory.created) == 2

        assert client.delete(f"/api/sessions/{first}").status_code == 204
        assert factory.created[0].close_calls == 1
        assert factory.created[1].close_calls == 0
        assert client.get(f"/api/sessions/{first}").status_code == 404


def test_duplicate_offer_cannot_replace_a_running_session_pipeline(monkeypatch):
    """A repeat offer cannot orphan the first worker or replace its session context."""
    client, app, factory, _transports = make_client(monkeypatch)
    with client:
        app.state.interview.runner.add_workers = AsyncMock()
        session_id = create_session(client)
        assert offer(client, session_id).status_code == 200
        assert (
            client.post(
                f"/api/sessions/{session_id}/offer",
                json={"sdp": "v=0", "type": "offer", "pc_id": "pc-1"},
            ).status_code
            == 409
        )
        assert len(factory.created) == 1
        assert factory.created[0].close_calls == 0


def test_client_disconnect_removes_session_and_closes_its_worker(monkeypatch):
    """An ungraceful browser disconnect releases the isolated server resources."""
    client, app, factory, transports = make_client(monkeypatch)
    with client:
        app.state.interview.runner.add_workers = AsyncMock()
        session_id = create_session(client)
        assert offer(client, session_id).status_code == 200
        client.portal.call(transports[0].fire, "on_client_disconnected")
        assert factory.created[0].close_calls == 1
        assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_offer_and_ice_reject_cross_session_or_unsupported_input(monkeypatch):
    """Negotiation accepts only a session's own opaque peer identity and valid candidate shape."""
    client, app, _factory, _transports = make_client(monkeypatch)
    with client:
        app.state.interview.runner.add_workers = AsyncMock()
        session_id = create_session(client)
        assert (
            client.post(f"/api/sessions/{session_id}/offer", json={"unexpected": 1}).status_code
            == 422
        )
        assert offer(client, session_id).status_code == 200
        assert (
            client.patch(
                f"/api/sessions/{session_id}/ice", json={"pc_id": "wrong", "candidates": []}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                f"/api/sessions/{session_id}/ice",
                json={"pc_id": "pc-1", "candidates": "not-a-list"},
            ).status_code
            == 422
        )
