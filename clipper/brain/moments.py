"""Find the best clip-worthy moments in a transcript.

Two scorers with the same output:
  • LLM scorer (when an API key is set) — reads the transcript like an editor would.
  • Heuristic scorer (free, no key) — loudness + speech energy + hook words.
Both return moments that start on a sentence boundary and respect CLIP_MIN_SEC..CLIP_MAX_SEC.
"""

import logging
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from .. import llm
from ..config import CLIP_MAX_SEC, CLIP_MIN_SEC, MOMENTS_PER_SOURCE
from .signals import clean_line, hook_strength, loudness_curve, starts_sentence, text_features

log = logging.getLogger(__name__)

MOMENT_TYPES = "funny | story | hot_take | reveal | rage | wholesome | drama | advice | money | fail | clutch"


class Moment(BaseModel):
    start: float = Field(description="seconds, absolute, must equal a line start time")
    end: float = Field(description="seconds, absolute, must equal a line end time")
    score: int = Field(ge=0, le=100, description="predicted virality 0-100")
    moment_type: str = Field(description=MOMENT_TYPES)
    hook_line: str = Field(description="the exact opening line that makes people stop scrolling")
    reason: str = Field(description="one short sentence: why this will get views")


class MomentList(BaseModel):
    moments: list[Moment]


# ---------- shared helpers ----------

def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def snap(moment: dict, transcript: dict) -> dict | None:
    """Snap to real word boundaries, trim dead air, enforce length. None if unusable."""
    words = transcript["words"]
    inside = [w for w in words if w["s"] >= moment["start"] - 0.5 and w["e"] <= moment["end"] + 0.5]
    if not inside:
        return None
    start = max(inside[0]["s"] - 0.15, 0.0)  # no dead air before the first word
    end = inside[-1]["e"] + 0.35  # let the last word ring out
    if end - start > CLIP_MAX_SEC:  # too long: cut at the last word that fits
        fitting = [w for w in inside if w["e"] - start <= CLIP_MAX_SEC - 0.35]
        end = fitting[-1]["e"] + 0.35
    if end - start < CLIP_MIN_SEC:
        return None
    return {**moment, "start": round(start, 2), "end": round(end, 2)}


def dedupe(moments: list[dict], limit: int) -> list[dict]:
    """Highest score first; drop anything overlapping an already-kept moment by >30%."""
    kept: list[dict] = []
    for m in sorted(moments, key=lambda m: m["score"], reverse=True):
        overlaps = any(
            min(m["end"], k["end"]) - max(m["start"], k["start"]) > 0.3 * (m["end"] - m["start"]) for k in kept
        )
        if not overlaps:
            kept.append(m)
        if len(kept) >= limit:
            break
    return kept


# ---------- heuristic scorer ----------

def _windows(segments: list[dict]) -> list[tuple[int, int]]:
    """Every sentence start → extend forward by whole sentences to ~12-28s."""
    out = []
    for i, seg in enumerate(segments):
        j = i
        while j + 1 < len(segments) and segments[j + 1]["end"] - seg["start"] <= CLIP_MAX_SEC:
            j += 1
            if segments[j]["end"] - seg["start"] >= 28:  # ~20-30s after jump cuts
                break
        if segments[j]["end"] - seg["start"] >= CLIP_MIN_SEC:
            out.append((i, j))
    return out


def heuristic(transcript: dict, wav_path: Path | None, offset: float = 0.0, limit: int = MOMENTS_PER_SOURCE) -> list[dict]:
    segs = transcript["segments"]
    wins = _windows(segs)
    if not wins:
        return []
    loud, hop = loudness_curve(wav_path) if wav_path and Path(wav_path).exists() else (None, 0.5)
    loud_med = float(np.median(loud)) if loud is not None else 1.0
    word_times = np.array([w["s"] for w in transcript["words"]]) if transcript["words"] else np.zeros(0)
    total_dur = max(segs[-1]["end"] - segs[0]["start"], 1)
    base_rate = len(word_times) / total_dur

    raw = []
    for i, j in wins:
        if not starts_sentence(segs[i]["text"], segs[i - 1]["text"] if i else None):
            continue  # mid-sentence openers confuse viewers
        start, end = segs[i]["start"], segs[j]["end"]
        text = " ".join(s["text"] for s in segs[i : j + 1])
        f = text_features(text)
        if f["ad_read"]:
            continue  # sponsor reads never go viral and can break campaign rules
        rate = ((word_times >= start) & (word_times <= end)).sum() / (end - start)
        energy = 0.0
        if loud is not None:
            a, b = int((start - offset) / hop), max(int((end - offset) / hop), int((start - offset) / hop) + 1)
            chunk = loud[a:b]
            if len(chunk):
                energy = float(np.percentile(chunk, 75) / loud_med) - 1
        value = (
            1.2 * energy
            + 0.8 * (rate / base_rate - 1 if base_rate else 0)
            + 6.0 * f["hook_words"]
            + 0.25 * min(f["laughs"] + f["exclaims"] + f["questions"], 4)
            + 0.15 * min(f["numbers"], 4)
            + 0.15 * min(f["swears"], 3)
            + 1.5 * hook_strength(segs[i]["text"])
            - (0.6 if clean_line(segs[i]["text"]) != segs[i]["text"].strip() else 0)  # opens with "um/so/like"
        )
        raw.append({"start": start, "end": end, "value": value, "hook_line": segs[i]["text"], "text": text})

    if not raw:
        return []
    # Percentile rank → 40..92 so heuristic scores never pretend to be as sure as a human editor
    values = np.array([r["value"] for r in raw])
    ranks = values.argsort().argsort() / max(len(values) - 1, 1)
    moments = []
    for r, rank in zip(raw, ranks):
        m = snap(
            {"start": r["start"], "end": r["end"], "score": int(40 + 52 * rank), "moment_type": "story",
             "hook_line": clean_line(r["hook_line"]), "reason": "heuristic: loudness, speech pace and hook words", "scorer": "heuristic"},
            transcript,
        )
        if m:
            moments.append(m)
    return dedupe(moments, limit)


