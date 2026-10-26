"""Which language Weave talks in: English, Hindi, Telugu, Kannada or Tamil, switchable at any time.

A language is stored per person as a code: "hi", "te", "kn", "ta" (native script) or "hi-Latn", ... (the same
language in Latin letters, the way many people text: "tum hindi me baat karo"). None means English.
People switch by asking ("speak telugu", "hindi me baat karo", "తెలుగులో మాట్లాడు") or simply by writing in a
script: a message in Kannada script gets Kannada replies."""

from __future__ import annotations

import re

NAMES = {"en": "English", "hi": "Hindi", "te": "Telugu", "kn": "Kannada", "ta": "Tamil"}
# Unicode blocks of the native scripts.
SCRIPTS = {"hi": (0x0900, 0x097F), "te": (0x0C00, 0x0C7F), "kn": (0x0C80, 0x0CFF), "ta": (0x0B80, 0x0BFF)}
# How people name each language, in English, in its own script, and common misspellings.
ALIASES = {
    "en": r"english|eng|angrezi|अंग्रेज़ी|अंग्रेजी|ఇంగ్లీష్|ಇಂಗ್ಲೀಷ್|ಇಂಗ್ಲಿಷ್|ஆங்கிலம்",
    "hi": r"hindi|hindhi|hindee|हिंदी|हिन्दी",
    "te": r"telugu|telgu|thelugu|telegu|తెలుగు|తెలుగులో",
    "kn": r"kannada|kanada|kannad|ಕನ್ನಡ|ಕನ್ನಡದಲ್ಲಿ",
    "ta": r"tamil|tamizh|thamizh|thamil|தமிழ்|தமிழில்",
}
# Words that make a message a request to switch ("speak", "baat karo", "matladu", ...).
SWITCH_WORDS = (r"talk|speak|reply|respond|chat|text|write|answer|switch|change|use|language|lang|in|please|pls|plz|"
                r"can|could|u|you|me|mein|main|mai|mei|baat|bol|bolo|karo|kar|lo|lona|matladu|matladandi|cheppu|"
                r"alli|li|maatadu|matadu|heli|la|pesu|pesungal|sollu|only|now|from|bro|bhai|anna|da|ra|ok|okay|"
                r"script|letters|roman|to|with|from|now|on|tum|aap|ab|se|mujhse|humse|karna|kariye|karoge|nuvvu|meeru|"
                r"naatho|ika|neenu|neevu|nanna|jothe|nee|neenga|ennoda|kitta|start|go|back|again|plz|pls|yaar")
# Romanized words of each language: a request typed like this wants replies typed like this.
ROMAN_HINTS = {
    "hi": r"\b(?:me|mein|mai|mei|baat|bolo|karo|kar|bhai|mujhse|se)\b",
    "te": r"\b(?:lo|matladu|matladandi|cheppu|ra|anna|nuvvu|naatho)\b",
    "kn": r"\b(?:alli|li|maatadu|matadu|heli|nanna|jothe)\b",
    "ta": r"\b(?:la|pesu|pesungal|sollu|da|ennoda|kitta)\b",
}
CONFIRM = {
    "en": "Sure! I'll talk in English from now on.",
    "hi": "ठीक है! अब से मैं हिंदी में बात करूँगा।",
    "hi-Latn": "Theek hai! Ab se main Hindi mein baat karunga.",
    "te": "సరే! ఇకపై నేను తెలుగులో మాట్లాడతాను.",
    "te-Latn": "Sare! Ikapai nenu Telugu lo matladutanu.",
    "kn": "ಸರಿ! ಇನ್ನು ಮುಂದೆ ನಾನು ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡುತ್ತೇನೆ.",
    "kn-Latn": "Sari! Innu munde naanu Kannada dalli maataduttene.",
    "ta": "சரி! இனி நான் தமிழில் பேசுவேன்.",
    "ta-Latn": "Sari! Ini naan Tamil la pesuven.",
}


def _name_of(word: str) -> str | None:
    for code, rx in ALIASES.items():
        if re.fullmatch(rf"(?:{rx})", word, re.I):
            return code
    return None


def script_language(text: str) -> str | None:
    """The language whose script most of the message's letters are in (Hindi, Telugu, Kannada, Tamil), or None."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 2:
        return None
    for code, (lo, hi) in SCRIPTS.items():
        if sum(1 for ch in letters if lo <= ord(ch) <= hi) / len(letters) >= 0.5:
            return code
    return None


def requested_language(text: str) -> str | None:
    """A language code if the message asks Weave to switch language ("speak telugu", "hindi me baat karo",
    "english please", "తెలుగులో మాట్లాడు"); None for anything else, including normal messages that merely mention a
    language ("make a website in hindi for my shop")."""
    t = text.strip().lower()
    words = re.findall(r"[\wऀ-෿']+", t)
    if not words or len(words) > 9:
        return None
    named = [c for c in (_name_of(w) for w in words) if c]
    if len(set(named)) != 1:
        return None
    code = named[0]
    others = [w for w in words if not _name_of(w)]
    # Everything else must be switching words: "can you talk in hindi", not "build a hindi news website".
    if any(not re.fullmatch(rf"(?:{SWITCH_WORDS})", w) and not script_language(w) for w in others):
        return None
    if code == "en":
        return "en"
    if re.search(r"\b(?:roman|english letters|hinglish)\b", t) or (
            not script_language(text) and re.search(ROMAN_HINTS[code], t)):
        return f"{code}-Latn"
    return code


def language_label(code: str | None) -> str:
    """How the language is described to the AI."""
    if not code or code == "en":
        return "English"
    base, _, script = code.partition("-")
    name = NAMES.get(base, "English")
    if script == "Latn":
        return f"{name}, written in English (Latin) letters the way people text it, e.g. Hinglish for Hindi"
    return f"{name}, in {name} script"


def needs_translation(code: str | None) -> bool:
    return bool(code) and code != "en"
