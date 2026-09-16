"""Safe launcher for the isolated Hugging Face speech-to-speech experiment.

The pinned upstream project exposes explicit ``BackendSpec`` registries.  This
module extends those registries in memory before importing its pipeline parser;
the upstream checkout and its installed package are never changed.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shlex
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UPSTREAM_REVISION = "16d7f98ff712fb082d53497937f5456667c84680"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PIPELINES = 4


class ConfigurationError(ValueError):
    """Raised before imports, microphones, or provider requests on bad setup."""


@dataclass(frozen=True)
class LaunchPlan:
    """Fully resolved launcher-owned settings and arguments for upstream."""

    command: str
    profile: str
    env_file: Path | None
    check_only: bool
    upstream_args: tuple[str, ...]
    uses_sarvam: bool
    uses_default_gemini: bool


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_env_file() -> Path | None:
    candidate = _repository_root() / "demo" / ".env"
    return candidate if candidate.is_file() else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m HFS.speech_to_speech",
        description="Run the isolated, loopback-only HF speech-to-speech experiment.",
    )
    parser.add_argument("command", choices=("serve", "local"))
    parser.add_argument(
        "--profile",
        choices=("sarvam", "upstream"),
        default="sarvam",
        help="Use local Sarvam plus Gemini defaults, or leave all backend selection to upstream.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional server-side dotenv file. Defaults to demo/.env only when it exists.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate credentials, bindability, and arguments, then exit before importing providers.",
    )
    return parser


def _remove_launcher_options(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse launcher flags while preserving every upstream-owned option."""
    parser = _parser()
    namespace, remaining = parser.parse_known_args(list(argv))
    return namespace, remaining


def _option_value(arguments: Sequence[str], *names: str) -> str | None:
    """Return the final value of an option expressed as ``--x value`` or ``--x=value``."""
    found: str | None = None
    index = 0
    while index < len(arguments):
        token = arguments[index]
        for name in names:
            if token == name:
                if index + 1 == len(arguments) or arguments[index + 1].startswith("--"):
                    raise ConfigurationError(f"{name} requires a value")
                found = arguments[index + 1]
                index += 1
                break
            if token.startswith(f"{name}="):
                found = token.partition("=")[2]
                if not found:
                    raise ConfigurationError(f"{name} requires a value")
                break
        index += 1
    return found


def _has_option(arguments: Sequence[str], *names: str) -> bool:
    return _option_value(arguments, *names) is not None


def _append_default(arguments: list[str], names: tuple[str, ...], *default: str) -> None:
    if not _has_option(arguments, *names):
        arguments.extend(default)


