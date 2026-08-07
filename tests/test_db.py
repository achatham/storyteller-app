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
