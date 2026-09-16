"""Language choices supported by Sarvam realtime recognition and streamed speech.

Provider lists: https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws
and https://docs.sarvam.ai/api-reference/text-to-speech/convert-stream.
"""

STT_LANGUAGES = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "or-IN": "Odia",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "gu-IN": "Gujarati",
    "as-IN": "Assamese",
    "ur-IN": "Urdu",
    "ne-IN": "Nepali",
    "kok-IN": "Konkani",
    "ks-IN": "Kashmiri",
    "sd-IN": "Sindhi",
    "sa-IN": "Sanskrit",
    "sat-IN": "Santali",
    "mni-IN": "Manipuri",
    "brx-IN": "Bodo",
    "mai-IN": "Maithili",
    "doi-IN": "Dogri",
}

TTS_LANGUAGES = {
    code: STT_LANGUAGES[code]
    for code in (
        "en-IN",
        "hi-IN",
        "bn-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "or-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
        "gu-IN",
    )
}

INTERVIEW_LANGUAGES = {
    "tanglish": "ta-IN",
    "hinglish": "hi-IN",
    "english": "en-IN",
    "hindi": "hi-IN",
    "bengali": "bn-IN",
    "kannada": "kn-IN",
    "malayalam": "ml-IN",
    "marathi": "mr-IN",
    "odia": "or-IN",
    "punjabi": "pa-IN",
    "tamil": "ta-IN",
    "telugu": "te-IN",
    "gujarati": "gu-IN",
}
