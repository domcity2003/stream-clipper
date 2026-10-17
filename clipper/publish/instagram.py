"""Instagram Reels via the official Instagram Graph API (resumable upload — no public URL needed).

Setup: Instagram account switched to Creator/Business → Meta developer app with Instagram API →
generate a long-lived access token → in accounts.yaml set `ig_user_id` and `token_env` (the name of
the .env variable holding that token, e.g. IG_TOKEN_MAIN).
"""

import os
import time
from pathlib import Path

import httpx

from .base import PostResult, Publisher

GRAPH = "https://graph.facebook.com/" + os.getenv("META_GRAPH_VERSION", "v23.0")
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/" + os.getenv("META_GRAPH_VERSION", "v23.0")


def _token(account: dict) -> str | None:
    return os.getenv(account.get("token_env", "IG_ACCESS_TOKEN"))


def _ok(r: httpx.Response) -> dict:
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram API error {r.status_code}: {data.get('error', data)}")
    return data


class InstagramPublisher(Publisher):
    platform = "instagram"

    def ready(self, account: dict) -> bool:
        return bool(_token(account) and account.get("ig_user_id"))

    def publish(self, video_path: str, meta: dict, account: dict) -> PostResult:
        token, user = _token(account), account["ig_user_id"]
        container = _ok(httpx.post(f"{GRAPH}/{user}/media", params={
            "media_type": "REELS", "upload_type": "resumable", "caption": meta["caption"],
            "share_to_feed": "true", "access_token": token,
        }))["id"]
        size = Path(video_path).stat().st_size
        with open(video_path, "rb") as f:
            _ok(httpx.post(f"{RUPLOAD}/{container}", content=f.read(), timeout=600, headers={
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
            }))
        for _ in range(60):  # Instagram processes the video before it can be published
            status = _ok(httpx.get(f"{GRAPH}/{container}", params={"fields": "status_code,status", "access_token": token}))
            if status.get("status_code") == "FINISHED":
                break
            if status.get("status_code") == "ERROR":
                raise RuntimeError(f"Instagram processing failed: {status.get('status')}")
            time.sleep(5)
        else:
            raise RuntimeError("Instagram processing timed out")
        media_id = _ok(httpx.post(f"{GRAPH}/{user}/media_publish", params={"creation_id": container, "access_token": token}))["id"]
        link = _ok(httpx.get(f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token})).get("permalink", "")
        return PostResult(platform_post_id=media_id, url=link)

    def stats(self, post_ids: list[str], account: dict) -> dict[str, dict]:
        out = {}
        for pid in post_ids:
            try:
                data = _ok(httpx.get(f"{GRAPH}/{pid}/insights", params={
                    "metric": "views,likes,comments,shares", "access_token": _token(account)}))
                vals = {m["name"]: m["values"][0]["value"] for m in data.get("data", [])}
                out[pid] = {k: vals.get(k) for k in ("views", "likes", "comments", "shares")}
            except Exception:
                continue
        return out
