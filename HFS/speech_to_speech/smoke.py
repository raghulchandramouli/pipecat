"""Exercise the local realtime server with cached synthetic speech."""

import asyncio
import base64
import hashlib
import json
import runpy
import time
import wave
from pathlib import Path

import websockets


async def main():
    root = Path(__file__).resolve().parents[1]
    helpers = runpy.run_path(str(root / "upstream/scripts/synthetic_conversation_realtime_client.py"))
    output = root / "results" / time.strftime("smoke-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "turns": [], "source_hashes": {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "speech_to_speech").glob("*.py")
    }}
    try:
        async with websockets.connect("ws://127.0.0.1:8765/v1/realtime") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert first["type"] == "session.created", first
            for index, prompt in enumerate(helpers["PROMPTS"][:2]):
                path = output / f"input-{index}.wav"
                helpers["synthesize_with_say"](prompt, path)
                pcm = helpers["load_pcm16_mono_16k"](path)
                await helpers["stream_prompt"](ws, pcm)
                events, audio = [], bytearray()
                async with asyncio.timeout(45):
                    while True:
                        event = json.loads(await ws.recv())
                        if event["type"] == "response.output_audio.delta":
                            audio.extend(base64.b64decode(event.pop("delta")))
                        events.append(event)
                        (output / f"events-{index}.json").write_text(json.dumps(events, indent=2))
                        if event["type"] in {"response.done", "error"}:
                            break
                (output / f"events-{index}.json").write_text(json.dumps(events, indent=2))
                with wave.open(str(output / f"reply-{index}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(audio)
                success = (events[-1]["type"] == "response.done"
                           and events[-1].get("response", {}).get("status") == "completed"
                           and len(audio) > 0
                           and any(e["type"] == "conversation.item.input_audio_transcription.completed"
                                   and e.get("transcript", "").strip() for e in events)
                           and not any("failed" in e["type"] for e in events))
                report["turns"].append({"prompt": prompt, "passed": success, "audio_bytes": len(audio)})
                print(json.dumps(report["turns"][-1]), flush=True)
                if not success:
                    break
            report["passed"] = len(report["turns"]) == 2 and all(t["passed"] for t in report["turns"])
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2))
        print(output, flush=True)
    return report["passed"]


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
