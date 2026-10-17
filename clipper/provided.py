"""Campaigns that hand you finished clips ("post the provided content").

No moment picking or re-editing: the brand already did that. We only
  1. fit the file to 9:16 if it isn't already (blurred-background pad, never a crop),
  2. add the campaign's watermark if the brief requires one,
  3. write per-platform captions + hashtags (the brief's own captions if it gives any),
and drop the result into the normal review → schedule → post flow.

Put files in data/inbox/<campaign_id>/ (the autopilot picks them up), or run
`./clip provided <campaign_id> [file-or-url ...]`.
"""

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

from . import db
from .brain.copywrite import write_copy
from .config import DATA, DEFAULT_NICHE, OUT, campaign, campaigns
from .fetch import is_local, media_duration, video_size
from .transcribe import transcribe

log = logging.getLogger(__name__)
INBOX = DATA / "inbox"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
POSITIONS = {  # overlay x:y expressions, 40px margin
    "top-left": "40:40", "top-right": "W-w-40:40", "bottom-left": "40:H-h-40",
    "bottom-right": "W-w-40:H-h-40", "center": "(W-w)/2:(H-h)/2", "top-center": "(W-w)/2:120",
}


def _fingerprint(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(4 << 20))  # first 4 MB is plenty to tell files apart
    return h.hexdigest()[:16]


def _download(url: str, dest_dir: Path) -> Path:
    """Links from briefs (YouTube, Drive, Dropbox, TikTok…) via yt-dlp, which handles most hosts."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o",
                          str(dest_dir / "%(id)s.%(ext)s"), "--print", "after_move:filepath", url],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip().splitlines()[-1])


def prepare(src: Path, out: Path, watermark: dict | None = None) -> Path:
    """Fit to 1080×1920 (pad, don't crop — it's the brand's framing) and overlay the watermark."""
    w, h, _ = video_size(src)
    vertical = abs(w / h - 9 / 16) < 0.02
    if vertical:
        chain = "[0:v]scale=1080:1920,setsar=1[base]"
    else:  # blurred copy behind, original centered
        chain = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                 "boxblur=30:5,eq=brightness=-0.12[bg];[b]scale=1080:-2[fg];"
                 "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    inputs = ["-i", str(src)]
    if watermark and watermark.get("file"):
        wm = Path(watermark["file"])
        if not wm.is_absolute():
            wm = DATA.parent / wm
        scale = float(watermark.get("scale", 0.28))
        opacity = float(watermark.get("opacity", 1.0))
        pos = POSITIONS.get(watermark.get("position", "bottom-right"), POSITIONS["bottom-right"])
        inputs += ["-i", str(wm)]
        chain += (f";[1:v]format=rgba,scale={int(1080 * scale)}:-1,colorchannelmixer=aa={opacity}[wm];"
                  f"[base][wm]overlay={pos}[v]")
    else:
        chain += ";[base]null[v]"
    if vertical and not (watermark and watermark.get("file")) and (w, h) == (1080, 1920):
        shutil.copy(src, out)  # already perfect: don't re-encode
        return out
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", chain, "-map", "[v]", "-map", "0:a?",
                    "-c:v", "h264_videotoolbox", "-b:v", "9M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


def ingest(campaign_id: str, items: list[str] | None = None) -> list[int]:
    """Add provided clips to the review queue. Idempotent: the same file is never added twice."""
    db.init()
    camp = campaign(campaign_id)
    if not camp:
        raise ValueError(f"Unknown campaign '{campaign_id}' (config/campaigns.yaml)")
    rules = camp.get("rules", {})
    inbox = INBOX / campaign_id
    if items is None:
        items = [str(p) for p in sorted(inbox.glob("*")) if p.suffix.lower() in VIDEO_EXT]
    captions = rules.get("captions") or []
    made = []
    for item in items:
        path = Path(item) if is_local(item) else _download(item, inbox / "_downloads")
        key = f"provided:{campaign_id}:{_fingerprint(path)}"
        if db.row("SELECT id FROM sources WHERE url = ?", key):
            continue
        dur = media_duration(path)
        src_id = db.insert("sources", url=key, kind="provided", campaign_id=campaign_id,
                           streamer=camp.get("creator"), title=path.stem, duration=dur, status="scored")
        m_id = db.insert("moments", source_id=src_id, start=0.0, end=dur, score=100, moment_type="provided",
                         hook_line="", reason="campaign-provided clip", scorer="provided", status="rendered")
        out = OUT / f"provided_{src_id}.mp4"
        try:
            prepare(path, out, rules.get("watermark"))
            text = ""
            try:  # what's said in the clip gives the copywriter something real to work with
                wav = OUT / f"provided_{src_id}.wav"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", str(wav)],
                               check=True)
                text = " ".join(w["w"] for w in transcribe(wav, OUT / f"provided_{src_id}.json")["words"])
                wav.unlink(missing_ok=True)
            except Exception as e:
                log.warning("Couldn't transcribe %s (%s); writing copy from the title only", path.name, e)
            copy = write_copy(text, camp.get("creator", ""), camp.get("creator", ""), "provided", "", "",
                              niche=camp.get("niche", DEFAULT_NICHE), campaign=camp)
            if captions:  # the brief's own captions beat ours: rotate through them
                copy.description = captions[len(made) % len(captions)]
            thumb = out.with_suffix(".jpg")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
                            "-vf", "scale=540:-2", str(thumb)], check=False)
            clip_id = db.insert("clips", moment_id=m_id, video_path=str(out), thumb_path=str(thumb),
                                duration=dur, layout="provided", hook_text=copy.hook_text,
                                titles=json.dumps(copy.titles), description=copy.description,
                                hashtags=json.dumps(copy.hashtags), status="review")
            made.append(clip_id)
            log.info("Provided clip %s → clip #%d (%s, %.0fs)", path.name, clip_id, campaign_id, dur)
        except Exception as e:
            db.update("moments", m_id, status="failed", error=str(e)[:500])
            log.exception("Failed to ingest %s", path)
    return made


def ingest_all() -> int:
    """Autopilot job: scan every campaign's inbox folder."""
    n = 0
    for c in campaigns():
        (INBOX / c["id"]).mkdir(parents=True, exist_ok=True)
        n += len(ingest(c["id"]))
    return n
