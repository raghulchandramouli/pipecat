"""Tests for honest speech comparison evidence accounting."""

import json

from HFS.speech_to_speech.comparison import NO_RESULT, build_report, main


def manifest(**kwargs):
    """Create a compact incomplete fixture."""
    base = {"name": "demo", "config": {"fixture": "f", "model": "m", "stt_model": "stt", "prompt": "p", "voice": "v", "language": "en", "device": "headset", "endpointing": "e", "validation_policy": "vp"}, "functional_qualification": True, "cancellation_qualification": True, "ratings": [{"language": "en", "rater": x, "naturalness": 4, "control": 4} for x in ("owner", "a", "b")]}
    base["turns"] = [{"turn_id": f"t{i}", "status": "pass", "session_id": str(i % 3), "progression_opportunities": 1, "control_opportunities": 1, "repair_opportunities": 1, "capture": {"synchronized": True, "sample_rate": 16000, "answer_end_sample": 0, "first_audible_sample": 1600, "interrupt_start_sample": 0, "last_old_audible_sample": 4000}} for i in range(30)]
    base.update(kwargs)
    return base


def test_zero_and_failure_denominators_are_retained():
    """Failed and unrun logical turns remain in the denominator."""
    r = build_report([manifest(turns=[{"status": "fail"}, {"status": "not_run"}])])
    assert r["status"] == NO_RESULT
    assert r["candidates"][0]["denominator"] == {"logical_turns": 2, "pass": 0, "fail": 1, "not_run": 1}
    assert r["candidates"][0]["audible_metrics"]["reply_latency_p95_ms"] == "NOT MEASURED"


def test_mismatch_and_cold_warm_never_produce_winner():
    """Mismatched candidate configurations cannot produce a winner."""
    a, b = manifest(name="a"), manifest(name="b", config={**manifest()["config"], "model": "other"})
    assert build_report([a, b])["winner"] is None


def test_cli_writes_report(tmp_path):
    """The CLI writes JSON output."""
    source, output = tmp_path / "run.json", tmp_path / "report.json"
    source.write_text(json.dumps(manifest()))
    assert main([str(source), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["status"] == NO_RESULT


def test_complete_fixture_qualifies_and_zero_latency_is_valid():
    """A complete matched fixture qualifies, while ties select no winner."""
    a = manifest(name="a")
    for turn in a["turns"]:
        turn["capture"]["first_audible_sample"] = 0
        turn["capture"]["last_old_audible_sample"] = 0
    b = manifest(name="b")
    for turn in b["turns"]:
        turn["capture"]["first_audible_sample"] = 0
        turn["capture"]["last_old_audible_sample"] = 0
    report = build_report([a, b])
    assert report["status"] == NO_RESULT
    assert all(x["qualified"] for x in report["candidates"])


def test_invalid_counts_ratings_and_capture_reject():
    """Invalid counts, ratings, and sample positions invalidate a run."""
    run = manifest()
    run["turns"][0]["progression_opportunities"] = True
    run["turns"][1]["capture"]["answer_end_sample"] = -1
    run["ratings"][0]["naturalness"] = 6
    summary = build_report([run])["candidates"][0]
    assert not summary["qualified"] and summary["errors"]
