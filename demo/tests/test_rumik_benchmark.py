"""Offline and private-host boundary coverage for the Rumik benchmark harness."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo.rumik import benchmark

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def manifest() -> dict[str, Any]:
    """Load the checked-in pinned manifest for host-attestation validation."""
    return json.loads(benchmark._DEFAULT_MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture
def host_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    """Create one complete operator-attested private-host metadata payload."""
    return {
        "hardware": {
            "profile": "cuda",
            "gpu_uuid": "GPU-abc",
            "driver": "555.42",
            "cuda": "12.4",
        },
        "revisions": {
            key: manifest[key] for key in ("model_revision", "server_revision", "server_sha256")
        },
        "provenance": {"attestation": "operator recorded this exclusive host"},
        "installed_versions": {"rumik": "1.0.0", "torch": "2.6.0"},
    }


def _fixture_directory(tmp_path: Path) -> Path:
    """Copy the portable fixture inputs so one test can change its request list."""
    fixture = tmp_path / "fixtures"
    fixture.mkdir()
    for name in ("sample.wav", "responses.json"):
        shutil.copy2(benchmark._DEFAULT_REQUESTS.parent / name, fixture / name)
    return fixture


@pytest.mark.asyncio
async def test_fixture_benchmark_writes_synthetic_status_and_wav_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fixture mode reports queue timing and documented success/error statuses without Rumik claims."""
    monkeypatch.setattr(benchmark, "_DEMO_ROOT", tmp_path)
    output = tmp_path / "rumik" / "fixture.json"

    report = await benchmark.run_benchmark(mode="fixture", output=output)

    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["mode"] == "fixture"
    assert report["provenance"] == {"kind": "synthetic_fixture", "measurements": "not_rumik"}
    assert report["pin_verification"]["remote_verified"] is False
    assert report["memory"] == benchmark._NEUTRAL_MEMORY
    assert report["manifest"]["model_revision"] == "0ed3c98684e14350c910129c0efa179841c41ad2"
    outcomes = {case["id"]: case for case in report["cases"]}
    assert outcomes["introduction"]["status"] == 200
    assert outcomes["introduction"]["wav_duration_seconds"] == pytest.approx(0.1)
    assert outcomes["invalid_speaker"]["status"] == 400
    assert outcomes["missing_input"]["status"] == outcomes["empty_input"]["status"] == 422
    assert all(case["ok"] for case in report["cases"])
    assert any(case["latency_seconds"]["queue_wait"] > 0 for case in report["cases"])
    assert all(
        case["latency_seconds"]["total"] >= case["latency_seconds"]["request"]
        for case in report["cases"]
    )


