import importlib


def test_init_is_idempotent_and_records_schema_version(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)

    db.init()
    db.init()
    with db.conn() as c:
        assert c.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    assert db.database_stats()["bytes"] > 0


def test_delete_book_removes_generated_epub(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    bid = db.create_book("Title", "Author", "book.pdf", "watercolor", 200, "5",
                         "application/pdf", b"%PDF-test")
    path = db.epub_path(bid)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"epub")

    db.delete_book(bid)
    assert db.get_book(bid) is None
    assert not path.exists()


def test_jobs_are_marked_interrupted_on_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    bid = db.create_book("Title", "", "book.pdf", "watercolor", 200, "5",
                         "application/pdf", b"%PDF-test")
    db.job_start(bid, "process", 123)
    assert db.interrupt_running_jobs() == 1
    job = db.jobs_for_book(bid)[0]
    assert job["status"] == "interrupted"
    assert job["detail"] == "server restarted"


def test_library_orders_by_last_read_then_upload(tmp_path, monkeypatch):
    """The hub lists the book you were most recently reading first, on every
    device, because the order comes from the server-side progress row. A book
    nobody has opened falls back to its upload time so a new import is at the
    top until something else is read."""
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    def mk(title):
        return db.create_book(title, "", "b.pdf", "watercolor", 200, "5",
                              "application/pdf", b"%PDF-test")
    older, newer, unread = mk("older"), mk("newer"), mk("unread")
    with db.conn() as c:                    # deterministic upload times
        for i, bid in enumerate((older, newer, unread)):
            c.execute("UPDATE books SET created_at=? WHERE id=?", (100 + i, bid))

    assert [b["title"] for b in db.list_books()] == ["unread", "newer", "older"]

    db.set_progress(older, 3)               # read the oldest upload -> it leads
    books = db.list_books()
    assert [b["title"] for b in books] == ["older", "unread", "newer"]
    assert books[0]["read_at"] > 0 and books[1]["read_at"] is None

    db.set_progress(newer, 1)               # another device reads a different book
    assert [b["title"] for b in db.list_books()] == ["newer", "older", "unread"]
