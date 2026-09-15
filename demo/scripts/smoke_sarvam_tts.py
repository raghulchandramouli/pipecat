"""Run one guarded Sarvam TTS request and save the returned WAV artifact.

Uses SARVAM_API_KEY from the environment or demo/.env, for example::

    demo/.venv/bin/python -m demo.scripts.smoke_sarvam_tts
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import wave
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo.interview.contracts import ReplyKind
from demo.interview.reply_guard import ReplyAuthorization, ReplyGuardProcessor
from demo.interview.sarvam_tts import InterviewSarvamTTSService
from pipecat.frames.frames import (
    ErrorFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.services.tts_service import TextAggregationMode
from pipecat.tests.utils import run_test

_ROOT = Path(__file__).resolve().parents[2]
_DEMO_ROOT = _ROOT / "demo"
_ADAPTER_SOURCE = _DEMO_ROOT / "interview" / "sarvam_tts.py"


def _output_dir(value: str) -> Path:
    """Resolve a requested artifact directory while keeping output under ``demo/``."""
    path = Path(value).expanduser().resolve()
    if path != _DEMO_ROOT and _DEMO_ROOT not in path.parents:
        raise argparse.ArgumentTypeError("output directory must be under demo/")
    return path


def _parser() -> argparse.ArgumentParser:
    """Build the explicit local smoke command interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=["en-IN", "hi-IN", "ta-IN"], default="en-IN")
    parser.add_argument("--speaker", default="shubh", help="configured Sarvam speaker")
    parser.add_argument(
        "--text",
        default="Thank you. Please describe the first signal that changed.",
        help="short spoken sentence for the measured request",
    )
    parser.add_argument(
        "--output-dir",
        type=_output_dir,
        default=_DEMO_ROOT / "sarvam" / "results",
        help="artifact directory under demo/",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="positive full-request timeout in seconds",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Run a real guard-to-adapter pipeline and persist its PCM as a valid WAV."""
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if not args.text.strip() or any(marker in args.text for marker in "●◐○"):
        raise ValueError("--text must be non-empty and cannot contain completion markers")
    captured_at = datetime.now(UTC).isoformat()
    adapter_source_sha256 = hashlib.sha256(_ADAPTER_SOURCE.read_bytes()).hexdigest()

    epoch = 1
    authorization = ReplyAuthorization(1, 1, 1, ReplyKind.QUESTION, 1, False)
    guard = ReplyGuardProcessor(
        lookup_authorization=lambda metadata: (
            authorization
            if metadata.get("interview_dispatch_id") == authorization.dispatch_id
            else None
        ),
        is_current=lambda candidate: candidate == authorization,
        output_metadata=lambda: {"interview_playback_epoch": epoch},
    )
    import os

    from dotenv import load_dotenv

    load_dotenv(_DEMO_ROOT / ".env", override=False)
    tts = InterviewSarvamTTSService(
        api_key=os.environ.get("SARVAM_API_KEY", ""),
        speaker=args.speaker,
        language_code=args.language,
        current_playback_epoch=lambda: epoch,
        timeout=args.timeout,
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    metadata = {
        "interview_response_token": authorization.response_token,
        "interview_dispatch_id": authorization.dispatch_id,
    }
    start = LLMFullResponseStartFrame()
    start.metadata.update(metadata)
    text = LLMTextFrame(f"● {args.text.strip()}")
    text.metadata.update(metadata)
    end = LLMFullResponseEndFrame()
    end.metadata.update(metadata)
    downstream, upstream = await run_test(Pipeline([guard, tts]), frames_to_send=[start, text, end])
    errors = [frame.error for frame in upstream if isinstance(frame, ErrorFrame)]
    audio = [frame for frame in downstream if isinstance(frame, TTSAudioRawFrame)]
    lifecycle = [
        frame
        for frame in downstream
        if isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSTextFrame, TTSStoppedFrame))
    ]
    if errors:
        raise RuntimeError("Sarvam TTS returned an error: " + "; ".join(errors))
    if (
        not audio
        or not isinstance(lifecycle[0], TTSStartedFrame)
        or not isinstance(lifecycle[-1], TTSStoppedFrame)
        or sum(isinstance(frame, TTSStartedFrame) for frame in lifecycle) != 1
        or sum(isinstance(frame, TTSStoppedFrame) for frame in lifecycle) != 1
        or sum(isinstance(frame, TTSTextFrame) for frame in lifecycle) != 1
    ):
        raise RuntimeError("Sarvam TTS did not produce one complete streamed lifecycle")
    context_ids = {frame.context_id for frame in lifecycle}
    if len(context_ids) != 1 or None in context_ids:
        raise RuntimeError("Sarvam TTS emitted an unscoped audio context")
    if hashlib.sha256(_ADAPTER_SOURCE.read_bytes()).hexdigest() != adapter_source_sha256:
        raise RuntimeError("adapter source changed while the smoke request was running")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = args.output_dir / "adapter-smoke.wav"
    pcm = b"".join(frame.audio for frame in audio)
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24_000)
        writer.writeframes(pcm)
    report = {
        "endpoint": "https://api.sarvam.ai/text-to-speech/stream",
        "captured_at_utc": captured_at,
        "adapter_source_sha256": adapter_source_sha256,
        "speaker": args.speaker,
        "model": "bulbul:v3",
        "language_code": args.language,
        "text": args.text.strip(),
        "timeout_seconds": args.timeout,
        "audio_path": str(wav_path.relative_to(_ROOT)),
        "audio_bytes": len(pcm),
        "context_id": context_ids.pop(),
        "lifecycle": [type(frame).__name__ for frame in lifecycle],
        "metrics": asdict(tts.last_metrics) if tts.last_metrics is not None else None,
        "worker_cleaned_up": tts._worker is None,
    }
    report_path = args.output_dir / "adapter-smoke.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    """Parse explicit CLI input, run one request, and print its local artifacts."""
    args = _parser().parse_args()
    try:
        report = asyncio.run(_run(args))
    except (RuntimeError, ValueError) as error:
        print(f"Sarvam adapter smoke failed: {error}")
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
