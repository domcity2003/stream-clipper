"""TikTok via the official Content Posting API.

Setup: developers.tiktok.com → create app → add Login Kit + Content Posting API →
scopes user.info.basic, video.upload, video.publish (+ video.list for stats) → set a redirect URI.
Put TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI in .env, then run
`python -m clipper auth tiktok <account_id>`.

Modes (per account, `tiktok_mode` in accounts.yaml):
  inbox  — uploads to your TikTok drafts/inbox; you tap Post in the app (works BEFORE TikTok's audit)
  direct — posts publicly (only after TikTok approves the app; unaudited apps can only post privately)
"""

import json
import math
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

from ..config import TOKENS
from .base import PostResult, Publisher

API = "https://open.tiktokapis.com/v2"
CHUNK = 10 * 1024 * 1024


def _token_file(account_id: str) -> Path:
    return TOKENS / f"tiktok_{account_id}.json"


def _save(account_id: str, tok: dict):
    tok["expires_at"] = time.time() + tok.get("expires_in", 86400)
    _token_file(account_id).write_text(json.dumps(tok))


def authorize(account_id: str):
    key, secret, redirect = os.environ["TIKTOK_CLIENT_KEY"], os.environ["TIKTOK_CLIENT_SECRET"], os.environ["TIKTOK_REDIRECT_URI"]
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "response_type": "code", "redirect_uri": redirect, "state": state,
        "scope": "user.info.basic,video.upload,video.publish,video.list",
    })
    print("1) Open this URL, log in to the TikTok account and approve:\n\n" + url)
    back = input("\n2) Paste the full URL you were redirected to: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    if q.get("state", [""])[0] != state:
        raise SystemExit("State mismatch — start again.")
    r = httpx.post(f"{API}/oauth/token/", data={
        "client_key": key, "client_secret": secret, "code": q["code"][0],
        "grant_type": "authorization_code", "redirect_uri": redirect,
    })
    r.raise_for_status()
    _save(account_id, r.json())
    print(f"Saved TikTok login for {account_id}")


def _access_token(account_id: str) -> str:
    tok = json.loads(_token_file(account_id).read_text())
    if tok["expires_at"] < time.time() + 300:
        r = httpx.post(f"{API}/oauth/token/", data={
            "client_key": os.environ["TIKTOK_CLIENT_KEY"], "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        })
        r.raise_for_status()
        tok = r.json()
        _save(account_id, tok)
    return tok["access_token"]


def _check(r: httpx.Response) -> dict:
    data = r.json()
    err = data.get("error", {})
    if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
        raise RuntimeError(f"TikTok API error {r.status_code}: {err}")
    return data.get("data", {})


class TikTokPublisher(Publisher):
    platform = "tiktok"

    def ready(self, account: dict) -> bool:
        return _token_file(account["id"]).exists() and bool(os.getenv("TIKTOK_CLIENT_KEY"))

    def publish(self, video_path: str, meta: dict, account: dict) -> PostResult:
        headers = {"Authorization": f"Bearer {_access_token(account['id'])}", "Content-Type": "application/json; charset=UTF-8"}
        size = Path(video_path).stat().st_size
        chunk = size if size <= CHUNK else CHUNK
        count = max(1, size // chunk)  # TikTok: the last chunk absorbs the remainder
        source_info = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        if account.get("tiktok_mode", "inbox") == "direct":
            body = {"post_info": {"title": meta["caption"], "privacy_level": account.get("privacy", "PUBLIC_TO_EVERYONE"),
                                  "disable_comment": False, "disable_duet": False, "disable_stitch": False},
                    "source_info": source_info}
            init = _check(httpx.post(f"{API}/post/publish/video/init/", headers=headers, json=body))
        else:
            init = _check(httpx.post(f"{API}/post/publish/inbox/video/init/", headers=headers, json={"source_info": source_info}))

        with open(video_path, "rb") as f:
            for i in range(count):
                start = i * chunk
                length = size - start if i == count - 1 else chunk
                f.seek(start)
                r = httpx.put(init["upload_url"], content=f.read(length), timeout=300, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}",
                })
                if r.status_code not in (200, 201, 206):
                    raise RuntimeError(f"TikTok upload chunk {i} failed: {r.status_code} {r.text[:200]}")

        publish_id = init["publish_id"]
        for _ in range(60):  # wait up to ~5 min for TikTok to process
            status = _check(httpx.post(f"{API}/post/publish/status/fetch/", headers=headers, json={"publish_id": publish_id}))
            state = status.get("status")
            if state in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
                post_id = str(ids[0]) if ids else publish_id
                url = f"https://www.tiktok.com/@{account.get('handle', '').lstrip('@')}/video/{post_id}" if ids else ""
                return PostResult(platform_post_id=post_id, url=url,
                                  status="posted" if state == "PUBLISH_COMPLETE" else "exported")
            if state == "FAILED":
                raise RuntimeError(f"TikTok processing failed: {status.get('fail_reason')}")
            time.sleep(5)
        raise RuntimeError("TikTok processing timed out")

    def stats(self, post_ids: list[str], account: dict) -> dict[str, dict]:
        headers = {"Authorization": f"Bearer {_access_token(account['id'])}", "Content-Type": "application/json"}
        numeric = [p for p in post_ids if p.isdigit()]
        out = {}
        for i in range(0, len(numeric), 20):
            data = _check(httpx.post(f"{API}/video/query/?fields=id,view_count,like_count,comment_count,share_count",
                                     headers=headers, json={"filters": {"video_ids": numeric[i : i + 20]}}))
            for v in data.get("videos", []):
                out[str(v["id"])] = {"views": v.get("view_count"), "likes": v.get("like_count"),
                                     "comments": v.get("comment_count"), "shares": v.get("share_count")}
        return out
