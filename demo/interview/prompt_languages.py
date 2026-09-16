"""Finite candidate-facing prompt catalog for the interview demo."""

from __future__ import annotations

from .languages import INTERVIEW_LANGUAGES

LANGUAGES = frozenset(INTERVIEW_LANGUAGES)

_INTRO = {
    "english": "Welcome! I’ll ask one question at a time. You can ask me to repeat, explain, wait, skip, or end.",
    "hindi": "नमस्ते! मैं एक बार में एक सवाल पूछूँगा। आप repeat, explain, थोड़ा wait, skip या end कह सकते हैं।",
    "bengali": "নমস্কার! আমি একবারে একটি প্রশ্ন করব। চাইলে repeat, explain, একটু wait, skip বা end বলতে পারেন।",
    "kannada": "ನಮಸ್ಕಾರ! ನಾನು ಒಂದೊಂದೇ ಪ್ರಶ್ನೆ ಕೇಳುತ್ತೇನೆ. ಬೇಕಾದರೆ repeat, explain, ಸ್ವಲ್ಪ wait, skip ಅಥವಾ end ಎಂದು ಹೇಳಬಹುದು.",
    "malayalam": "നമസ്കാരം! ഞാൻ ഓരോ ചോദ്യമായി ചോദിക്കും. വേണമെങ്കിൽ repeat, explain, കുറച്ച് wait, skip അല്ലെങ്കിൽ end എന്ന് പറയാം.",
    "marathi": "नमस्कार! मी एकावेळी एक प्रश्न विचारेन. हवे असल्यास repeat, explain, थोडा wait, skip किंवा end म्हणा.",
    "odia": "ନମସ୍କାର! ମୁଁ ଗୋଟିଏ କରି ପ୍ରଶ୍ନ ପଚାରିବି। ଚାହିଲେ repeat, explain, ଟିକେ wait, skip କିମ୍ବା end କହିପାରିବେ।",
    "punjabi": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਮੈਂ ਇੱਕ ਵਾਰ ਵਿੱਚ ਇੱਕ ਸਵਾਲ ਪੁੱਛਾਂਗਾ। ਤੁਸੀਂ repeat, explain, ਥੋੜ੍ਹਾ wait, skip ਜਾਂ end ਕਹਿ ਸਕਦੇ ਹੋ।",
    "tamil": "வணக்கம்! நான் ஒரு கேள்வியாகக் கேட்கிறேன். வேண்டுமென்றால் repeat, explain, கொஞ்சம் wait, skip அல்லது end கேட்கலாம்.",
    "telugu": "నమస్కారం! నేను ఒక్కో ప్రశ్న అడుగుతాను. కావాలంటే repeat, explain, కొంచెం wait, skip లేదా end చెప్పండి.",
    "gujarati": "નમસ્તે! હું એક સમયે એક સવાલ પૂછીશ. તમે repeat, explain, થોડું wait, skip અથવા end કહી શકો છો.",
    "tanglish": "வணக்கம்! நாம ஒரு கேள்வியா பேசலாம். வேண்டும்னா repeat, explain, கொஞ்சம் wait, skip அல்லது end சொல்லுங்க.",
    "hinglish": "नमस्ते! मैं एक-एक question पूछूँगा। आप repeat, explain, थोड़ा wait, skip या end बोल सकते हैं।",
}

