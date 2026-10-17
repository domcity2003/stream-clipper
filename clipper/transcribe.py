"""Speech → text with word-level timestamps, locally and free (faster-whisper)."""

import json
import logging
from functools import lru_cache
from pathlib import Path

from .config import WHISPER_MODEL

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    log.info("Loading whisper model '%s' (first run downloads it)", WHISPER_MODEL)
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribe(audio_path: Path, out_json: Path, offset: float = 0.0) -> dict:
    """`offset` is added to every timestamp, so times are always relative to the full source video
    even when only a section of it was downloaded. Returns {"language", "duration", "segments": [{start, end, text}], "words": [{w, s, e}]}.
    Cached on disk, so re-running a stage never re-transcribes."""
    if out_json.exists():
        return json.loads(out_json.read_text())
    segments, info = _model().transcribe(str(audio_path), word_timestamps=True, vad_filter=True)
    result = {"language": info.language, "duration": info.duration, "segments": [], "words": []}
    for seg in segments:
        result["segments"].append({"start": round(float(seg.start) + offset, 2), "end": round(float(seg.end) + offset, 2), "text": seg.text.strip()})
        for w in seg.words or []:
            result["words"].append({"w": w.word.strip(), "s": round(float(w.start) + offset, 2), "e": round(float(w.end) + offset, 2)})
    out_json.write_text(json.dumps(result))
    return result


def words_between(transcript: dict, start: float, end: float) -> list[dict]:
    """Words inside [start, end], re-timed so the clip starts at 0."""
    return [
        {"w": w["w"], "s": round(w["s"] - start, 2), "e": round(w["e"] - start, 2)}
        for w in transcript["words"]
        if w["s"] >= start - 0.05 and w["e"] <= end + 0.05
    ]


def text_between(transcript: dict, start: float, end: float) -> str:
    return " ".join(w["w"] for w in transcript["words"] if start <= w["s"] <= end)
