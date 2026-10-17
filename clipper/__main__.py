"""Command line.  Run `python -m clipper --help` (or `./clip --help`) to see everything."""

import argparse
import logging
import sys

from . import db


def _secs(v: str | None) -> float | None:
    if v is None:
        return None
    total = 0.0
    for part in v.split(":"):
        total = total * 60 + float(part)
    return total


def main(argv=None):
    p = argparse.ArgumentParser(prog="clipper", description="Automated clipping pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("clip", help="Clip one video now (link or local file) and wait for the results")
    s.add_argument("url")
    s.add_argument("--start", help="only use the video from here (e.g. 20:00)")
    s.add_argument("--end", help="…to here (e.g. 45:00)")
    s.add_argument("--campaign", help="campaign id from config/campaigns.yaml")

    s = sub.add_parser("add", help="Queue a video for the background pipeline")
    s.add_argument("url")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--campaign")

    sub.add_parser("process", help="Run all pending pipeline steps")
    s = sub.add_parser("provided", help="Queue a campaign's ready-made clips (files/links, or its inbox folder)")
    s.add_argument("campaign", help="campaign id from config/campaigns.yaml")
    s.add_argument("items", nargs="*", help="video files or links (default: data/inbox/<campaign>/)")
    sub.add_parser("discover", help="Look for new videos (campaigns + Twitch)")
    sub.add_parser("schedule", help="Give approved clips posting slots")
    sub.add_parser("publish", help="Publish posts that are due")
    sub.add_parser("metrics", help="Pull views/likes for posted clips")
    sub.add_parser("check", help="Check setup and API keys")
    sub.add_parser("cleanup", help="Delete old raw downloads")

    s = sub.add_parser("run", help="Run everything on autopilot + the review app")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open the review app from your phone")
    s.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("serve", help="Only the review app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    s = sub.add_parser("auth", help="Connect a posting account")
    s.add_argument("platform", choices=["youtube", "tiktok"])
    s.add_argument("account_id")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "LiteLLM", "faster_whisper", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    db.init()

    if a.cmd in ("clip", "add"):
        from .pipeline import add_source, process

        s0, s1 = _secs(a.start), _secs(a.end)
        section = (s0, s1) if s0 is not None and s1 is not None else None
        sid = add_source(a.url, kind="campaign" if a.campaign else "url", campaign_id=a.campaign, section=section)
        print(f"Queued source #{sid}")
        if a.cmd == "clip":
            process(max_items=100)
            clips = db.rows("SELECT c.video_path, m.score FROM clips c JOIN moments m ON m.id = c.moment_id "
                            "WHERE m.source_id = ? ORDER BY m.score DESC", sid)
            src = db.row("SELECT status, error FROM sources WHERE id = ?", sid)
            if src["status"] == "failed":
                print(f"❌ Failed: {src['error']}")
                return 1
            print(f"\n{len(clips)} clips ready for review:")
            for c in clips:
                print(f"  [{c['score']}] {c['video_path']}")
    elif a.cmd == "process":
        from .pipeline import process

        print(f"{process(max_items=500)} steps done")
    elif a.cmd == "provided":
        from .provided import INBOX, ingest

        made = ingest(a.campaign, a.items or None)
        print(f"{len(made)} new clip(s) ready for review" if made else
              f"Nothing new. Drop the campaign's clips in {INBOX / a.campaign}/ or pass files/links.")
    elif a.cmd == "discover":
        from .discover import discover_all

        discover_all()
    elif a.cmd == "schedule":
        from .scheduler import schedule_approved

        print(f"{schedule_approved()} posts scheduled")
    elif a.cmd == "publish":
        from .scheduler import publish_due

        print(f"{publish_due()} posts published/exported")
    elif a.cmd == "metrics":
        from .metrics import collect

        collect()
    elif a.cmd == "check":
        from .check import run

        return 0 if run() else 1
    elif a.cmd == "cleanup":
        from .pipeline import cleanup_raw

        cleanup_raw()
    elif a.cmd == "auth":
        if a.platform == "youtube":
            from .publish.youtube import authorize
        else:
            from .publish.tiktok import authorize
        authorize(a.account_id)
    elif a.cmd in ("run", "serve"):
        import uvicorn

        if a.cmd == "run":
            from .autopilot import start

            start()
        print(f"\nReview app: http://{'localhost' if a.host == '127.0.0.1' else a.host}:{a.port}\n")
        uvicorn.run("clipper.web.app:app", host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