_QUESTIONS = {
    "Teamwork": {
        "english": "Have you helped someone finish a task? Tell me about it.",
        "hindi": "क्या आपने किसी को task पूरा करने में help की है? बताइए।",
        "bengali": "কাউকে কোনও কাজ শেষ করতে সাহায্য করেছিলেন? বলুন।",
        "kannada": "ಯಾರಿಗಾದರೂ ಕೆಲಸ ಮುಗಿಸಲು ಸಹಾಯ ಮಾಡಿದ್ದೀರಾ? ಹೇಳಿ.",
        "malayalam": "ആരെയെങ്കിലും ഒരു ജോലി തീർക്കാൻ സഹായിച്ചിട്ടുണ്ടോ? പറയൂ.",
        "marathi": "तुम्ही कोणाला task पूर्ण करायला मदत केली आहे का? सांगा.",
        "odia": "କାହାକୁ କାମ ଶେଷ କରିବାରେ ସାହାଯ୍ୟ କରିଥିଲେ କି? କୁହନ୍ତୁ।",
        "punjabi": "ਕੀ ਤੁਸੀਂ ਕਿਸੇ ਦੀ task ਪੂਰੀ ਕਰਨ ਵਿੱਚ ਮਦਦ ਕੀਤੀ ਹੈ? ਦੱਸੋ।",
        "tamil": "யாருக்காவது ஒரு task முடிக்க help செய்திருக்கீங்களா? சொல்லுங்க.",
        "telugu": "ఎవరైనా task పూర్తి చేయడానికి help చేశారా? చెప్పండి.",
        "gujarati": "તમે કોઈને task પૂરું કરવામાં help કરી છે? કહો.",
        "tanglish": "நீங்க யாருக்காவது ஒரு task முடிக்க உதவி செஞ்சிருக்கீங்களா? அதைப் பத்தி சொல்லுங்க.",
        "hinglish": "क्या आपने किसी का task पूरा करने में help की है? बताइए.",
    },
    "Communication": {
        "english": "Have you made a difficult idea clear to someone? Tell me how.",
        "hindi": "क्या आपने कोई मुश्किल idea किसी को समझाया है? कैसे?",
        "bengali": "কঠিন কোনও idea কাউকে সহজ করে বুঝিয়েছেন? কীভাবে?",
        "kannada": "ಕಷ್ಟವಾದ idea ಅನ್ನು ಯಾರಿಗಾದರೂ ಸ್ಪಷ್ಟವಾಗಿ ಹೇಳಿದ್ದೀರಾ? ಹೇಗೆ?",
        "malayalam": "ബുദ്ധിമുട്ടുള്ള ഒരു idea ആരെയെങ്കിലും വ്യക്തമായി പറഞ്ഞിട്ടുണ്ടോ? എങ്ങനെ?",
        "marathi": "तुम्ही एखादी कठीण idea कोणाला समजावली आहे का? कशी?",
        "odia": "କଠିନ idea କାହାକୁ ସହଜରେ ବୁଝାଇଥିଲେ କି? କେମିତି?",
        "punjabi": "ਕੀ ਤੁਸੀਂ ਕੋਈ ਔਖੀ idea ਕਿਸੇ ਨੂੰ ਸਮਝਾਈ ਹੈ? ਕਿਵੇਂ?",
        "tamil": "ஒரு கஷ்டமான idea-வை யாருக்காவது clear-ஆ explain செய்திருக்கீங்களா? எப்படி?",
        "telugu": "కష్టమైన idea ఎవరికైనా clear గా explain చేశారా? ఎలా?",
        "gujarati": "તમે કોઈ મુશ્કેલ idea કોઈને સમજાવી છે? કેવી રીતે?",
        "tanglish": "ஒரு கஷ்டமான idea-வை யாருக்காவது clear-ஆ explain செய்திருக்கீங்களா? எப்படி?",
        "hinglish": "क्या आपने कोई difficult idea किसी को clearly समझाया है? कैसे?",
    },
    "Conflict resolution": {
        "english": "Tell me about a disagreement you handled.",
        "hindi": "किसी disagreement को आपने कैसे handle किया?",
        "bengali": "কোনও মতভেদ আপনি কীভাবে সামলেছিলেন?",
        "kannada": "ಒಂದು ಭಿನ್ನಾಭಿಪ್ರಾಯವನ್ನು ಹೇಗೆ ನಿಭಾಯಿಸಿದ್ದೀರಿ?",
        "malayalam": "ഒരു അഭിപ്രായവ്യത്യാസം എങ്ങനെ കൈകാര്യം ചെയ്തു?",
        "marathi": "एखादा मतभेद तुम्ही कसा handle केला?",
        "odia": "ଗୋଟିଏ ମତଭେଦକୁ କିପରି ସମ୍ଭାଳିଥିଲେ?",
        "punjabi": "ਤੁਸੀਂ ਕੋਈ disagreement ਕਿਵੇਂ handle ਕੀਤਾ?",
        "tamil": "ஒரு disagreement-ஐ எப்படி handle செய்தீங்க?",
        "telugu": "ఒక disagreement ను ఎలా handle చేశారు?",
        "gujarati": "તમે કોઈ disagreement કેવી રીતે handle કર્યો?",
        "tanglish": "ஒரு disagreement வந்தப்போ எப்படி handle செஞ்சீங்க?",
        "hinglish": "कोई disagreement आया तो आपने कैसे handle किया?",
    },
    "Ownership": {
        "english": "Tell me about a time you took responsibility for a mistake.",
        "hindi": "किसी mistake की responsibility आपने कब ली?",
        "bengali": "কোনও ভুলের দায়িত্ব আপনি কবে নিয়েছিলেন?",
        "kannada": "ಒಂದು ತಪ್ಪಿನ ಜವಾಬ್ದಾರಿ ನೀವು ಯಾವಾಗ ತೆಗೆದುಕೊಂಡಿರಿ?",
        "malayalam": "ഒരു തെറ്റിന്റെ ഉത്തരവാദിത്വം നിങ്ങൾ എടുത്ത സമയം പറയൂ.",
        "marathi": "एखाद्या चुकीची responsibility तुम्ही कधी घेतली?",
        "odia": "ଗୋଟିଏ ଭୁଲର ଦାୟିତ୍ୱ କେବେ ନେଇଥିଲେ?",
        "punjabi": "ਕਿਸੇ mistake ਦੀ responsibility ਤੁਸੀਂ ਕਦੋਂ ਲਈ?",
        "tamil": "ஒரு mistake-க்கு responsibility எடுத்த நேரத்தைப் பற்றி சொல்லுங்க.",
        "telugu": "ఒక mistake కి responsibility తీసుకున్న సందర్భం చెప్పండి.",
        "gujarati": "કોઈ mistake ની responsibility તમે ક્યારે લીધી?",
        "tanglish": "ஒரு mistake நடந்தப்போ responsibility எடுத்த time-ஐ சொல்லுங்க.",
        "hinglish": "किसी mistake की responsibility आपने कब ली?",
    },
    "Resilience": {
        "english": "Tell me about a setback and what you learned.",
        "hindi": "किसी setback के बाद आपने क्या सीखा?",
        "bengali": "কোনও setback-এর পর কী শিখেছিলেন?",
        "kannada": "ಒಂದು setback ನಂತರ ಏನು ಕಲಿತಿರಿ?",
        "malayalam": "ഒരു setback കഴിഞ്ഞ് എന്താണ് പഠിച്ചത്?",
        "marathi": "एखाद्या setback नंतर तुम्ही काय शिकलात?",
        "odia": "ଗୋଟିଏ setback ପରେ କଣ ଶିଖିଥିଲେ?",
        "punjabi": "ਕਿਸੇ setback ਤੋਂ ਬਾਅਦ ਤੁਸੀਂ ਕੀ ਸਿੱਖਿਆ?",
        "tamil": "ஒரு setback-க்கு அப்புறம் என்ன கற்றுக்கிட்டீங்க?",
        "telugu": "ఒక setback తర్వాత ఏం నేర్చుకున్నారు?",
        "gujarati": "કોઈ setback પછી તમે શું શીખ્યા?",
        "tanglish": "ஒரு setback வந்ததுக்கப்புறம் என்ன கத்துக்கிட்டீங்க?",
        "hinglish": "किसी setback के बाद आपने क्या सीखा?",
    },
}

