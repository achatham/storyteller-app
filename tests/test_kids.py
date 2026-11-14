import importlib
import sqlite3

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
    book = lambda t: server.db.create_book(t, "", "b.pdf", "watercolor", 200,  # noqa: E731
                                           "application/pdf", b"%PDF-")
    return server, book


def test_words_follow_the_books_default_kid_unless_told_otherwise(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": " Ada "})["id"]
    ben = server.api_kid_add({"name": "Ben"})["id"]
    mine, shared = book("Ada's book"), book("Read together")
    server.api_book_kid(mine, {"kid_id": ada})

    assert server.api_vocab_add(mine, {"word": "gruel"})["kid_id"] == ada
    assert server.api_vocab_add(shared, {"word": "cutlass"})["kid_id"] is None
    assert server.api_vocab_add(mine, {"word": "spyglass", "kid_id": ben})["kid_name"] == "Ben"
    assert server.api_vocab_add(mine, {"word": "brig", "kid_id": None})["kid_id"] is None
    assert server.api_book(mine)["kid_id"] == ada
    with pytest.raises(HTTPException) as e:
        server.api_vocab_add(mine, {"word": "x", "kid_id": 999})
    assert e.value.status_code == 404


def test_a_kid_has_one_card_per_word_across_books(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": "Ada"})["id"]
    ben = server.api_kid_add({"name": "Ben"})["id"]
    b1, b2 = book("One"), book("Two")
    first = server.api_vocab_add(b1, {"word": "melancholy", "context": "first", "kid_id": ada})
    again = server.api_vocab_add(b2, {"word": "melancholy", "context": "second", "kid_id": ada})
    assert first["new"] and not again["new"] and again["id"] == first["id"]
    assert again["context"] == "first" and again["seen_count"] == 2
    assert server.api_vocab_add(b2, {"word": "melancholy", "kid_id": ben})["new"]  # Ben's own


def test_assigning_merges_into_a_kids_existing_word(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": "Ada"})["id"]
    b1, b2 = book("One"), book("Two")
    hers = server.api_vocab_add(b1, {"word": "gruel", "kid_id": ada})["id"]
    loose = server.api_vocab_add(b2, {"word": "gruel", "context": "shared book"})["id"]
    server.api_vocab_review(loose, {"rating": "easy"})       # graded before sorting

    out = server.api_vocab_assign(loose, {"kid_id": ada})
    assert out["merged"] and out["id"] == hers and out["seen_count"] == 2
    assert out["reviews"] == 1 and out["due_at"]             # took over the card
    assert server.db.vocab_get(loose) is None
    with server.db.conn() as c:
        assert c.execute("SELECT vocab_id FROM vocab_reviews").fetchone()[0] == hers

    moved = server.api_vocab_add(b2, {"word": "brig"})["id"]
    out = server.api_vocab_assign(moved, {"kid_id": ada})
    assert not out["merged"] and out["id"] == moved and out["kid_id"] == ada


def test_setting_a_books_kid_sorts_its_unassigned_words(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": "Ada"})["id"]
    ben = server.api_kid_add({"name": "Ben"})["id"]
    b = book("B")
    server.api_vocab_add(b, {"word": "gruel"})
    server.api_vocab_add(b, {"word": "brig"})
    server.api_vocab_add(b, {"word": "cutlass", "kid_id": ben})
    assert server.api_book_kid(b, {"kid_id": ada})["assigned"] == 2
    kids = {w["word"]: w["kid_name"] for w in server.api_vocab_list()}
    assert kids == {"gruel": "Ada", "brig": "Ada", "cutlass": "Ben"}   # Ben's stays his
    assert server.api_book_kid(b, {"kid_id": None})["assigned"] == 0
    assert server.api_book(b)["kid_id"] is None


def test_reader_highlights_the_books_words_and_its_kids_words(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": "Ada"})["id"]
    ben = server.api_kid_add({"name": "Ben"})["id"]
    old, new, other = book("Old"), book("New"), book("Ben's")
    server.api_vocab_add(old, {"word": "gruel", "kid_id": ada})
    server.api_vocab_add(other, {"word": "brig", "kid_id": ben})
    server.api_vocab_add(new, {"word": "cutlass"})
    server.api_book_kid(new, {"kid_id": ada})
    r = server.api_book_vocab(new)
    assert r["kid_id"] == ada and [k["name"] for k in r["kids"]] == ["Ada", "Ben"]
    assert sorted(r["words"]) == ["cutlass", "gruel"]


def test_deleting_kids_and_books(monkeypatch, tmp_path):
    server, book = setup(monkeypatch, tmp_path)
    ada = server.api_kid_add({"name": "Ada"})["id"]
    ben = server.api_kid_add({"name": "Ben"})["id"]
    b = book("B")
    server.api_book_kid(b, {"kid_id": ben})
    keep = server.api_vocab_add(b, {"word": "gruel", "kid_id": ada})["id"]
    server.api_vocab_add(b, {"word": "brig", "kid_id": None})
    server.api_vocab_add(b, {"word": "cutlass"})               # Ben's

    server.api_kid_delete(ben)                                 # his words go with him
    assert server.api_book(b)["kid_id"] is None
    assert {w["word"] for w in server.api_vocab_list()} == {"gruel", "brig"}

    server.db.delete_book(b)                                   # Ada keeps hers
    rows = server.api_vocab_list()
    assert [(w["id"], w["book_id"], w["title"]) for w in rows] == [(keep, None, None)]
    with pytest.raises(HTTPException):
        server.api_kid_add({"name": "  "})


def test_migrates_a_pre_kids_vocab_table_keeping_reviews(monkeypatch, tmp_path):
    path = tmp_path / "storyteller.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
        INSERT INTO books VALUES (1, 'B'), (2, 'C');
        CREATE TABLE vocab (id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER REFERENCES books(id) ON DELETE CASCADE, word TEXT NOT NULL,
            context TEXT, page INTEGER, created_at REAL, last_seen_at REAL,
            seen_count INTEGER DEFAULT 1, srs TEXT, due_at REAL, reviewed_at REAL,
            reviews INTEGER DEFAULT 0, UNIQUE (book_id, word));
        CREATE TABLE vocab_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,
            vocab_id INTEGER REFERENCES vocab(id) ON DELETE CASCADE, book_id INTEGER,
            rating INTEGER, reviewed_at REAL, log TEXT);
        INSERT INTO vocab(book_id, word, reviews) VALUES (1, 'gruel', 1), (2, 'gruel', 0);
        INSERT INTO vocab_reviews(vocab_id, rating) VALUES (1, 3);
    """)
    c.close()
    server, _ = setup(monkeypatch, tmp_path)
    with server.db.conn() as c:
        assert [tuple(r) for r in c.execute("SELECT id, book_id, kid_id, word, reviews FROM vocab")] \
            == [(1, 1, None, "gruel", 1), (2, 2, None, "gruel", 0)]
        assert [tuple(r) for r in c.execute("SELECT vocab_id, rating FROM vocab_reviews")] == [(1, 3)]
    ada = server.api_kid_add({"name": "Ada"})["id"]
    out = server.api_vocab_assign(2, {"kid_id": ada})
    assert out["kid_id"] == ada
    assert server.api_vocab_assign(1, {"kid_id": ada})["id"] == 2   # same word -> one card
