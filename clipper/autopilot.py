"""The background schedule when running `python -m clipper run`."""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import discover, metrics, pipeline, provided, scheduler

log = logging.getLogger(__name__)


def start() -> BackgroundScheduler:
    s = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
    s.add_job(discover.discover_all, "interval", minutes=60, next_run_time=datetime.now(), id="discover")
    s.add_job(pipeline.process, "interval", minutes=2, next_run_time=datetime.now(), id="process")
    s.add_job(provided.ingest_all, "interval", minutes=5, next_run_time=datetime.now(), id="provided")
    s.add_job(scheduler.schedule_approved, "interval", minutes=1, id="schedule")
    s.add_job(scheduler.publish_due, "interval", minutes=1, id="publish")
    s.add_job(metrics.collect, "interval", hours=6, id="metrics")
    s.add_job(pipeline.cleanup_raw, "interval", hours=24, id="cleanup")
    s.start()
    log.info("Autopilot started: discover hourly, process every 2 min, publish every minute, stats every 6h")
    return s
