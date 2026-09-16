"""Language catalogs for Gemini Live native speech.

Gemini Live supports native multilingual audio. The output-language keys are
stable application choices; input codes are a preference supplied to the
conversation prompt rather than a separate speech-recognition configuration.
"""

from __future__ import annotations

_GEMINI_LANGUAGE_PAIRS = (
    ("af", "Afrikaans"),
    ("ak", "Akan"),
    ("sq", "Albanian"),
    ("am", "Amharic"),
    ("ar", "Arabic"),
    ("hy", "Armenian"),
    ("as", "Assamese"),
    ("az", "Azerbaijani"),
    ("eu", "Basque"),
    ("be", "Belarusian"),
    ("bn", "Bengali"),
    ("bs", "Bosnian"),
    ("bg", "Bulgarian"),
    ("my", "Burmese"),
    ("ca", "Catalan"),
    ("ceb", "Cebuano"),
    ("zh", "Chinese"),
    ("hr", "Croatian"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("nl", "Dutch"),
    ("en", "English"),
    ("et", "Estonian"),
    ("fo", "Faroese"),
    ("fil", "Filipino"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("gl", "Galician"),
    ("ka", "Georgian"),
    ("de", "German"),
    ("el", "Greek"),
    ("gu", "Gujarati"),
    ("ha", "Hausa"),
    ("iw", "Hebrew"),
    ("hi", "Hindi"),
    ("hu", "Hungarian"),
    ("is", "Icelandic"),
    ("id", "Indonesian"),
    ("ga", "Irish"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("kn", "Kannada"),
    ("kk", "Kazakh"),
    ("km", "Khmer"),
    ("rw", "Kinyarwanda"),
    ("ko", "Korean"),
    ("ku", "Kurdish"),
    ("ky", "Kyrgyz"),
    ("lo", "Lao"),
    ("lv", "Latvian"),
    ("lt", "Lithuanian"),
    ("mk", "Macedonian"),
    ("ms", "Malay"),
    ("ml", "Malayalam"),
    ("mt", "Maltese"),
    ("mi", "Maori"),
    ("mr", "Marathi"),
    ("mn", "Mongolian"),
    ("ne", "Nepali"),
    ("no", "Norwegian"),
    ("or", "Odia"),
    ("om", "Oromo"),
    ("ps", "Pashto"),
    ("fa", "Persian"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("pa", "Punjabi"),
    ("qu", "Quechua"),
    ("ro", "Romanian"),
    ("rm", "Romansh"),
    ("ru", "Russian"),
    ("sr", "Serbian"),
    ("sd", "Sindhi"),
    ("si", "Sinhala"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("so", "Somali"),
    ("st", "Southern Sotho"),
    ("es", "Spanish"),
    ("sw", "Swahili"),
    ("sv", "Swedish"),
    ("tg", "Tajik"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("th", "Thai"),
    ("tn", "Tswana"),
    ("tr", "Turkish"),
    ("tk", "Turkmen"),
    ("uk", "Ukrainian"),
    ("ur", "Urdu"),
    ("uz", "Uzbek"),
    ("vi", "Vietnamese"),
    ("cy", "Welsh"),
    ("fy", "Western Frisian"),
    ("wo", "Wolof"),
    ("yo", "Yoruba"),
    ("zu", "Zulu"),
)

GEMINI_INPUT_LANGUAGES = dict(_GEMINI_LANGUAGE_PAIRS)
"""Official Gemini Live BCP-47 input-preference codes mapped to display names."""


def _stable_key(name: str) -> str:
    return name.casefold().replace(" ", "_")


GEMINI_LANGUAGES = {
    "tanglish": "Tanglish — Tamil + English",
    "hinglish": "Hinglish — Hindi + English",
    **{_stable_key(name): name for _, name in _GEMINI_LANGUAGE_PAIRS},
}
"""Stable output-language keys, including the application's mixed-language modes."""

GEMINI_LANGUAGE_CODES = {
    "tanglish": "ta",
    "hinglish": "hi",
    **{_stable_key(name): code for code, name in _GEMINI_LANGUAGE_PAIRS},
}
"""Gemini language code associated with each stable output-language key."""
