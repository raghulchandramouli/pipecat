"""Local Apple Silicon Rumik server configuration and thread-admission coverage."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from demo.rumik.admission import AdmissionQuarantinedError, AdmissionTimeoutError
from demo.rumik.mac_server import (
    MacRumikOSS,
    MacServerConfig,
    configure_model_caches,
    create_admission,
    create_app,
    resolve_runtime,
)


class _MPS:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _Torch:
    float32 = object()
    float16 = object()

    def __init__(self, mps_available: bool) -> None:
        self.backends = SimpleNamespace(mps=_MPS(mps_available))
        self.cpu_threads: int | None = None

    def set_num_threads(self, count: int) -> None:
        self.cpu_threads = count


class _Model:
    def __init__(self) -> None:
        self.config = SimpleNamespace(speakers=("Ira", "Aisha"))
        self.devices: list[str] = []

    def eval(self) -> _Model:
        return self

    def to(self, device: str) -> _Model:
        self.devices.append(device)
        return self


class _Transformers:
    def __init__(self) -> None:
        self.model = _Model()
        self.mimi = _Model()
        self.model_load: dict[str, object] | None = None
        self.mimi_load: dict[str, object] | None = None
        self.AutoTokenizer = SimpleNamespace(from_pretrained=lambda *_args, **_kwargs: object())
        self.AutoFeatureExtractor = SimpleNamespace(
            from_pretrained=lambda *_args, **_kwargs: SimpleNamespace(sampling_rate=24_000)
        )
        self.AutoModelForCausalLM = SimpleNamespace(from_pretrained=self._load_model)
        self.MimiModel = SimpleNamespace(from_pretrained=self._load_mimi)

    def _load_model(self, _root: Path, **kwargs: object) -> _Model:
        self.model_load = kwargs
        return self.model

    def _load_mimi(self, _root: Path, **kwargs: object) -> _Model:
        self.mimi_load = kwargs
        return self.mimi


class _Request:
    def __init__(self, **values: object) -> None:
        self.values = values


@pytest.mark.parametrize(
    ("requested", "mps_available", "expected"),
    (("auto", True, "mps"), ("auto", False, "cpu"), ("cpu", True, "cpu")),
)
def test_resolve_runtime_selects_mps_or_cpu(
    requested: str, mps_available: bool, expected: str
) -> None:
    """Automatic selection uses MPS when available and otherwise remains on CPU."""
    torch = _Torch(mps_available)

    runtime = resolve_runtime(torch, requested, "float32")

    assert runtime.device == expected
    assert runtime.dtype is torch.float32


def test_resolve_runtime_rejects_unavailable_mps_and_cpu_float16() -> None:
    """Explicit MPS and reduced precision fail rather than silently changing local execution."""
    with pytest.raises(RuntimeError, match="MPS"):
        resolve_runtime(_Torch(False), "mps", "float32")
    with pytest.raises(ValueError, match="float16"):
        resolve_runtime(_Torch(False), "cpu", "float16")


def test_mac_engine_loads_float32_mps_and_applies_cpu_thread_limit(tmp_path: Path) -> None:
    """The local engine sends the selected dtype/device to model and codec loaders."""
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    torch = _Torch(True)
    transformers = _Transformers()
    config = MacServerConfig(repo_id=str(checkpoint), device="mps", dtype="float32")

    engine = MacRumikOSS(
        config,
        torch_module=torch,
        transformers_module=transformers,
    )

    assert engine.device == "mps"
    assert transformers.model_load == {
        "trust_remote_code": True,
        "dtype": torch.float32,
        "attn_implementation": "sdpa",
    }
    assert transformers.mimi_load == {"dtype": torch.float32}
    assert transformers.model.devices == ["mps"]
    assert transformers.mimi.devices == ["mps"]

    cpu_torch = _Torch(False)
    MacRumikOSS(
        MacServerConfig(repo_id=str(checkpoint), device="cpu", cpu_threads=3),
        torch_module=cpu_torch,
        transformers_module=_Transformers(),
    )
    assert cpu_torch.cpu_threads == 3


def test_configure_model_caches_stays_under_repository_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model cache environment variables never default to a user-global cache directory."""
    for name in ("HF_HOME", "HF_MODULES_CACHE", "TORCH_HOME"):
        monkeypatch.delenv(name, raising=False)

    configure_model_caches()

    for name in ("HF_HOME", "HF_MODULES_CACHE", "TORCH_HOME"):
        value = Path(os.environ[name])
        assert value.is_relative_to(Path(__file__).resolve().parents[2] / "models")


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_release_synchronous_inference_thread() -> None:
    """A waiting request cannot enter a second ``to_thread`` synthesis while the first runs."""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls: list[int] = []

    class Engine:
        def synthesize(self, request: _Request) -> bytes:
            request_id = int(request.values["id"])
            calls.append(request_id)
            if request_id == 1:
                loop.call_soon_threadsafe(started.set)
                release.wait()
            return str(request_id).encode()

    gate = create_admission(
        Engine(),  # type: ignore[arg-type]
        _Request,
        MacServerConfig(timeout_seconds=2.0),
    )
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(gate.submit({"id": 2}))
        await asyncio.sleep(0.01)
        assert calls == [1]
        release.set()
        assert (await asyncio.wait_for(second, timeout=1)).value == b"2"
        assert calls == [1, 2]
    finally:
        release.set()
        await gate.close()


@pytest.mark.asyncio
async def test_timed_out_thread_quarantines_without_admitting_a_second_thread() -> None:
    """Timeout cannot treat cancellation of ``to_thread`` as proof that inference stopped."""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls: list[int] = []

    class Engine:
        def synthesize(self, request: _Request) -> bytes:
            calls.append(int(request.values["id"]))
            loop.call_soon_threadsafe(started.set)
            release.wait()
            return b"done"

    gate = create_admission(
        Engine(),  # type: ignore[arg-type]
        _Request,
        MacServerConfig(timeout_seconds=0.01),
    )
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        queued = asyncio.create_task(gate.submit({"id": 2}))
        with pytest.raises(AdmissionTimeoutError):
            await first
        with pytest.raises(AdmissionQuarantinedError):
            await queued
        await asyncio.sleep(0.01)
        assert calls == [1]
    finally:
        release.set()
        await gate.close()


def test_completed_local_errors_return_http_400_without_quarantining_server() -> None:
    """Invalid speakers and completed engine errors leave later speech requests available."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    class Engine:
        runtime = SimpleNamespace(device="mps", dtype_name="float32")

        def synthesize(self, request: object) -> bytes:
            speaker = request.speaker  # type: ignore[attr-defined]
            text = request.input  # type: ignore[attr-defined]
            if speaker != "Ira":
                raise ValueError("speaker must be one of: Ira")
            if text == "runtime-error":
                raise RuntimeError("codec rejected generated frames")
            return b"RIFFvalid-wav"

    app = create_app(MacServerConfig(), engine=Engine())  # type: ignore[arg-type]
    with TestClient(app) as client:
        invalid_speaker = client.post(
            "/v1/audio/speech", json={"input": "hello", "speaker": "Nope"}
        )
        valid_after_speaker = client.post("/v1/audio/speech", json={"input": "hello"})
        runtime_error = client.post("/v1/audio/speech", json={"input": "runtime-error"})
        valid_after_runtime = client.post("/v1/audio/speech", json={"input": "again"})

    assert invalid_speaker.status_code == 400
    assert valid_after_speaker.status_code == 200
    assert runtime_error.status_code == 400
    assert valid_after_runtime.status_code == 200
