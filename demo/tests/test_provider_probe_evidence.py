"""Failure-path tests for the provider probe evidence contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from demo.scripts import probe_sarvam_realtime
from demo.scripts.probe_evidence import ProbeEvidence, ProbeFailure, load_pcm16_mono_wav


def _write_wav(path: Path, *, channels: int = 1, width: int = 2, rate: int = 24_000) -> None:
    """Create a real WAV fixture for subprocess and resampling checks."""
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(rate * channels * width // 10))


def _manifest(output: Path) -> dict[str, object]:
    """Read the only manifest created under one test output directory."""
    manifests = list(output.glob("*/manifest.json"))
    assert len(manifests) == 1
    return json.loads(manifests[0].read_text())


def test_invalid_stereo_wav_fails_before_network_and_retains_manifest(tmp_path: Path) -> None:
    """The executable rejects actual malformed audio without opening a provider socket."""
    input_wav = tmp_path / "stereo.wav"
    output = tmp_path / "results"
    _write_wav(input_wav, channels=2)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "demo.scripts.probe_sarvam_realtime",
            "--input-wav",
            str(input_wav),
            "--output-dir",
            str(output),
            "--timeout",
            "1",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "input failed: input" in completed.stderr
    report = _manifest(output)
    assert report["outcome"] == "failure"
    assert report["category"] == "input"


@pytest.mark.parametrize(
    ("channels", "width", "rate", "raw"),
    [(1, 1, 16_000, False), (1, 2, 7_000, False), (1, 2, 16_000, True)],
)
def test_wav_validation_rejects_unsupported_pcm_shapes_and_corruption(
    tmp_path: Path, channels: int, width: int, rate: int, raw: bool
) -> None:
    """Real files with bad codec, rate, or container data cannot reach STT."""
    path = tmp_path / "invalid.wav"
    if raw:
        path.write_bytes(b"not-a-wave-file")
    else:
        _write_wav(path, channels=channels, width=width, rate=rate)
    with pytest.raises(ProbeFailure, match="input"):
        load_pcm16_mono_wav(path)


def test_wav_validation_resamples_a_real_pcm16_input(tmp_path: Path) -> None:
    """A valid non-16 kHz mono recording is resampled once before STT sends it."""
    path = tmp_path / "resample.wav"
    _write_wav(path, rate=24_000)
    pcm, details = load_pcm16_mono_wav(path)
    assert details["input_sample_rate_hz"] == 24_000
    assert details["output_sample_rate_hz"] == 16_000
    assert len(pcm) == details["output_bytes"]


def test_missing_key_stops_before_probe_call_and_saves_configuration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank credential produces evidence before any provider operation can run."""
    input_wav = tmp_path / "input.wav"
    output = tmp_path / "results"
    _write_wav(input_wav)
    monkeypatch.setenv("SARVAM_API_KEY", "")
    monkeypatch.setattr(probe_sarvam_realtime, "load_dotenv", lambda *_args, **_kwargs: False)

    async def should_not_run(*_args: object) -> dict[str, object]:
        raise AssertionError("provider probe should not run without a credential")

    monkeypatch.setattr(probe_sarvam_realtime, "_probe", should_not_run)
    args = argparse.Namespace(
        input_wav=input_wav, output_dir=output, timeout=1.0, silence=False, empty=False
    )
    assert asyncio.run(probe_sarvam_realtime._run(args)) == 1
    report = _manifest(output)
    assert report["category"] == "configuration"
    assert report["stage"] == "configuration"


class _ProviderFault(Exception):
    """Provider-shaped fault with a status code but no response body."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (_ProviderFault(401), "authentication"),
        (_ProviderFault(429), "rate_limited"),
        (TimeoutError(), "timeout"),
    ],
)
def test_provider_faults_finalize_real_evidence_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: BaseException, expected: str
) -> None:
    """Auth, throttling, and timeout faults use stable categories and no error bodies."""
    input_wav = tmp_path / "input.wav"
    output = tmp_path / "results"
    _write_wav(input_wav)
    monkeypatch.setenv("SARVAM_API_KEY", "test-key-canary")

    async def fail_provider(*_args: object) -> dict[str, object]:
        raise fault

    monkeypatch.setattr(probe_sarvam_realtime, "_probe", fail_provider)
    args = argparse.Namespace(
        input_wav=input_wav, output_dir=output, timeout=1.0, silence=False, empty=False
    )
    assert asyncio.run(probe_sarvam_realtime._run(args)) == 1
    report = _manifest(output)
    serialized = json.dumps(report)
    assert report["outcome"] == "failure"
    assert report["category"] == expected
    assert "test-key-canary" not in serialized


def test_interruption_and_initialization_failure_are_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation and setup faults finalize the same reserved run directory."""
    input_wav = tmp_path / "input.wav"
    _write_wav(input_wav)
    monkeypatch.setenv("SARVAM_API_KEY", "test-key-canary")

    async def interrupt(*_args: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(probe_sarvam_realtime, "_probe", interrupt)
    interrupted = tmp_path / "interrupted"
    args = argparse.Namespace(
        input_wav=input_wav, output_dir=interrupted, timeout=1.0, silence=False, empty=False
    )
    assert asyncio.run(probe_sarvam_realtime._run(args)) == 1
    assert _manifest(interrupted)["category"] == "interrupted"

    def fail_setup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provider-body-canary")

    monkeypatch.setattr(probe_sarvam_realtime, "load_dotenv", fail_setup)
    initialized = tmp_path / "initialized"
    args.output_dir = initialized
    assert asyncio.run(probe_sarvam_realtime._run(args)) == 1
    report = _manifest(initialized)
    assert report["category"] == "initialization"
    assert "provider-body-canary" not in json.dumps(report)


def test_output_collision_does_not_modify_existing_artifact(tmp_path: Path) -> None:
    """A non-directory output target fails without overwriting the existing artifact."""
    target = tmp_path / "occupied"
    target.write_text("keep-me")
    evidence = ProbeEvidence(target, "probe", {"safe": True})
    with pytest.raises(ProbeFailure, match="output"):
        evidence.begin()
    assert target.read_text() == "keep-me"
