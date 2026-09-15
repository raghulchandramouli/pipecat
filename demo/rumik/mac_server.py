"""Apple Silicon HTTP wrapper for the pinned Rumik checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import logging
import os
import resource
import sys
import wave
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import ModuleType
from typing import Any

from .admission import (
    AdmissionClosedError,
    AdmissionOverloadedError,
    AdmissionQuarantinedError,
    SingleFlightAdmission,
)

_REVISION = "0ed3c98684e14350c910129c0efa179841c41ad2"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MODEL_ROOT = _REPOSITORY_ROOT / "models"
_DEFAULT_REPOSITORY = _MODEL_ROOT / "rumik-oss-1" / _REVISION
_DEFAULT_SOURCE_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "rumik-source" / _REVISION / "server.py"
)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacServerConfig:
    """Configuration for one local Rumik process.

    Parameters:
        repo_id: Local checkpoint directory.
        device: ``"auto"``, ``"mps"``, or ``"cpu"`` model device selection.
        dtype: ``"float32"`` or ``"float16"`` model loading dtype.
        cpu_threads: Positive PyTorch CPU thread limit for CPU inference.
        queue_capacity: Number of waiting requests behind the one active inference.
        timeout_seconds: Positive inference timeout before permanent admission quarantine.
        source_path: Pinned upstream server used for request schema and browser UI.
    """

    repo_id: str = str(_DEFAULT_REPOSITORY)
    device: str = "auto"
    dtype: str = "float32"
    cpu_threads: int | None = None
    queue_capacity: int = 2
    timeout_seconds: float = 600.0
    source_path: Path = _DEFAULT_SOURCE_PATH

    def __post_init__(self) -> None:
        """Validate local-only runtime settings before model loading.

        Raises:
            ValueError: If a setting cannot describe one bounded local server.
        """
        if not isinstance(self.repo_id, str) or not self.repo_id.strip():
            raise ValueError("repo_id must be a non-empty string")
        if self.device not in {"auto", "mps", "cpu"}:
            raise ValueError("device must be 'auto', 'mps', or 'cpu'")
        if self.dtype not in {"float32", "float16"}:
            raise ValueError("dtype must be 'float32' or 'float16'")
        if self.cpu_threads is not None and (
            not isinstance(self.cpu_threads, int)
            or isinstance(self.cpu_threads, bool)
            or self.cpu_threads <= 0
        ):
            raise ValueError("cpu_threads must be a positive integer when supplied")
        if (
            isinstance(self.queue_capacity, bool)
            or not isinstance(self.queue_capacity, int)
            or self.queue_capacity != 2
        ):
            raise ValueError("queue_capacity is fixed at 2 for this local server")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if not self.source_path.is_file():
            raise ValueError("source_path must name the pinned upstream server.py")


@dataclass(frozen=True)
class MacRuntime:
    """Resolved PyTorch device and loading dtype.

    Parameters:
        device: Resolved PyTorch device name.
        dtype: Resolved PyTorch dtype object passed to model loaders.
        dtype_name: Requested dtype name for runtime reporting.
    """

    device: str
    dtype: Any
    dtype_name: str


@dataclass(frozen=True)
class LocalSynthesisError:
    """A completed local synthesis validation failure.

    Parameters:
        detail: Safe error text suitable for the HTTP response body.
    """

    detail: str


def resolve_runtime(torch_module: Any, device: str, dtype: str) -> MacRuntime:
    """Resolve a valid MPS or CPU runtime without silently changing an explicit request.

    Args:
        torch_module: Imported PyTorch module.
        device: Requested ``"auto"``, ``"mps"``, or ``"cpu"`` device.
        dtype: Requested ``"float32"`` or ``"float16"`` dtype.

    Returns:
        Runtime device and PyTorch dtype suitable for model loading.

    Raises:
        ValueError: If the device/dtype values are unsupported.
        RuntimeError: If explicitly requested MPS is unavailable.
    """
    if device not in {"auto", "mps", "cpu"}:
        raise ValueError("device must be 'auto', 'mps', or 'cpu'")
    if dtype not in {"float32", "float16"}:
        raise ValueError("dtype must be 'float32' or 'float16'")

    mps = getattr(getattr(torch_module, "backends", None), "mps", None)
    mps_available = bool(mps is not None and mps.is_available())
    if device == "mps" and not mps_available:
        raise RuntimeError("MPS was requested but is unavailable in this PyTorch build")
    resolved_device = "mps" if device == "mps" or (device == "auto" and mps_available) else "cpu"
    if resolved_device == "cpu" and dtype == "float16":
        raise ValueError("float16 is supported only with an available MPS device")
    return MacRuntime(resolved_device, getattr(torch_module, dtype), dtype)


def configure_model_caches() -> None:
    """Keep Hugging Face and PyTorch model caches inside this repository's model root."""
    hf_home = _MODEL_ROOT / ".hf-cache"
    hf_modules = _MODEL_ROOT / ".hf-modules"
    torch_home = _MODEL_ROOT / ".torch-cache"
    for path in (hf_home, hf_modules, torch_home):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_MODULES_CACHE"] = str(hf_modules)
    os.environ["TORCH_HOME"] = str(torch_home)


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one local source file.

    Args:
        path: File to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_health(torch_module: Any, engine: MacRumikOSS | None) -> dict[str, Any]:
    """Return local runtime facts suitable for measured host evidence.

    Args:
        torch_module: Imported PyTorch module.
        engine: Loaded engine, or ``None`` while startup is in progress.

    Returns:
        Runtime selection and process/MPS allocation measurements.
    """
    report: dict[str, Any] = {
        "loaded": engine is not None,
        "max_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "mps_current_allocated_bytes": None,
        "mps_driver_allocated_bytes": None,
    }
    if engine is None:
        return report
    report.update(
        {
            "requested_device": engine._config.device,
            "resolved_device": engine.runtime.device,
            "requested_dtype": engine._config.dtype,
            "resolved_dtype": engine.runtime.dtype_name,
            "cpu_threads": engine._config.cpu_threads,
        }
    )
    if engine.runtime.device == "mps":
        mps = torch_module.mps
        report["mps_current_allocated_bytes"] = int(mps.current_allocated_memory())
        report["mps_driver_allocated_bytes"] = int(mps.driver_allocated_memory())
    return report