@pytest.mark.asyncio
async def test_fixture_timeout_and_saved_wavs_are_reported_under_the_output_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit benchmark deadline and only validated successful WAVs become local artifacts."""
    monkeypatch.setattr(benchmark, "_DEMO_ROOT", tmp_path)
    output = tmp_path / "results" / "mac-listening.json"

    report = await benchmark.run_benchmark(
        mode="fixture", output=output, request_timeout_seconds=180.0, save_wav=True
    )

    assert report["timeout_seconds"] == 180.0
    assert report["save_wav"] is True
    successful = [case for case in report["cases"] if case["status"] == 200]
    assert len(successful) == 4
    for case in successful:
        artifact = Path(case["audio_artifact_path"])
        assert artifact.is_relative_to(output.parent)
        assert artifact.exists()
        assert benchmark._wav_duration(artifact.read_bytes()) == pytest.approx(0.1)
    assert all(
        "audio_artifact_path" not in case for case in report["cases"] if case["status"] != 200
    )


@pytest.mark.asyncio
async def test_live_benchmark_forwards_a_600_second_timeout_through_http_and_saves_wav(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host_metadata: dict[str, Any],
) -> None:
    """Live admission uses its configured deadline in the real HTTP transport path."""
    requests_path = tmp_path / "requests.json"
    requests_path.write_text(
        json.dumps(
            [
                {
                    "id": "listen",
                    "payload": {"input": "A short test.", "speaker": "Ira"},
                    "expected_status": 200,
                }
            ]
        ),
        encoding="utf-8",
    )
    metadata_path = tmp_path / "host.json"
    metadata_path.write_text(json.dumps(host_metadata), encoding="utf-8")
    output = tmp_path / "live.json"
    options: list[dict[str, Any]] = []
    http_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        http_requests.append(request)
        return httpx.Response(
            200,
            content=benchmark._DEFAULT_SAMPLE.read_bytes(),
            headers={"Content-Type": "audio/wav"},
        )

    monkeypatch.setattr(benchmark, "_DEMO_ROOT", tmp_path)
    monkeypatch.setattr(
        benchmark.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 8000))],
    )
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", _mock_client_factory(handler, options))

    report = await benchmark.run_benchmark(
        mode="live",
        output=output,
        requests_path=requests_path,
        endpoint="http://private.test/v1/audio/speech",
        host_metadata_path=metadata_path,
        exclusive_server=True,
        warmups=0,
        request_timeout_seconds=600,
        save_wav=True,
    )

    assert report["timeout_seconds"] == 600.0
    assert options == [{"trust_env": False, "follow_redirects": False, "timeout": 600.0}]
    assert len(http_requests) == 1
    assert http_requests[0].method == "POST"
    case = report["cases"][0]
    artifact = Path(case["audio_artifact_path"])
    assert artifact.is_relative_to(output.parent)
    assert benchmark._wav_duration(artifact.read_bytes()) == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_malformed_fixture_wav_fails_case_and_cli_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 200 response with a malformed audio body fails validation rather than claiming synthesis."""
    fixture = _fixture_directory(tmp_path)
    requests = fixture / "requests.json"
    requests.write_text(
        json.dumps(
            [
                {
                    "id": "bad_audio",
                    "payload": {
                        "input": "short",
                        "speaker": "Ira",
                        "fixture_response": "malformed_wav",
                    },
                    "expected_status": 200,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "_DEMO_ROOT", tmp_path)
    output = tmp_path / "benchmark.json"

    report = await benchmark.run_benchmark(
        mode="fixture", output=output, requests_path=requests, warmups=0
    )
    assert len(report["cases"]) == 1
    case = report["cases"][0]
    assert case["id"] == "bad_audio"
    assert case["expected_status"] == case["status"] == 200
    assert case["ok"] is False and case["error"] == {"kind": "invalid_wav"}
    assert case["latency_seconds"]["total"] >= 0
    assert (
        await asyncio.to_thread(
            benchmark.main,
            [
                "--mode",
                "fixture",
                "--output",
                str(tmp_path / "cli.json"),
                "--requests",
                str(requests),
                "--warmups",
                "0",
            ],
        )
        == 2
    )


@pytest.mark.parametrize("payload", [b"not a WAV", b"RIFF\x00\x00\x00\x00WAVE"])
def test_wav_duration_rejects_malformed_or_truncated_data(payload: bytes) -> None:
    """A header alone cannot manufacture a valid duration measurement."""
    with pytest.raises(ValueError):
        benchmark._wav_duration(payload)


def test_wav_header_cannot_claim_audio_frames_missing_from_the_body():
    """A valid WAV header with truncated PCM must not produce a duration result."""
    sample = benchmark._DEFAULT_SAMPLE.read_bytes()
    with pytest.raises(ValueError, match="truncated"):
        benchmark._wav_duration(sample[:-2])


def test_private_endpoint_validation_rejects_public_credentials_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live endpoints must be private resolved POST URLs without credential-bearing components."""
    monkeypatch.setattr(
        benchmark.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 9000))],
    )
    assert (
        benchmark.validate_private_endpoint("http://private.test:9000/v1/audio/speech").hostname
        == "private.test"
    )
    for endpoint in (
        "https://user:secret@private.test/v1/audio/speech",
        "http://private.test/v1/audio/speech?token=secret",
        "ftp://private.test/path",
    ):
        with pytest.raises(ValueError):
            benchmark.validate_private_endpoint(endpoint)

    monkeypatch.setattr(
        benchmark.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 9000))],
    )
    with pytest.raises(ValueError, match="private"):
        benchmark.validate_private_endpoint("http://public.test/v1/audio/speech")


def test_operator_metadata_matches_manifest_and_memory_needs_complete_measurement(
    manifest: dict[str, Any], host_metadata: dict[str, Any]
) -> None:
    """Only complete operator facts become live provenance or a memory measurement."""
    validated = benchmark._validate_host_metadata(host_metadata, manifest)
    assert validated is host_metadata
    assert benchmark._memory_report(host_metadata) == benchmark._NEUTRAL_MEMORY

    measured = {
        **host_metadata,
        "memory": {
            "measured": True,
            "value": 23.5,
            "units": "GiB",
            "scope": "allocated",
            "device": "GPU-abc",
            "source": "nvidia-smi",
            "captured_at_utc": "2026-09-12T00:00:00Z",
        },
    }
    assert benchmark._memory_report(measured) == {
        "value": 23.5,
        "status": "operator_reported_measurement",
        "units": "GiB",
        "scope": "allocated",
        "device": "GPU-abc",
        "source": "nvidia-smi",
        "captured_at_utc": "2026-09-12T00:00:00Z",
    }
    host_metadata["revisions"]["server_sha256"] = "different"
    with pytest.raises(ValueError, match="server_sha256"):
        benchmark._validate_host_metadata(host_metadata, manifest)


def test_apple_silicon_mps_or_cpu_metadata_does_not_need_cuda_fields(
    manifest: dict[str, Any], host_metadata: dict[str, Any]
) -> None:
    """An explicit Apple Silicon profile reports its actual MPS or CPU backend without fake CUDA data."""
    host_metadata["hardware"] = {
        "profile": "macos_apple_silicon",
        "backend": "mps",
        "device_name": "Apple M4 Max",
    }
    assert benchmark._validate_host_metadata(host_metadata, manifest) is host_metadata
    host_metadata["hardware"]["backend"] = "cuda"
    with pytest.raises(ValueError, match="mps or cpu"):
        benchmark._validate_host_metadata(host_metadata, manifest)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), True])
def test_request_timeout_must_be_positive_and_finite(timeout: float) -> None:
    """Invalid CLI timeout values fail before they can configure admission or HTTP work."""
    with pytest.raises(ValueError, match="timeout"):
        benchmark._validate_timeout_seconds(timeout)


class _MockClient:
    """An HTTPX client shim backed by a mock transport and observable constructor options."""

    def __init__(self, handler, options: list[dict[str, Any]]) -> None:
        self._client = _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        self._options = options

    async def __aenter__(self):
        """Enter the wrapped client scope."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Close the wrapped mock client."""
        await self._client.aclose()

    def stream(self, *args: Any, **kwargs: Any):
        """Delegate stream creation to the HTTPX mock transport client."""
        return self._client.stream(*args, **kwargs)


def _mock_client_factory(handler, options: list[dict[str, Any]]):
    """Return an AsyncClient replacement which records network-safety settings."""

    def factory(**kwargs: Any) -> _MockClient:
        options.append(kwargs)
        return _MockClient(handler, options)

    return factory


@pytest.mark.asyncio
async def test_private_http_does_not_follow_public_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect response stays definitive locally and never produces a second request."""
    requests: list[httpx.Request] = []
    options: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://public.example/steal"})

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", _mock_client_factory(handler, options))
    result = await benchmark._post_private_json("http://127.0.0.1/v1/audio/speech", {"input": "x"})

    assert result.status == 302
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert options == [
        {"trust_env": False, "follow_redirects": False, "timeout": benchmark._TIMEOUT_SECONDS}
    ]


@pytest.mark.asyncio
async def test_private_http_timeout_and_cancellation_close_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete HTTP scope is cancellable and respects the single local deadline."""
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowClient:
        async def __aenter__(self):
            entered.set()
            return self

        async def __aexit__(self, *args: Any) -> None:
            closed.set()

        def stream(self, *args: Any, **kwargs: Any):
            class SlowStream:
                async def __aenter__(self):
                    entered.set()
                    await asyncio.Event().wait()

                async def __aexit__(self, *args: Any) -> None:
                    return None

            return SlowStream()

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: SlowClient())
    task = asyncio.create_task(
        benchmark._post_private_json("http://127.0.0.1/v1/audio/speech", {"input": "x"})
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=b"")

    options: list[dict[str, Any]] = []
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", _mock_client_factory(slow_handler, options))
    monkeypatch.setattr(benchmark, "_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(TimeoutError):
        await benchmark._post_private_json("http://127.0.0.1/v1/audio/speech", {"input": "x"})


@pytest.mark.asyncio
async def test_output_path_is_rejected_before_live_endpoint_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disallowed artifact location prevents all live endpoint work."""
    called = False

    def validate(endpoint: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(benchmark, "validate_private_endpoint", validate)
    with pytest.raises(ValueError, match="under demo"):
        await benchmark.run_benchmark(
            mode="live",
            output=tmp_path / "outside-demo.json",
            endpoint="http://127.0.0.1/v1/audio/speech",
            host_metadata_path=tmp_path / "missing.json",
            exclusive_server=True,
        )
    assert called is False
