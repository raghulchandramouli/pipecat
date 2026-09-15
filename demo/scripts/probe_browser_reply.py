"""Probe the live coaching contract without storing a model response or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from demo.interview.browser_contract import BrowserInterviewSetup
from demo.interview.coaching import (
    build_coaching_prompt,
    parse_coaching_reply,
    validate_coaching_reply,
)
from demo.interview.config import QuestionRubric
from demo.interview.contracts import AcceptedAnswer, CompletionStatus, SegmentId
from demo.interview.pipeline import HINGLISH_INSTRUCTION, _config_for_setup, default_live_config
from demo.interview.reply_guard import parse_completion
from pipecat.turns.user_turn_completion_mixin import USER_TURN_COMPLETION_INSTRUCTIONS

SYNTHETIC_SEGMENTS = (
    "मैंने 1 backend project में slow database queries की problem solve की।",
    "पहले logs और query timings देखें",
    "फिर composite index add किया",
    "Response time 2 second se 200 milliseconds ho gaya.",
    "मैंने load test से result verify किया।",
)
SYNTHETIC_TRANSCRIPT = " ".join(SYNTHETIC_SEGMENTS)
CURRENT_QUESTION = (
    "Question 1 of 1 for this mid Backend engineer interview: role-relevant reasoning. "
    "Clear reasoning, reliable systems thinking, and concrete examples. "
    "Aap Hindi, English, ya Hinglish mein jawab de sakte hain."
)


def _parse_error_category(error: Exception) -> str:
    """Return a stable parse-failure category without recording model text."""
    if isinstance(error, json.JSONDecodeError):
        return "json_decode"
    if isinstance(error, ValueError):
        return "schema_validation"
    return "unexpected_parse_error"


def _validation_reason(error: ValueError) -> str:
    """Return an allowlisted validation outcome without exposing model content."""
    known = {
        "a follow-up reply must contain exactly one question",
        "a grounded follow-up requires the current finalized answer",
        "a grounded follow-up must anchor its question in a current-answer quote",
        "evidence competency is not configured",
        "evidence quote is not a verbatim candidate transcript substring",
        "evidence references an unknown candidate answer",
        "evidence uses a forbidden assessment ground",
        "spoken reply uses a forbidden assessment ground",
    }
    return str(error) if str(error) in known else "validation_rejected"


async def main() -> None:
    """Request one synthetic coaching reply and save only its validation outcome."""
    demo = Path(__file__).resolve().parents[1]
    load_dotenv(demo / ".env", override=False)
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key.strip():
        raise RuntimeError("GOOGLE_API_KEY is required for the browser coaching probe")

    config = _config_for_setup(
        default_live_config(),
        BrowserInterviewSetup(
            rubric=[
                QuestionRubric(
                    competency="role-relevant reasoning",
                    guidance="Clear reasoning, reliable systems thinking, and concrete examples.",
                )
            ]
        ),
    )
    answer = AcceptedAnswer(
        2,
        "question-1",
        tuple(SegmentId(1, index) for index in range(len(SYNTHETIC_SEGMENTS))),
        SYNTHETIC_TRANSCRIPT,
    )
    prompt = build_coaching_prompt(
        role=config.role,
        difficulty=config.difficulty,
        duration_minutes=config.duration_minutes,
        current_question=CURRENT_QUESTION,
        rubric=(config.question_rubric[0],),
        accepted_answers=(),
        current_answer=answer,
    )
    report: dict[str, object] = {
        "utc": datetime.now(UTC).isoformat(),
        "scope": "one full synthetic browser-answer transcript through the coaching contract",
        "model": config.providers.gemini.model,
        "thinking_level": config.providers.gemini.thinking_level,
        "transcript_sha256": hashlib.sha256(SYNTHETIC_TRANSCRIPT.encode()).hexdigest(),
        "segment_count": len(SYNTHETIC_SEGMENTS),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "language_instruction_sha256": hashlib.sha256(HINGLISH_INSTRUCTION.encode()).hexdigest(),
        "completion_status": "not_requested",
        "completion_valid": False,
        "coaching_json_valid": False,
        "verbatim_quote_valid": False,
    }
    client = genai.Client(api_key=api_key)
    try:
        try:
            async with asyncio.timeout(45):
                response = await client.aio.models.generate_content(
                    model=config.providers.gemini.model,
                    contents=[
                        types.Content(role="model", parts=[types.Part(text=CURRENT_QUESTION)]),
                        types.Content(
                            role="user",
                            parts=[types.Part(text=SYNTHETIC_TRANSCRIPT), types.Part(text=prompt)],
                        ),
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            f"{USER_TURN_COMPLETION_INSTRUCTIONS}\n\n{HINGLISH_INSTRUCTION}"
                        ),
                        thinking_config=types.ThinkingConfig(
                            thinking_level=config.providers.gemini.thinking_level
                        ),
                        max_output_tokens=4096,
                    ),
                )
        except TimeoutError:
            report["provider_outcome"] = "timeout"
            return
        except Exception as error:
            report["provider_outcome"] = type(error).__name__
            return
        completion = parse_completion(response.text or "")
        report["provider_outcome"] = "response_received"
        report["completion_status"] = completion.status.value
        report["completion_valid"] = completion.is_valid
        if completion.status is not CompletionStatus.COMPLETE or not completion.is_valid:
            return
        try:
            coaching = parse_coaching_reply(completion.text)
        except Exception as error:
            report["coaching_parse_error"] = _parse_error_category(error)
            return
        report["coaching_json_valid"] = True
        try:
            validate_coaching_reply(
                coaching,
                rubric=(config.question_rubric[0],),
                accepted_answers=(),
                current_answer=answer,
            )
        except ValueError as error:
            report["validation_reason"] = _validation_reason(error)
        else:
            report["verbatim_quote_valid"] = True
    finally:
        await client.aio.aclose()
        output = demo / "browser/results/coaching-probe.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, separators=(",", ":")))
        if not report["verbatim_quote_valid"]:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
