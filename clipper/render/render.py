"""Turn a [start, end] slice of a 16:9 video into a finished 1080×1920 short.

Frames are decoded by FFmpeg, composed in Python (crop/layout + captions + hook), and piped back
into FFmpeg for hardware H.264 encoding with loudness-normalized audio.

Layouts:
  facetrack — follow the speaker's face (podcasts, IRL, just-chatting)          [default when faces found]
  split     — facecam box on top, gameplay below (needs `facecam: [x, y, w, h]` fractions in streamer config)
  blurfill  — whole 16:9 frame in the middle over a blurred copy (anything else)
"""

import logging
import subprocess
from pathlib import Path

import cv2
import numpy as np

from ..fetch import video_size
from .captions import CaptionTrack, blend, hook_banner
from .facetrack import track
from .tighten import tighten as jump_cut

log = logging.getLogger(__name__)

OW, OH, FPS = 1080, 1920, 30
HOOK_SECONDS = 3.0
ZOOM_END = 1.06  # slow push-in over the clip — subtle motion keeps eyes on screen
PUNCH = 1.14     # zoom level toggled at jump cuts: hides the jump and resets attention
MIN_PUNCH_GAP = 1.5  # seconds between zoom toggles, so the frame doesn't twitch on tiny cuts


def _punch_times(cuts: list[float]) -> list[float]:
    """Subset of cut points to toggle the zoom on (at most one per MIN_PUNCH_GAP)."""
    out, last = [], 0.0
    for c in cuts:
        if c - last >= MIN_PUNCH_GAP:
            out.append(c)
            last = c
    return out


def _decode(src: str, start: float, dur: float, h: int):
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
           "-vf", f"fps={FPS},scale=-2:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def _cover(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Scale to fill w×h, center-cropping the overflow."""
    ih, iw = img.shape[:2]
    s = max(w / iw, h / ih)
    r = cv2.resize(img, (int(np.ceil(iw * s)), int(np.ceil(ih * s))), interpolation=cv2.INTER_LINEAR)
    y0, x0 = (r.shape[0] - h) // 2, (r.shape[1] - w) // 2
    return r[y0 : y0 + h, x0 : x0 + w]


def _facetrack_frame(frame: np.ndarray, cx: float, zoom: float) -> np.ndarray:
    fh, fw = frame.shape[:2]
    ch = int(fh / zoom)
    cw = int(ch * 9 / 16)
    x0 = int(np.clip(cx * fw - cw / 2, 0, fw - cw))
    y0 = (fh - ch) // 2
    return cv2.resize(frame[y0 : y0 + ch, x0 : x0 + cw], (OW, OH), interpolation=cv2.INTER_LINEAR)


def _blurfill_frame(frame: np.ndarray, zoom: float) -> np.ndarray:
    small = _cover(cv2.resize(frame, (frame.shape[1] // 4, frame.shape[0] // 4)), OW // 4, OH // 4)
    bg = cv2.resize(cv2.GaussianBlur(small, (0, 0), 12), (OW, OH))
    bg = (bg * 0.55).astype(np.uint8)  # darken so the foreground pops
    fh, fw = frame.shape[:2]
    trim = int(fw * (1 - 1 / (zoom + 0.1)) / 2)  # slight side trim makes the strip taller
    fg = frame[:, trim : fw - trim]
    fg_h = int(OW * fg.shape[0] / fg.shape[1])
    fg = cv2.resize(fg, (OW, fg_h), interpolation=cv2.INTER_LINEAR)
    y0 = (OH - fg_h) // 2
    bg[y0 : y0 + fg_h] = fg
    return bg


def _split_frame(frame: np.ndarray, cam: list[float]) -> np.ndarray:
    fh, fw = frame.shape[:2]
    x, y, w, h = int(cam[0] * fw), int(cam[1] * fh), int(cam[2] * fw), int(cam[3] * fh)
    top_h = int(OH * 0.38)
    out = np.empty((OH, OW, 3), np.uint8)
    out[:top_h] = _cover(frame[y : y + h, x : x + w], OW, top_h)
    out[top_h:] = _cover(frame, OW, OH - top_h)
    return out


def render(src: str, start: float, end: float, out_path: Path, words: list[dict], hook_text: str = "",
           layout: str = "auto", facecam: list[float] | None = None, tight: bool = True) -> dict:
    """`words` must be clip-relative (0 = start). Returns {"layout", "duration", "thumb"}.

    tight=True first jump-cuts pauses/fillers out (render/tighten.py) and punches the zoom at cuts."""
    dur = end - start
    punches: list[float] = []
    tight_path = out_path.with_suffix(".tight.mov")
    if tight and words:
        src, dur, words, cuts = jump_cut(src, start, dur, words, tight_path)
        start, punches = 0.0, _punch_times(cuts)
    w, h, _ = video_size(src)
    dh = min(h, 1080) if h >= 720 else 1080  # work at ≤1080p; upscale tiny sources
    dw = int(round(w * dh / h / 2) * 2)

    ft = None
    if layout in ("auto", "facetrack"):
        ft = track(src, start, dur, w, h)
        if layout == "auto":
            layout = "facetrack" if ft.coverage >= 0.4 else ("split" if facecam else "blurfill")
    if layout == "split" and not facecam:
        layout = "blurfill"

    caption_y = {"facetrack": int(OH * 0.70), "split": int(OH * 0.38), "blurfill": int(OH * 0.76)}[layout]
    captions = CaptionTrack(words, OW, caption_y)
    hook = hook_banner(hook_text, OW, int(OH * 0.065)) if hook_text else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.mp4")
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}", "-r", str(FPS), "-i", "-",
         "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
         "-map", "0:v", "-map", "1:a?",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-c:v", "h264_videotoolbox", "-b:v", "9M", "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-movflags", "+faststart", "-shortest", str(tmp)],
        stdin=subprocess.PIPE,
    )
    dec = _decode(src, start, dur, dh)
    frame_bytes = dw * dh * 3
    thumb_t, thumb = min(1.0, dur / 2), None
    i = 0
    while True:
        buf = dec.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
        t = i / FPS
        zoom = 1 + (ZOOM_END - 1) * (t / dur)
        if sum(1 for p in punches if p <= t) % 2:
            zoom *= PUNCH
        if layout == "facetrack" and ft.face_at(t):
            out = _facetrack_frame(frame, ft.x_at(t), zoom)
        elif layout == "facetrack":  # wide shot / no face: show the whole frame instead of an empty crop
            out = _blurfill_frame(frame, zoom)
        elif layout == "split":
            out = _split_frame(frame, facecam)
        else:
            out = _blurfill_frame(frame, zoom)
        if hook and t < HOOK_SECONDS:
            blend(out, hook)
        cap = captions.at(t)
        if cap:
            blend(out, cap)
        if thumb is None and t >= thumb_t:
            thumb = out.copy()
        enc.stdin.write(out.tobytes())
        i += 1
    dec.wait()
    enc.stdin.close()
    if enc.wait() != 0 or i == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"render failed ({i} frames decoded)")
    tmp.replace(out_path)
    tight_path.unlink(missing_ok=True)
    thumb_path = out_path.with_suffix(".jpg")
    if thumb is not None:
        cv2.imwrite(str(thumb_path), thumb, [cv2.IMWRITE_JPEG_QUALITY, 88])
    log.info("Rendered %s (%s, %.1fs, %d frames)", out_path.name, layout, dur, i)
    return {"layout": layout, "duration": i / FPS, "thumb": str(thumb_path) if thumb is not None else None}
