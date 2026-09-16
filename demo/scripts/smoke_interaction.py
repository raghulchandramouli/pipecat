"""Exercise negotiated interview controls against a live browser and providers.

The report records browser-event arrival times. They measure neither physical
microphone capture nor audible speaker playback.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

from demo.scripts.smoke_browser import INIT

INTERACTION_INIT = (
    INIT
    + """
window.interviewOutbound = [];
const EvidencePeer = window.RTCPeerConnection;
window.RTCPeerConnection = class extends EvidencePeer {
  createDataChannel(...args) {
    const channel = super.createDataChannel(...args);
    const send = channel.send.bind(channel);
    channel.send = message => {
      try { window.interviewOutbound.push({at: performance.now(), ...JSON.parse(message)}); } catch {}
      return send(message);
    };
    return channel;
  }
};
"""
)


def _wav_chunk(params: Any, data: bytes) -> bytes:
    """Return one standards-compliant PCM WAV chunk using source parameters."""
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setparams(params)
        output.writeframes(data)
    return result.getvalue()


def split_wav_at_midpoint_silence(path: Path) -> tuple[bytes, bytes, dict[str, int]]:
    """Split a PCM WAV near its midpoint at the quietest short sample window."""
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if params.comptype != "NONE" or params.sampwidth != 2:
            raise ValueError("candidate fixture must be uncompressed 16-bit PCM WAV")
        frames = source.readframes(params.nframes)
    if params.nframes < params.framerate:
        raise ValueError("candidate fixture is too short to split")

    frame_width = params.nchannels * params.sampwidth
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    window_frames = max(1, params.framerate // 25)
    low, high = params.nframes * 35 // 100, params.nframes * 65 // 100
    best_frame, best_energy = params.nframes // 2, float("inf")
    for frame in range(low, high, window_frames):
        end = min(params.nframes, frame + window_frames)
        channel_start, channel_end = frame * params.nchannels, end * params.nchannels
        energy = sum(abs(sample) for sample in samples[channel_start:channel_end])
        if energy < best_energy:
            best_frame, best_energy = frame + (end - frame) // 2, energy
    split_byte = best_frame * frame_width
    return (
        _wav_chunk(params, frames[:split_byte]),
        _wav_chunk(params, frames[split_byte:]),
        {
            "split_frame": best_frame,
            "sample_rate": params.framerate,
            "total_frames": params.nframes,
        },
    )


async def _event_count(page: Page) -> int:
    return await page.evaluate("window.interviewEvidence.length")


async def _wait_for_action_result(page: Page, start: int, *, timeout: int = 20_000) -> None:
    await page.wait_for_function(
        "start => window.interviewEvidence.slice(start).some(event => "
        "event.type === 'interview.command_result' && event.outcome === 'applied')",
        arg=start,
        timeout=timeout,
    )


async def _wait_for_reply_after(page: Page, start: int, *, timeout: int = 60_000) -> None:
    await page.wait_for_function(
        "start => window.interviewEvidence.slice(start).some(event => "
        "event.type === 'interview.reply' && typeof event.text === 'string' && event.text.trim())",
        arg=start,
        timeout=timeout,
    )


async def _wait_for_reply_completion(page: Page, start: int, *, timeout: int = 45_000) -> None:
    """Wait for a reply, its speaking event, and a stable waiting status."""
    await page.wait_for_function(
        "start => { const events = window.interviewEvidence.slice(start); "
        "const reply = events.find(event => event.type === 'interview.reply'); "
        "return reply && events.some(event => event.status === 'Speaking' && "
        "event.at >= reply.at && event.playback_epoch === reply.playback_epoch); }",
        arg=start,
        timeout=10_000,
    )
    await page.wait_for_function(
        "start => { const events = window.interviewEvidence.slice(start); "
        "const reply = events.find(event => event.type === 'interview.reply' && event.text?.trim()); "
        "if (!reply) return false; const speaking = events.find(event => event.type === 'interview.status' && "
        "event.status === 'Speaking' && event.at >= reply.at && "
        "event.playback_epoch === reply.playback_epoch); if (!speaking) return false; "
        "const statuses = events.filter(event => event.type === 'interview.status' && event.at >= speaking.at); "
        "return ['Listening', 'Giving you time'].includes(statuses.at(-1)?.status); }",
        arg=start,
        timeout=timeout,
    )


async def _events(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate("window.interviewEvidence")


def _timings(
    events: list[dict[str, Any]], *, answer_start: int, input_stop: float
) -> dict[str, float | None]:
    """Derive event-arrival deltas for the answer path without claiming audibility."""
    answer_events = events[answer_start:]
    finals = [
        event
        for event in answer_events
        if event.get("type") == "interview.caption"
        and event.get("final")
        and event.get("speaker", "You") == "You"
    ]
    last_final = finals[-1] if finals else None
    thinking = next(
        (
            event
            for event in answer_events
            if (event.get("status") == "Thinking" or event.get("interaction_state") == "thinking")
            and last_final is not None
            and event["at"] >= last_final["at"]
        ),
        None,
    )
    reply = next(
        (
            event
            for event in answer_events
            if event.get("type") == "interview.reply"
            and last_final is not None
            and event["at"] >= last_final["at"]
        ),
        None,
    )
    speaking = next(
        (
            event
            for event in answer_events
            if event.get("type") == "interview.status"
            and event.get("status") == "Speaking"
            and reply is not None
            and event["at"] >= reply["at"]
        ),
        None,
    )
    return {
        "last_final_at_ms": last_final.get("at") if last_final else None,
        "thinking_at_ms": thinking.get("at") if thinking else None,
        "reply_at_ms": reply.get("at") if reply else None,
        "speaking_at_ms": speaking.get("at") if speaking else None,
        "last_final_to_thinking_ms": (thinking["at"] - last_final["at"])
        if thinking and last_final
        else None,
        "thinking_to_reply_ms": (reply["at"] - thinking["at"]) if reply and thinking else None,
        "reply_to_speaking_ms": (speaking["at"] - reply["at"]) if speaking and reply else None,
        "synthetic_input_stop_to_reply_ms": (reply["at"] - input_stop) if reply else None,
    }


async def main() -> None:
    """Run the bounded interaction flow and save evidence for manual review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7871")
    parser.add_argument("--chromium", help="Optional Chromium executable path.")
    parser.add_argument("--output-dir", type=Path, help="Evidence directory under demo/.")
    parser.add_argument(
        "--candidate",
        type=Path,
        default=Path("demo/browser/results/candidate/adapter-smoke.wav"),
        help="PCM WAV answer fixture, relative paths resolve from the repository root.",
    )
    parser.add_argument(
        "--language", choices=("english", "tanglish", "hinglish"), default="tanglish"
    )
    args = parser.parse_args()
    demo = Path(__file__).resolve().parents[1]
    out = (args.output_dir or demo / "browser/results/interaction").resolve()
    if demo not in out.parents:
        parser.error("--output-dir must be under demo/")
    out.mkdir(parents=True, exist_ok=True)
    candidate_path = (
        args.candidate if args.candidate.is_absolute() else demo.parent / args.candidate
    )
    first_chunk, second_chunk, split = split_wav_at_midpoint_silence(candidate_path)
    report: dict[str, Any] = {
        "utc": datetime.now(UTC).isoformat(),
        "scope": "Chromium UI, synthetic split microphone audio, real WebRTC and live providers",
        "timing_note": "Client event arrival deltas; not proof of physical microphone or audible playback.",
        "url": args.url,
        "language": args.language,
        "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "audio_split": split,
        "passed": False,
    }
    page: Page | None = None
    browser = None
    playwright = None
    page_errors: list[str] = []
    console_errors: list[str] = []
    try:
        playwright = await async_playwright().start()
        if playwright:
            browser = await playwright.chromium.launch(executable_path=args.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            await page.add_init_script(INTERACTION_INIT)
            report["stage"] = "opening"
            await page.goto(args.url, wait_until="domcontentloaded")
            await page.screenshot(path=str(out / "opening.png"), full_page=True)
            await page.locator('select[name="language"]').select_option(args.language)
            await page.locator('select[name="duration_minutes"]').select_option("5")
            await page.locator('button[type="submit"]').click()
            await page.locator("#permission-button").click()
            await page.wait_for_function(
                "() => window.interviewEvidence.filter(event => event.type === 'interview.reply').length >= 2",
                timeout=45_000,
            )
            await page.wait_for_function(
                "() => { const statuses = window.interviewEvidence.filter(event => "
                "event.type === 'interview.status'); return statuses.filter(event => event.status === 'Speaking').length >= 2 && "
                "statuses.at(-1)?.status === 'Listening'; }",
                timeout=45_000,
            )
            await page.wait_for_function(
                "() => { const controls = document.querySelector('#interaction-controls'); "
                "const prompt = document.querySelector('#question-text'); "
                "const explain = document.querySelector('button[data-action=explain]'); "
                "return !controls.hidden && Boolean(prompt?.textContent?.trim()) && explain && !explain.disabled; }",
                timeout=45_000,
            )
            opening_question = await page.locator("#question-text").inner_text()
            report["opening_question"] = opening_question
            await page.screenshot(path=str(out / "negotiated-controls.png"), full_page=True)

            report["stage"] = "explain"
            explain_start = await _event_count(page)
            await page.locator('button[data-action="explain"]').click()
            await _wait_for_action_result(page, explain_start)
            await _wait_for_reply_after(page, explain_start)
            assert await page.locator("#question-text").inner_text() == opening_question, (
                "Explain changed the active question"
            )
            report["explain_same_question"] = True
            await page.screenshot(path=str(out / "explained.png"), full_page=True)
            await _wait_for_reply_completion(page, explain_start)
            await page.wait_for_function(
                "() => !document.querySelector('button[data-action=thinking]').disabled",
                timeout=45_000,
            )

            report["stage"] = "thinking_and_answer"
            thinking_start = await _event_count(page)
            await page.locator('button[data-action="thinking"]').click()
            await _wait_for_action_result(page, thinking_start)
            await page.wait_for_function(
                "start => window.interviewEvidence.slice(start).some(event => event.interaction_state === 'thinking')",
                arg=thinking_start,
                timeout=20_000,
            )
            await _wait_for_reply_after(page, thinking_start)
            await _wait_for_reply_completion(page, thinking_start)
            answer_start = await _event_count(page)
            chunk_one_stop = await page.evaluate(
                "async audio => { await window.playInterviewInput(audio); return performance.now(); }",
                base64.b64encode(first_chunk).decode(),
            )
            await asyncio.sleep(1.5)
            pause_events = (await _events(page))[answer_start:]
            assert not any(
                event.get("type") == "interview.reply" and event.get("at", 0) >= chunk_one_stop
                for event in pause_events
            ), "interviewer replied before the split answer resumed"
            input_stop = await page.evaluate(
                "async audio => { await window.playInterviewInput(audio); return performance.now(); }",
                base64.b64encode(second_chunk).decode(),
            )
            await _wait_for_reply_after(page, answer_start)
            await page.wait_for_function(
                "start => { const events = window.interviewEvidence.slice(start); "
                "const reply = events.find(event => event.type === 'interview.reply'); "
                "return reply && events.some(event => event.type === 'interview.status' && "
                "event.status === 'Speaking' && event.at >= reply.at); }",
                arg=answer_start,
                timeout=45_000,
            )
            answer_events = await _events(page)
            report["no_reply_during_pause"] = True
            report["timings_ms"] = _timings(
                answer_events, answer_start=answer_start, input_stop=input_stop
            )
            report["grounded_reply"] = next(
                (
                    event.get("text")
                    for event in answer_events[answer_start:]
                    if event.get("type") == "interview.reply" and event.get("text", "").strip()
                ),
                None,
            )
            assert report["grounded_reply"], "no reply arrived after the candidate final"
            candidate_finals = [
                event
                for event in answer_events[answer_start:]
                if event.get("type") == "interview.caption"
                and event.get("final")
                and event.get("speaker", "You") == "You"
            ]
            candidate_final = candidate_finals[-1] if candidate_finals else None
            grounded_reply = next(
                (
                    event
                    for event in answer_events[answer_start:]
                    if event.get("type") == "interview.reply"
                    and candidate_final is not None
                    and event["at"] >= candidate_final["at"]
                ),
                None,
            )
            assert grounded_reply is not None, "reply was not released after a candidate final"
            follow_up = next(
                (
                    event
                    for event in answer_events[answer_start:]
                    if event.get("type") == "interview.status"
                    and event.get("phase") == "follow_up"
                    and event["at"] >= grounded_reply["at"]
                ),
                None,
            )
            assert follow_up is not None, (
                "candidate reply did not enter the interview follow-up phase"
            )
            report["grounding"] = {
                "candidate_final_before_reply": candidate_final["at"] <= grounded_reply["at"],
                "follow_up_phase_after_reply": True,
                "question_before_answer": opening_question,
                "question_after_reply": await page.locator("#question-text").inner_text(),
            }
            await page.screenshot(path=str(out / "grounded-reply.png"), full_page=True)

            report["stage"] = "end"
            await page.locator("#end-button").click()
            await page.wait_for_function(
                "() => document.querySelector('#end-button').disabled && "
                "document.querySelector('#status-label').textContent === 'Ended'",
                timeout=10_000,
            )
            report["end_closed"] = True
            report["passed"] = not page_errors and not any(
                event.get("type") == "interview.error" for event in await _events(page)
            )
            assert not await page.locator("#notice").inner_text(), (
                "unexpected playback/recovery notice"
            )
            report["stage"] = "ended"
    except Exception as error:
        report["passed"] = False
        report["failure"] = f"{type(error).__name__}: {str(error)[:800]}"
    finally:
        if page is not None:
            try:
                await page.screenshot(path=str(out / "final.png"), full_page=True)
            except Exception as screenshot_error:
                report["screenshot_failure"] = str(screenshot_error)[:300]
            try:
                report["events"] = await _events(page)
                report["failure_events"] = [
                    event
                    for event in report["events"]
                    if event.get("type") == "interview.error"
                    or (
                        event.get("type") == "interview.command_result"
                        and event.get("outcome") != "applied"
                    )
                ]
                report["outbound"] = await page.evaluate("window.interviewOutbound")
                report["notice"] = await page.locator("#notice").inner_text()
            except Exception as evidence_error:
                report["evidence_failure"] = str(evidence_error)[:300]
        report["page_errors"] = page_errors
        report["console_errors"] = console_errors
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
        (out / "current.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "events"}, ensure_ascii=False
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
