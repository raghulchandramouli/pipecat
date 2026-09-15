"""Record safe manual-endpoint protocol evidence using synthetic Hinglish audio."""

import argparse
import asyncio
import audioop
import base64
import json
import os
import wave
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from websockets.asyncio.client import connect


async def main() -> None:
    """Send two delimited segments and save only allowlisted provider fields."""
    demo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--silence", action="store_true")
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args()
    load_dotenv(demo / ".env", override=False)
    with wave.open(str(demo / "sarvam/results/hinglish/adapter-smoke.wav")) as wav:
        pcm, _ = audioop.ratecv(wav.readframes(wav.getnframes()), 2, 1, 24000, 16000, None)
    params = dict(
        language_code="auto",
        model="saaras:v3-realtime",
        mode="codemix",
        endpointing="manual",
        sample_rate="16000",
        encoding="linear16",
        return_timestamps="true",
    )
    evidence = []
    sent_bytes = 0
    boundaries = []
    async with connect(
        "wss://api.sarvam.ai/speech-to-text-realtime/ws?" + urlencode(params),
        additional_headers={"API-SUBSCRIPTION-KEY": os.environ["SARVAM_API_KEY"]},
        open_timeout=20,
    ) as ws:

        async def receive() -> None:
            async for raw in ws:
                message = json.loads(raw)
                safe = {
                    k: message[k]
                    for k in (
                        "event",
                        "utterance_idx",
                        "text",
                        "start_s",
                        "end_s",
                        "code",
                        "is_fatal",
                    )
                    if k in message
                }
                evidence.append(safe)
                if message.get("event") != "transcript.partial":
                    print(json.dumps(safe, ensure_ascii=False), flush=True)
                if message.get("event") == "session.end":
                    return

        async def send() -> None:
            nonlocal sent_bytes

            async def audio(data: bytes) -> None:
                nonlocal sent_bytes
                for offset in range(0, len(data), 3200):
                    chunk = data[offset : offset + 3200]
                    await ws.send(
                        json.dumps(
                            {"event": "audio_input", "audio": base64.b64encode(chunk).decode()}
                        )
                    )
                    sent_bytes += len(chunk)
                    await asyncio.sleep(len(chunk) / 32000)

            if args.silence:
                await audio(bytes(16000))
            segments = [pcm, bytes(16000), pcm] if args.empty else [pcm, pcm]
            for data in segments:
                start_s = sent_bytes / 32000
                await ws.send(json.dumps({"event": "speech_start"}))
                await audio(data)
                await ws.send(json.dumps({"event": "speech_end"}))
                boundaries.append({"start_s": start_s, "end_s": sent_bytes / 32000})
                if args.silence:
                    await audio(bytes(8000))
                await asyncio.sleep(0.15)
            await asyncio.sleep(3)
            await ws.send(json.dumps({"event": "end"}))

        async with asyncio.timeout(30):
            await asyncio.gather(receive(), send())
    report = dict(
        utc=datetime.now(UTC).isoformat(),
        scope="synthetic manual protocol probe",
        events=evidence,
        sent_boundaries=boundaries,
    )
    name = "realtime-probe-silence.json" if args.silence else "realtime-probe.json"
    (demo / "sarvam/results/hinglish" / name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
