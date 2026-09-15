"""Exact whole-utterance voice controls for interview flow."""

from __future__ import annotations

import re
from enum import StrEnum


class VoiceControl(StrEnum):
    """Supported interview controls."""

    REPEAT = "repeat"
    SKIP = "skip"
    THINKING = "thinking"
    END = "end"


_CONTROLS = {
    VoiceControl.REPEAT: {"repeat", "repeat the question"},
    VoiceControl.SKIP: {"skip", "skip this question"},
    VoiceControl.THINKING: {
        "i need thinking time",
        "i need time to think",
        "thinking time",
        "let me think",
        "give me a moment",
        "give me a minute",
    },
    VoiceControl.END: {"end interview", "end the interview", "stop interview"},
}
_POLITE = {"please", "can you", "could you", "thanks", "thank you"}


def parse_voice_control(text: str) -> VoiceControl | None:
    """Return a control only when the whole normalized utterance is a command."""
    normalized = re.sub(r"[^\w\s]+", " ", text.casefold())
    words = normalized.split()
    while words and " ".join(words[:2]) in _POLITE:
        words = words[2:]
    while words and words[0] in _POLITE:
        words = words[1:]
    while words and " ".join(words[-2:]) in _POLITE:
        words = words[:-2]
    while words and words[-1] in _POLITE:
        words = words[:-1]
    command = " ".join(words)
    return next((control for control, phrases in _CONTROLS.items() if command in phrases), None)