_GENERIC = {
    "english": "Tell me about a real situation related to {competency}.",
    "hindi": "{competency} से जुड़ी कोई real situation बताइए।",
    "bengali": "{competency}-এর সঙ্গে যুক্ত একটি বাস্তব ঘটনা বলুন।",
    "kannada": "{competency} ಗೆ ಸಂಬಂಧಿಸಿದ ನಿಜವಾದ ಘಟನೆ ಹೇಳಿ.",
    "malayalam": "{competency} യുമായി ബന്ധപ്പെട്ട ഒരു യഥാർത്ഥ സംഭവം പറയൂ.",
    "marathi": "{competency} शी संबंधित एक real situation सांगा.",
    "odia": "{competency} ସହିତ ଜଡ଼ିତ ଏକ ବାସ୍ତବ ଘଟଣା କୁହନ୍ତୁ।",
    "punjabi": "{competency} ਨਾਲ ਜੁੜੀ real situation ਦੱਸੋ।",
    "tamil": "{competency} சம்பந்தமான ஒரு real situation-ஐ சொல்லுங்க.",
    "telugu": "{competency} కి సంబంధించిన నిజమైన situation చెప్పండి.",
    "gujarati": "{competency} સાથે જોડાયેલી real situation કહો.",
    "tanglish": "{competency} சம்பந்தமான ஒரு real situation-ஐ சொல்லுங்க.",
    "hinglish": "{competency} से जुड़ी कोई real situation बताइए.",
}


