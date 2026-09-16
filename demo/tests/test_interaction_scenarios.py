"""Structural coverage for the interaction acceptance scenario pack."""

from __future__ import annotations

from pathlib import Path

from pipecat.evals.scenario import load_scenario_file

SCENARIO_DIR = Path(__file__).parents[1] / "evals" / "interaction"
EXPECTED = {
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


def test_all_interaction_scenarios_load_and_cover_the_acceptance_matrix():
    """Fixtures load and contain meaningful timing, interruption, and language checks."""
    scenarios = [load_scenario_file(path) for path in sorted(SCENARIO_DIR.glob("*.yaml"))]
    assert {scenario.name for scenario in scenarios} == EXPECTED
    scripted = {scenario.name: scenario for scenario in scenarios if hasattr(scenario, "turns")}
    assert any(
        expectation.absent and expectation.within_ms >= 1500
        for turn in scripted["interaction_hesitation"].turns
        for expectation in turn.expect
    )
    interrupt_events = {
        expectation.event
        for turn in scripted["interaction_interrupt"].turns
        for expectation in turn.expect
    }
    assert "bot_interrupted" in interrupt_events
    for language in ("english", "tanglish", "hinglish"):
        text = " ".join(
            turn.user or "" for turn in scripted[f"interaction_{language}"].turns
        ).lower()
        assert "example" in text
        assert "detail" in text
