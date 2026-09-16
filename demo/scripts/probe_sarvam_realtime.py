"""Record safe Sarvam realtime STT evidence from a bounded WAV input."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from websockets.asyncio.client import connect

from demo.scripts.probe_evidence import (
    ProbeEvidence,
    ProbeFailure,
    classify_failure,
    load_pcm16_mono_wav,
    require_credential,
)

_DEMO = Path(__file__).resolve().parents[1]
_DEFAULT_INPUT = _DEMO / "sarvam" / "results" / "hinglish" / "adapter-smoke.wav"
_DEFAULT_OUTPUT = _DEMO / "sarvam" / "results" / "hinglish"


def _parser() -> argparse.ArgumentParser:
    """Build the standalone realtime STT probe command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-wav", type=Path, default=_DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--silence", action="store_true")
    parser.add_argument("--empty", action="store_true")
    return parser


def _safe_event(message: dict[str, object]) -> dict[str, object]:
    """Retain protocol metadata, never transcript or arbitrary provider payloads."""
    safe = {
        key: message[key]
        for key in ("event", "utterance_idx", "start_s", "end_s", "code", "is_fatal")
        if key in message
    }
    if isinstance(message.get("text"), str):
        safe["text_characters"] = len(message["text"])
    return safe


async def _probe(args: argparse.Namespace, pcm: bytes) -> dict[str, object]:
    """Send manual boundaries and return allowlisted protocol observations."""
    params = {
        "language_code": "auto",
        "model": "saaras:v3-realtime",
        "mode": "codemix",
        "endpointing": "manual",
        "sample_rate": "16000",
        "encoding": "linear16",
        "return_timestamps": "true",
    }
    key = os.environ.get("SARVAM_API_KEY")
    require_credential(key, name="SARVAM_API_KEY")
    events: list[dict[str, object]] = []
    boundaries: list[dict[str, float]] = []
    sent_bytes = 0

    async with connect(
        "wss://api.sarvam.ai/speech-to-text-realtime/ws?" + urlencode(params),
        additional_headers={"API-SUBSCRIPTION-KEY": key},
        open_timeout=min(args.timeout, 20.0),
        close_timeout=min(args.timeout, 10.0),
    ) as websocket:

        async def receive() -> None:
            async for raw in websocket:
                try:
                    incoming = json.loads(raw)
                except json.JSONDecodeError:
                    raise ProbeFailure(
                        "stt", "provider", "Check the provider service and retry once."
                    )
                if not isinstance(incoming, dict):
                    raise ProbeFailure(
                        "stt", "provider", "Check the provider service and retry once."
                    )
                safe = _safe_event(incoming)
                events.append(safe)
                if incoming.get("event") != "transcript.partial":
                    print(json.dumps(safe, ensure_ascii=False), flush=True)
                if incoming.get("event") == "session.end":
                    return

        async def send_audio(data: bytes) -> None:
            nonlocal sent_bytes
            for offset in range(0, len(data), 3200):
                chunk = data[offset : offset + 3200]
                await websocket.send(
                    json.dumps({"event": "audio_input", "audio": base64.b64encode(chunk).decode()})
                )
                sent_bytes += len(chunk)
                await asyncio.sleep(len(chunk) / 32_000)

        async def send() -> None:
            if args.silence:
                await send_audio(bytes(16_000))
            segments = [pcm, bytes(16_000), pcm] if args.empty else [pcm, pcm]
            for segment in segments:
                start_s = sent_bytes / 32_000
                await websocket.send(json.dumps({"event": "speech_start"}))
                await send_audio(segment)
                await websocket.send(json.dumps({"event": "speech_end"}))
                boundaries.append({"start_s": start_s, "end_s": sent_bytes / 32_000})
                if args.silence:
                    await send_audio(bytes(8_000))
                await asyncio.sleep(0.15)
            await websocket.send(json.dumps({"event": "end"}))

        async with asyncio.timeout(args.timeout):
            await asyncio.gather(receive(), send())
    return {"events": events, "sent_boundaries": boundaries, "sent_bytes": sent_bytes}


async def _run(args: argparse.Namespace) -> int:
    """Create evidence before validation, then finalize every terminal outcome."""
    evidence = ProbeEvidence(
        args.output_dir,
        "sarvam-realtime-stt",
        {
            "input_wav": args.input_wav.name,
            "timeout_seconds": args.timeout,
            "silence": args.silence,
            "empty_segment": args.empty,
            "requested_sample_rate_hz": 16_000,
        },
    )
    try:
        evidence.begin()
    except ProbeFailure as failure:
        print(f"{failure.stage} failed: {failure.category}. {failure.action}", file=sys.stderr)
        return 1
    stage = "initialization"
    try:
        if args.timeout <= 0:
            raise ProbeFailure(
                "configuration", "configuration", "Set --timeout to a positive value."
            )
        pcm, input_details = load_pcm16_mono_wav(args.input_wav)
        load_dotenv(_DEMO / ".env", override=False)
        require_credential(os.environ.get("SARVAM_API_KEY"), name="SARVAM_API_KEY")
        stage = "stt"
        result = await _probe(args, pcm)
        result["input"] = input_details
        evidence.finish(stage="stt", outcome="success", details=result)
    except BaseException as error:
        failure = classify_failure(error, stage=stage)
        try:
            evidence.finish(
                stage=failure.stage,
                outcome="failure",
                category=failure.category,
                action=failure.action,
            )
        except ProbeFailure:
            print(
                "output failed: output. Choose a writable output directory and inspect stderr.",
                file=sys.stderr,
            )
        print(
            f"{failure.stage} failed: {failure.category}. {failure.action} "
            f"Artifact: {evidence.directory}",
            file=sys.stderr,
        )
        return 1
    print(f"stt succeeded. Artifact: {evidence.directory}")
    return 0


def main() -> int:
    """Run the probe and retain an allowlisted manifest for any outcome."""
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
