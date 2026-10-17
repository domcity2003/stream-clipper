"""Find new things to clip, automatically.

  • Campaigns: every `source_urls` entry, plus the latest uploads of any `source_channels`.
  • Twitch: the most-viewed viewer-made clips from each allowed streamer in the last 24h
    (viewers already voted on what's good — the strongest free signal there is).
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import TOKENS, campaigns, streamers
from .pipeline import add_source

log = logging.getLogger(__name__)


# ---------- campaigns ----------

def latest_uploads(channel_url: str, n: int = 3) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(n), "--print", "url",
         "--no-warnings", channel_url],
        capture_output=True, text=True,
    )
    return [u for u in out.stdout.split() if u.startswith("http")]


def discover_campaigns() -> int:
    added = 0
    for c in campaigns():
        if c["id"].startswith("example"):
            continue
        urls = list(c.get("source_urls", []))
        for ch in c.get("source_channels", []):
            urls += latest_uploads(ch, c.get("latest_n", 3))
        for url in urls:
            if "XXXXXXXX" in url:
                continue
            add_source(url, kind="campaign", campaign_id=c["id"], streamer=c.get("creator"))
            added += 1
    return added


# ---------- Twitch ----------

def _twitch_token() -> str | None:
    cid, secret = os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        return None
    cache = TOKENS / "twitch_app.json"
    if cache.exists():
        tok = json.loads(cache.read_text())
        if tok["expires_at"] > time.time() + 300:
            return tok["access_token"]
    r = httpx.post("https://id.twitch.tv/oauth2/token",
                   data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    r.raise_for_status()
    tok = r.json()
    tok["expires_at"] = time.time() + tok["expires_in"]
    cache.write_text(json.dumps(tok))
    return tok["access_token"]


def _helix(path: str, params: dict) -> list[dict]:
    token = _twitch_token()
    r = httpx.get(f"https://api.twitch.tv/helix/{path}", params=params,
                  headers={"Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json().get("data", [])


def twitch_top_clips(handle: str, hours: int = 24, n: int = 5, min_views: int = 50) -> list[dict]:
    users = _helix("users", {"login": handle})
    if not users:
        log.warning("Twitch user %s not found", handle)
        return []
    started = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    clips = _helix("clips", {"broadcaster_id": users[0]["id"], "started_at": started, "first": 50})
    clips = [c for c in clips if c["view_count"] >= min_views]
    # Several viewers clipping the same moment = a guaranteed highlight; keep one clip per moment
    clips.sort(key=lambda c: c["view_count"], reverse=True)
    picked, seen_offsets = [], []
    for c in clips:
        off = c.get("vod_offset")
        if off is not None and any(abs(off - s) < 45 for s in seen_offsets):
            continue
        picked.append(c)
        if off is not None:
            seen_offsets.append(off)
        if len(picked) >= n:
            break
    return picked


def discover_twitch() -> int:
    if not _twitch_token():
        log.info("Twitch keys not set; skipping Twitch discovery")
        return 0
    added = 0
    for s in streamers():
        if s.get("platform", "twitch") != "twitch" or not s.get("allowed_to_clip") or s["handle"].startswith("example"):
            continue
        for clip in twitch_top_clips(s["handle"], n=s.get("max_clips_per_stream", 5)):
            add_source(clip["url"], kind="twitch_clip", streamer=s["handle"])
            added += 1
    return added


def discover_all() -> int:
    n = discover_campaigns() + discover_twitch()
    log.info("Discovery queued %d sources", n)
    return n
