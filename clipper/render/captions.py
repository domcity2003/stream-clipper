"""Word-by-word "pop" captions and the hook banner, drawn with Pillow.

(Homebrew's FFmpeg has no libass/drawtext, and drawing ourselves gives full style control anyway.)
Overlays are pre-rendered once per caption state, then alpha-blended onto frames — fast.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..brain.signals import is_filler
from ..config import CAPTION_FONT

WHITE, YELLOW, BLACK = (255, 255, 255), (255, 226, 52), (0, 0, 0)


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(CAPTION_FONT, size)


@dataclass
class Overlay:
    image: np.ndarray  # H×W×4 BGRA
    x: int
    y: int


def blend(frame: np.ndarray, ov: Overlay) -> None:
    """Alpha-blend overlay onto a BGR frame in place (clipped to frame bounds)."""
    h, w = ov.image.shape[:2]
    fh, fw = frame.shape[:2]
    x0, y0 = max(ov.x, 0), max(ov.y, 0)
    x1, y1 = min(ov.x + w, fw), min(ov.y + h, fh)
    if x0 >= x1 or y0 >= y1:
        return
    img = ov.image[y0 - ov.y : y1 - ov.y, x0 - ov.x : x1 - ov.x]
    alpha = img[..., 3:4].astype(np.float32) / 255.0
    region = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (region * (1 - alpha) + img[..., :3].astype(np.float32) * alpha).astype(np.uint8)


def _to_bgra(img: Image.Image) -> np.ndarray:
    arr = np.array(img)  # RGBA
    return arr[..., [2, 1, 0, 3]].copy()


def chunk_words(words: list[dict], max_words: int = 3, max_chars: int = 16, gap: float = 0.45) -> list[list[dict]]:
    """Group words into short on-screen phrases; break on punctuation and pauses."""
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = sum(len(x["w"]) + 1 for x in cur)
        if (
            nxt is None
            or len(cur) >= max_words
            or chars + len(nxt["w"]) > max_chars
            or w["w"].endswith((".", "?", "!", ","))
            or nxt["s"] - w["e"] > gap
        ):
            chunks.append(cur)
            cur = []
    return chunks


def _clean(word: str) -> str:
    return word.upper().strip(",.")


class CaptionTrack:
    """Big centered captions; the word being spoken is highlighted yellow."""

    def __init__(self, words: list[dict], frame_w: int, y_center: int, font_size: int = 84):
        self.frame_w, self.y_center, self.base_size = frame_w, y_center, font_size
        self.chunks = chunk_words([w for w in words if not is_filler(w["w"])])  # no "UM" on screen
        self.cache: dict[tuple[int, int], Overlay] = {}
        # Each chunk stays up until the next begins (if the gap is short) to avoid flicker
        self.spans = []
        for k, ch in enumerate(self.chunks):
            end = ch[-1]["e"]
            if k + 1 < len(self.chunks) and self.chunks[k + 1][0]["s"] - end < 0.35:
                end = self.chunks[k + 1][0]["s"]
            self.spans.append((ch[0]["s"], end))

    def _render(self, k: int, active: int) -> Overlay:
        words = [_clean(w["w"]) for w in self.chunks[k]]
        size = self.base_size
        max_w = self.frame_w - 140
        while True:
            font = _font(size)
            space = font.getlength(" ")
            widths = [font.getlength(w) for w in words]
            total = sum(widths) + space * (len(words) - 1)
            if total <= max_w or size <= 44:
                break
            size -= 4
        stroke = max(size // 9, 4)
        pad = stroke * 2 + 10
        ascent, descent = font.getmetrics()
        img = Image.new("RGBA", (int(total) + pad * 2, ascent + descent + pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        x = pad
        for i, (w, wd) in enumerate(zip(words, widths)):
            draw.text((x, pad), w, font=font, fill=YELLOW if i == active else WHITE,
                      stroke_width=stroke, stroke_fill=BLACK)
            x += wd + space
        arr = _to_bgra(img)
        h, wdt = arr.shape[:2]
        return Overlay(arr, (self.frame_w - wdt) // 2, self.y_center - h // 2)

    def at(self, t: float) -> Overlay | None:
        for k, (s, e) in enumerate(self.spans):
            if s <= t < e:
                chunk = self.chunks[k]
                active = max((i for i, w in enumerate(chunk) if w["s"] <= t), default=0)
                key = (k, active)
                if key not in self.cache:
                    self.cache[key] = self._render(k, active)
                return self.cache[key]
        return None


def hook_banner(text: str, frame_w: int, y_top: int, font_size: int = 62) -> Overlay | None:
    """White rounded box with black text — the 'why you should keep watching' line for the first seconds."""
    text = " ".join(text.split())
    if not text:
        return None
    font = _font(font_size)
    max_w = frame_w - 160
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    lines = lines[:3]
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 6
    pad_x, pad_y = 34, 26
    box_w = int(max(font.getlength(l) for l in lines)) + pad_x * 2
    box_h = line_h * len(lines) + pad_y * 2
    img = Image.new("RGBA", (box_w + 12, box_h + 12), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((6, 10, box_w + 6, box_h + 10), radius=26, fill=(0, 0, 0, 90))  # soft shadow
    draw.rounded_rectangle((0, 0, box_w, box_h), radius=26, fill=(255, 255, 255, 255))
    for i, line in enumerate(lines):
        lw = font.getlength(line)
        draw.text(((box_w - lw) / 2, pad_y + i * line_h), line, font=font, fill=BLACK)
    arr = _to_bgra(img)
    return Overlay(arr, (frame_w - arr.shape[1]) // 2, y_top)
