"""Exercise Gemini native audio, a paused answer, and browser interview controls."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

from demo.scripts.smoke_interaction import INTERACTION_INIT, split_wav_at_midpoint_silence


async def main() -> None:
    """Save real-provider browser evidence without claiming physical audibility."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7871")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--language", default="tanglish")
    parser.add_argument("--follow-up-candidate", type=Path)
    args = parser.parse_args()
    out = Path("demo/browser/results/gemini-conversation") / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    out.mkdir(parents=True)
    first, second, _ = split_wav_at_midpoint_silence(args.candidate)
    report = {
        "model": "gemini-3.8-live",
        "passed": False,
        "scope": "Live WebRTC with synthetic microphone audio; audio receipt is not a listening assessment.",
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(INTERACTION_INIT)

        async def count():
            return await page.evaluate("window.interviewEvidence.length")

        async def wait_reply(start):
            await page.wait_for_function(
                "n => window.interviewEvidence.slice(n).some(e => e.type === 'interview.reply' && e.text?.trim())",
                arg=start,
                timeout=60000,
            )
            await page.wait_for_function(
                "n => {const es=window.interviewEvidence.slice(n); return es.some(e=>e.status==='Speaking') && ['Listening','Giving you time'].includes(es.filter(e=>e.type==='interview.status').at(-1)?.status)}",
                arg=start,
                timeout=60000,
            )

        async def action(name):
            start = await count()
            await page.locator(f'button[data-action="{name}"]').click()
            await page.wait_for_function(
                "n => window.interviewEvidence.slice(n).some(e=>e.type==='interview.command_result' && e.outcome==='applied')",
                arg=start,
                timeout=10000,
            )
            return start

        try:
            report["stage"] = "opening"
            await page.goto(args.url)
            await page.locator('select[name="language"]').select_option(args.language)
            await page.screenshot(path=str(out / "setup.png"), full_page=True)
            await page.locator('button[type="submit"]').click()
            await page.locator("#permission-button").click()
            await wait_reply(0)
            report["stage"] = "explain"
            await wait_reply(await action("explain"))
            report["stage"] = "thinking"
            start = await action("thinking")
            await page.wait_for_function(
                "n => window.interviewEvidence.slice(n).some(e=>e.interaction_state==='thinking')",
                arg=start,
                timeout=10000,
            )
            await asyncio.sleep(2)
            report["stage"] = "paused_answer"
            start = await count()
            await page.evaluate(
                "audio => window.playInterviewInput(audio)", base64.b64encode(first).decode()
            )
            await asyncio.sleep(1.5)
            events = await page.evaluate("window.interviewEvidence")
            assert not any(
                e.get("status") == "Speaking" or e.get("type") == "interview.reply"
                for e in events[start:]
            ), "Bot replied during candidate pause"
            await page.evaluate(
                "audio => window.playInterviewInput(audio)", base64.b64encode(second).decode()
            )
            await wait_reply(start)
            await page.screenshot(path=str(out / "follow-up.png"), full_page=True)
            if args.follow_up_candidate:
                report["stage"] = "natural_follow_up"
                start = await count()
                await page.evaluate(
                    "audio => window.playInterviewInput(audio)",
                    base64.b64encode(args.follow_up_candidate.read_bytes()).decode(),
                )
                await wait_reply(start)
                assert not (await page.locator("#question-count").inner_text()).strip()
                await page.screenshot(path=str(out / "natural-conversation.png"), full_page=True)
            report["stage"] = "repeat"
            await wait_reply(await action("repeat"))
            report["stage"] = "skip"
            await wait_reply(await action("skip"))
            report["received_audio"] = await page.evaluate(
                "async () => { const all=[]; for(const pc of window.interviewPeers) { const stats=await pc.getStats(); stats.forEach(s=>{if(s.type==='inbound-rtp' && s.kind==='audio') all.push({bytesReceived:s.bytesReceived,totalSamplesReceived:s.totalSamplesReceived});}); } return all; }"
            )
            assert any(item.get("bytesReceived", 0) > 0 for item in report["received_audio"]), (
                "No audio arrived over WebRTC"
            )
            report["stage"] = "end"
            await page.locator("#end-button").click()
            await page.wait_for_function(
                "document.querySelector('#end-button').disabled && document.querySelector('#status-label').textContent === 'Ended'",
                timeout=10000,
            )
            report["passed"] = True
        except Exception as error:
            report["error"] = str(error)
        finally:
            report["events"] = await page.evaluate("window.interviewEvidence")
            report["page_errors"] = errors
            report["notice"] = await page.locator("#notice").inner_text()
            if (
                errors
                or report["notice"]
                or any(e.get("type") == "interview.error" for e in report["events"])
            ):
                report["passed"] = False
            await page.screenshot(path=str(out / "final.png"), full_page=True)
            await browser.close()
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                json.dumps({k: v for k, v in report.items() if k != "events"}, ensure_ascii=False)
            )
            print(out / "report.json")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