def _key(language: str) -> str:
    value = language.casefold().strip()
    return value if value in LANGUAGES else "english"


def introduction(language: str) -> str:
    """Return the localized opening prompt."""
    return _INTRO[_key(language)]


def question(language: str, competency: str) -> str:
    """Return a known competency prompt or localized generic prompt."""
    key = _key(language)
    entry = next(
        (v for k, v in _QUESTIONS.items() if k.casefold() == competency.casefold().strip()), None
    )
    return entry[key] if entry else _GENERIC[key].format(competency=competency)


def closing(language: str) -> str:
    """Return the localized closing prompt."""
    key = _key(language)
    endings = {
        "english": "Thanks for sharing your time and thoughts.",
        "hindi": "अपना time और thoughts share करने के लिए धन्यवाद।",
        "bengali": "আপনার সময় ও কথা ভাগ করে নেওয়ার জন্য ধন্যবাদ।",
        "kannada": "ನಿಮ್ಮ ಸಮಯ ಮತ್ತು ಆಲೋಚನೆಗಳನ್ನು ಹಂಚಿಕೊಂಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು.",
        "malayalam": "നിങ്ങളുടെ സമയവും ചിന്തകളും പങ്കുവെച്ചതിന് നന്ദി.",
        "marathi": "तुमचा वेळ आणि विचार share केल्याबद्दल धन्यवाद.",
        "odia": "ଆପଣଙ୍କ ସମୟ ଓ ଭାବନା ବାଣ୍ଟିଥିବାରୁ ଧନ୍ୟବାଦ।",
        "punjabi": "ਆਪਣਾ ਸਮਾਂ ਅਤੇ ਵਿਚਾਰ ਸਾਂਝੇ ਕਰਨ ਲਈ ਧੰਨਵਾਦ।",
        "tamil": "உங்க நேரமும் எண்ணங்களும் பகிர்ந்ததுக்கு நன்றி.",
        "telugu": "మీ సమయం మరియు ఆలోచనలు పంచుకున్నందుకు ధన్యవాదాలు.",
        "gujarati": "તમારો સમય અને વિચારો share કરવા બદલ આભાર.",
        "tanglish": "உங்க time-ம் thoughts-ம் share செய்ததுக்கு நன்றி.",
        "hinglish": "अपना time और thoughts share करने के लिए धन्यवाद.",
    }
    return endings[key]
