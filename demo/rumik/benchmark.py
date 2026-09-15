"""Offline and private-host benchmark harness for the pinned Rumik service."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import socket
import time
import urllib.parse
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from io import BytesIO
from math import isfinite
from pathlib import Path
from typing import Any

import httpx

from .admission import (
    AdmissionClosedError,
    AdmissionOverloadedError,
    AdmissionQuarantinedError,
    SingleFlightAdmission,
)

_QUEUE_CAPACITY = 2
_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_ROOT = Path(__file__).resolve().parent
_DEFAULT_REQUESTS = _DEFAULT_ROOT / "fixtures" / "requests.json"
_DEFAULT_SAMPLE = _DEFAULT_ROOT / "fixtures" / "sample.wav"
_DEFAULT_MANIFEST = _DEFAULT_ROOT / "manifest.json"
_DEMO_ROOT = _DEFAULT_ROOT.parent
_REQUIRED_HOST_METADATA = ("hardware", "revisions", "provenance", "installed_versions")
_NEUTRAL_MEMORY = {
    "value": None,
    "status": "unavailable",
    "reason": "No host-provided measured memory value is available.",
}


@dataclass(frozen=True)
class HTTPResult:
    """One bounded HTTP-like response returned by a benchmark transport.

    Parameters:
        status: HTTP status returned by the server or fixture.
        body: Response body, bounded before it reaches a case validator.
        content_type: Content type returned by the server or fixture.
    """

    status: int
    body: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class Case:
    """One benchmark request and its expected HTTP status."""

    case_id: str
    payload: dict[str, Any]
    expected_status: int


def _read_json(path: Path) -> Any:
    """Read one UTF-8 JSON value without accepting a missing file."""
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def load_cases(path: Path) -> list[Case]:
    """Load fixture cases while validating their small portable schema."""
    raw_cases = _read_json(path)
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("request fixtures must be a non-empty JSON list")
    cases: list[Case] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("each request fixture must be an object")
        case_id = raw.get("id")
        payload = raw.get("payload")
        expected_status = raw.get("expected_status")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise ValueError("fixture ids must be unique non-empty strings")
        if not isinstance(payload, dict) or not isinstance(expected_status, int):
            raise ValueError("fixture cases require object payload and integer expected_status")
        seen.add(case_id)
        cases.append(Case(case_id, payload, expected_status))
    return cases


def _read_manifest(path: Path) -> dict[str, Any]:
    """Read the pinned model/server manifest used for every output artifact."""
    manifest = _read_json(path)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    return manifest


def _wav_duration(payload: bytes) -> float:
    """Validate the pinned fixture/server WAV format and return its duration."""
    try:
        with wave.open(BytesIO(payload), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            pcm = wav.readframes(frames)
    except (EOFError, wave.Error) as error:
        raise ValueError("response is not a valid WAV file") from error
    if (channels, sample_width, sample_rate) != (1, 2, 24_000) or frames < 1:
        raise ValueError("response WAV must be mono 24000 Hz PCM16")
    if len(pcm) != frames * channels * sample_width:
        raise ValueError("response WAV PCM payload is truncated")
    return frames / sample_rate


def _is_private_address(value: str) -> bool:
    """Return whether one resolved address stays inside a private host boundary."""
    address = ipaddress.ip_address(value)
    permitted = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("::1/128"),
    )
    return any(address in network for network in permitted)


def validate_private_endpoint(endpoint: str) -> urllib.parse.ParseResult:
    """Validate a credential-free private POST URL and reject public resolution."""
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "live endpoint must be a credential-free private HTTP(S) URL without query"
        )
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as error:
        raise ValueError("live endpoint host cannot be resolved") from error
    if not addresses or not all(_is_private_address(item[4][0]) for item in addresses):
        raise ValueError("live endpoint must resolve only to loopback or private addresses")
    return parsed


async def _post_private_json(
    endpoint: str, payload: dict[str, Any], timeout_seconds: float | None = None
) -> HTTPResult:
    """Bound the complete cancellable HTTP operation, including headers and body."""
    timeout_seconds = _validate_timeout_seconds(
        _TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    async with asyncio.timeout(timeout_seconds):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout_seconds
        ) as client:
            async with client.stream(
                "POST",
                endpoint,
                json=payload,
                headers={"Accept": "audio/wav", "Accept-Encoding": "identity"},
            ) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
                    raise ValueError("response body exceeds benchmark limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ValueError("response body exceeds benchmark limit")
                return HTTPResult(
                    response.status_code, bytes(body), response.headers.get("Content-Type")
                )


def _fixture_transport(
    sample: bytes, responses: Mapping[str, Any]
) -> Callable[[dict[str, Any]], Awaitable[HTTPResult]]:
    """Build a deterministic local transport with one valid WAV and documented errors."""

    def response(name: str) -> HTTPResult:
        specification = responses[name]
        if not isinstance(specification, Mapping) or not isinstance(
            specification.get("status"), int
        ):
            raise ValueError("fixture response schema is invalid")
        if specification.get("body_file") == "sample.wav":
            body = sample
        elif "body_utf8" in specification and isinstance(specification["body_utf8"], str):
            body = specification["body_utf8"].encode("utf-8")
        else:
            body = json.dumps(specification.get("json", {}), sort_keys=True).encode("utf-8")
        return HTTPResult(specification["status"], body, specification.get("content_type"))

    async def transport(payload: dict[str, Any]) -> HTTPResult:
        await asyncio.sleep(0.01)
        if "input" not in payload:
            return response("missing_input")
        if not isinstance(payload["input"], str):
            return response("invalid_input_type")
        if not payload["input"]:
            return response("empty_input")
        if payload.get("speaker", "Ira") not in {"Ira", "Aisha", "Siya", "Zoya"}:
            return response("invalid_speaker")
        if payload.get("fixture_response") == "malformed_wav":
            return response("malformed_wav")
        return response("success")

    return transport


def _memory_report(host_metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only explicitly host-measured memory, never local process or GPU guesses."""
    if host_metadata is None:
        return dict(_NEUTRAL_MEMORY)
    memory = host_metadata.get("memory")
    if isinstance(memory, Mapping) and memory.get("measured") is True:
        value = memory.get("value")
        fields = ("units", "scope", "device", "source", "captured_at_utc")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
            and value >= 0
            and all(isinstance(memory.get(key), str) and memory[key].strip() for key in fields)
            and memory["units"] in {"bytes", "MiB", "GiB"}
        ):
            return {
                "value": value,
                "status": "operator_reported_measurement",
                **{key: memory[key] for key in fields},
            }
    return dict(_NEUTRAL_MEMORY)