# ---------- LLM scorer ----------

SYSTEM = (
    "You are a top short-form video editor who has made thousands of viral TikTok/Shorts/Reels clips "
    "from podcasts and streams. You pick moments that make a stranger stop scrolling and watch to the end."
)

PROMPT = """Source: "{title}" ({creator})
{campaign_notes}
Below is a timestamped transcript. Each line is [start–end] text, times in m:ss.

Pick up to {limit} standalone clip moments. Rules:
- A viewer with ZERO context must understand and enjoy it.
- The FIRST sentence must itself be the hook: the most shocking, funny, or controversial line. No setup, no "so", no context-building. If the setup is needed, the moment is not good enough — pick another.
- SHORT wins: completion rate is the #1 ranking signal. Ideal is 18–35 seconds; hard range {min_s:.0f}–{max_s:.0f}s. Only exceed 40s if every second is gripping.
- End ON the punchline / payoff / reaction — cut within a second after it. No wind-down.
- start/end must be copied from line boundaries below, converted to SECONDS (e.g. 2:05.3 → 125.3).
- Score honestly: 85+ = would go viral, 60-84 = solid, <60 = don't bother. Don't inflate.
- NEVER pick sponsor/ad reads, promo codes, or intros/outros. No gambling or betting content.
- Best performers, in order: contrarian takes ("saving money is bad advice"), specific number stories ("$0 to $10M in 18 months"), blunt hard truths/callouts, one concrete usable tactic (gets saved), personal failure → turning point → lesson.
- Also good: surprising reveals, funny exchanges, emotional peaks, relatable struggles.
- Skip moments that need earlier context, and generic advice with no specifics.

Transcript:
{lines}"""


def _llm_chunk(segments: list[dict], title: str, creator: str, notes: str, limit: int) -> list[dict]:
    lines = "\n".join(f"[{_fmt(s['start'])}–{_fmt(s['end'])}] {s['text']}" for s in segments)
    lo, hi = segments[0]["start"], segments[-1]["end"]
    prompt = PROMPT.format(
        title=title, creator=creator or "unknown creator", campaign_notes=notes, limit=limit,
        min_s=CLIP_MIN_SEC, max_s=CLIP_MAX_SEC, lines=lines,
    ) + f"\n\nValid start/end values are between {lo:.1f} and {hi:.1f} seconds — anything outside is rejected."
    picks = [m.model_dump() | {"scorer": "llm"} for m in llm.ask_json(prompt, MomentList, SYSTEM).moments]
    valid = [m for m in picks if lo - 1 <= m["start"] < m["end"] <= hi + 1]
    if len(valid) < len(picks):  # models sometimes invent timestamps; those can't be snapped to real words
        log.warning("Dropped %d LLM picks with timestamps outside %.0f–%.0fs", len(picks) - len(valid), lo, hi)
    return valid


def with_llm(transcript: dict, title: str, creator: str = "", notes: str = "", limit: int = MOMENTS_PER_SOURCE) -> list[dict]:
    segs = transcript["segments"]
    if not segs:
        return []
    chunk_sec, overlap = 30 * 60, 60  # 30-minute chunks keep prompts small and picks spread out
    t0, t_end = segs[0]["start"], segs[-1]["end"]
    n_chunks = max(1, int(np.ceil((t_end - t0) / chunk_sec)))
    per_chunk = max(2, int(np.ceil(limit * 1.5 / n_chunks)))
    found = []
    for k in range(n_chunks):
        a, b = t0 + k * chunk_sec - overlap, t0 + (k + 1) * chunk_sec + overlap
        chunk = [s for s in segs if s["start"] >= a and s["end"] <= b]
        if chunk:
            found += _llm_chunk(chunk, title, creator, notes, per_chunk)
    snapped = [m for m in (snap(m, transcript) for m in found) if m]
    return dedupe(snapped, limit)


def find_moments(transcript: dict, wav_path: Path | None, title: str = "", creator: str = "", notes: str = "",
                 offset: float = 0.0, limit: int = MOMENTS_PER_SOURCE) -> list[dict]:
    if llm.available():
        try:
            picked = with_llm(transcript, title, creator, notes, limit)
            if picked:
                return picked
            log.warning("LLM returned no usable moments; falling back to heuristic")
        except Exception as e:  # provider down / quota hit: keep the pipeline moving
            log.warning("LLM scoring failed (%s); falling back to heuristic", str(e)[:200])
    return heuristic(transcript, wav_path, offset, limit)
