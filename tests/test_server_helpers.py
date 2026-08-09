import importlib
import io
import zipfile

import pytest
from fastapi import HTTPException


def server(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    # db caches its path at import, so reload it first -- otherwise every test in
    # this file shares whichever database the first one happened to create.
    import webapp.db
    importlib.reload(webapp.db)
    import webapp.server as module
    return importlib.reload(module)


def epub_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", "<container/>")
    return out.getvalue()


def test_valid_upload_requires_matching_signature(monkeypatch, tmp_path):
    module = server(monkeypatch, tmp_path)
    assert module._valid_upload("book.pdf", b"%PDF-1.7\n") == "application/pdf"
    assert module._valid_upload("book.epub", epub_bytes()) == "application/epub+zip"
    with pytest.raises(HTTPException):
        module._valid_upload("book.pdf", b"not a pdf")
    with pytest.raises(HTTPException):
        module._valid_upload("book.epub", b"PK\x03\x04not really an epub")


def test_cancel_only_selected_local_job(monkeypatch, tmp_path):
    module = server(monkeypatch, tmp_path)
    module.db.init()
    bid = module.db.create_book("Title", "", "book.pdf", "watercolor", 200, "5",
                                "application/pdf", b"%PDF-test")

    class Proc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process, epub = Proc(), Proc()
    module._children[("process", bid)] = process
    module._children[("epub", bid)] = epub
    module.db.job_start(bid, "process", 1)
    module.db.job_start(bid, "epub", 2)

    assert module._cancel_local_jobs(bid, {"process"}) == ["process"]
    assert process.terminated
    assert not epub.terminated
    assert ("epub", bid) in module._children
    assert {j["kind"]: j["status"] for j in module.db.jobs_for_book(bid)}["process"] == "cancelled"


def test_upload_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("STORY_MAX_UPLOADS_PER_MINUTE", "2")
    module = server(monkeypatch, tmp_path)

    class Client:
        host = "127.0.0.1"

    class Request:
        client = Client()

    module._check_upload_rate(Request())
    module._check_upload_rate(Request())
    with pytest.raises(HTTPException) as exc:
        module._check_upload_rate(Request())
    assert exc.value.status_code == 429


def test_parse_when_accepts_dates_datetimes_and_epochs(monkeypatch, tmp_path):
    module = server(monkeypatch, tmp_path)
    import datetime

    assert module._parse_when(None, "start") is None
    assert module._parse_when("", "start") is None
    assert module._parse_when("1786303931", "start") == 1786303931.0

    midnight = datetime.datetime(2026, 8, 1).timestamp()
    assert module._parse_when("2026-08-01", "start") == midnight
    assert module._parse_when("2026-08-01T09:30", "start") == midnight + 9.5 * 3600
    assert module._parse_when("2026-08-01 09:30:15", "start") == midnight + 9.5 * 3600 + 15

    # a bare date as the upper bound covers that whole day (exclusive next midnight)
    assert module._parse_when("2026-08-01", "end", end_of_day=True) == midnight + 86400
    # ...but an explicit time is taken literally
    assert module._parse_when("2026-08-01T09:30", "end", end_of_day=True) == midnight + 9.5 * 3600

    with pytest.raises(HTTPException) as exc:
        module._parse_when("last tuesday", "start")
    assert exc.value.status_code == 400


def test_history_export_filters_by_date_range(monkeypatch, tmp_path):
    module = server(monkeypatch, tmp_path)
    module.db.init()
    bid = module.db.create_book("Title", "Author", "book.pdf", "watercolor", 200, "5",
                                "application/pdf", b"%PDF-test")
    with module.db.conn() as c:
        c.execute("UPDATE books SET num_pages=100 WHERE id=?", (bid,))

    import datetime

    def at(day, hour):
        return datetime.datetime(2026, 8, day, hour).timestamp()

    # three half-hour sessions on the 1st, 5th and 9th
    for day, start_pos, end_pos in ((1, 0, 9), (5, 10, 24), (9, 25, 59)):
        with module.db.conn() as c:
            c.execute("INSERT INTO reading_log(book_id,started_at,updated_at,"
                      "start_pos,end_pos,events) VALUES (?,?,?,?,?,?)",
                      (bid, at(day, 12), at(day, 12) + 1800, start_pos, end_pos, 12))

    assert module.api_history_export()["count"] == 3
    assert module.api_history_export(start="2026-08-05")["count"] == 2
    assert module.api_history_export(end="2026-08-05")["count"] == 2   # end date included
    assert module.api_history_export(start="2026-08-05", end="2026-08-05")["count"] == 1
    assert module.api_history_export(start="2026-08-02", end="2026-08-04")["count"] == 0

    newest = module.api_history_export(start="2026-08-09")["sessions"][0]
    assert newest["title"] == "Title"
    assert (newest["start_page"], newest["end_page"]) == (26, 60)   # 0-based -> page numbers
    assert newest["pages_read"] == 35
    assert newest["duration_seconds"] == 1800
    assert newest["percent_complete"] == 60.0
    assert newest["started_at_iso"].startswith("2026-08-09T12:00:00")

    with pytest.raises(HTTPException) as exc:
        module.api_history_export(start="2026-08-09", end="2026-08-01")
    assert exc.value.status_code == 400


def test_history_export_covers_sessions_straddling_the_boundary(monkeypatch, tmp_path):
    """A session running across midnight belongs to both days' windows, so an
    export of either day still sees it."""
    module = server(monkeypatch, tmp_path)
    module.db.init()
    bid = module.db.create_book("Title", "Author", "book.pdf", "watercolor", 200, "5",
                                "application/pdf", b"%PDF-test")
    import datetime
    start = datetime.datetime(2026, 8, 4, 23, 45).timestamp()
    with module.db.conn() as c:
        c.execute("INSERT INTO reading_log(book_id,started_at,updated_at,"
                  "start_pos,end_pos,events) VALUES (?,?,?,?,?,?)",
                  (bid, start, start + 3600, 0, 20, 20))

    assert module.api_history_export(start="2026-08-04", end="2026-08-04")["count"] == 1
    assert module.api_history_export(start="2026-08-05", end="2026-08-05")["count"] == 1


def test_history_export_flags_truncation(monkeypatch, tmp_path):
    module = server(monkeypatch, tmp_path)
    module.db.init()
    bid = module.db.create_book("Title", "Author", "book.pdf", "watercolor", 200, "5",
                                "application/pdf", b"%PDF-test")
    import datetime
    base = datetime.datetime(2026, 8, 1, 12).timestamp()
    for n in range(3):
        with module.db.conn() as c:
            c.execute("INSERT INTO reading_log(book_id,started_at,updated_at,"
                      "start_pos,end_pos,events) VALUES (?,?,?,?,?,?)",
                      (bid, base + n * 7200, base + n * 7200 + 600, n, n + 1, 2))

    assert module.api_history_export(limit=2)["truncated"] is True
    assert module.api_history_export(limit=5)["truncated"] is False


def test_parse_when_uses_the_configured_timezone(monkeypatch, tmp_path):
    """Bare dates resolve in the server's zone, which is why the container pins TZ.
    Left on UTC, an evening's reading here lands under the following day."""
    import os
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    module = server(monkeypatch, tmp_path)
    module.db.init()
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        pacific = ZoneInfo("America/Los_Angeles")
        assert module._parse_when("2026-08-01", "start") == \
            datetime(2026, 8, 1, tzinfo=pacific).timestamp()
        # the upper bound still stretches to the end of that day, in the same zone
        assert module._parse_when("2026-08-01", "end", end_of_day=True) == \
            datetime(2026, 8, 2, tzinfo=pacific).timestamp()
        # an evening session stays on the day it was actually read
        evening = datetime(2026, 8, 1, 20, 30, tzinfo=pacific).timestamp()
        assert module._iso(evening).startswith("2026-08-01T20:30:00-07:00")
        assert module.api_history_export()["timezone"] == "America/Los_Angeles"
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
