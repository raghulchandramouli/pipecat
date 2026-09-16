"""Short, conversational copy used by interview interaction controls."""

from __future__ import annotations

_LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "ta": "ta",
    "tamil": "ta",
    "tanglish": "ta",
    "hi": "hi",
    "hindi": "hi",
    "hinglish": "hi",
    "bengali": "bn",
    "kannada": "kn",
    "malayalam": "ml",
    "marathi": "mr",
    "odia": "or",
    "punjabi": "pa",
    "telugu": "te",
    "gujarati": "gu",
}

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "thinking": "Take your time. I’m listening.",
        "checkin": "I’m still here. Would you like a little more time?",
        "recovery": "I missed that. Please say it again, or ask me to repeat the question.",
        "opening": "Take a moment if you need it. You can ask me to repeat or explain any question.",
        "retry_response": "I couldn’t finish that reply. Please try again.",
        "retry_transcription": "I couldn’t catch your words. Please say that part again.",
        "restart_answer": "Sure, start your answer again whenever you’re ready.",
        "explain": "Here’s another way to look at it: {question}",
        "no_example_exhausted": "That’s okay. Let’s continue with the question as a practice situation.",
        "explanation_unavailable": "I can’t explain that further right now. You can ask me to repeat it.",
        "recovery_exhausted": "I still couldn’t catch that. Please repeat the question or restart your answer.",
        "connection_lost": "The connection was lost. Please start a new interview.",
        "ack_unknown": "I didn’t recognize that action. You can ask me to repeat, explain, wait, or skip.",
    },
    "ta": {
        "thinking": "பரவாயில்லை, நேரம் எடுத்துக்கொள்ளுங்கள். நான் கேட்கிறேன்.",
        "checkin": "நான் இங்கே இருக்கிறேன். இன்னும் கொஞ்சம் time வேண்டுமா?",
        "recovery": "அது சரியாக கேட்கவில்லை. இன்னொரு முறை சொல்லுங்கள்; வேண்டுமென்றால் question-ஐ repeat செய்கிறேன்.",
        "opening": "தேவைப்பட்டால் கொஞ்சம் time எடுத்துக்கொள்ளுங்கள். எந்த question-ஐயும் repeat அல்லது explain கேட்கலாம்.",
        "retry_response": "என் reply முழுமையாக வரவில்லை. இன்னொரு முறை try செய்யலாம்.",
        "retry_transcription": "உங்கள் வார்த்தைகள் தெளிவாக கேட்கவில்லை. அந்த பகுதியை மீண்டும் சொல்லுங்கள்.",
        "restart_answer": "சரி, தயாரானதும் answer-ஐ மீண்டும் தொடங்குங்கள்.",
        "explain": "இதை இன்னொரு விதமாகப் பார்ப்போம்: {question}",
        "no_example_exhausted": "பரவாயில்லை. இதை ஒரு practice situation-ஆக வைத்து தொடரலாம்.",
        "explanation_unavailable": "இதை இப்போது மேலும் explain செய்ய முடியவில்லை. Repeat கேட்கலாம்.",
        "recovery_exhausted": "இன்னும் தெளிவாக கேட்கவில்லை. Question-ஐ repeat செய்யுங்கள் அல்லது answer-ஐ restart செய்யுங்கள்.",
        "connection_lost": "Connection துண்டிக்கப்பட்டது. புதிய interview தொடங்குங்கள்.",
        "ack_unknown": "அந்த action புரியவில்லை. Repeat, explain, wait அல்லது skip கேட்கலாம்.",
    },
    "hi": {
        "thinking": "कोई बात नहीं, आराम से सोचिए। मैं सुन रहा हूँ।",
        "checkin": "मैं यहीं हूँ। क्या आपको थोड़ा और time चाहिए?",
        "recovery": "वह ठीक से सुनाई नहीं दिया। फिर से बोलिए; चाहें तो question repeat कर दूँ।",
        "opening": "ज़रूरत हो तो थोड़ा time लीजिए। किसी भी question को repeat या explain करने को कह सकते हैं।",
        "retry_response": "मेरा reply पूरा नहीं हुआ। एक बार फिर try करते हैं।",
        "retry_transcription": "आपकी बात साफ़ सुनाई नहीं दी। वह हिस्सा फिर से बोलिए।",
        "restart_answer": "ठीक है, ready हों तो अपना answer फिर से शुरू कीजिए।",
        "explain": "इसे ऐसे भी समझ सकते हैं: {question}",
        "no_example_exhausted": "कोई बात नहीं। इसे एक practice situation मानकर आगे बढ़ते हैं।",
        "explanation_unavailable": "मैं अभी इसे और explain नहीं कर पा रहा। आप repeat करने को कह सकते हैं।",
        "recovery_exhausted": "फिर भी साफ़ सुनाई नहीं दिया। Question repeat या answer restart कीजिए।",
        "connection_lost": "Connection टूट गया। कृपया नया interview शुरू कीजिए।",
        "ack_unknown": "मैंने वह action नहीं समझा। Repeat, explain, wait या skip कह सकते हैं।",
    },
}