def _load_upstream(path: Path) -> ModuleType:
    """Load the pinned upstream server module without modifying its source.

    Args:
        path: Pinned ``server.py`` path.

    Returns:
        Loaded upstream server module.

    Raises:
        RuntimeError: If the pinned module cannot be loaded.
    """
    spec = importlib.util.spec_from_file_location("rumik_pinned_server", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load pinned Rumik server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MacRumikOSS:
    """Local MPS/CPU Rumik engine preserving the upstream waveform implementation."""

    def __init__(
        self,
        config: MacServerConfig,
        *,
        torch_module: Any,
        transformers_module: Any,
    ) -> None:
        """Load a Rumik model, codec, and tokenizer on the selected local device.

        Args:
            config: Local model and device configuration.
            torch_module: Imported PyTorch module.
            transformers_module: Imported Transformers module.
        """
        self._config = config
        self._torch = torch_module
        runtime = resolve_runtime(torch_module, config.device, config.dtype)
        if runtime.device == "cpu" and config.cpu_threads is not None:
            torch_module.set_num_threads(config.cpu_threads)
        root = Path(config.repo_id)
        if not root.is_dir():
            raise FileNotFoundError(f"local Rumik checkpoint does not exist: {root}")
        self.runtime = runtime
        self.device = runtime.device
        self.tokenizer = transformers_module.AutoTokenizer.from_pretrained(
            root, trust_remote_code=True
        )
        self.model = (
            transformers_module.AutoModelForCausalLM.from_pretrained(
                root,
                trust_remote_code=True,
                dtype=runtime.dtype,
                attn_implementation="sdpa",
            )
            .eval()
            .to(runtime.device)
        )
        self.mimi = (
            transformers_module.MimiModel.from_pretrained(root / "codec", dtype=runtime.dtype)
            .eval()
            .to(runtime.device)
        )
        self.sample_rate = int(
            transformers_module.AutoFeatureExtractor.from_pretrained(root / "codec").sampling_rate
        )
        self.speakers = tuple(self.model.config.speakers)

    def synthesize(self, req: Any) -> bytes:
        """Synthesize one upstream-compatible mono PCM16 WAV response.

        Args:
            req: Pinned upstream request model with speaker and generation fields.

        Returns:
            Complete 24 kHz mono PCM16 WAV payload.

        Raises:
            ValueError: If the requested speaker is not part of the checkpoint.
        """
        if req.speaker not in self.speakers:
            raise ValueError(f"speaker must be one of: {', '.join(self.speakers)}")
        with self._torch.inference_mode():
            prompt = f"<text>{req.speaker}: {req.input}<audio>"
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
            output = self.model.generate_audio(
                **inputs,
                max_new_tokens=req.max_new_tokens,
                min_new_tokens=8,
                temperature=req.temperature,
                top_k=req.top_k,
                do_sample=True,
            )[0].tolist()
            audio_tokens = output[inputs.input_ids.shape[1] :]
            codes = self.model.audio_tokens_to_codes(audio_tokens).to(self.device)
            audio = self.mimi.decode(codes).audio_values[0, 0].float().cpu().clamp(-1, 1)
            pcm = (audio.numpy() * 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm.tobytes())
        return buffer.getvalue()


def create_admission(
    engine: MacRumikOSS,
    request_type: Callable[..., Any],
    config: MacServerConfig,
) -> SingleFlightAdmission:
    """Create the single exclusive admission gate for synchronous local synthesis.

    Args:
        engine: Loaded local model engine.
        request_type: Pinned upstream Pydantic request model.
        config: Bounded local admission configuration.

    Returns:
        One gate that prevents overlapping inference threads.
    """

    async def transport(payload: dict[str, Any]) -> bytes | LocalSynthesisError:
        request = request_type(**payload)
        try:
            return await asyncio.to_thread(engine.synthesize, request)
        except (RuntimeError, ValueError) as error:
            return LocalSynthesisError(str(error))

    return SingleFlightAdmission(
        transport,
        queue_capacity=config.queue_capacity,
        timeout_seconds=config.timeout_seconds,
    )


def create_app(config: MacServerConfig, *, engine: MacRumikOSS | None = None) -> Any:
    """Create a local HTTP app with the pinned Rumik input and WAV response contract.

    Args:
        config: Local model, device, and admission configuration.
        engine: Preloaded local engine for embedding or controlled test startup.

    Returns:
        Configured FastAPI application.
    """
    configure_model_caches()
    try:
        import torch
        import transformers
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, Response
    except ImportError as error:
        raise RuntimeError(
            "install the pinned Rumik server requirements before starting"
        ) from error

    upstream = _load_upstream(config.source_path)
    app = FastAPI(title="rumik-oss-1")
    app.state.rumik_engine = None
    app.state.rumik_admission = None

    @app.on_event("startup")
    async def start_engine() -> None:
        loaded_engine = engine or await asyncio.to_thread(
            MacRumikOSS,
            config,
            torch_module=torch,
            transformers_module=transformers,
        )
        app.state.rumik_engine = loaded_engine
        app.state.rumik_admission = create_admission(loaded_engine, upstream.SpeechRequest, config)
        _LOGGER.info(
            "Rumik local engine loaded device=%s dtype=%s checkpoint=%s",
            loaded_engine.runtime.device,
            loaded_engine.runtime.dtype_name,
            config.repo_id,
        )

    @app.on_event("shutdown")
    async def stop_engine() -> None:
        admission = app.state.rumik_admission
        if admission is not None:
            await admission.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return upstream.HTML

    @app.get("/health")
    def health() -> dict[str, Any]:
        engine = app.state.rumik_engine
        return {
            "status": "ok" if engine is not None else "starting",
            "model": "rumik-ai/rumik-oss-1",
            "runtime": _runtime_health(torch, engine),
            "compatibility": {
                "upstream_server_sha256": _sha256(config.source_path),
                "local_wrapper_sha256": _sha256(Path(__file__)),
            },
        }

    async def speech(req: Any) -> Any:
        admission = app.state.rumik_admission
        if admission is None:
            raise HTTPException(status_code=503, detail="local Rumik server is starting")
        try:
            admitted = await admission.submit(req.model_dump())
            if isinstance(admitted.value, LocalSynthesisError):
                raise HTTPException(status_code=400, detail=admitted.value.detail)
            return Response(admitted.value, media_type="audio/wav")
        except AdmissionOverloadedError as error:
            raise HTTPException(status_code=429, detail="local Rumik queue is full") from error
        except AdmissionQuarantinedError as error:
            raise HTTPException(
                status_code=503, detail="local Rumik server requires recovery"
            ) from error
        except AdmissionClosedError as error:
            raise HTTPException(status_code=503, detail="local Rumik server is closing") from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    speech.__annotations__["req"] = upstream.SpeechRequest
    app.post("/v1/audio/speech")(speech)

    return app


def main() -> None:
    """Run one Apple Silicon Rumik HTTP process."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=str(_DEFAULT_REPOSITORY))
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--dtype", choices=("float32", "float16"), default="float32")
    parser.add_argument("--cpu-threads", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6006)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()
    config = MacServerConfig(
        repo_id=args.repo_id,
        device=args.device,
        dtype=args.dtype,
        cpu_threads=args.cpu_threads,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError(
            "install the pinned Rumik server requirements before starting"
        ) from error
    uvicorn.run(create_app(config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
