"""Common interface for every place a clip can go."""

from dataclasses import dataclass


@dataclass
class PostResult:
    platform_post_id: str
    url: str = ""
    status: str = "posted"  # posted | exported


class Publisher:
    platform = ""

    def ready(self, account: dict) -> bool:
        """True if credentials for this account exist and it can post right now."""
        raise NotImplementedError

    def publish(self, video_path: str, meta: dict, account: dict) -> PostResult:
        """meta = output of brain.copywrite.format_for(platform, ...)"""
        raise NotImplementedError

    def stats(self, post_ids: list[str], account: dict) -> dict[str, dict]:
        """{post_id: {views, likes, comments, shares}} — best effort."""
        return {}


def get(platform: str) -> Publisher:
    from .export import ExportPublisher
    from .instagram import InstagramPublisher
    from .tiktok import TikTokPublisher
    from .youtube import YouTubePublisher

    return {"youtube": YouTubePublisher, "tiktok": TikTokPublisher, "instagram": InstagramPublisher}.get(
        platform, ExportPublisher
    )()
