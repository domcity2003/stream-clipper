"""SQLite storage. Each pipeline stage moves rows from one status to the next.

sources  new → audio → transcribed → scored        (or failed)
moments  new → rendered                            (or failed / skipped)
clips    review → approved | rejected → scheduled → posted | exported
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,              -- campaign | twitch_clip | url | file
    campaign_id TEXT,
    streamer TEXT,
    title TEXT,
    duration REAL,
    section_start REAL,              -- optional: only process this part of the video
    section_end REAL,
    audio_path TEXT,
    transcript_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (url, section_start, section_end)
);
CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    start REAL NOT NULL,
    end REAL NOT NULL,
    score INTEGER NOT NULL,
    moment_type TEXT,
    hook_line TEXT,
    reason TEXT,
    scorer TEXT,                     -- llm | heuristic
    status TEXT NOT NULL DEFAULT 'new',
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    moment_id INTEGER NOT NULL REFERENCES moments(id),
    video_path TEXT NOT NULL,
    thumb_path TEXT,
    duration REAL,
    layout TEXT,
    hook_text TEXT,
    titles TEXT,                     -- JSON list of title variants
    description TEXT,
    hashtags TEXT,                   -- JSON list
    chosen_title INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'review',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    account_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    posted_at TEXT,
    platform_post_id TEXT,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | posted | exported | failed
    error TEXT,
    UNIQUE (clip_id, account_id)
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    captured_at TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)


def rows(sql: str, *args) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def row(sql: str, *args) -> dict | None:
    found = rows(sql, *args)
    return found[0] if found else None


def execute(sql: str, *args) -> int:
    """Run a write and return lastrowid."""
    with connect() as c:
        return c.execute(sql, args).lastrowid


def insert(table: str, **values) -> int:
    if table in ("sources", "moments", "clips"):
        values.setdefault("created_at", now())
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *values.values())


def update(table: str, row_id: int, **values):
    sets = ", ".join(f"{k} = ?" for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", *values.values(), row_id)


def clip_copy(clip: dict) -> dict:
    """Decode the JSON columns of a clip row."""
    return {
        **clip,
        "titles": json.loads(clip.get("titles") or "[]"),
        "hashtags": json.loads(clip.get("hashtags") or "[]"),
    }