def _validate_runtime_options(arguments: Sequence[str]) -> None:
    """Validate the small set of resource and network controls this launcher owns."""
    host = _option_value(arguments, "--host") or DEFAULT_HOST
    if host != DEFAULT_HOST:
        raise ConfigurationError(
            f"--host must be {DEFAULT_HOST} for this unauthenticated local experiment"
        )

    raw_port = _option_value(arguments, "--port") or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigurationError("--port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("--port must be an integer between 1 and 65535")

    raw_pipelines = _option_value(arguments, "--num_pipelines", "--num-pipelines")
    if raw_pipelines is not None:
        try:
            pipelines = int(raw_pipelines)
        except ValueError as exc:
            raise ConfigurationError("--num_pipelines must be an integer") from exc
        if not 1 <= pipelines <= MAX_PIPELINES:
            raise ConfigurationError(f"--num_pipelines must be between 1 and {MAX_PIPELINES}")


def _bounded_number(
    arguments: Sequence[str],
    option: str,
    *,
    minimum: float,
    maximum: float,
    integral: bool = False,
) -> None:
    value = _option_value(arguments, option)
    if value is None:
        return
    try:
        parsed = int(value) if integral else float(value)
    except ValueError as exc:
        kind = "integer" if integral else "number"
        raise ConfigurationError(f"{option} must be a {kind}") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{option} must be between {minimum:g} and {maximum:g}")


def _validate_sarvam_options(arguments: Sequence[str], *, stt: str | None, tts: str | None) -> None:
    """Bound request and buffer controls before either native handler starts."""
    if stt == "sarvam":
        _bounded_number(arguments, "--sarvam_stt_timeout", minimum=0.1, maximum=120.0)
        _bounded_number(
            arguments,
            "--sarvam_stt_max_pending_audio_bytes",
            minimum=320,
            maximum=4 * 1024 * 1024,
            integral=True,
        )
    if tts == "sarvam":
        _bounded_number(arguments, "--sarvam_tts_timeout", minimum=0.1, maximum=120.0)
        _bounded_number(
            arguments, "--sarvam_tts_blocksize", minimum=1, maximum=16_384, integral=True
        )


def build_launch_plan(argv: Sequence[str] | None = None) -> LaunchPlan:
    """Apply a profile without interpreting options belonging to the upstream parser."""
    namespace, remaining = _remove_launcher_options(sys.argv[1:] if argv is None else argv)
    arguments = list(remaining)
    env_file = namespace.env_file
    if env_file is None:
        env_file = _default_env_file()
    elif not env_file.is_file():
        raise ConfigurationError(f"--env-file does not exist or is not a file: {env_file}")

    if namespace.profile == "sarvam":
        _append_default(arguments, ("--stt",), "--stt", "sarvam")
        _append_default(arguments, ("--tts",), "--tts", "sarvam")
        _append_default(
            arguments, ("--llm_backend", "--llm-backend"), "--llm_backend", "chat-completions"
        )
        _append_default(arguments, ("--model_name", "--model-name"), "--model_name", GEMINI_MODEL)
        _append_default(
            arguments,
            ("--responses_api_base_url", "--responses-api-base-url"),
            "--responses_api_base_url",
            GEMINI_OPENAI_BASE_URL,
        )
        _append_default(
            arguments,
            ("--responses_api_reasoning_effort", "--responses-api-reasoning-effort"),
            "--responses_api_reasoning_effort",
            "low",
        )

    _append_default(arguments, ("--host",), "--host", DEFAULT_HOST)
    _append_default(arguments, ("--port",), "--port", str(DEFAULT_PORT))
    _validate_runtime_options(arguments)

    stt = _option_value(arguments, "--stt")
    tts = _option_value(arguments, "--tts")
    _validate_sarvam_options(arguments, stt=stt, tts=tts)
    llm = _option_value(arguments, "--llm_backend", "--llm-backend")
    base_url = _option_value(arguments, "--responses_api_base_url", "--responses-api-base-url")
    return LaunchPlan(
        command=namespace.command,
        profile=namespace.profile,
        env_file=env_file,
        check_only=namespace.check,
        upstream_args=tuple(arguments),
        uses_sarvam=stt == "sarvam" or tts == "sarvam",
        uses_default_gemini=llm == "chat-completions" and base_url == GEMINI_OPENAI_BASE_URL,
    )


def load_env_file(path: Path | None, environ: dict[str, str] | None = None) -> None:
    """Load a minimal dotenv file without replacing already-set process variables."""
    if path is None:
        return
    target = os.environ if environ is None else environ
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"invalid dotenv assignment at {path}:{line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ConfigurationError(f"invalid dotenv variable name at {path}:{line_number}")
        try:
            value_parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigurationError(f"invalid dotenv value at {path}:{line_number}") from exc
        value = "" if not value_parts else " ".join(value_parts)
        target.setdefault(name, value)


def _require_environment(name: str) -> None:
    if not os.environ.get(name, "").strip():
        raise ConfigurationError(
            f"{name} is required; set it in demo/.env or the server environment"
        )


def _ensure_loopback_port(arguments: Sequence[str]) -> None:
    raw_port = _option_value(arguments, "--port")
    assert raw_port is not None
    port = int(raw_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((DEFAULT_HOST, port))
        except OSError as exc:
            raise ConfigurationError(
                f"{DEFAULT_HOST}:{port} is already in use; choose a free --port"
            ) from exc


def preflight(plan: LaunchPlan) -> None:
    """Validate local configuration before importing the upstream pipeline or opening a device."""
    load_env_file(plan.env_file)
    if plan.uses_sarvam:
        _require_environment("SARVAM_API_KEY")
    if plan.uses_default_gemini:
        _require_environment("GOOGLE_API_KEY")
        # Upstream's generic OpenAI-compatible handler only reads its explicit
        # api_key or OPENAI_API_KEY.  This maps the Google key into that client
        # process without ever mapping a Sarvam key to bearer authentication.
        os.environ.setdefault("OPENAI_API_KEY", os.environ["GOOGLE_API_KEY"])
    _ensure_loopback_port(plan.upstream_args)


def _handler_factory(module_name: str, class_name: str, *, tts: bool):
    """Create a factory matching the pinned registry's ``HandlerContext`` contract."""

    def create(context: Any, config: dict[str, Any]) -> Any:
        handler_class = getattr(importlib.import_module(module_name), class_name)
        setup_kwargs = dict(config)
        setup_kwargs.pop("gen_kwargs", None)
        # The registry prefix keeps the two argument dataclasses collision-free
        # for HfArgumentParser. Both native handlers deliberately use the
        # provider-specific setup name rather than an OpenAI-shaped ``api_key``.
        if "api_key" in setup_kwargs:
            setup_kwargs["sarvam_api_key"] = setup_kwargs.pop("api_key")
        if tts:
            setup_kwargs.update(
                cancel_scope=context.cancel_scope,
                speculative_turns=context.speculative_turns,
            )
            setup_args = (context.should_listen,)
        else:
            setup_kwargs.update(
                speculative_turns=context.speculative_turns,
                pipeline_index=context.pipeline_index,
            )
            setup_args = ()
        return handler_class(
            context.stop_event,
            queue_in=context.queue_in,
            queue_out=context.queue_out,
            setup_args=setup_args,
            setup_kwargs=setup_kwargs,
        )

    return create


def register_sarvam_backends() -> None:
    """Add local Sarvam handlers through the pinned upstream registry seam once."""
    importlib.import_module("speech_to_speech")
    registry = importlib.import_module("speech_to_speech.backend_registry")
    stt_adapter = importlib.import_module("HFS.speech_to_speech.sarvam_stt")
    tts_adapter = importlib.import_module("HFS.speech_to_speech.sarvam_tts")
    backend_spec = registry.BackendSpec

    for backends, kind, config_type, module, class_name, prefix, is_tts in (
        (
            registry.STT_BACKENDS,
            "stt",
            stt_adapter.SarvamSTTHandlerArguments,
            "HFS.speech_to_speech.sarvam_stt",
            "SarvamSTTHandler",
            "sarvam_stt",
            False,
        ),
        (
            registry.TTS_BACKENDS,
            "tts",
            tts_adapter.SarvamTTSHandlerArguments,
            "HFS.speech_to_speech.sarvam_tts",
            "SarvamTTSHandler",
            "sarvam_tts",
            True,
        ),
    ):
        existing = backends.get("sarvam")
        if existing is not None:
            if existing.config_type is not config_type:
                raise ConfigurationError(
                    "upstream already defines an incompatible 'sarvam' backend"
                )
            continue
        backends["sarvam"] = backend_spec(
            "sarvam",
            kind,
            config_type,
            _handler_factory(module, class_name, tts=is_tts),
            config_prefix=prefix,
            capabilities=registry.BackendCapabilities(streams_audio_chunks=not is_tts),
        )


def run(plan: LaunchPlan) -> None:
    """Perform preflight then delegate signal handling and shutdown to upstream."""
    preflight(plan)
    if plan.check_only:
        print("HF speech-to-speech preflight passed.")
        return
    if plan.uses_sarvam:
        register_sarvam_backends()
    pipeline = importlib.import_module("speech_to_speech.s2s_pipeline")
    pipeline.run_pipeline_command(plan.command, plan.upstream_args)


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point with concise configuration failures."""
    try:
        run(build_launch_plan(argv))
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


__all__ = [
    "ConfigurationError",
    "GEMINI_MODEL",
    "GEMINI_OPENAI_BASE_URL",
    "LaunchPlan",
    "UPSTREAM_REVISION",
    "build_launch_plan",
    "load_env_file",
    "main",
    "preflight",
    "register_sarvam_backends",
    "run",
]
