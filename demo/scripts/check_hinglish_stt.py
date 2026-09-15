"""Transcribe the synthetic Hinglish smoke WAV with Sarvam's code-mixed mode."""

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv


def main():
    """Save a live provider round-trip result without recording any credentials."""
    demo = Path(__file__).resolve().parents[1]
    load_dotenv(demo / ".env", override=False)
    path = demo / "sarvam/results/hinglish/adapter-smoke.wav"
    start = time.monotonic()
    with path.open("rb") as audio:
        response = httpx.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
            files={"file": (path.name, audio, "audio/wav")},
            data={"model": "saaras:v3", "mode": "codemix", "language_code": "hi-IN"},
            timeout=30,
            trust_env=False,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Sarvam STT returned HTTP {response.status_code}")
    body = response.json()
    result = {
        "model": "saaras:v3",
        "mode": "codemix",
        "input": str(path.relative_to(demo)),
        "transcript": body.get("transcript"),
        "language_code": body.get("language_code"),
        "request_seconds": time.monotonic() - start,
        "scope": "Synthetic TTS audio through REST STT; not live microphone/realtime acceptance.",
    }
    (path.parent / "stt-roundtrip.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