# Keep every provider language on spoken copy, including recovery branches.
_EXTRA_COPY = {
    "bn": {
        "thinking": "সময় নিন, আমি শুনছি।",
        "checkin": "আমি এখানেই আছি, আরও একটু সময় লাগবে?",
        "recovery": "ঠিক শুনতে পাইনি, আবার বলুন।",
        "opening": "চাইলে repeat বা explain বলতে পারেন।",
        "retry_response": "উত্তরটি শেষ হয়নি, আবার চেষ্টা করুন।",
        "retry_transcription": "কথাটি পরিষ্কার শুনিনি, আবার বলুন।",
        "restart_answer": "ঠিক আছে, প্রস্তুত হলে উত্তরটি আবার শুরু করুন।",
        "explain": "অন্যভাবে বলি: {question}",
    },
    "kn": {
        "thinking": "ಸಮಯ ತೆಗೆದುಕೊಳ್ಳಿ, ನಾನು ಕೇಳುತ್ತಿದ್ದೇನೆ.",
        "checkin": "ನಾನು ಇಲ್ಲೇ ಇದ್ದೇನೆ, ಇನ್ನೂ ಸ್ವಲ್ಪ ಸಮಯ ಬೇಕಾ?",
        "recovery": "ಸರಿಯಾಗಿ ಕೇಳಿಸಲಿಲ್ಲ, ಮತ್ತೆ ಹೇಳಿ.",
        "opening": "ಬೇಕಾದರೆ repeat ಅಥವಾ explain ಎಂದು ಹೇಳಿ.",
        "retry_response": "ಉತ್ತರ ಪೂರ್ಣವಾಗಲಿಲ್ಲ, ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
        "retry_transcription": "ಮಾತು ಸ್ಪಷ್ಟವಾಗಿ ಕೇಳಲಿಲ್ಲ, ಮತ್ತೆ ಹೇಳಿ.",
        "restart_answer": "ಸರಿ, ಸಿದ್ಧರಾದಾಗ ಉತ್ತರ ಮತ್ತೆ ಪ್ರಾರಂಭಿಸಿ.",
        "explain": "ಇನ್ನೊಂದು ರೀತಿಯಲ್ಲಿ ಹೇಳುವುದಾದರೆ: {question}",
    },
    "ml": {
        "thinking": "സമയം എടുക്കൂ, ഞാൻ കേൾക്കുന്നുണ്ട്.",
        "checkin": "ഞാൻ ഇവിടെയുണ്ട്, കുറച്ച് കൂടി സമയം വേണോ?",
        "recovery": "വ്യക്തമായി കേട്ടില്ല, വീണ്ടും പറയൂ.",
        "opening": "വേണമെങ്കിൽ repeat അല്ലെങ്കിൽ explain എന്ന് പറയാം.",
        "retry_response": "ഉത്തരം പൂർണ്ണമായില്ല, വീണ്ടും ശ്രമിക്കൂ.",
        "retry_transcription": "സംസാരം വ്യക്തമായി കേട്ടില്ല, വീണ്ടും പറയൂ.",
        "restart_answer": "ശരി, തയ്യാറാകുമ്പോൾ ഉത്തരം വീണ്ടും തുടങ്ങൂ.",
        "explain": "മറ്റൊരു രീതിയിൽ പറയാം: {question}",
    },
    "mr": {
        "thinking": "वेळ घ्या, मी ऐकतोय.",
        "checkin": "मी इथेच आहे, अजून थोडा वेळ हवा का?",
        "recovery": "मला नीट ऐकू आले नाही, पुन्हा सांगा.",
        "opening": "हवे असल्यास repeat किंवा explain म्हणा.",
        "retry_response": "उत्तर पूर्ण झाले नाही, पुन्हा प्रयत्न करा.",
        "retry_transcription": "तुमचे बोलणे स्पष्ट ऐकू आले नाही, पुन्हा सांगा.",
        "restart_answer": "ठीक आहे, तयार झाल्यावर उत्तर पुन्हा सुरू करा.",
        "explain": "हे दुसऱ्या प्रकारे सांगतो: {question}",
    },
    "or": {
        "thinking": "ସମୟ ନିଅନ୍ତୁ, ମୁଁ ଶୁଣୁଛି।",
        "checkin": "ମୁଁ ଏଠାରେ ଅଛି, ଆଉ ଟିକେ ସମୟ ଦରକାର କି?",
        "recovery": "ଠିକ୍ ଭାବେ ଶୁଣିପାରିଲି ନାହିଁ, ପୁଣି କୁହନ୍ତୁ।",
        "opening": "ଚାହିଲେ repeat କିମ୍ବା explain କହନ୍ତୁ।",
        "retry_response": "ଉତ୍ତର ସରିଲା ନାହିଁ, ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।",
        "retry_transcription": "କଥା ସ୍ପଷ୍ଟ ଶୁଣିଲି ନାହିଁ, ପୁଣି କୁହନ୍ତୁ।",
        "restart_answer": "ଠିକ୍ ଅଛି, ପ୍ରସ୍ତୁତ ହେଲେ ଉତ୍ତର ପୁଣି ଆରମ୍ଭ କରନ୍ତୁ।",
        "explain": "ଅନ୍ୟ ଭାବେ କହିଲେ: {question}",
    },
    "pa": {
        "thinking": "ਆਰਾਮ ਨਾਲ ਸਮਾਂ ਲਓ, ਮੈਂ ਸੁਣ ਰਿਹਾ ਹਾਂ।",
        "checkin": "ਮੈਂ ਇੱਥੇ ਹੀ ਹਾਂ, ਹੋਰ ਥੋੜ੍ਹਾ ਸਮਾਂ ਚਾਹੀਦਾ?",
        "recovery": "ਠੀਕ ਸੁਣਿਆ ਨਹੀਂ, ਫਿਰ ਦੱਸੋ।",
        "opening": "ਚਾਹੋ ਤਾਂ repeat ਜਾਂ explain ਕਹੋ।",
        "retry_response": "ਜਵਾਬ ਪੂਰਾ ਨਹੀਂ ਹੋਇਆ, ਫਿਰ ਕੋਸ਼ਿਸ਼ ਕਰੋ।",
        "retry_transcription": "ਗੱਲ ਸਾਫ਼ ਨਹੀਂ ਸੁਣੀ, ਫਿਰ ਦੱਸੋ।",
        "restart_answer": "ਠੀਕ ਹੈ, ਤਿਆਰ ਹੋ ਕੇ ਜਵਾਬ ਫਿਰ ਸ਼ੁਰੂ ਕਰੋ।",
        "explain": "ਇਸ ਨੂੰ ਹੋਰ ਤਰੀਕੇ ਨਾਲ ਕਹੀਏ: {question}",
    },
    "te": {
        "thinking": "సమయం తీసుకోండి, నేను వింటున్నాను.",
        "checkin": "నేను ఇక్కడే ఉన్నాను, ఇంకొంచెం సమయం కావాలా?",
        "recovery": "సరిగ్గా వినిపించలేదు, మళ్లీ చెప్పండి.",
        "opening": "కావాలంటే repeat లేదా explain అని చెప్పండి.",
        "retry_response": "జవాబు పూర్తికాలేదు, మళ్లీ ప్రయత్నించండి.",
        "retry_transcription": "మీ మాట స్పష్టంగా వినిపించలేదు, మళ్లీ చెప్పండి.",
        "restart_answer": "సరే, సిద్ధమైనప్పుడు జవాబు మళ్లీ మొదలుపెట్టండి.",
        "explain": "ఇంకో విధంగా చెప్పాలంటే: {question}",
    },
    "gu": {
        "thinking": "સમય લો, હું સાંભળું છું.",
        "checkin": "હું અહીં જ છું, થોડો વધુ સમય જોઈએ?",
        "recovery": "બરાબર સંભળાયું નહીં, ફરી કહો.",
        "opening": "ઈચ્છો તો repeat અથવા explain કહો.",
        "retry_response": "જવાબ પૂરો થયો નથી, ફરી પ્રયાસ કરો.",
        "retry_transcription": "તમારી વાત સ્પષ્ટ સંભળાઈ નહીં, ફરી કહો.",
        "restart_answer": "બરાબર, તૈયાર હો ત્યારે જવાબ ફરી શરૂ કરો.",
        "explain": "આને બીજી રીતે કહીએ: {question}",
    },
}
for _code, _values in _EXTRA_COPY.items():
    _COPY[_code] = _values

