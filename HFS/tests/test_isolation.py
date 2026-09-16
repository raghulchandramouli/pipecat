"""Isolation coverage for the runnable HFS launcher."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from queue import Queue
from threading import Event

import pytest

import HFS.speech_to_speech.launcher as launcher
from HFS.speech_to_speech.launcher import (
    DEFAULT_HOST,
    GEMINI_MODEL,
    ConfigurationError,
    build_launch_plan,
    preflight,
    register_sarvam_backends,
)


def _credentials_file(tmp_path: Path) -> Path:
    path = tmp_path / "credentials.env"
    path.write_text("SARVAM_API_KEY=sarvam-file\nGOOGLE_API_KEY=google-file\n", encoding="utf-8")
    return path


def test_default_env_file_resolves_inside_repository(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repository"
    module = root / "HFS" / "speech_to_speech" / "launcher.py"
    credentials = root / "demo" / ".env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("# local credentials\n")
    monkeypatch.setattr(launcher, "__file__", str(module))
    assert build_launch_plan(["serve"]).env_file == credentials


def test_default_profile_uses_native_sarvam_and_google_without_exposing_keys(
    tmp_path, monkeypatch
) -> None:
    """Profile defaults select handlers without putting credentials in argv."""
    credentials = _credentials_file(tmp_path)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plan = build_launch_plan(["serve", "--env-file", str(credentials), "--port", "18465"])

    assert plan.uses_sarvam
    assert plan.uses_default_gemini
    assert "sarvam" in plan.upstream_args
    assert GEMINI_MODEL in plan.upstream_args
    assert "sarvam-file" not in plan.upstream_args
    assert "google-file" not in plan.upstream_args

    monkeypatch.setattr(launcher, "_ensure_loopback_port", lambda _args: None)
    preflight(plan)
    assert os.environ["OPENAI_API_KEY"] == "google-file"


def test_missing_key_stops_before_importing_provider(tmp_path, monkeypatch) -> None:
    """A missing required credential fails before the upstream pipeline imports."""
    credentials = tmp_path / "empty.env"
    credentials.write_text("# intentionally empty\n", encoding="utf-8")
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    plan = build_launch_plan(["serve", "--env-file", str(credentials), "--port", "18466"])

    with pytest.raises(ConfigurationError, match="SARVAM_API_KEY"):
        preflight(plan)
    assert "speech_to_speech.s2s_pipeline" not in sys.modules


def test_sarvam_request_limits_fail_before_handler_import() -> None:
    """Unbounded or nonpositive request controls are rejected by the launcher."""
    with pytest.raises(ConfigurationError, match="sarvam_tts_timeout"):
        build_launch_plan(["serve", "--sarvam_tts_timeout", "0"])
    with pytest.raises(ConfigurationError, match="sarvam_stt_max_pending_audio_bytes"):
        build_launch_plan(["serve", "--sarvam_stt_max_pending_audio_bytes", "999999999"])


def test_occupied_port_is_reported_without_touching_the_owner(tmp_path, monkeypatch) -> None:
    """Port preflight leaves an already-running, unrelated socket alone."""
    credentials = _credentials_file(tmp_path)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
            owner.bind((DEFAULT_HOST, 0))
            port = owner.getsockname()[1]
            owner.listen()
            plan = build_launch_plan(["serve", "--env-file", str(credentials), "--port", str(port)])
            with pytest.raises(ConfigurationError, match="already in use"):
                preflight(plan)
            assert owner.fileno() >= 0
    except PermissionError:
        pytest.skip("the execution sandbox does not permit local socket binding")


def test_env_file_does_not_override_server_environment(tmp_path, monkeypatch) -> None:
    """Server-supplied credentials take precedence over dotenv values."""
    credentials = _credentials_file(tmp_path)
    monkeypatch.setenv("SARVAM_API_KEY", "from-server")
    monkeypatch.setenv("GOOGLE_API_KEY", "from-server")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plan = build_launch_plan(["serve", "--env-file", str(credentials), "--port", "18467"])

    monkeypatch.setattr(launcher, "_ensure_loopback_port", lambda _args: None)
    preflight(plan)
    assert os.environ["SARVAM_API_KEY"] == "from-server"
    assert os.environ["GOOGLE_API_KEY"] == "from-server"


def test_module_help_runs_with_pythonpath_unset() -> None:
    """Module help remains available from the repository without PYTHONPATH."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-u", "-m", "HFS.speech_to_speech", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "loopback-only" in completed.stdout


def test_pinned_registry_constructs_both_native_handlers(monkeypatch) -> None:
    """The upstream registry normalizes isolated CLI fields into native handler setup."""
    monkeypatch.setenv("SARVAM_API_KEY", "contract-only")
    plan = build_launch_plan(["serve", "--port", "18468"])
    register_sarvam_backends()

    from speech_to_speech.backend_registry import HandlerContext, create_backend_handler
    from speech_to_speech.pipeline.cancel_scope import CancelScope
    from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
    from speech_to_speech.s2s_pipeline import parse_arguments

    arguments = parse_arguments(plan.upstream_args, command="serve")
    assert arguments.stt_backend.spec.capabilities.streams_audio_chunks
    context = HandlerContext(
        Event(),
        Queue(),
        Queue(),
        Queue(),
        Event(),
        CancelScope(),
        SpeculativeTurnTracker(),
        0,
        16_000,
        False,
        0.5,
    )
    stt = create_backend_handler(arguments.stt_backend, context)
    tts = create_backend_handler(arguments.tts_backend, context)

    assert type(stt).__name__ == "SarvamSTTHandler"
    assert stt._api_key == "contract-only"
    assert type(tts).__name__ == "SarvamTTSHandler"
    assert tts.api_key == "contract-only"
