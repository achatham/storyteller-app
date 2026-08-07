import importlib
import io
import zipfile

import pytest
from fastapi import HTTPException


def server(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
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