_COMPETENCIES = {
    "Teamwork": {
        "en": "Think of a time you worked with someone toward a shared goal.",
        "ta": "ஒருவருடன் சேர்ந்து ஒரு common goal அடைய முயன்ற ஒரு situation நினைத்துப் பாருங்கள்.",
        "hi": "ऐसी situation सोचिए जहाँ आपने किसी के साथ मिलकर एक common goal पूरा किया हो।",
    },
    "Communication": {
        "en": "Think of a time you made a difficult idea clear to someone.",
        "ta": "ஒரு கஷ்டமான idea-வை இன்னொருவருக்கு clear-ஆ explain செய்த ஒரு situation நினைத்துப் பாருங்கள்.",
        "hi": "ऐसी situation सोचिए जहाँ आपने कोई मुश्किल idea किसी को clearly समझाया हो।",
    },
    "Conflict resolution": {
        "en": "Think of a disagreement you handled and how you reached a way forward.",
        "ta": "ஒரு disagreement-ஐ handle செய்து, இருவரும் முன்னேற ஒரு வழி கண்ட situation நினைத்துப் பாருங்கள்.",
        "hi": "ऐसी disagreement सोचिए जिसे आपने handle करके आगे बढ़ने का रास्ता निकाला हो।",
    },
    "Ownership": {
        "en": "Think of a time you took responsibility when something went wrong.",
        "ta": "ஏதாவது தவறாக நடந்தபோது responsibility எடுத்த ஒரு situation நினைத்துப் பாருங்கள்.",
        "hi": "ऐसी situation सोचिए जहाँ कुछ गलत होने पर आपने responsibility ली हो।",
    },
    "Resilience": {
        "en": "Think of a setback, how you responded, and what you learned.",
        "ta": "ஒரு setback-க்கு எப்படி respond செய்தீர்கள், என்ன கற்றுக்கொண்டீர்கள் என்று நினைத்துப் பாருங்கள்.",
        "hi": "किसी setback पर आपने कैसे respond किया और क्या सीखा, ऐसी situation सोचिए।",
    },
}

