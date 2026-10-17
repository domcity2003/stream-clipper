"""The review app: approve clips from your phone, add videos, see what's scheduled and what's working."""

import json
import threading

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import db, metrics, pipeline, scheduler
from ..config import OUT, campaigns
from pathlib import Path

app = FastAPI(title="Stream Clipper")
app.mount("/media", StaticFiles(directory=OUT), name="media")
env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
db.init()


def _page(name: str, **ctx) -> HTMLResponse:
    counts = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
    return HTMLResponse(env.get_template(name).render(counts=counts, **ctx))


def _kick_pipeline():
    threading.Thread(target=pipeline.process, daemon=True).start()


def _clip_rows(where: str, order: str = "m.score DESC, c.id DESC", *args) -> list[dict]:
    rows = db.rows(f"""
        SELECT c.*, m.score, m.moment_type, m.reason, m.scorer, m.start AS m_start, m.end AS m_end,
               s.title AS source_title, s.url AS source_url, s.streamer, s.campaign_id
        FROM clips c JOIN moments m ON m.id = c.moment_id JOIN sources s ON s.id = m.source_id
        WHERE {where} ORDER BY {order}""", *args)
    out = []
    for r in rows:
        r = db.clip_copy(r)
        r["file"] = Path(r["video_path"]).name
        r["thumb"] = Path(r["thumb_path"]).name if r["thumb_path"] else None
        out.append(r)
    return out


@app.get("/", response_class=HTMLResponse)
def review():
    return _page("review.html", clips=_clip_rows("c.status = 'review'"), tab="review")


@app.post("/clips/{clip_id}/approve")
def approve(clip_id: int, chosen_title: int = Form(0), title: str = Form(""), description: str = Form(""),
            hashtags: str = Form(""), hook_text: str = Form("")):
    clip = db.row("SELECT * FROM clips WHERE id = ?", clip_id)
    if not clip:
        raise HTTPException(404)
    titles = json.loads(clip["titles"] or "[]")
    if title.strip() and 0 <= chosen_title < len(titles):
        titles[chosen_title] = title.strip()  # your edit wins
    tags = [t if t.startswith("#") else f"#{t}" for t in hashtags.replace(",", " ").split()]
    db.update("clips", clip_id, status="approved", chosen_title=chosen_title, titles=json.dumps(titles),
              description=description.strip(), hashtags=json.dumps(tags))
    scheduler.schedule_approved()
    return {"ok": True}


@app.post("/clips/{clip_id}/reject")
def reject(clip_id: int):
    db.update("clips", clip_id, status="rejected")
    return {"ok": True}


@app.get("/scheduled", response_class=HTMLResponse)
def scheduled():
    posts = db.rows("""
        SELECT p.*, c.titles, c.chosen_title, c.thumb_path FROM posts p JOIN clips c ON c.id = p.clip_id
        ORDER BY CASE p.status WHEN 'scheduled' THEN 0 ELSE 1 END, p.scheduled_at DESC LIMIT 200""")
    for p in posts:
        titles = json.loads(p["titles"] or "[]")
        p["title"] = titles[p["chosen_title"] or 0] if titles else ""
        p["thumb"] = Path(p["thumb_path"]).name if p["thumb_path"] else None
    return _page("scheduled.html", posts=posts, tab="scheduled")


@app.post("/posts/publish-now")
def publish_now():
    threading.Thread(target=scheduler.publish_due, daemon=True).start()
    return RedirectResponse("/scheduled", status_code=303)


@app.get("/add", response_class=HTMLResponse)
def add_form():
    sources = db.rows("SELECT * FROM sources ORDER BY id DESC LIMIT 30")
    return _page("add.html", campaigns=[c for c in campaigns() if not c["id"].startswith("example")],
                 sources=sources, tab="add")


def _secs(v: str) -> float | None:
    """'1:02:30', '62:30' or '3750' → seconds."""
    v = v.strip()
    if not v:
        return None
    total = 0.0
    for part in v.split(":"):
        total = total * 60 + float(part)
    return total


@app.post("/add")
def add(url: str = Form(...), campaign_id: str = Form(""), start: str = Form(""), end: str = Form("")):
    s, e = _secs(start), _secs(end)
    pipeline.add_source(url.strip(), kind="campaign" if campaign_id else "url", campaign_id=campaign_id or None,
                        section=(s, e) if s is not None and e is not None else None)
    _kick_pipeline()
    return RedirectResponse("/add", status_code=303)


@app.post("/sources/{source_id}/retry")
def retry(source_id: int):
    src = db.row("SELECT * FROM sources WHERE id = ?", source_id)
    if src and src["status"] == "failed":
        status = "transcribed" if src["transcript_path"] else ("audio" if src["audio_path"] else "new")
        db.update("sources", source_id, status=status, error=None)
        db.execute("UPDATE moments SET status = 'new', error = NULL WHERE source_id = ? AND status = 'failed'", source_id)
        _kick_pipeline()
    return RedirectResponse("/add", status_code=303)


@app.get("/stats", response_class=HTMLResponse)
def stats():
    return _page("stats.html", s=metrics.summary(), tab="stats",
                 pipeline={
                     "sources": db.rows("SELECT status, COUNT(*) n FROM sources GROUP BY status"),
                     "moments": db.rows("SELECT status, COUNT(*) n FROM moments GROUP BY status"),
                     "posts": db.rows("SELECT status, COUNT(*) n FROM posts GROUP BY status"),
                 })
