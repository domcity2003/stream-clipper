"""Pull views/likes for posted clips, and summarize what's working (the learning loop)."""

import logging
from collections import defaultdict
from datetime import datetime

from . import db
from .config import accounts
from .publish.base import get as get_publisher

log = logging.getLogger(__name__)


def collect() -> int:
    db.init()
    accs = {a["id"]: a for a in accounts()}
    posts = db.rows("SELECT * FROM posts WHERE status = 'posted' AND platform_post_id IS NOT NULL")
    by_acc = defaultdict(list)
    for p in posts:
        by_acc[p["account_id"]].append(p)
    saved = 0
    for acc_id, items in by_acc.items():
        acc = accs.get(acc_id)
        if not acc:
            continue
        publisher = get_publisher(acc["platform"])
        if not publisher.ready(acc):
            continue
        try:
            stats = publisher.stats([p["platform_post_id"] for p in items], acc)
        except Exception as e:
            log.warning("Stats for %s failed: %s", acc_id, e)
            continue
        for p in items:
            s = stats.get(p["platform_post_id"])
            if s:
                db.insert("metrics", post_id=p["id"], captured_at=db.now(), **s)
                saved += 1
    log.info("Saved %d metric snapshots", saved)
    return saved


def summary() -> dict:
    """Latest views per post, rolled up by the things we can change: creator, moment type, hook style, hour."""
    latest = db.rows("""
        SELECT p.id, p.platform, p.posted_at, c.id AS clip_id, c.duration, m.moment_type, m.score, m.scorer,
               s.streamer, s.campaign_id, c.chosen_title,
               (SELECT views FROM metrics x WHERE x.post_id = p.id ORDER BY captured_at DESC LIMIT 1) AS views
        FROM posts p JOIN clips c ON c.id = p.clip_id JOIN moments m ON m.id = c.moment_id JOIN sources s ON s.id = m.source_id
        WHERE p.status = 'posted'
    """)
    title_styles = {0: "curiosity", 1: "quote", 2: "stakes"}

    def rollup(key):
        agg = defaultdict(lambda: {"posts": 0, "views": 0})
        for r in latest:
            k = key(r)
            agg[k]["posts"] += 1
            agg[k]["views"] += r["views"] or 0
        return sorted(({"key": k, **v, "avg": v["views"] // max(v["posts"], 1)} for k, v in agg.items()),
                      key=lambda x: x["avg"], reverse=True)

    return {
        "total_posts": len(latest),
        "total_views": sum(r["views"] or 0 for r in latest),
        "by_creator": rollup(lambda r: r["streamer"] or "?"),
        "by_moment_type": rollup(lambda r: r["moment_type"] or "?"),
        "by_title_style": rollup(lambda r: title_styles.get(r["chosen_title"] or 0, "?")),
        "by_platform": rollup(lambda r: r["platform"]),
        "by_hour": rollup(lambda r: datetime.fromisoformat(r["posted_at"]).hour if r["posted_at"] else "?"),
        "top_clips": sorted(latest, key=lambda r: r["views"] or 0, reverse=True)[:10],
    }
