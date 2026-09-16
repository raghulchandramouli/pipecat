"""Measure the real five-minute browser deadline without accelerating the clock."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

from demo.scripts.smoke_interaction import INTERACTION_INIT


async def main() -> None:
    """Verify automatic ending and browser media cleanup after five elapsed minutes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7871")
    args = parser.parse_args()
    out = Path("demo/browser/results/duration") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    report = {"passed": False, "duration_minutes": 5, "checkpoints": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(
            INTERACTION_INIT
            + """
          const capture = navigator.mediaDevices.getUserMedia;
          navigator.mediaDevices.getUserMedia = async (...args) => {
            const stream = await capture(...args);
            window.durationTracks = stream.getTracks();
            return stream;
          };
        """
        )
        try:
            await page.goto(args.url)
            await page.locator('[name="duration_minutes"]').select_option("5")
            await page.locator('button[type="submit"]').click()
            await page.locator("#permission-button").click()
            await page.wait_for_function(
                "window.interviewOutbound.some(e => e.type === 'interview.ready')", timeout=30000
            )
            started = await page.evaluate(
                "window.interviewOutbound.find(e => e.type === 'interview.ready').at"
            )
            for target in [30, 60, 90, 120, 150, 180, 210, 240, 270, 295]:
                elapsed = await page.evaluate("performance.now()") - started
                await asyncio.sleep(max(0, target - elapsed / 1000))
                checkpoint = await page.evaluate("""() => ({
                  at: performance.now(), status: document.querySelector('#status-label').textContent,
                  ended: window.interviewEvidence.some(e => e.type === 'interview.ended')
                })""")
                checkpoint["elapsed_seconds"] = (checkpoint.pop("at") - started) / 1000
                report["checkpoints"].append(checkpoint)
                print(json.dumps(checkpoint), flush=True)
                assert not checkpoint["ended"], "Interview ended before five minutes"
                if target in (90, 180):
                    action = "thinking" if target == 90 else "repeat"
                    await page.locator(f'[data-action="{action}"]').click()
            await page.wait_for_function(
                "window.interviewEvidence.some(e => e.type === 'interview.ended')", timeout=15000
            )
            ended = await page.evaluate(
                "window.interviewEvidence.find(e => e.type === 'interview.ended').at"
            )
            report["elapsed_seconds"] = (ended - started) / 1000
            assert 299 <= report["elapsed_seconds"] <= 305
            await page.wait_for_function(
                """() =>
              window.durationTracks.every(track => track.readyState === 'ended') &&
              window.interviewPeers.every(peer => peer.connectionState === 'closed') &&
              document.querySelector('#status-label').textContent === 'Ended'
            """,
                timeout=10000,
            )
            report["media_closed"] = True
            report["restart_visible"] = await page.locator("#restart-button").is_visible()
            assert report["restart_visible"]
            await page.screenshot(path=str(out / "ended.png"), full_page=True)
            report["passed"] = True
        except Exception as error:
            report["error"] = str(error)
        finally:
            report["page_errors"] = errors
            report["events"] = await page.evaluate("window.interviewEvidence")
            if errors:
                report["passed"] = False
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in report.items() if k != "events"}), flush=True)
            print(out / "report.json", flush=True)
            await browser.close()
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
