import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db
    importlib.reload(webapp.db)
    import webapp.srs
    importlib.reload(webapp.srs)
    import webapp.server as server
    server = importlib.reload(server)
    server.db.init()
    bid = server.db.create_book("B", "", "b.pdf", "watercolor", 200, "application/pdf", b"%PDF-")
    return server, bid


def test_first_grade_schedules_in_days_not_minutes(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    knew = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    missed = server.api_vocab_add(bid, {"word": "cutlass"})["id"]
    a = server.api_vocab_review(knew, {"rating": "good"})
    b = server.api_vocab_review(missed, {"rating": "again"})
    assert a["interval_days"] >= 1.5 and a["reviews"] == 1
    assert 0.9 <= b["interval_days"] < a["interval_days"]


def test_intervals_grow_when_known_and_shrink_on_a_miss(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    srs = server.srs
    vid = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    now = datetime(2026, 11, 14, 19, tzinfo=timezone.utc)
    gaps = []
    for rating in ("good", "good", "good", "again"):
        out = srs.review(vid, rating, now)
        gaps.append(out["interval_days"])
        now = datetime.fromtimestamp(out["due_at"], timezone.utc) + timedelta(hours=1)
    assert gaps[0] < gaps[1] < gaps[2] and gaps[3] < gaps[2]
    w = server.db.vocab_get(vid)
    assert w["reviews"] == 4 and w["srs"] and w["due_at"] == out["due_at"]
    with server.db.conn() as c:
        ratings = [r["rating"] for r in c.execute(
            "SELECT rating FROM vocab_reviews WHERE vocab_id=? ORDER BY id", (vid,))]
    assert ratings == [3, 3, 3, 1]          # FSRS Good, Good, Good, Again


def test_each_grade_schedules_further_out_than_the_last(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    days = {}
    for rating in ("again", "hard", "good", "easy"):
        vid = server.api_vocab_add(bid, {"word": f"w{rating}"})["id"]
        days[rating] = server.api_vocab_review(vid, {"rating": rating})["interval_days"]
    assert days["again"] <= days["hard"] < days["good"] < days["easy"]


def test_easy_brings_difficulty_back_down_after_a_miss(monkeypatch, tmp_path):
    # With only again/good a missed word stays at its raised difficulty for good;
    # that's why all four buttons exist.
    server, bid = setup(monkeypatch, tmp_path)
    srs = server.srs
    a = server.api_vocab_add(bid, {"word": "a"})["id"]
    b = server.api_vocab_add(bid, {"word": "b"})["id"]
    now = datetime(2026, 11, 14, 19, tzinfo=timezone.utc)
    da = srs.review(a, "again", now)["difficulty"]
    db_ = srs.review(b, "again", now)["difficulty"]
    later = now + timedelta(days=2)
    assert srs.review(a, "good", later)["difficulty"] == pytest.approx(da, abs=0.05)
    assert srs.review(b, "easy", later)["difficulty"] < db_ - 1


def test_old_knew_flag_still_grades(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    vid = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    assert server.api_vocab_review(vid, {"knew": True})["interval_days"] >= 1.5
    with server.db.conn() as c:
        assert c.execute("SELECT rating FROM vocab_reviews").fetchone()[0] == 3


def test_review_validates_input_and_history_goes_with_the_word(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    vid = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    with pytest.raises(HTTPException) as e:
        server.api_vocab_review(vid, {"rating": "meh"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        server.api_vocab_review(9999, {"rating": "good"})
    assert e.value.status_code == 404
    server.api_vocab_review(vid, {"rating": "good"})
    server.api_vocab_delete(vid)
    with server.db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM vocab_reviews").fetchone()[0] == 0


def test_existing_vocab_table_gains_srs_columns(monkeypatch, tmp_path):
    import sqlite3
    path = tmp_path / "storyteller.db"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE vocab (id INTEGER PRIMARY KEY, book_id INTEGER, word TEXT, "
                    "context TEXT, page INTEGER, created_at REAL, last_seen_at REAL, "
                    "seen_count INTEGER DEFAULT 1, UNIQUE (book_id, word));")
    c.close()
    server, _ = setup(monkeypatch, tmp_path)
    with server.db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(vocab)")}
    assert {"srs", "due_at", "reviewed_at", "reviews"} <= cols
