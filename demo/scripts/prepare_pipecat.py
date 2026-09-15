"""Copy the local framework into a demo-local build directory for installation."""

from pathlib import Path
from shutil import copy2, copytree, ignore_patterns


def main() -> None:
    """Prepare package metadata without writing build artifacts into framework source."""
    root = Path(__file__).resolve().parents[2]
    destination = root / "demo" / ".build" / "pipecat"
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        copy2(root / filename, destination / filename)
    copytree(
        root / "src",
        destination / "src",
        dirs_exist_ok=True,
        ignore=ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
    )
    print(destination)


if __name__ == "__main__":
    main()
