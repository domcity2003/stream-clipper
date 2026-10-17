"""Paths, settings and YAML config loading. Everything else imports from here."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA = ROOT / "data"
RAW = DATA / "raw"  # downloaded audio / video sections, auto-deleted after RAW_RETENTION_DAYS
OUT = DATA / "out"  # finished clips + thumbnails
EXPORT = DATA / "export"  # ready-to-post folders (video + caption) for manual posting
TOKENS = DATA / "tokens"  # OAuth tokens for publishing accounts
DB_PATH = DATA / "clipper.db"

for d in (RAW, OUT, EXPORT, TOKENS):
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")

# Homebrew's FFmpeg can't verify HTTPS certificates on this Mac; point OpenSSL at certifi's bundle
# (inherited by the yt-dlp → ffmpeg subprocesses that download video sections).
import certifi  # noqa: E402

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini/gemini-flash-latest")
# Backups when the main model is overloaded (same key). Comma-separated; only same-provider models by default.
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "LLM_FALLBACK_MODELS", "gemini/gemini-3.8-flash,gemini/gemini-flash-lite-latest" if LLM_MODEL.startswith("gemini/") else ""
).split(",") if m.strip()]
MIN_SCORE = int(os.getenv("MIN_SCORE", "60"))
MOMENTS_PER_SOURCE = int(os.getenv("MOMENTS_PER_SOURCE", "6"))
CLIP_MIN_SEC = float(os.getenv("CLIP_MIN_SEC", "12"))
CLIP_MAX_SEC = float(os.getenv("CLIP_MAX_SEC", "50"))  # raw length; jump cuts shrink it ~20-30% → ~20-38s final
# Hashtag bank used unless a campaign/streamer sets its own (config/hashtags.yaml → niches)
DEFAULT_NICHE = os.getenv("DEFAULT_NICHE", "business_money")
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "3"))
CAPTION_FONT = os.getenv("CAPTION_FONT", "/System/Library/Fonts/Supplemental/Arial Black.ttf")
# Base URL where data/out is publicly reachable (needed only for Instagram publishing)
PUBLIC_MEDIA_BASE_URL = os.getenv("PUBLIC_MEDIA_BASE_URL", "")


def load_yaml(name: str) -> dict:
    """Load config/<name>.yaml, falling back to config/<name>.example.yaml so things run before setup."""
    for candidate in (CONFIG_DIR / f"{name}.yaml", CONFIG_DIR / f"{name}.example.yaml"):
        if candidate.exists():
            return yaml.safe_load(candidate.read_text()) or {}
    return {}


def streamers() -> list[dict]:
    return load_yaml("streamers").get("streamers", [])


DEFAULT_SLOTS = ["11:00", "15:00", "19:00", "21:30"]


def accounts() -> list[dict]:
    """Real accounts from config/accounts.yaml. Until that exists, one manual-export account per
    platform, so approved clips still get scheduled into ready-to-post folders."""
    path = CONFIG_DIR / "accounts.yaml"
    if path.exists():
        return (yaml.safe_load(path.read_text()) or {}).get("accounts", [])
    return [
        {"id": f"{p}_main", "platform": p, "handle": "", "daily_cap": 3, "slots": DEFAULT_SLOTS[:3],
         "timezone": "America/Chicago"}
        for p in ("tiktok", "youtube", "instagram")
    ]


def campaigns() -> list[dict]:
    return load_yaml("campaigns").get("campaigns", [])


def campaign(campaign_id: str | None) -> dict | None:
    return next((c for c in campaigns() if c["id"] == campaign_id), None) if campaign_id else None


def hashtags() -> dict:
    return load_yaml("hashtags")