def _validate_host_metadata(metadata: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Require operator-attested host facts that exactly match the local pinned manifest."""
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(key), dict) or not metadata[key]
        for key in _REQUIRED_HOST_METADATA
    ):
        raise ValueError(
            "host metadata must include non-empty hardware, revisions, provenance, and installed_versions"
        )
    hardware = metadata["hardware"]
    profile = hardware.get("profile")
    if profile == "cuda":
        if not all(
            isinstance(hardware.get(key), str) and hardware[key].strip()
            for key in ("gpu_uuid", "driver", "cuda")
        ):
            raise ValueError(
                "CUDA hardware must attest non-empty gpu_uuid, driver, and cuda values"
            )
    elif profile == "macos_apple_silicon":
        backend = hardware.get("backend")
        if (
            backend not in {"mps", "cpu"}
            or not isinstance(hardware.get("device_name"), str)
            or not hardware["device_name"].strip()
        ):
            raise ValueError(
                "Apple Silicon hardware must attest a non-empty device_name and mps or cpu backend"
            )
    else:
        raise ValueError("host hardware profile must be cuda or macos_apple_silicon")
    provenance = metadata["provenance"]
    if not isinstance(provenance.get("attestation"), str) or not provenance["attestation"].strip():
        raise ValueError("host provenance must include a non-empty operator attestation")
    revisions = metadata["revisions"]
    for key in ("model_revision", "server_revision", "server_sha256"):
        if revisions.get(key) != manifest.get(key):
            raise ValueError(f"host revision {key} does not match the local manifest")
    if not all(
        isinstance(value, str) and value.strip()
        for value in metadata["installed_versions"].values()
    ):
        raise ValueError("installed versions must contain explicit non-empty version strings")
    return metadata


def _validate_timeout_seconds(value: float) -> float:
    """Validate one shared HTTP and admission deadline supplied by an explicit CLI run."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError("request timeout must be finite and positive")
    return float(value)


def _case_error(error: BaseException) -> dict[str, str]:
    """Return a non-sensitive exception classification for one failed admission."""
    if isinstance(error, AdmissionOverloadedError):
        kind = "admission_overloaded"
    elif isinstance(error, AdmissionQuarantinedError):
        kind = "admission_quarantined"
    elif isinstance(error, AdmissionClosedError):
        kind = "admission_closed"
    elif isinstance(error, TimeoutError):
        kind = "timeout"
    else:
        kind = type(error).__name__
    return {"kind": kind}


async def _run_case(
    admission: SingleFlightAdmission, case: Case, *, artifact_directory: Path | None = None
) -> dict[str, Any]:
    """Submit one fixture/live case and report client-observed admission timing."""
    started = time.perf_counter()
    try:
        admitted = await admission.submit(case.payload)
    except Exception as error:
        return {
            "id": case.case_id,
            "expected_status": case.expected_status,
            "status": None,
            "error": _case_error(error),
            "latency_seconds": {
                "queue_wait": None,
                "request": None,
                "total": time.perf_counter() - started,
            },
        }
    total = time.perf_counter() - started
    response = admitted.value
    if not isinstance(response, HTTPResult):
        return {
            "id": case.case_id,
            "expected_status": case.expected_status,
            "status": None,
            "error": {"kind": "unexpected_transport_value"},
            "latency_seconds": {
                "queue_wait": admitted.queue_wait_seconds,
                "request": admitted.request_seconds,
                "total": total,
            },
        }
    outcome: dict[str, Any] = {
        "id": case.case_id,
        "expected_status": case.expected_status,
        "status": response.status,
        "ok": response.status == case.expected_status,
        "latency_seconds": {
            "queue_wait": admitted.queue_wait_seconds,
            "request": admitted.request_seconds,
            "total": total,
        },
    }
    if response.status == 200:
        try:
            if (
                response.content_type is None
                or response.content_type.split(";", 1)[0].strip().lower() != "audio/wav"
            ):
                raise ValueError("response content type is not audio/wav")
            outcome["wav_duration_seconds"] = _wav_duration(response.body)
            if artifact_directory is not None:
                artifact_directory.mkdir(parents=True, exist_ok=True)
                filename = hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:16] + ".wav"
                artifact = artifact_directory / filename
                artifact.write_bytes(response.body)
                outcome["audio_artifact_path"] = str(artifact)
        except ValueError:
            outcome["ok"] = False
            outcome["error"] = {"kind": "invalid_wav"}
    elif response.status != case.expected_status:
        outcome["error"] = {"kind": "unexpected_status"}
    return outcome


