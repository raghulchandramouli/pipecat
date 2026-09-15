"""Exercise the real browser UI with synthetic microphone audio and live providers."""

import argparse
import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

INIT = """
window.interviewEvidence = [];
window.interviewPeers = [];
window.interviewPlaybackEvidence = [];
const NativePeer = window.RTCPeerConnection;
window.RTCPeerConnection = class extends NativePeer {
  constructor(...args) { super(...args); window.interviewPeers.push(this); }
  createDataChannel(...args) {
    const channel = super.createDataChannel(...args);
    channel.addEventListener('message', e => {
      try {
        const event = JSON.parse(e.data);
        window.interviewEvidence.push({at: performance.now(), ...event});
        if (event.type === 'interview.interruption') setTimeout(() => {
          const audio = document.querySelector('audio');
          window.interviewPlaybackEvidence.push({at: performance.now(), epoch: event.playback_epoch,
            muted: audio?.muted, paused: audio?.paused, detached: !audio?.srcObject});
        }, 0);
      } catch {}
    });
    return channel;
  }
};
navigator.mediaDevices.getUserMedia = async () => {
  const context = new AudioContext({sampleRate: 48000});
  const destination = context.createMediaStreamDestination();
  const silence = context.createConstantSource();
  silence.offset.value = 0; silence.connect(destination); silence.start();
  await context.resume();
  window.playInterviewInput = async (b64) => {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const buffer = await context.decodeAudioData(bytes.buffer);
    const source = context.createBufferSource(); source.buffer = buffer;
    source.connect(destination); source.start();
    await new Promise(resolve => {source.onended = resolve;});
  };
  return destination.stream;
};
"""


async def main() -> None:
    """Save browser events and screenshots, preserving failure evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7860")
    parser.add_argument("--chromium", help="Optional Chromium executable path.")
    parser.add_argument("--interrupt-opening", action="store_true")
    args = parser.parse_args()
    demo = Path(__file__).resolve().parents[1]
    out = demo / "browser/results"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "utc": datetime.now(UTC).isoformat(),
        "scope": "Chromium UI, synthetic microphone, real WebRTC and live providers",
        "passed": False,
        "interrupt_opening": args.interrupt_opening,
        "source_sha256": {
            str(p.relative_to(demo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((demo / "interview").rglob("*"))
            if p.suffix in {".py", ".js", ".html", ".css"}
        },
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=args.chromium, headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(INIT)
        try:
            report["stage"] = "opening"
            await page.goto(args.url)
            await page.screenshot(
                path=str(
                    out / ("interruption-setup.png" if args.interrupt_opening else "setup.png")
                ),
                full_page=True,
            )
            await page.locator('select[name="duration_minutes"]').select_option("5")
            await page.locator('button[type="submit"]').click()
            await page.locator("#permission-button").click()
            await page.wait_for_function(
                "window.interviewEvidence.filter(e=>e.type==='interview.reply').length >= 2",
                timeout=45000,
            )
            if args.interrupt_opening:
                report["stage"] = "interrupt_opening"
                await page.wait_for_function(
                    "window.interviewEvidence.some(e=>e.status==='Speaking')", timeout=45000
                )
                audio = base64.b64encode(
                    (out / "end-command/adapter-smoke.wav").read_bytes()
                ).decode()
                await page.evaluate("audio => window.playInterviewInput(audio)", audio)
                await page.wait_for_function(
                    "window.interviewEvidence.some(e=>e.type==='interview.ended')", timeout=45000
                )
                assert await page.evaluate(
                    "window.interviewPlaybackEvidence.some(e=>e.muted && e.paused && e.detached)"
                ), "interruption did not detach browser playback"
            else:
                await page.wait_for_function(
                    "window.interviewEvidence.filter(e=>e.status==='Speaking').length >= 2 && window.interviewEvidence.filter(e=>e.type==='interview.status').at(-1)?.status === 'Listening'",
                    timeout=45000,
                )
                report["stage"] = "candidate_answer"
                before = await page.evaluate("window.interviewEvidence.length")
                audio = base64.b64encode(
                    (out / "candidate/adapter-smoke.wav").read_bytes()
                ).decode()
                await page.evaluate("audio => window.playInterviewInput(audio)", audio)
                await page.wait_for_function(
                    "n => window.interviewEvidence.slice(n).some(e=>e.type==='interview.reply' && e.text !== 'Take your time. Is there anything you would like to add?')",
                    arg=before,
                    timeout=60000,
                )
                await page.screenshot(path=str(out / "conversation.png"), full_page=True)
                await page.wait_for_function(
                    "window.interviewEvidence.filter(e=>e.type==='interview.status').at(-1)?.status === 'Listening'",
                    timeout=45000,
                )
                report["stage"] = "spoken_end"
                audio = base64.b64encode(
                    (out / "end-command/adapter-smoke.wav").read_bytes()
                ).decode()
                await page.evaluate("audio => window.playInterviewInput(audio)", audio)
                await page.wait_for_function(
                    "window.interviewEvidence.some(e=>e.type==='interview.ended')", timeout=45000
                )
            report["passed"] = not errors
            report["stage"] = "ended"
        except Exception as error:
            report["failure"] = type(error).__name__ + ": " + str(error)[:600]
            await page.screenshot(
                path=str(
                    out / ("interruption-failure.png" if args.interrupt_opening else "failure.png")
                ),
                full_page=True,
            )
        finally:
            report["events"] = await page.evaluate("window.interviewEvidence")
            report["playback_interruptions"] = await page.evaluate(
                "window.interviewPlaybackEvidence"
            )
            report["page_errors"] = errors
            report["notice"] = await page.locator("#notice").inner_text()
            (
                out
                / ("browser-interruption.json" if args.interrupt_opening else "browser-smoke.json")
            ).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            if await page.locator("#end-button").is_enabled():
                await page.locator("#end-button").click(timeout=2000)
            await browser.close()
            (
                out
                / ("browser-interruption.json" if args.interrupt_opening else "browser-smoke.json")
            ).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"events", "source_sha256", "playback_interruptions"}
            },
            ensure_ascii=False,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
