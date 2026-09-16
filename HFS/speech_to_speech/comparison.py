"""Honest comparison report generation for speech-to-speech run manifests.

This module deliberately only analyses evidence supplied by a run.  It never
infers audibility from server events and never turns missing values into zero.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

NOT_MEASURED = "NOT MEASURED"
NO_RESULT = "NO QUALIFIED RESULT"


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return default


def _config(run: dict[str, Any]) -> dict[str, Any]:
    c = dict(run.get("config") or run.get("condition") or {})
    for key in ("model", "prompt", "voice", "language", "device", "fixture",
                "endpointing", "validation_policy"):
        if key in run and key not in c:
            c[key] = run[key]
    return c


def _finite_number(value: Any, integer: bool = False) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and (not integer or isinstance(value, int)))


def _count(value: Any) -> bool:
    return _finite_number(value, True) and value >= 0


def _turns(run: dict[str, Any]) -> list[dict[str, Any]]:
    value = _first(run, "turns", "logical_turns", "attempts", default=[])
    return value if isinstance(value, list) else []


def _status(turn: dict[str, Any]) -> str:
    value = str(turn.get("status", "fail")).lower()
    return value if value in {"pass", "fail", "not_run"} else "fail"


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * p
    low, high = math.floor(rank), math.ceil(rank)
    return values[low] if low == high else values[low] + (values[high] - values[low]) * (rank - low)


def _capture_latency(turn: dict[str, Any], kind: str) -> float | None:
    capture = turn.get("capture") or turn.get("audible_capture")
    if not isinstance(capture, dict) or capture.get("synchronized") is not True:
        return None
    rate = capture.get("sample_rate")
    if not _finite_number(rate) or rate <= 0:
        return None
    if kind == "reply":
        start, end = capture.get("answer_end_sample"), capture.get("first_audible_sample")
    else:
        start, end = capture.get("interrupt_start_sample"), capture.get("last_old_audible_sample")
    if not _finite_number(start, True) or not _finite_number(end, True) or start < 0 or end < 0:
        return None
    return (end - start) * 1000 / rate if end >= start else None


def _stage_latency(turn: dict[str, Any], kind: str) -> float | None:
    keys = ("stage_reply_latency_ms", "reply_latency_ms", "answer_end_to_reply_ms") if kind == "reply" else (
        "stage_interruption_latency_ms", "interruption_latency_ms")
    value = _first(turn, *keys)
    return float(value) if _finite_number(value) and value >= 0 else None


def _gate(run: dict[str, Any], name: str) -> bool:
    gate = _first(run, name, name.removesuffix("_qualification"), default=None)
    if isinstance(gate, dict):
        if "status" in gate and "passed" in gate and ((gate["status"] == "pass") != (gate["passed"] is True)):
            return False
        return gate.get("status") == "pass" or gate.get("passed") is True
    return gate is True


def _ratings(run: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[str]]:
    ratings = run.get("ratings") or run.get("human_ratings") or []
    if not isinstance(ratings, list):
        return False, [], ["ratings must be a list"]
    errors = []
    for i, rating in enumerate(ratings):
        if (not isinstance(rating, dict) or not _finite_number(rating.get("naturalness")) or
                not _finite_number(rating.get("control")) or not (1 <= rating.get("naturalness", 0) <= 5) or
                not (1 <= rating.get("control", 0) <= 5)):
            errors.append(f"rating {i}: malformed or out of range")
    usable = [r for r in ratings if isinstance(r, dict) and
              _finite_number(r.get("naturalness")) and _finite_number(r.get("control")) and
              1 <= r["naturalness"] <= 5 and 1 <= r["control"] <= 5]
    # Every offered language needs owner + two speakers.
    configured = _config(run).get("languages") or [_config(run).get("language")]
    if isinstance(configured, str):
        configured = [configured]
    languages = set(configured) - {None}
    owner_by_lang: dict[str, set[str]] = {str(x): set() for x in languages}
    speakers_by_lang: dict[str, set[str]] = {str(x): set() for x in languages}
    for r in usable:
        lang = str(r.get("language"))
        owner = str(r.get("rater_id") or r.get("rater") or r.get("role") or "")
        if lang in owner_by_lang and owner:
            if str(r.get("role", "")).lower() == "owner" or owner == "owner":
                owner_by_lang[lang].add(owner)
            else:
                speakers_by_lang[lang].add(owner)
    complete = all(len(o) == 1 and len(speakers_by_lang[k] - o) >= 2
                   for k, o in owner_by_lang.items()) and bool(usable)
    meets = complete and all(r["naturalness"] >= 4 and r["control"] >= 4 for r in usable)
    return meets and not errors, usable, errors


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    """Validate and summarize one candidate run."""
    turns = _turns(run)
    errors: list[str] = []
    config = _config(run)
    required = ("fixture", "model", "stt_model", "prompt", "voice", "language", "device", "endpointing", "validation_policy")
    errors.extend(f"missing config: {k}" for k in required if k not in config or config[k] in (None, ""))
    if not isinstance(_first(run, "turns", "logical_turns", "attempts", default=[]), list):
        errors.append("turns must be a list")
    ids: set[str] = set()
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            errors.append(f"turn {i}: malformed record")
            continue
        if not turn.get("turn_id") or str(turn["turn_id"]) in ids:
            errors.append(f"turn {i}: missing or duplicate turn_id")
        ids.add(str(turn.get("turn_id")))
        if str(turn.get("status", "")).lower() not in {"pass", "fail", "not_run"}:
            errors.append(f"turn {i}: invalid status")
    statuses = [_status(t) if isinstance(t, dict) else "fail" for t in turns]
    audible_reply = [_capture_latency(t, "reply") for t in turns]
    audible_interrupt = [_capture_latency(t, "interrupt") for t in turns]
    stage_reply = [_stage_latency(t, "reply") for t in turns]
    stage_interrupt = [_stage_latency(t, "interrupt") for t in turns]
    audible_reply = [x for x in audible_reply if x is not None]
    audible_interrupt = [x for x in audible_interrupt if x is not None]
    stage_reply = [x for x in stage_reply if x is not None]
    stage_interrupt = [x for x in stage_interrupt if x is not None]
    ratings_ok, ratings, rating_errors = _ratings(run)
    errors.extend(rating_errors)
    functional = _gate(run, "functional_qualification")
    cancellation = _gate(run, "cancellation_qualification")
    pass_turns = [t for t in turns if isinstance(t, dict) and _status(t) == "pass"]
    rates = {(t.get("capture") or {}).get("sample_rate") for t in pass_turns}
    capture_ok = len(audible_reply) == len(pass_turns) and len(audible_interrupt) == len(pass_turns) and bool(pass_turns) and len(rates) == 1 and None not in rates and all(
        (t.get("capture") or {}).get("synchronized") is True for t in pass_turns)
    sample_count = len(turns)
    sessions = set(str(t.get("session_id")) for t in turns if t.get("session_id") is not None)
    language_value = config.get("language")
    languages = {str(language_value)} if isinstance(language_value, str) and language_value else set()
    languages |= {str(t.get("language")) for t in turns if t.get("language")}
    count_fields = ("progression_opportunities", "control_opportunities", "repair_opportunities",
                    "opportunities", "accidental_advances")
    for i, turn in enumerate(pass_turns):
        for field in count_fields:
            if field in turn and not _count(turn[field]):
                errors.append(f"turn {i}: {field} must be a nonnegative integer")
    opportunities = sum(int(t.get("progression_opportunities", t.get("opportunities", 0)) or 0)
                        for t in pass_turns)
    accidental = sum(int(t.get("accidental_advances", 0) or 0) for t in pass_turns)
    coverage = {"progression": {}, "control": {}, "repair": {}}
    for turn in pass_turns:
        sid = str(turn.get("session_id"))
        for label, field in (("progression", "progression_opportunities"), ("control", "control_opportunities"),
                             ("repair", "repair_opportunities")):
            coverage[label][sid] = coverage[label].get(sid, 0) + int(turn.get(field, 0) or 0)
    coverage_ok = all(all(value > 0 for value in values.values()) for values in coverage.values())
    conditions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for turn in turns:
        if isinstance(turn, dict):
            key = (str(turn.get("language", config.get("language"))),
                   str(turn.get("device", config.get("device"))))
            conditions.setdefault(key, []).append(turn)
    condition_counts = {f"{language}/{device}": {
        "logical_turns": len(group),
        "sessions": len({str(t.get("session_id")) for t in group if t.get("session_id") is not None}),
    } for (language, device), group in conditions.items()}
    conditions_ok = bool(condition_counts) and all(
        x["logical_turns"] >= 30 and x["sessions"] >= 3 for x in condition_counts.values())
    if not conditions_ok:
        errors.append("each language/device condition requires 30 turns across 3 sessions")
    if isinstance(_first(run, "functional_qualification", default=None), dict):
        gate = run["functional_qualification"]
        if "status" in gate and "passed" in gate and ((gate["status"] == "pass") != (gate["passed"] is True)):
            errors.append("contradictory functional qualification gate")
    if isinstance(_first(run, "cancellation_qualification", default=None), dict):
        gate = run["cancellation_qualification"]
        if "status" in gate and "passed" in gate and ((gate["status"] == "pass") != (gate["passed"] is True)):
            errors.append("contradictory cancellation qualification gate")
    if len(ids) != len(turns):
        errors.append("turn_id values must be unique")
    qualified = (sample_count >= 30 and len(sessions) >= 3 and functional and cancellation and
                 capture_ok and ratings_ok and coverage_ok and conditions_ok and opportunities > 0 and accidental == 0 and
                 all(s == "pass" for s in statuses) and
                 (_percentile(audible_reply, .95) if _percentile(audible_reply, .95) is not None else math.inf) <= 5000 and
                 (_percentile(audible_interrupt, .95) if _percentile(audible_interrupt, .95) is not None else math.inf) <= 500 and not errors)
    groups = {}
    for group in ("cold", "warm"):
        selected = [t for t in turns if str(t.get("warmth", t.get("temperature", ""))).lower() == group]
        vals = [v for v in (_capture_latency(t, "reply") for t in selected) if v is not None]
        groups[group] = {"logical_turns": len(selected), "reply_latency_p95_ms": _percentile(vals, .95)}
    return {
        "name": run.get("name") or run.get("system") or run.get("candidate") or "unknown",
        "config": config, "status": "pass" if qualified else NO_RESULT,
        "qualified": qualified, "sample_count": sample_count,
        "sessions": len(sessions), "languages": sorted(languages),
        "denominator": {"logical_turns": sample_count, "pass": statuses.count("pass"),
                        "fail": statuses.count("fail"), "not_run": statuses.count("not_run")},
        "stage_metrics": {"reply_latency_p95_ms": _percentile(stage_reply, .95),
                           "interruption_latency_p95_ms": _percentile(stage_interrupt, .95),
                           "reply_latency_samples": len(stage_reply)},
        "warm_cold": groups,
        "audible_metrics": {"reply_latency_p95_ms": _percentile(audible_reply, .95) if capture_ok else NOT_MEASURED,
                             "interruption_latency_p95_ms": _percentile(audible_interrupt, .95) if audible_interrupt else NOT_MEASURED},
        "gates": {"functional": functional, "cancellation": cancellation,
                  "capture_synchronized": capture_ok, "human_ratings": ratings_ok,
                  "exploratory_30_across_3_sessions": sample_count >= 30 and len(sessions) >= 3,
                  "accidental_advances": accidental, "opportunities": opportunities,
                  "progression_controls_zero_accidental": opportunities > 0 and accidental == 0},
        "coverage": coverage,
        "condition_counts": condition_counts,
        "ratings": ratings,
        "errors": errors,
    }


def build_report(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a comparison report without promoting incomplete evidence."""
    summaries = [summarize_run(m) for m in manifests]
    comparable = len(summaries) == 2 and all(not s["errors"] for s in summaries) and all(
        all(summaries[0]["config"].get(k) == summaries[1]["config"].get(k)
            for k in ("fixture", "model", "stt_model", "prompt", "voice", "language", "device", "endpointing", "validation_policy"))
        for _ in [0])
    winner = None
    if comparable and all(x["qualified"] for x in summaries):
        values = [x["audible_metrics"]["reply_latency_p95_ms"] for x in summaries]
        if values[0] != values[1]:
            winner = summaries[values.index(min(values))]["name"]
    return {"schema_version": 1, "status": winner or NO_RESULT, "winner": winner,
            "comparable": comparable, "candidates": summaries,
            "reason": None if winner else "missing, failed, or mismatched qualification evidence"}


# Names retained as a small import-friendly API for report consumers.
compare_manifests = build_report
generate_report = build_report


def load_manifests(paths: list[str | Path]) -> list[dict[str, Any]]:
    """Load JSON manifests from disk."""
    result = []
    for path in paths:
        value = json.loads(Path(path).read_text())
        result.extend(value if isinstance(value, list) else [value])
    return [x for x in result if isinstance(x, dict)]


def main(argv: list[str] | None = None) -> int:
    """Run the comparison report command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--output", "-o", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(load_manifests(args.manifests))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        parser.error(f"invalid manifest: {exc}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
