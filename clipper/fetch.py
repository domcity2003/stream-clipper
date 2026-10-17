"""Downloading with yt-dlp (YouTube, Twitch, Kick, and ~1,700 other sites) plus local-file helpers.

Strategy to save disk and time: download *audio only* for the whole source (small, fast),
find the moments from the transcript, then download *video* only for the few seconds we clip.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

YTDLP = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"]
VIDEO_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b"


def is_local(url: str) -> bool:
    return Path(url).expanduser().exists()


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed: " + " | ".join(tail))
    return proc.stdout


def probe(url: str) -> dict:
    """Title + duration (seconds) without downloading."""
    if is_local(url):
        return {"title": Path(url).stem, "duration": media_duration(url)}
    info = json.loads(_run([*YTDLP, "-J", "--no-download", "--no-playlist", url]))
    return {"title": info.get("title"), "duration": info.get("duration"), "uploader": info.get("uploader")}


def download_audio(url: str, dest: Path, section: tuple[float, float] | None = None) -> Path:
    """16 kHz mono WAV, which is what the transcriber wants. Returns the file path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    wav = dest.with_suffix(".wav")
    if wav.exists():
        return wav
    if is_local(url):
        src = url
    else:
        tmpl = str(dest.with_suffix("")) + ".src.%(ext)s"
        cmd = [*YTDLP, "-f", "ba/b", "--no-playlist", "-o", tmpl, url]
        if section:
            cmd += ["--download-sections", f"*{section[0]:.2f}-{section[1]:.2f}"]
        _run(cmd)
        src = str(next(dest.parent.glob(dest.stem + ".src.*")))
    cut = ["-ss", str(section[0]), "-to", str(section[1])] if section and is_local(url) else []
    _run(["ffmpeg", "-y", "-v", "error", *cut, "-i", src, "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    if not is_local(url):
        Path(src).unlink(missing_ok=True)
    return wav


def download_video_section(url: str, start: float, end: float, dest: Path) -> Path:
    """Just the [start, end] seconds of the video (plus audio), as mp4."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    if is_local(url):
        # Local files are cut directly at render time, no copy needed
        return Path(url).expanduser()
    tmpl = str(dest.with_suffix("")) + ".%(ext)s"
    _run(
        [*YTDLP, "-f", VIDEO_FORMAT, "--merge-output-format", "mp4", "--no-playlist",
         "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts", "-o", tmpl, url]
    )
    return dest


def media_duration(path: str | Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def video_size(path: str | Path) -> tuple[int, int, float]:
    """(width, height, fps) of the first video stream."""
    out = _run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(path)]
    )
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return s["width"], s["height"], float(num) / float(den or 1)


def check_tools() -> dict[str, bool]:
    return {"ffmpeg": shutil.which("ffmpeg") is not None, "ffprobe": shutil.which("ffprobe") is not None}
