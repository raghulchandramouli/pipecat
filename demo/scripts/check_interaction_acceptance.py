"""Check that the interaction acceptance pack is complete and parseable.

This is an artifact check. It never calls a provider and therefore cannot turn
an unrun live or human trial into a pass. Use ``--frontend`` only after the
browser test environment is prepared; skipped tests are failures.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pipecat.evals.scenario import load_scenario_file

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "demo" / "evals" / "interaction"
REQUIRED = {
    "interaction_ordinary_answer",
    "interaction_hesitation",
    "interaction_explain",
    "interaction_no_example",
    "interaction_mixed_help",
    "interaction_thinking",
    "interaction_interrupt",
    "interaction_explicit_controls",
    "interaction_recovery",
    "interaction_english",
    "interaction_tanglish",
    "interaction_hinglish",
    "interaction_simulated_conversation",
}


def check_scenarios() -> list[str]:
    """Load every interaction scenario and verify the required coverage."""
    errors: list[str] = []
    found: list[str] = []
    paths = sorted(SCENARIO_DIR.glob("*.yaml"))
    if not paths:
        return [f"no YAML scenarios found under {SCENARIO_DIR}"]
    for path in paths:
        try:
            scenario = load_scenario_file(path)
        except Exception as exc:  # report all broken fixtures in one run
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if scenario.name in found:
            errors.append(f"{path.name}: duplicate scenario name {scenario.name!r}")
        found.append(scenario.name)
        if not getattr(scenario, "source_path", None):
            errors.append(f"{path.name}: loader did not retain source_path")
    found_set = set(found)
    missing = REQUIRED - found_set
    extra = found_set - REQUIRED
    if missing:
        errors.append("missing required scenarios: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unexpected duplicate/unknown scenario names: " + ", ".join(sorted(extra)))
    return errors


def check_frontend() -> list[str]:
    """Run the focused frontend suite and reject skips or incomplete collection."""
    test_files = [
        ROOT / "demo/tests/test_browser_frontend.py",
        ROOT / "demo/tests/test_interaction_frontend.py",
    ]
    missing = [str(path) for path in test_files if not path.is_file()]
    if missing:
        return ["required frontend coverage files are missing: " + ", ".join(missing)]
    with tempfile.NamedTemporaryFile(suffix=".xml") as report:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "demo/pytest.ini",
            *(str(path.relative_to(ROOT)) for path in test_files),
            "-q",
            "--disable-warnings",
            f"--junitxml={report.name}",
        ]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        output = result.stdout + result.stderr
        try:
            root = ET.parse(report.name).getroot()
        except (ET.ParseError, OSError) as exc:
            return [f"frontend suite did not produce readable JUnit XML: {exc}", output.strip()]
    errors: list[str] = []
    if result.returncode:
        errors.append(f"frontend suite failed (exit {result.returncode})")
    cases = root.findall(".//testcase")
    totals = {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    if totals["skipped"] or totals["failures"] or totals["errors"]:
        errors.append(f"frontend JUnit result is not clean: {totals}")
    if totals["tests"] <= 0:
        errors.append(f"frontend JUnit result has no complete test count: {totals}")
    if errors:
        errors.append(output.strip())
    return errors


def main() -> int:
    """Run artifact checks, optionally followed by the browser frontend suite."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", action="store_true", help="run the focused browser suite")
    args = parser.parse_args()
    errors = check_scenarios()
    if args.frontend:
        errors.extend(check_frontend())
    if errors:
        print("INTERACTION ACCEPTANCE: FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"INTERACTION ACCEPTANCE: ARTIFACTS PASS ({len(REQUIRED)} scenarios parsed)")
    if args.frontend:
        print("INTERACTION ACCEPTANCE: FRONTEND PASS (zero skips)")
    else:
        print("Frontend suite: NOT RUN (use --frontend in prepared demo/.venv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
