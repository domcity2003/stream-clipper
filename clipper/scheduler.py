"""Turns approved clips into scheduled posts, and publishes posts when their time comes."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .brain.copywrite import format_for
from .config import accounts, campaign
from .publish.base import get as get_publisher
from .publish.export import ExportPublisher

log = logging.getLogger(__name__)


def _targets(clip: dict) -> list[dict]:
    """Which accounts get this clip: the campaign's accounts, else accounts in the source's niche, else all."""
    src = db.row("SELECT s.* FROM sources s JOIN moments m ON m.source_id = s.id WHERE m.id = ?", clip["moment_id"])
    camp = campaign(src["campaign_id"])
    accs = accounts()
    if camp and camp.get("accounts"):
        return [a for a in accs if a["id"] in camp["accounts"]]
    return accs


def next_slot(account: dict, after: datetime) -> datetime:
    """Earliest free slot for this account after `after`, respecting slot times and daily_cap."""
    tz = ZoneInfo(account.get("timezone", "UTC"))
    taken = {p["scheduled_at"] for p in db.rows("SELECT scheduled_at FROM posts WHERE account_id = ?", account["id"])}
    per_day: dict[str, int] = {}
    for t in taken:
        d = datetime.fromisoformat(t).astimezone(tz).date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
    cap = account.get("daily_cap", 3)
    day = after.astimezone(tz).date()
    for _ in range(60):
        if per_day.get(day.isoformat(), 0) < cap:
            for hhmm in sorted(account.get("slots", ["12:00"])):
                h, m = map(int, hhmm.split(":"))
                slot = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                iso = slot.astimezone(timezone.utc).isoformat(timespec="seconds")
                if slot > after and iso not in taken:
                    return slot.astimezone(timezone.utc)
        day += timedelta(days=1)
    raise RuntimeError(f"No free slot in the next 60 days for {account['id']}")


def schedule_approved() -> int:
    db.init()
    n = 0
    now = datetime.now(timezone.utc)
    for clip in db.rows("SELECT * FROM clips WHERE status = 'approved' ORDER BY id"):
        for acc in _targets(clip):
            if db.row("SELECT 1 FROM posts WHERE clip_id = ? AND account_id = ?", clip["id"], acc["id"]):
                continue
            slot = next_slot(acc, now)
            db.insert("posts", clip_id=clip["id"], account_id=acc["id"], platform=acc["platform"],
                      scheduled_at=slot.isoformat(timespec="seconds"))
            n += 1
        db.update("clips", clip["id"], status="scheduled")
    if n:
        log.info("Scheduled %d posts", n)
    return n


def _meta(post: dict) -> dict:
    clip = db.clip_copy(db.row("SELECT * FROM clips WHERE id = ?", post["clip_id"]))
    src = db.row("SELECT s.* FROM sources s JOIN moments m ON m.source_id = s.id WHERE m.id = ?", clip["moment_id"])
    camp = campaign(src["campaign_id"])
    title = clip["titles"][clip["chosen_title"] or 0] if clip["titles"] else ""
    creator = (camp or {}).get("creator") or src["streamer"] or ""
    meta = format_for(post["platform"], title, clip["description"] or "", clip["hashtags"], creator,
                      source_url=src["url"] if src["kind"] != "file" else "", campaign=camp)
    return meta | {"clip_id": clip["id"], "video_path": clip["video_path"]}


def publish_due(now: datetime | None = None) -> int:
    """Publish every scheduled post whose time has come. Falls back to a ready-to-post export
    folder when that platform isn't connected yet — nothing is ever silently dropped."""
    db.init()
    now = now or datetime.now(timezone.utc)
    accs = {a["id"]: a for a in accounts()}
    done = 0
    for post in db.rows("SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_at"):
        if datetime.fromisoformat(post["scheduled_at"]) > now:
            continue
        acc = accs.get(post["account_id"])
        if not acc:
            db.update("posts", post["id"], status="failed", error="account no longer in accounts.yaml")
            continue
        meta = _meta(post)
        publisher = get_publisher(acc["platform"])
        if not publisher.ready(acc):
            publisher = ExportPublisher()
        try:
            res = publisher.publish(meta["video_path"], meta, acc)
            db.update("posts", post["id"], status=res.status, platform_post_id=res.platform_post_id, url=res.url,
                      posted_at=db.now())
            done += 1
            log.info("Post %s → %s (%s) %s", post["id"], acc["id"], res.status, res.url)
        except Exception as e:
            log.exception("Publishing post %s failed", post["id"])
            db.update("posts", post["id"], status="failed", error=str(e)[:500])
    posted_clips = db.rows(
        "SELECT clip_id FROM posts GROUP BY clip_id HAVING SUM(status = 'scheduled') = 0 AND SUM(status IN ('posted','exported')) > 0"
    )
    for r in posted_clips:
        db.execute("UPDATE clips SET status = 'posted' WHERE id = ? AND status = 'scheduled'", r["clip_id"])
    return done
