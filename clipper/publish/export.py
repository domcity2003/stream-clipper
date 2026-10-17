"""Manual mode: a ready-to-post folder per clip/platform (video + caption.txt).

Works with zero API setup — AirDrop the folder to your phone, paste the caption, post.
Also the automatic fallback whenever a platform's API isn't connected yet.
"""

import shutil
from datetime import datetime

from ..config import EXPORT
from .base import PostResult, Publisher


class ExportPublisher(Publisher):
    platform = "export"

    def ready(self, account: dict) -> bool:
        return True

    def publish(self, video_path: str, meta: dict, account: dict) -> PostResult:
        day = datetime.now().strftime("%Y-%m-%d")
        folder = EXPORT / day / f"{account['id']}__{meta.get('clip_id', 'clip')}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, folder / "video.mp4")
        lines = [f"PLATFORM: {account['platform']}   ACCOUNT: {account.get('handle') or account['id']}", ""]
        if meta.get("title"):
            lines += ["TITLE:", meta["title"], ""]
        if meta.get("caption"):
            lines += ["CAPTION:", meta["caption"], ""]
        if meta.get("description"):
            lines += ["DESCRIPTION:", meta["description"], ""]
        (folder / "caption.txt").write_text("\n".join(lines))
        return PostResult(platform_post_id=str(folder), url=str(folder), status="exported")
