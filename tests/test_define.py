import importlib

import pytest
from fastapi import HTTPException


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    monkeypatch.delenv("STORY_FIRESTORE_PROJECT", raising=False)
    import webapp.db
    importlib.reload(webapp.db)
    import webapp.define as define
    define = importlib.reload(define)
    import webapp.server as server
    server = importlib.reload(server)
    server.db.init()
    calls = []

    def fake_text_json(prompt, schema=None, model=None, thinking_level=None):
        calls.append(prompt)
        return {"definition": f"meaning #{len(calls)}", "part_of_speech": "noun",
                "example": "I ate my gruel."}

    monkeypatch.setattr(define.gem, "text_json", fake_text_json)
    return define, server, calls


def test_definition_is_generated_once_then_cached(monkeypatch, tmp_path):
    define, _, calls = setup(monkeypatch, tmp_path)
    a = define.define("gruel", "A bowl of thin gruel.", "Oliver Twist")
    b = define.define("gruel", "A bowl of  thin gruel.")     # whitespace doesn't matter
    assert (a["cached"], b["cached"]) == (False, True)
    assert a["definition"] == b["definition"] == "meaning #1"
    assert len(calls) == 1 and "Oliver Twist" in calls[0] and "thin gruel" in calls[0]
    assert define.backend() == "sqlite"


def test_each_sense_gets_its_own_definition(monkeypatch, tmp_path):
    define, _, calls = setup(monkeypatch, tmp_path)
    assert define.cache_key("spot", "the black spot") != define.cache_key("spot", "a spot of tea")
    define.define("spot", "He was given the black spot.")
    define.define("spot", "Would you like a spot of tea?")
    assert len(calls) == 2
    assert "/" not in define.cache_key("don't", "x")


def test_broken_cache_still_serves_a_definition(monkeypatch, tmp_path):
    define, _, calls = setup(monkeypatch, tmp_path)

    class Broken:
        def get(self, key):
            raise RuntimeError("firestore down")

        def put(self, key, rec):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(define, "_cache", Broken())
    assert define.define("gruel", "gruel")["definition"] == "meaning #1"
    assert define.define("gruel", "gruel")["cached"] is False   # retried, not cached
    assert len(calls) == 2


def test_definition_endpoint(monkeypatch, tmp_path):
    _, server, calls = setup(monkeypatch, tmp_path)
    bid = server.db.create_book("Oliver", "", "o.pdf", "watercolor", 200, "application/pdf", b"%PDF-")
    row = server.api_vocab_add(bid, {"word": "Gruel", "context": "Please, sir, more gruel."})
    d = server.api_vocab_definition(row["id"])
    assert d["word"] == "gruel" and d["definition"] == "meaning #1" and d["part_of_speech"] == "noun"
    assert server.api_vocab_definition(row["id"])["cached"] is True
    assert len(calls) == 1
    with pytest.raises(HTTPException) as e:
        server.api_vocab_definition(9999)
    assert e.value.status_code == 404


def test_definition_endpoint_reports_generation_failure(monkeypatch, tmp_path):
    define, server, _ = setup(monkeypatch, tmp_path)
    bid = server.db.create_book("B", "", "b.pdf", "watercolor", 200, "application/pdf", b"%PDF-")
    row = server.api_vocab_add(bid, {"word": "gruel"})

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(define.gem, "text_json", boom)
    with pytest.raises(HTTPException) as e:
        server.api_vocab_definition(row["id"])
    assert e.value.status_code == 502
