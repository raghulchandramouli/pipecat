"""Source-provenance boundaries for non-answer interview interaction."""

import pytest

from demo.interview.interaction import (
    SourceDisposition,
    SourceSpan,
    answer_text_from_spans,
    validate_source_spans,
)


def test_mixed_help_keeps_exact_answer_source_and_excludes_help() -> None:
    """Only answer ranges contribute evidence after a complete source partition."""
    source = "I handled customer complaints. What do you mean by conflict?"
    answer_end = source.index(" What")
    spans = (
        SourceSpan(start=0, end=answer_end, disposition=SourceDisposition.ANSWER),
        SourceSpan(start=answer_end, end=len(source), disposition=SourceDisposition.HELP),
    )

    assert validate_source_spans(source, spans) == spans
    assert answer_text_from_spans(source, spans) == "I handled customer complaints."


@pytest.mark.parametrize(
    "spans",
    [
        (SourceSpan(start=0, end=2, disposition=SourceDisposition.ANSWER),),
        (
            SourceSpan(start=0, end=2, disposition=SourceDisposition.ANSWER),
            SourceSpan(start=3, end=4, disposition=SourceDisposition.HELP),
        ),
        (
            SourceSpan(start=0, end=3, disposition=SourceDisposition.ANSWER),
            SourceSpan(start=2, end=4, disposition=SourceDisposition.HELP),
        ),
    ],
)
def test_source_spans_must_cover_the_exact_immutable_input(spans) -> None:
    """Gaps and overlap cannot silently remove candidate material."""
    with pytest.raises(ValueError):
        validate_source_spans("four", spans)


def test_uncertain_source_cannot_be_accepted_as_an_answer() -> None:
    """A classifier must request clarification instead of reclaiming uncertainty."""
    spans = (SourceSpan(start=0, end=5, disposition=SourceDisposition.UNCERTAIN),)
    with pytest.raises(ValueError, match="uncertain"):
        answer_text_from_spans("okay!", spans)
