#!/usr/bin/env python3
"""Create the isolated, pinned environment for the HFS experiment.

The script deliberately refuses to replace an existing checkout.  It records
the upstream revision and the resolved environment under ``HFS`` while leaving
the repository's root and ``demo`` environments untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

UPSTREAM_URL = "https://github.com/huggingface/speech-to-speech.git"
UPSTREAM_REVISION = "16d7f98ff712fb082d53497937f5456667c84680"
HFS_ROOT = Path(__file__).resolve().parent


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    """Run one command and return stdout, surfacing failures without cleanup."""
    completed = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, env=env)
    return completed.stdout.strip()


def _current_revision(checkout: Path) -> str:
    return _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])


def _prepare_checkout(checkout: Path, *, reuse_pinned: bool) -> None:
    if checkout.exists():
        if not reuse_pinned:
            raise RuntimeError(
                f"refusing to modify existing {checkout}; inspect it or rerun with --reuse-pinned "
                "only after confirming the revision"
            )
        if not (checkout / ".git").exists():
            raise RuntimeError(f"{checkout} is not a Git checkout")
        revision = _current_revision(checkout)
        if revision != UPSTREAM_REVISION:
            raise RuntimeError(f"{checkout} is at {revision}, expected pinned {UPSTREAM_REVISION}")
        return

    _run(["git", "clone", UPSTREAM_URL, str(checkout)])
    try:
        _run(["git", "-C", str(checkout), "checkout", "--detach", UPSTREAM_REVISION])
    except BaseException:
        # Preserve the partial checkout for inspection instead of deleting it.
        raise


def _uv_environment(venv: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
    environment["UV_CACHE_DIR"] = str(HFS_ROOT / ".cache" / "uv")
    return environment


def bootstrap(*, reuse_pinned: bool, sync: bool) -> None:
    """Clone the exact source revision and resolve dependencies in ``HFS/.venv``."""
    checkout = HFS_ROOT / "upstream"
    venv = HFS_ROOT / ".venv"
    _prepare_checkout(checkout, reuse_pinned=reuse_pinned)
    if _current_revision(checkout) != UPSTREAM_REVISION:
        raise RuntimeError("upstream revision changed while bootstrapping")
    if not sync:
        return

    environment = _uv_environment(venv)
    _run(["uv", "sync", "--project", str(checkout)], env=environment)
    upstream_lock = checkout / "uv.lock"
    if not upstream_lock.is_file():
        raise RuntimeError("upstream sync completed without generating uv.lock")
    lock_archive = HFS_ROOT / "dependency-lock.toml"
    shutil.copy2(upstream_lock, lock_archive)
    freeze = _run([str(venv / "bin" / "python"), "-m", "pip", "freeze"], env=environment)
    (HFS_ROOT / "requirements-resolved.txt").write_text(freeze + "\n", encoding="utf-8")
    (HFS_ROOT / "bootstrap-manifest.json").write_text(
        json.dumps(
            {
                "upstream_revision": UPSTREAM_REVISION,
                "upstream_path": str(checkout),
                "project_environment": str(venv),
                "uv_cache_dir": environment["UV_CACHE_DIR"],
                "dependency_lock": str(lock_archive),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Parse bootstrap options."""
    parser = argparse.ArgumentParser(description="Bootstrap the isolated HFS environment.")
    parser.add_argument(
        "--reuse-pinned",
        action="store_true",
        help="Use an existing upstream checkout only when it is exactly at the required revision.",
    )
    parser.add_argument(
        "--no-sync", action="store_true", help="Only prepare or verify the source checkout."
    )
    args = parser.parse_args()
    try:
        bootstrap(reuse_pinned=args.reuse_pinned, sync=not args.no_sync)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
