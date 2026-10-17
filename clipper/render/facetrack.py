"""Where is the face? Used to keep the speaker centered when cropping 16:9 → 9:16.

Uses MediaPipe's face detector (robust to caps, angles, and faces in paintings/posters);
falls back to OpenCV Haar cascades if MediaPipe isn't available.
"""

import subprocess
import warnings
from dataclasses import dataclass

import cv2
import numpy as np

SAMPLE_FPS = 5
SAMPLE_W = 480


@dataclass
class FaceTrack:
    times: np.ndarray  # seconds (clip-relative)
    xs: np.ndarray  # smoothed horizontal face center, 0..1 of frame width
    coverage: float  # fraction of samples where a face was found
    has_face: np.ndarray | None = None  # per sample: is a face on screen around this time?

    def x_at(self, t: float) -> float:
        return float(np.interp(t, self.times, self.xs)) if len(self.times) else 0.5

    def face_at(self, t: float) -> bool:
        """False during stretches (≥1s) with no face — e.g. a wide shot — where cropping would show empty space."""
        if self.has_face is None or not len(self.times):
            return True
        return bool(self.has_face[min(int(round(t * SAMPLE_FPS)), len(self.has_face) - 1)])


def _frames(src: str, start: float, dur: float, w: int, h: int):
    sh = int(round(SAMPLE_W * h / w / 2) * 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
           "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = SAMPLE_W * sh * 3
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(sh, SAMPLE_W, 3)
    proc.wait()


def _mediapipe_detector():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import mediapipe as mp
        det = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.55)

        def detect(rgb):
            res = det.process(rgb)
            out = []
            for d in res.detections or []:
                b = d.location_data.relative_bounding_box
                out.append((b.xmin + b.width / 2, b.width * b.height))
            return out

        return detect
    except Exception:
        return None


def _haar_detector():
    frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

    def detect(rgb):
        eq = cv2.equalizeHist(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
        cands = [((x + fw / 2) / SAMPLE_W, fw * fh) for x, y, fw, fh in _detect(frontal, eq)]
        if not cands:  # people on podcasts often face each other: try side profiles (both directions)
            cands = [((x + fw / 2) / SAMPLE_W, fw * fh) for x, y, fw, fh in _detect(profile, eq)]
            cands += [(1 - (x + fw / 2) / SAMPLE_W, fw * fh) for x, y, fw, fh in _detect(profile, cv2.flip(eq, 1))]
        return cands

    return detect


def track(src: str, start: float, dur: float, w: int, h: int) -> FaceTrack:
    detect = _mediapipe_detector() or _haar_detector()
    raw: list[float | None] = []
    prev = None
    for rgb in _frames(src, start, dur, w, h):
        cands = detect(rgb)
        if not cands:
            raw.append(None)
            continue
        near = [c for c in cands if prev is not None and abs(c[0] - prev) < 0.12]
        # Stay on the same person if still visible; otherwise jump to the biggest (closest) face
        pick = max(near or cands, key=lambda c: c[1])[0]
        raw.append(pick)
        prev = pick

    n = len(raw)
    found = [x for x in raw if x is not None]
    coverage = len(found) / n if n else 0.0
    if not found:
        return FaceTrack(np.array([0.0]), np.array([0.5]), 0.0)

    # Fill gaps with the last known position, then smooth — but snap instantly on camera cuts
    filled, last = [], found[0]
    for x in raw:
        last = x if x is not None else last
        filled.append(last)
    smooth, cur = [], filled[0]
    for x in filled:
        cur = x if abs(x - cur) > 0.2 else cur + 0.35 * (x - cur)
        smooth.append(cur)
    times = np.arange(n) / SAMPLE_FPS
    # "No face" zones: samples with no detection within ±0.4s; ignore gaps shorter than 1s (detector blinks)
    seen = np.array([x is not None for x in raw])
    near = np.array([seen[max(0, i - 2) : i + 3].any() for i in range(n)])
    has_face = near.copy()
    i = 0
    while i < n:
        if not near[i]:
            j = i
            while j < n and not near[j]:
                j += 1
            if j - i < SAMPLE_FPS:  # shorter than 1s → keep cropping
                has_face[i:j] = True
            i = j
        else:
            i += 1
    return FaceTrack(times, np.array(smooth), coverage, has_face)


def _detect(cascade, img):
    return cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=6, minSize=(28, 28))