_SCENARIOS = {
    "Teamwork": {
        "en": "Imagine a friend needs help organising a small event. What would you do together?",
        "ta": "ஒரு friend ஒரு small event organise செய்ய help கேட்கிறார் என்று imagine செய்யுங்கள். சேர்ந்து என்ன செய்வீர்கள்?",
        "hi": "Imagine कीजिए कि एक friend को छोटा event organise करने में help चाहिए। आप साथ में क्या करेंगे?",
    },
    "Communication": {
        "en": "Imagine a classmate is confused by your instructions. How would you make them clear?",
        "ta": "உங்கள் instructions ஒரு classmate-க்கு புரியவில்லை என்று imagine செய்யுங்கள். அதை எப்படி clear செய்வீர்கள்?",
        "hi": "Imagine कीजिए कि classmate आपकी instructions से confused है। आप उन्हें कैसे clear करेंगे?",
    },
    "Conflict resolution": {
        "en": "Imagine a housemate disagrees about a household task. How would you find a fair way forward?",
        "ta": "வீட்டு வேலையைப் பற்றி ஒரு housemate-உடன் disagreement என்று imagine செய்யுங்கள். fair-ஆக எப்படி முன்னேறுவீர்கள்?",
        "hi": "Imagine कीजिए कि household task पर housemate से disagreement है। fair रास्ता कैसे निकालेंगे?",
    },
    "Ownership": {
        "en": "Imagine you accidentally damage a friend’s item. What would you do next?",
        "ta": "தவறுதலாக ஒரு friend-ன் பொருளை damage செய்ததாக imagine செய்யுங்கள். அடுத்து என்ன செய்வீர்கள்?",
        "hi": "Imagine कीजिए कि आपसे friend की चीज़ accidentally damage हो गई। आगे क्या करेंगे?",
    },
    "Resilience": {
        "en": "Imagine your plan for a weekend activity gets cancelled. How would you respond and adapt?",
        "ta": "Weekend activity plan cancel ஆகிவிட்டது என்று imagine செய்யுங்கள். எப்படி respond செய்து adapt செய்வீர்கள்?",
        "hi": "Imagine कीजिए कि weekend activity का plan cancel हो गया। आप कैसे respond और adapt करेंगे?",
    },
}


def _language(language: str) -> str:
    value = language.casefold().strip()
    return _LANGUAGE_ALIASES.get(value, value if value in _COPY else "en")


def get_copy(language: str, key: str, **kwargs: object) -> str:
    """Return localized interaction copy, falling back to English."""
    catalog = _COPY[_language(language)]
    if key not in _COPY["en"]:
        raise KeyError(f"unknown interaction copy key: {key}")
    return catalog.get(key, _COPY["en"][key]).format(**kwargs)


def get_explanation(language: str, competency: str) -> str | None:
    """Return one localized no-example explanation, if competency is known."""
    entry = next(
        (v for k, v in _COMPETENCIES.items() if k.casefold() == competency.casefold().strip()), None
    )
    return None if entry is None else entry[_language(language)]


def get_scenario(language: str, competency: str) -> str | None:
    """Return one localized practice scenario question, if competency is known."""
    entry = next(
        (v for k, v in _SCENARIOS.items() if k.casefold() == competency.casefold().strip()), None
    )
    return None if entry is None else entry[_language(language)]
