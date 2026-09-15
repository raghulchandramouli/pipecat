"""Download the pinned Rumik weights and codec into the repository models directory."""

import os
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
MODELS = DEMO.parent / "models"
REVISION = "0ed3c98684e14350c910129c0efa179841c41ad2"
os.environ["HF_HOME"] = str(MODELS / ".hf-cache")
os.environ["HF_MODULES_CACHE"] = str(MODELS / ".hf-modules")


def main() -> None:
    """Fetch an immutable complete snapshot without changing the inspected source copy."""
    from huggingface_hub import snapshot_download

    destination = snapshot_download(
        "rumik-ai/rumik-oss-1",
        revision=REVISION,
        local_dir=MODELS / "rumik-oss-1" / REVISION,
        max_workers=4,
    )
    print(f"Pinned model ready: {destination}", flush=True)


if __name__ == "__main__":
    main()
