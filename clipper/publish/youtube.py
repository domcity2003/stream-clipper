"""YouTube Shorts via the official YouTube Data API v3.

Setup (one time): Google Cloud project → enable "YouTube Data API v3" → OAuth consent screen →
OAuth client ID (Desktop app) → download JSON to config/youtube_client_secret.json.
Then: `python -m clipper auth youtube <account_id>` opens a browser to sign in to the channel.

Note: until the Google project passes YouTube's API audit, uploads are forced to PRIVATE.
Default quota ≈ 6 uploads/day per project.
"""

import os
from pathlib import Path

from ..config import ROOT, TOKENS
from .base import PostResult, Publisher

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def _secrets_file() -> Path:
    return ROOT / os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "config/youtube_client_secret.json")


def _token_file(account_id: str) -> Path:
    return TOKENS / f"youtube_{account_id}.json"


def authorize(account_id: str):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(_secrets_file()), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _token_file(account_id).write_text(creds.to_json())
    print(f"Saved YouTube login for {account_id}")


def _client(account_id: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = _token_file(account_id)
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


class YouTubePublisher(Publisher):
    platform = "youtube"

    def ready(self, account: dict) -> bool:
        return _token_file(account["id"]).exists()

    def publish(self, video_path: str, meta: dict, account: dict) -> PostResult:
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta.get("tags", [])[:15],
                        "categoryId": "24"},  # Entertainment
            "status": {"privacyStatus": account.get("privacy", "public"), "selfDeclaredMadeForKids": False},
        }
        request = _client(account["id"]).videos().insert(
            part="snippet,status", body=body, media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True)
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        vid = response["id"]
        return PostResult(platform_post_id=vid, url=f"https://youtube.com/shorts/{vid}")

    def stats(self, post_ids: list[str], account: dict) -> dict[str, dict]:
        out = {}
        yt = _client(account["id"])
        for i in range(0, len(post_ids), 50):
            resp = yt.videos().list(part="statistics", id=",".join(post_ids[i : i + 50])).execute()
            for item in resp.get("items", []):
                s = item["statistics"]
                out[item["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                   "comments": int(s.get("commentCount", 0)), "shares": None}
        return out
