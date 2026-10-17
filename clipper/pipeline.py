"""The assembly line. Each stage picks up rows in one status and moves them to the next.

  source:  new ─audio→ audio ─transcribe→ transcribed ─score→ scored
  moment:  new ─render+copy→ rendered   (creates a clip in status 'review')

Every stage is idempotent (outputs are cached on disk), so a crash or a sleeping Mac just
means the next run picks up where it left off.
"""

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import db
from .brain.copywrite import write_copy
from .brain.moments import find_moments
from .config import DEFAULT_NICHE, MIN_SCORE, MOMENTS_PER_SOURCE, OUT, RAW, RAW_RETENTION_DAYS, campaign, streamers
from .fetch import download_audio, download_video_section, is_local, probe
from .render.render import render
from .transcribe import text_between, transcribe, words_between

log = logging.getLogger(__name__)
SHORT_SOURCE_SEC = 90  # sources this short (e.g. Twitch clips) are downloaded whole


def add_source(url: str, kind: str = "url", campaign_id: str | None = None, streamer: str | None = None,
               section: tuple[float, float] | None = None) -> int:
    db.init()
    existing = db.row(
        "SELECT id FROM sources WHERE url = ? AND section_start IS ? AND section_end IS ?",
        url, section[0] if section else None, section[1] if section else None,
    )
    if existing:
        return existing["id"]
    return db.insert("sources", url=url, kind=kind, campaign_id=campaign_id, streamer=streamer,
                     section_start=section[0] if section else None, section_end=section[1] if section else None)


def _src_dir(source: dict) -> Path:
    return RAW / f"source_{source['id']}"


def _section(source: dict) -> tuple[float, float] | None:
    if source["section_start"] is None:
        return None
    return source["section_start"], source["section_end"]


def _streamer_cfg(name: str | None) -> dict:
    return next((s for s in streamers() if name and s.get("handle", "").lower() == name.lower()), {})


# ---------- stages ----------

def stage_audio(source: dict):
    info = probe(source["url"])
    wav = download_audio(source["url"], _src_dir(source) / "audio", _section(source))
    db.update("sources", source["id"], title=info.get("title"), duration=info.get("duration"),
              streamer=source["streamer"] or info.get("uploader"), audio_path=str(wav), status="audio")


def stage_transcribe(source: dict):
    out = _src_dir(source) / "transcript.json"
    transcribe(Path(source["audio_path"]), out, offset=source["section_start"] or 0.0)
    db.update("sources", source["id"], transcript_path=str(out), status="transcribed")


def stage_score(source: dict):
    transcript = json.loads(Path(source["transcript_path"]).read_text())
    camp = campaign(source["campaign_id"])
    notes = f"Campaign brief: {camp['rules'].get('notes', '')}" if camp else ""
    limit = 1 if (source["duration"] or 0) <= SHORT_SOURCE_SEC else MOMENTS_PER_SOURCE
    moments = find_moments(transcript, Path(source["audio_path"]), title=source["title"] or "",
                           creator=(camp or {}).get("creator") or source["streamer"] or "", notes=notes,
                           offset=source["section_start"] or 0.0, limit=limit)
    for m in moments:
        db.insert("moments", source_id=source["id"], start=m["start"], end=m["end"], score=m["score"],
                  moment_type=m["moment_type"], hook_line=m["hook_line"], reason=m["reason"], scorer=m["scorer"],
                  status="new" if m["score"] >= MIN_SCORE else "skipped")
    log.info("Source %s: %d moments (%d above min score %d)", source["id"], len(moments),
             sum(m["score"] >= MIN_SCORE for m in moments), MIN_SCORE)
    db.update("sources", source["id"], status="scored")


def stage_render(moment: dict):
    source = db.row("SELECT * FROM sources WHERE id = ?", moment["source_id"])
    transcript = json.loads(Path(source["transcript_path"]).read_text())
    camp = campaign(source["campaign_id"])
    creator = (camp or {}).get("creator") or source["streamer"] or ""
    streamer_cfg = _streamer_cfg(source["streamer"])

    # Get the pixels: local files are used directly; remote sources download just this slice (+1s padding)
    if is_local(source["url"]):
        video, offset = source["url"], 0.0
    elif (source["duration"] or 0) <= SHORT_SOURCE_SEC:
        video = download_video_section(source["url"], 0, source["duration"], _src_dir(source) / "full.mp4")
        offset = 0.0
    else:
        offset = max(moment["start"] - 1.0, 0.0)
        video = download_video_section(source["url"], offset, moment["end"] + 1.0,
                                       _src_dir(source) / f"moment_{offset:.2f}_{moment['end'] + 1.0:.2f}.mp4")

    text = text_between(transcript, moment["start"], moment["end"])
    copy = write_copy(text, source["title"] or "", creator, moment["moment_type"], moment["reason"] or "",
                      moment["hook_line"] or "", niche=(camp or {}).get("niche") or streamer_cfg.get("niche", DEFAULT_NICHE), campaign=camp)
    out = OUT / f"clip_{moment['id']}.mp4"
    result = render(str(video), moment["start"] - offset, moment["end"] - offset, out,
                    words_between(transcript, moment["start"], moment["end"]), hook_text=copy.hook_text,
                    layout=streamer_cfg.get("layout", "auto"), facecam=streamer_cfg.get("facecam"))
    db.insert("clips", moment_id=moment["id"], video_path=str(out), thumb_path=result["thumb"],
              duration=result["duration"], layout=result["layout"], hook_text=copy.hook_text,
              titles=json.dumps(copy.titles), description=copy.description, hashtags=json.dumps(copy.hashtags))
    db.update("moments", moment["id"], status="rendered")


# ---------- runner ----------

SOURCE_STAGES = {"new": stage_audio, "audio": stage_transcribe, "transcribed": stage_score}


_busy = threading.Lock()


def process(max_items: int = 50) -> int:
    """Advance everything that's pending by one or more stages. Returns number of steps done.
    Only one processor runs at a time (scheduler + web UI can both trigger it)."""
    if not _busy.acquire(blocking=False):
        return 0
    try:
        return _process(max_items)
    finally:
        _busy.release()


def _process(max_items: int) -> int:
    db.init()
    steps = 0
    while steps < max_items:
        source = db.row("SELECT * FROM sources WHERE status IN ('new','audio','transcribed') ORDER BY id LIMIT 1")
        moment = db.row("SELECT * FROM moments WHERE status = 'new' ORDER BY score DESC, id LIMIT 1")
        if moment:  # finish clips first: they're closest to making money
            table, item, fn = "moments", moment, stage_render
        elif source:
            table, item, fn = "sources", source, SOURCE_STAGES[source["status"]]
        else:
            break
        t0 = time.time()
        try:
            fn(item)
            log.info("%s #%s: %s done in %.0fs", table, item["id"], fn.__name__, time.time() - t0)
        except Exception as e:
            log.exception("%s #%s failed in %s", table, item["id"], fn.__name__)
            db.update(table, item["id"], status="failed", error=f"{fn.__name__}: {e}"[:500])
        steps += 1
    return steps


def cleanup_raw():
    """Delete downloaded raw media older than RAW_RETENTION_DAYS (finished clips are kept)."""
    cutoff = time.time() - RAW_RETENTION_DAYS * 86400
    for d in RAW.glob("source_*"):
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            log.info("Cleaned up %s", d.name)
