"""Shared, sanitized evidence handling for provider-backed demo probes."""

from __future__ import annotations

import asyncio
import audioop
import json
import os
import secrets
import tempfile
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeFailure(Exception):
    """A failure that can be reported without retaining provider content."""

    stage: str
    category: str
    action: str

    def __str__(self) -> str:
        return f"{self.stage}: {self.category}"


_ACTIONS = {
    "authentication": "Check the configured credential and provider entitlement; do not retry automatically.",
    "configuration": "Set the required configuration and run the probe again.",
    "input": "Provide a mono PCM16 WAV between 8 kHz and 96 kHz; it will be resampled to 16 kHz.",
    "rate_limited": "Wait for the provider retry window, then retry once.",
    "timeout": "Check provider availability and increase --timeout only when appropriate.",
    "interrupted": "The probe was interrupted; inspect the saved manifest before retrying.",
    "initialization": "Check local dependencies and configuration before retrying.",
    "output": "Choose a writable output directory that does not contain a conflicting run.",
    "provider": "Check the provider status and retry after resolving the reported service issue.",
    "validation": "Inspect the local probe configuration and try again.",
}


def action_for(category: str) -> str:
    """Return the operator action for a stable, allowlisted category."""
    return _ACTIONS.get(category, _ACTIONS["provider"])


def classify_failure(error: BaseException, *, stage: str) -> ProbeFailure:
    """Classify an exception without exposing its message, body, or headers."""
    if isinstance(error, ProbeFailure):
        return error
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        category = "timeout"
    elif isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
        category = "interrupted"
    elif isinstance(error, (OSError, PermissionError)) and stage == "output":
        category = "output"
    else:
        status_code = getattr(error, "status_code", None) or getattr(error, "code", None)
        response = getattr(error, "response", None)
        status_code = status_code or getattr(response, "status_code", None)
        if status_code in {401, 403}:
            category = "authentication"
        elif status_code == 429:
            category = "rate_limited"
        elif stage == "initialization":
            category = "initialization"
        else:
            category = "provider"
    return ProbeFailure(stage, category, action_for(category))


def require_credential(value: str | None, *, name: str) -> None:
    """Reject a missing credential before a probe opens a provider connection."""
    if not value or not value.strip():
        raise ProbeFailure(
            "configuration",
            "configuration",
            f"Set {name} in the environment or demo/.env, then run the probe again.",
        )


def _atomic_json(path: Path, content: dict[str, Any], *, replace: bool = True) -> None:
    """Replace a manifest atomically inside its already-reserved run directory."""
    encoded = (json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class ProbeEvidence:
    """Own a collision-safe evidence directory and its sanitized manifest."""

    def __init__(
        self,
        output_base: Path,
        probe: str,
        config: dict[str, Any],
        *,
        reserve_run_directory: bool = True,
    ):
        """Initialize a run that is reserved by :meth:`begin`.

        Args:
            output_base: Parent evidence directory, or an explicit empty run directory.
            probe: Allowlisted probe name.
            config: Allowlisted configuration included in the manifest.
            reserve_run_directory: Create a fresh child run directory when true.
        """
        self.output_base = output_base.expanduser().resolve()
        self.probe = probe
        self.config = config
        self.reserve_run_directory = reserve_run_directory
        self.run_id = ""
        self.directory: Path | None = None
        self.manifest_path: Path | None = None
        self._manifest: dict[str, Any] = {}

    def begin(self) -> Path:
        """Reserve a new directory and persist an initial manifest before setup."""
        try:
            self.output_base.mkdir(parents=True, exist_ok=True)
            if not self.reserve_run_directory:
                manifest_path = self.output_base / "manifest.json"
                if manifest_path.exists():
                    raise FileExistsError(manifest_path)
                self.run_id = self.output_base.name
                self.directory = self.output_base
                self.manifest_path = manifest_path
                self._manifest = {
                    "artifact_path": str(self.output_base),
                    "config": self.config,
                    "outcome": "running",
                    "probe": self.probe,
                    "run_id": self.run_id,
                    "stage": "initialization",
                    "started_at_utc": datetime.now(UTC).isoformat(),
                }
                _atomic_json(manifest_path, self._manifest, replace=False)
                return self.output_base
            for _ in range(20):
                run_id = (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    + f"-{os.getpid()}-{secrets.token_hex(4)}"
                )
                directory = self.output_base / run_id
                try:
                    directory.mkdir()
                except FileExistsError:
                    continue
                self.run_id = run_id
                self.directory = directory
                self.manifest_path = directory / "manifest.json"
                self._manifest = {
                    "artifact_path": str(directory),
                    "config": self.config,
                    "outcome": "running",
                    "probe": self.probe,
                    "run_id": run_id,
                    "stage": "initialization",
                    "started_at_utc": datetime.now(UTC).isoformat(),
                }
                _atomic_json(self.manifest_path, self._manifest, replace=False)
                return directory
            raise FileExistsError("could not reserve a unique run directory")
        except OSError as error:
            raise ProbeFailure("output", "output", action_for("output")) from error

    def artifact(self, name: str) -> Path:
        """Return an artifact path within this run after validating its file name."""
        if self.directory is None or Path(name).name != name:
            raise RuntimeError("evidence run is not initialized")
        path = self.directory / name
        if path.exists():
            raise ProbeFailure("output", "output", action_for("output"))
        return path

    def finish(
        self,
        *,
        stage: str,
        outcome: str,
        category: str = "success",
        action: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Atomically persist the terminal manifest without provider request content."""
        if self.manifest_path is None:
            raise ProbeFailure("output", "output", action_for("output"))
        self._manifest.update(
            {
                "action": action
                or ("Evidence saved." if outcome == "success" else action_for(category)),
                "category": category,
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "outcome": outcome,
                "stage": stage,
            }
        )
        if details:
            self._manifest["details"] = details
        try:
            _atomic_json(self.manifest_path, self._manifest)
        except OSError as error:
            raise ProbeFailure("output", "output", action_for("output")) from error


def load_pcm16_mono_wav(path: Path) -> tuple[bytes, dict[str, int]]:
    """Load a bounded mono PCM16 WAV and resample it to 16 kHz when necessary."""
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.getnframes()
            if reader.getcomptype() != "NONE" or channels != 1 or sample_width != 2:
                raise ProbeFailure("input", "input", action_for("input"))
            if not 8_000 <= sample_rate <= 96_000 or frames > sample_rate * 60:
                raise ProbeFailure("input", "input", action_for("input"))
            pcm = reader.readframes(frames)
    except ProbeFailure:
        raise
    except (OSError, EOFError, wave.Error) as error:
        raise ProbeFailure("input", "input", action_for("input")) from error
    if len(pcm) != frames * sample_width:
        raise ProbeFailure("input", "input", action_for("input"))
    if sample_rate != 16_000:
        try:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sample_rate, 16_000, None)
        except audioop.error as error:
            raise ProbeFailure("input", "input", action_for("input")) from error
    return pcm, {
        "input_sample_rate_hz": sample_rate,
        "input_frames": frames,
        "output_sample_rate_hz": 16_000,
        "output_bytes": len(pcm),
    }


def write_pcm16_wav(path: Path, pcm: bytes, *, sample_rate: int) -> None:
    """Write a PCM artifact atomically and never expose a partially-written WAV."""
    if len(pcm) % 2:
        raise ProbeFailure("validation", "validation", action_for("validation"))
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        with wave.open(temporary, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(pcm)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
