from datetime import datetime, timezone

import pytest

from clipper import db
from clipper.brain.copywrite import _clean_tags, fallback, format_for
from clipper.brain.moments import dedupe, snap
from clipper.config import CLIP_MAX_SEC
from clipper.brain.signals import starts_sentence, text_features
from clipper.render.captions import chunk_words


def words(text, start=0.0, step=0.3):
    return [{"w": w, "s": round(start + i * step, 2), "e": round(start + i * step + 0.25, 2)} for i, w in enumerate(text.split())]


# ---------- captions ----------

def test_chunks_break_on_punctuation_and_size():
    chunks = chunk_words(words("I paid ten thousand dollars. Nobody believed me at all"))
    texts = [" ".join(w["w"] for w in c) for c in chunks]
    i = next(k for k, t in enumerate(texts) if t.endswith("dollars."))  # sentence end closes a chunk
    assert texts[i + 1].startswith("Nobody")
    assert all(len(c) <= 3 for c in chunks)


def test_chunks_break_on_pause():
    ws = words("hello there") + words("friend", start=3.0)
    assert len(chunk_words(ws)) == 2


# ---------- moments ----------

def transcript(n_words=200):
    return {"words": words(" ".join(f"w{i}" for i in range(n_words)), step=0.4), "segments": []}


def test_snap_trims_to_words_and_enforces_min_length():
    t = transcript()
    m = snap({"start": 10.0, "end": 40.0, "score": 80}, t)
    assert m and 9.3 <= m["start"] <= 10.0 and m["end"] <= 40.9  # ≤0.15s lead-in before the first word
    assert snap({"start": 10.0, "end": 14.0, "score": 80}, t) is None  # too short


def test_snap_caps_max_length():
    m = snap({"start": 0.0, "end": CLIP_MAX_SEC + 30, "score": 80}, transcript(400))
    assert m["end"] - m["start"] <= CLIP_MAX_SEC


def test_dedupe_drops_overlaps_keeps_best():
    ms = [{"start": 0, "end": 30, "score": 70}, {"start": 5, "end": 35, "score": 90}, {"start": 40, "end": 70, "score": 60}]
    kept = dedupe(ms, 5)
    assert [m["score"] for m in kept] == [90, 60]


# ---------- signals ----------

def test_sentence_start_detection():
    assert starts_sentence("How do I tip?", "That was crazy.")
    assert not starts_sentence("to learn as you get more famous.", "It's something you have")
    assert not starts_sentence("and then he left", None)


def test_ad_reads_detected():
    assert text_features("Use code THEO for a deposit match on PrizePicks")["ad_read"]
    assert not text_features("I gave away a million dollars")["ad_read"]


# ---------- copy ----------

def test_hashtags_cleaned_required_first_and_blocklisted():
    tags = _clean_tags(["viral", "#f4f", "#Podcast", "#podcast"], required=["#ad"], blocklist=["#f4f"])
    assert tags[0] == "#ad" and "#f4f" not in tags and tags.count("#Podcast") + tags.count("#podcast") == 1


def test_youtube_title_gets_shorts_tag_and_credit():
    out = format_for("youtube", "Crazy story", "desc", ["#a"], "MrBeast", "https://y.t/x")
    assert out["title"].endswith("#shorts") and "MrBeast" in out["description"]


def test_fallback_copy_has_three_titles():
    c = fallback("I spent a million dollars on this. Nobody believed me.", "MrBeast", "I spent a million dollars", ["#a"])
    assert len(c.titles) == 3 and len(c.hook_text.split()) <= 8


# ---------- scheduling ----------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()


def test_next_slot_respects_cap_and_order(temp_db):
    from clipper.scheduler import next_slot

    acc = {"id": "a", "timezone": "UTC", "slots": ["09:00", "15:00"], "daily_cap": 1}
    after = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    first = next_slot(acc, after)
    assert first == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    src = db.insert("sources", url="u", kind="url")
    mom = db.insert("moments", source_id=src, start=0, end=20, score=80)
    clip = db.insert("clips", moment_id=mom, video_path="x")
    db.insert("posts", clip_id=clip, account_id="a", platform="youtube", scheduled_at=first.isoformat(timespec="seconds"))
    # daily cap 1 is used up → next free slot is tomorrow morning, not 15:00 today
    assert next_slot(acc, after) == datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)


def test_jump_cuts_drop_fillers_pauses_and_dead_air():
    from clipper.render.tighten import keep_intervals, remap

    ws = [{"w": "hi", "s": 0.5, "e": 0.9}, {"w": "um", "s": 1.0, "e": 1.3},
          {"w": "there", "s": 1.4, "e": 1.8}, {"w": "friend", "s": 3.0, "e": 3.4}]
    spans = keep_intervals(ws, 5.0, [(2.0, 2.9)])
    assert spans[0][0] > 0.3 and spans[-1][1] < 3.6          # leading/trailing dead air trimmed
    assert not any(a < 1.15 < b for a, b in spans)            # "um" cut
    assert not any(a < 2.5 < b for a, b in spans)             # pause cut
    assert remap(3.0, spans) < 1.6                            # later words move earlier
