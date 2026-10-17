"""Cheap, key-free signals used by the heuristic scorer (and as hints for the LLM)."""

import re
import wave
from pathlib import Path

import numpy as np

HOOK_WORDS = {
    "insane", "crazy", "craziest", "never", "ever", "worst", "best", "secret", "million", "millions",
    "billion", "thousand", "dollars", "money", "free", "died", "dead", "kill", "fired", "quit", "banned",
    "lied", "truth", "honestly", "actually", "wait", "what", "why", "how", "nobody", "everyone",
    "biggest", "first", "last", "only", "impossible", "scared", "terrified", "shocked", "hate", "love",
    "broke", "rich", "famous", "crying", "cried", "punch", "fight", "illegal", "arrested", "caught",
}
LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|laugh\w*)\b", re.I)
NUMBERY = re.compile(r"(\$|\d|percent|hundred|thousand|million|billion)", re.I)
AD_READ = re.compile(
    r"\b(promo code|use code|code \w+ for|sponsor\w*|brought to you|download the app|sign up (at|for|today)|"
    r"link in (the )?(bio|description)|first deposit|prize ?picks|draftkings|fanduel|betterhelp|athletic greens|"
    r"\bag1\b|manscaped|squarespace|hellofresh|raid shadow|\.com/\w+|percent off|free trial)",
    re.I,
)
FILLERS = {"um", "uh", "umm", "uhh", "erm", "hmm", "mm", "mhm", "ah"}
LEADING_FILLER = re.compile(r"^(?:(?:um+|uh+|erm|hmm|so|like|and|but|well|you know|i mean|yeah|okay|oh)[,.]?\s+)+", re.I)
SWEAR = re.compile(r"\b(fuck\w*|shit\w*|damn|hell|bitch\w*|ass)\b", re.I)


def loudness_curve(wav_path: Path, hop: float = 0.5) -> tuple[np.ndarray, float]:
    """RMS loudness every `hop` seconds of a 16 kHz mono WAV → (values, hop)."""
    with wave.open(str(wav_path)) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    size = int(rate * hop)
    n = len(audio) // size
    if n == 0:
        return np.zeros(1), hop
    frames = audio[: n * size].reshape(n, size)
    return np.sqrt((frames**2).mean(axis=1)) + 1e-6, hop


def text_features(text: str) -> dict:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    n = max(len(words), 1)
    return {
        "hook_words": sum(w in HOOK_WORDS for w in words) / n,
        "laughs": len(LAUGH.findall(text)),
        "numbers": len(NUMBERY.findall(text)),
        "swears": len(SWEAR.findall(text)),
        "questions": text.count("?"),
        "exclaims": text.count("!"),
        "ad_read": len(AD_READ.findall(text)),
        "you": sum(w in ("you", "your", "you're") for w in words) / n,
    }


def hook_strength(first_sentence: str) -> float:
    """0..1: does the opening line grab attention on its own?"""
    f = text_features(first_sentence)
    s = 0.0
    s += min(f["hook_words"] * 4, 0.4)
    s += 0.2 if f["numbers"] else 0
    s += 0.2 if f["questions"] else 0
    s += 0.1 if f["swears"] else 0
    s += 0.1 if len(first_sentence.split()) <= 12 else 0  # short, punchy openers win
    return min(s, 1.0)


def starts_sentence(text: str, previous_text: str | None) -> bool:
    """True if this line looks like the start of a sentence (not a mid-sentence fragment)."""
    t = text.lstrip(" \"'-")
    first_ok = bool(t) and (t[0].isupper() or t[0].isdigit())
    prev_ok = previous_text is None or previous_text.rstrip().endswith((".", "?", "!", '"'))
    return first_ok and prev_ok


def is_filler(word: str) -> bool:
    return word.lower().strip(",.?!") in FILLERS


def clean_line(text: str) -> str:
    """Drop ums/uhs and weak leading words ("So, um, like…") so a line can stand as a hook."""
    words = [w for w in text.split() if not is_filler(w)]
    out = LEADING_FILLER.sub("", " ".join(words)).strip()
    return out[:1].upper() + out[1:] if out else out
