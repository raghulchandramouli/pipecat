"""Record this Mac and the running local Rumik process for a live benchmark."""

import hashlib
import importlib.metadata
import json
import platform
import subprocess
from datetime import UTC, datetime, timezone
from pathlib import Path

import httpx

DEMO = Path(__file__).resolve().parents[1]


def sysctl(name: str) -> str:
    """Read one public hardware identifier from macOS."""
    return subprocess.check_output(["/usr/sbin/sysctl", "-n", name], text=True).strip()


def main() -> None:
    """Save measured host metadata without assuming a CUDA device."""
    response = httpx.get("http://127.0.0.1:6006/health", timeout=10, trust_env=False)
    response.raise_for_status()
    health = response.json()
    manifest = json.loads((DEMO / "rumik/manifest.json").read_text())
    wrapper_sha = hashlib.sha256((DEMO / "rumik/mac_server.py").read_bytes()).hexdigest()
    assert health["compatibility"]["local_wrapper_sha256"] == wrapper_sha
    assert health["compatibility"]["upstream_server_sha256"] == manifest["server_sha256"]
    now = datetime.now(UTC).isoformat()
    chip = sysctl("machdep.cpu.brand_string")
    runtime = health["runtime"]
    host = {
        "hardware": {
            "profile": "macos_apple_silicon",
            "backend": runtime["resolved_device"],
            "device_name": chip,
            "unified_memory_bytes": int(sysctl("hw.memsize")),
            "os": platform.platform(),
            "dtype": runtime["resolved_dtype"],
        },
        "revisions": {
            **{
                key: manifest[key] for key in ("model_revision", "server_revision", "server_sha256")
            },
            "local_wrapper_sha256": wrapper_sha,
        },
        "provenance": {
            "attestation": "Local Mac selected by owner; pinned download hashes verified; runtime read from local process health.",
            "captured_at_utc": now,
            "health": health,
        },
        "installed_versions": {
            package: importlib.metadata.version(package)
            for package in (
                "torch",
                "transformers",
                "huggingface-hub",
                "fastapi",
                "uvicorn",
                "httpx",
                "safetensors",
                "numpy",
            )
        },
        "memory": {
            "measured": True,
            "value": runtime["mps_driver_allocated_bytes"]
            if runtime["resolved_device"] == "mps"
            else runtime["max_rss_bytes"],
            "units": "bytes",
            "scope": "Metal driver process allocation snapshot"
            if runtime["resolved_device"] == "mps"
            else "process maximum RSS",
            "device": chip,
            "source": "local /health runtime measurement",
            "captured_at_utc": now,
        },
    }
    path = DEMO / "rumik/host-mac.json"
    path.write_text(json.dumps(host, indent=2) + "\n")
    print(f"Host metadata: {path}")
    print(json.dumps(host["hardware"], indent=2))


if __name__ == "__main__":
    main()