async def _run_cases(
    admission: SingleFlightAdmission,
    cases: Sequence[Case],
    *,
    concurrency: int = _QUEUE_CAPACITY,
    artifact_directory: Path | None = None,
) -> list[dict[str, Any]]:
    """Run cases with bounded client concurrency so queue delay remains observable."""
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(case: Case) -> dict[str, Any]:
        async with semaphore:
            return await _run_case(admission, case, artifact_directory=artifact_directory)

    return list(await asyncio.gather(*(guarded(case) for case in cases)))


async def run_benchmark(
    *,
    mode: str,
    output: Path,
    requests_path: Path = _DEFAULT_REQUESTS,
    manifest_path: Path = _DEFAULT_MANIFEST,
    endpoint: str | None = None,
    host_metadata_path: Path | None = None,
    exclusive_server: bool = False,
    warmups: int = 1,
    request_timeout_seconds: float = _TIMEOUT_SECONDS,
    save_wav: bool = False,
) -> dict[str, Any]:
    """Run fixture or explicitly authorized private-host benchmark and write JSON output."""
    if warmups < 0:
        raise ValueError("warmups must not be negative")
    timeout_seconds = _validate_timeout_seconds(request_timeout_seconds)
    resolved_output = output.resolve()
    if resolved_output != _DEMO_ROOT and _DEMO_ROOT not in resolved_output.parents:
        raise ValueError("benchmark output must be under demo/")
    cases = load_cases(requests_path)
    manifest = _read_manifest(manifest_path)
    host_metadata: Mapping[str, Any] | None = None
    if mode == "fixture":
        sample = (
            _DEFAULT_SAMPLE
            if requests_path == _DEFAULT_REQUESTS
            else requests_path.parent / "sample.wav"
        )
        responses_path = requests_path.parent / "responses.json"
        fixture_responses = _read_json(responses_path)
        if not isinstance(fixture_responses, dict):
            raise ValueError("fixture responses must be a JSON object")
        transport = _fixture_transport(sample.read_bytes(), fixture_responses)
        provenance: dict[str, Any] = {"kind": "synthetic_fixture", "measurements": "not_rumik"}
    elif mode == "live":
        if endpoint is None or host_metadata_path is None or not exclusive_server:
            raise ValueError(
                "live mode requires --endpoint, --host-metadata, and --exclusive-server"
            )
        validate_private_endpoint(endpoint)
        host_metadata = _validate_host_metadata(_read_json(host_metadata_path), manifest)

        async def transport(payload: dict[str, Any]) -> HTTPResult:
            return await _post_private_json(endpoint, payload, timeout_seconds)

        provenance = {
            "kind": "private_live_host",
            "endpoint_host": urllib.parse.urlparse(endpoint).hostname,
        }
    else:
        raise ValueError("mode must be fixture or live")

    admission = SingleFlightAdmission(
        transport, queue_capacity=_QUEUE_CAPACITY, timeout_seconds=timeout_seconds
    )
    valid_warmup = next((case for case in cases if case.expected_status == 200), None)
    if valid_warmup is None:
        raise ValueError("fixture cases require one successful warmup case")
    try:
        warmup_results = [await _run_case(admission, valid_warmup) for _ in range(warmups)]
        artifact_directory = output.parent / f"{output.stem}-audio" if save_wav else None
        results = await _run_cases(admission, cases, artifact_directory=artifact_directory)
    finally:
        await admission.close()
    report: dict[str, Any] = {
        "run_utc": datetime.now(UTC).isoformat(),
        "mode": mode,
        "manifest": manifest,
        "queue_capacity": _QUEUE_CAPACITY,
        "timeout_seconds": timeout_seconds,
        "save_wav": save_wav,
        "warmups": {"count": warmups, "results": warmup_results},
        "cases": results,
        "memory": _memory_report(host_metadata),
        "provenance": provenance,
        "pin_verification": {
            "remote_verified": False,
            "reason": "Benchmark records operator-attested host metadata and does not probe remote pins.",
        },
    }
    if host_metadata is not None:
        report["hardware"] = host_metadata["hardware"]
        report["revisions"] = host_metadata["revisions"]
        report["host_provenance"] = host_metadata["provenance"]
        report["installed_versions"] = host_metadata["installed_versions"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    """Build the explicit CLI parser without initiating benchmark work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=Path, default=_DEFAULT_REQUESTS)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--endpoint")
    parser.add_argument("--host-metadata", type=Path)
    parser.add_argument("--exclusive-server", action="store_true")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--request-timeout-seconds", type=float, default=_TIMEOUT_SECONDS)
    parser.add_argument("--save-wav", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse explicit CLI arguments and write one benchmark artifact."""
    args = _parser().parse_args(argv)
    try:
        report = asyncio.run(
            run_benchmark(
                mode=args.mode,
                output=args.output,
                requests_path=args.requests,
                manifest_path=args.manifest,
                endpoint=args.endpoint,
                host_metadata_path=args.host_metadata,
                exclusive_server=args.exclusive_server,
                warmups=args.warmups,
                request_timeout_seconds=args.request_timeout_seconds,
                save_wav=args.save_wav,
            )
        )
    except (OSError, ValueError, AdmissionOverloadedError, AdmissionQuarantinedError) as error:
        _parser().error(str(error))
    outcomes = [*report["warmups"]["results"], *report["cases"]]
    return 0 if all(result.get("ok") for result in outcomes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
