"""Jump-cut editing: drop pauses and filler words, speed up slightly, keep word timings in sync.

Top clip pages cut every breath and "um" so the clip never sits still. We do it from the word
timestamps: keep each run of words, cut any gap longer than MAX_GAP, then concatenate the kept
pieces (video + audio) into one tight intermediate file that the renderer treats as the source.
"""

import logging
import subprocess
from pathlib import Path

import numpy as np

from ..brain.signals import is_filler

log = logging.getLogger(__name__)

MAX_GAP = 0.22  # silences longer than this get cut
PAD = 0.07      # keep a sliver around each word so consonants aren't clipped
SPEED = 1.08    # slight speed-up: reads as "energetic", not "sped up"


def _silences(src: str, start: float, dur: float) -> list[tuple[float, float]]:
    """Pauses from the audio itself (word timestamps stretch over pauses, so they miss most of them).
    Threshold is relative to the clip's own speech level, so quiet and loud mics both work."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
                          "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    hop = 320  # 20 ms
    n = len(x) // hop
    if n == 0:
        return []
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    quiet = rms < np.median(rms) * 0.18  # ~15 dB under typical speech
    out, i = [], 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * 0.02 >= MAX_GAP:
                out.append((i * 0.02, j * 0.02))
            i = j
        else:
            i += 1
    return out


def keep_intervals(words: list[dict], dur: float, silences: list[tuple[float, float]] = ()) -> list[tuple[float, float]]:
    """Clip-relative [a, b) spans to keep: speech minus filler words and pauses longer than MAX_GAP."""
    cut = [(max(w["s"] - 0.02, 0.0), w["e"] + 0.02) for w in words if is_filler(w["w"])]
    cut += [(a + PAD, b - PAD) for a, b in silences if b - a > 2 * PAD]
    spoken = [w for w in words if not is_filler(w["w"])]
    if not spoken:
        return [(0.0, dur)]
    spans = [(max(spoken[0]["s"] - PAD, 0.0), min(spoken[-1]["e"] + PAD, dur))]  # trim dead air at both ends
    for a, b in sorted(cut):
        nxt = []
        for s0, s1 in spans:
            if b <= s0 or a >= s1:
                nxt.append((s0, s1))
            else:
                if a > s0:
                    nxt.append((s0, a))
                if b < s1:
                    nxt.append((b, s1))
        spans = nxt
    # also cut word-timestamp gaps (clean audio with a noisy room tone never reads as "quiet")
    out = []
    for s0, s1 in spans:
        inside = [w for w in spoken if w["e"] > s0 and w["s"] < s1]
        for w0, w1 in zip(inside, inside[1:]):
            if w1["s"] - w0["e"] > MAX_GAP + 2 * PAD and s0 < w0["e"] + PAD < s1:
                out.append((s0, w0["e"] + PAD))
                s0 = max(s0, w1["s"] - PAD)
        out.append((s0, s1))
    return [(a, b) for a, b in out if b - a > 0.08]


def remap(t: float, spans: list[tuple[float, float]]) -> float:
    """Original clip time → time in the tightened (and sped-up) clip."""
    out = 0.0
    for a, b in spans:
        if t <= a:
            break
        out += min(t, b) - a
    return out / SPEED


def tighten(src: str, start: float, dur: float, words: list[dict], out: Path) -> tuple[str, float, list[dict], list[float]]:
    """Write the jump-cut version of src[start:start+dur] to `out`.

    Returns (path, new_duration, remapped_words, cut_times) — cut_times are where the picture jumps,
    in the new timeline (the renderer punches the zoom in/out there to hide the jump).
    """
    spans = keep_intervals(words, dur, _silences(src, start, dur))
    parts, labels = [], []
    for i, (a, b) in enumerate(spans):
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
        labels.append(f"[v{i}][a{i}]")
    graph = ("".join(parts) + "".join(labels) + f"concat=n={len(spans)}:v=1:a=1[cv][ca];"
             f"[cv]setpts=PTS/{SPEED}[v];[ca]atempo={SPEED}[a]")
    script = out.with_suffix(".filter.txt")
    script.write_text(graph)  # can be long (one trim per word run) → pass via file, not argv
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
           "-/filter_complex", str(script), "-map", "[v]", "-map", "[a]",
           "-c:v", "h264_videotoolbox", "-b:v", "20M", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True)
    script.unlink(missing_ok=True)

    new_words = [{"w": w["w"], "s": round(remap(w["s"], spans), 3), "e": round(remap(w["e"], spans), 3)}
                 for w in words if not is_filler(w["w"])]
    cuts, t = [], 0.0
    for a, b in spans[:-1]:
        t += (b - a) / SPEED
        cuts.append(round(t, 3))
    new_dur = sum(b - a for a, b in spans) / SPEED
    log.info("Tightened %.1fs → %.1fs (%d cuts)", dur, new_dur, len(cuts))
    return str(out), new_dur, new_words, cuts
