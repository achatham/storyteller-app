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
    a = server.api_vocab_review(knew, {"knew": True})
    b = server.api_vocab_review(missed, {"knew": False})
    assert a["interval_days"] >= 1.5 and a["reviews"] == 1
    assert 0.9 <= b["interval_days"] < a["interval_days"]


def test_intervals_grow_when_known_and_shrink_on_a_miss(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    srs = server.srs
    vid = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    now = datetime(2026, 11, 14, 19, tzinfo=timezone.utc)
    gaps = []
    for knew in (True, True, True, False):
        out = srs.review(vid, knew, now)
        gaps.append(out["interval_days"])
        now = datetime.fromtimestamp(out["due_at"], timezone.utc) + timedelta(hours=1)
    assert gaps[0] < gaps[1] < gaps[2] and gaps[3] < gaps[2]
    w = server.db.vocab_get(vid)
    assert w["reviews"] == 4 and w["srs"] and w["due_at"] == out["due_at"]
    with server.db.conn() as c:
        ratings = [r["rating"] for r in c.execute(
            "SELECT rating FROM vocab_reviews WHERE vocab_id=? ORDER BY id", (vid,))]
    assert ratings == [3, 3, 3, 1]          # FSRS Good, Good, Good, Again


def test_review_validates_input_and_history_goes_with_the_word(monkeypatch, tmp_path):
    server, bid = setup(monkeypatch, tmp_path)
    vid = server.api_vocab_add(bid, {"word": "gruel"})["id"]
    with pytest.raises(HTTPException) as e:
        server.api_vocab_review(vid, {"knew": "yes"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        server.api_vocab_review(9999, {"knew": True})
    assert e.value.status_code == 404
    server.api_vocab_review(vid, {"knew": True})
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
