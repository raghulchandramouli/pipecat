"""Whole utterance voice controls for the interview flow."""

from __future__ import annotations

import unicodedata

from .interaction import InteractionAction

VoiceControl = InteractionAction


_CONTROLS = {
    VoiceControl.REPEAT: {
        "repeat",
        "repeat the question",
        "say that again",
        "once more",
        "thirumba sollunga",
        "thirumbi sollunga",
        "kelviyai thirumba sollunga",
        "கேள்வியை திரும்ப சொல்லுங்கள்",
        "திரும்ப சொல்லுங்க",
        "மீண்டும் சொல்லுங்கள்",
        "phir se boliye",
        "sawal phir se boliye",
        "सवाल फिर से बोलिए",
        "दोबारा बताइए",
    },
    VoiceControl.EXPLAIN: {
        "explain",
        "explain the question",
        "what do you mean",
        "konjam explain pannunga",
        "kelviya konjam vilakkunga",
        "இந்த கேள்வியை விளக்குங்கள்",
        "கொஞ்சம் விளக்குங்கள்",
        "கேள்வி புரியல",
        "samjha dijiye",
        "sawal samjha dijiye",
        "यह सवाल समझाइए",
        "थोड़ा समझाइए",
        "सवाल समझ नहीं आया",
    },
    VoiceControl.THINKING: {
        "i need thinking time",
        "i need time to think",
        "thinking time",
        "let me think",
        "give me a moment",
        "give me a minute",
        "konjam yosikka time venum",
        "yosikka konjam time venum",
        "கொஞ்சம் யோசிக்க நேரம் வேண்டும்",
        "கொஞ்சம் நேரம் வேண்டும்",
        "கொஞ்சம் டைம் வேணும்",
        "mujhe sochne ka time chahiye",
        "thoda sochne ka waqt chahiye",
        "मुझे सोचने का समय चाहिए",
        "थोड़ा समय दीजिए",
        "एक मिनट दीजिए",
    },
    VoiceControl.SKIP: {
        "skip",
        "skip this question",
        "skip the question",
        "move to the next question",
        "next question",
        "indha kelviyai skip pannunga",
        "adutha kelvikku pogalaam",
        "இந்த கேள்வியை தவிர்க்கவும்",
        "அடுத்த கேள்விக்கு போகலாம்",
        "is sawal ko skip kijiye",
        "agla sawal poochiye",
        "इस सवाल को छोड़ दीजिए",
        "अगला सवाल पूछिए",
    },
    VoiceControl.END: {
        "end interview",
        "end the interview",
        "stop interview",
        "stop the interview",
        "finish interview",
        "interview mudichidunga",
        "nerkaanalai mudikkavum",
        "நேர்காணலை முடிக்கவும்",
        "interview khatam kijiye",
        "interview band kijiye",
        "इंटरव्यू खत्म कीजिए",
        "इंटरव्यू बंद कीजिए",
    },
    VoiceControl.RETRY_RESPONSE: {
        "retry response",
        "try that response again",
        "reply retry pannunga",
        "जवाब फिर से भेजिए",
    },
    VoiceControl.RETRY_TRANSCRIPTION: {
        "retry transcription",
        "transcription retry pannunga",
        "என் பதிலை மீண்டும் கேளுங்கள்",
        "मेरी बात फिर से सुनिए",
    },
    VoiceControl.RESTART_ANSWER: {
        "restart my answer",
        "start my answer again",
        "answerai restart pannunga",
        "பதிலை மறுபடியும் தொடங்குகிறேன்",
        "मेरा जवाब फिर से शुरू करें",
    },
}
_POLITE = {
    "please",
    "can you",
    "could you",
    "would you",
    "kindly",
    "thanks",
    "thank you",
    "தயவு செய்து",
    "कृपया",
}


def _normalize(text: str) -> str:
    """Normalize matching while preserving combining marks and raw source."""
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(
        "".join(" " if unicodedata.category(c).startswith("P") else c for c in text).split()
    )


def _strip_polite(command: str) -> str:
    words = command.split()
    changed = True
    while words and changed:
        changed = False
        for phrase in _POLITE:
            tokens = phrase.split()
            if words[: len(tokens)] == tokens:
                words = words[len(tokens) :]
                changed = True
                break
            if words[-len(tokens) :] == tokens:
                words = words[: -len(tokens)]
                changed = True
                break
    return " ".join(words)


def parse_voice_control(text: str) -> VoiceControl | None:
    """Return a control only when the whole normalized utterance is a command."""
    command = _strip_polite(_normalize(text))
    return next(
        (
            control
            for control, phrases in _CONTROLS.items()
            if command in {_normalize(phrase) for phrase in phrases}
        ),
        None,
    )
