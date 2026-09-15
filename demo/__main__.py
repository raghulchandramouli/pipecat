"""Offline configuration smoke entry point: run with ``python -m demo``."""

from demo.interview.config import Difficulty, InterviewConfig, QuestionRubric, RoleConfig


def main() -> None:
    """Validate a sample interview configuration without contacting providers."""
    config = InterviewConfig(
        role=RoleConfig(title="Backend engineer"),
        difficulty=Difficulty.MID,
        duration_minutes=30,
        question_rubric=(
            QuestionRubric(
                competency="Incident response",
                guidance="Explain the diagnosis, your contribution, and the outcome.",
            ),
        ),
    )
    print("Interview foundation ready (offline; no microphone or provider calls).")
    print(f"Role: {config.role.title}; duration: {config.duration_minutes} minutes")
    print(
        "Run the offline interview: PYTHONPATH=src demo/.venv/bin/python -m demo.scripted_interview"
    )


if __name__ == "__main__":
    main()
