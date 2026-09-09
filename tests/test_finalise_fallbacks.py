"""What a page ends the bake as when the critic never scored it.

Gemini's child-safety filter refuses to grade some pages -- all three critique tiers
come back blocked -- so "no score" is a routine outcome, not an error. A page must
never lose its picture, or its place in a future bake, because of it.
"""
import importlib

import pytest


@pytest.fixture
def bake(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    import webapp.batch_bake as bb
    importlib.reload(bb)
    bid = db.create_book("T", "A", "book.pdf", "watercolor", 200,
                         "application/pdf", b"%PDF-test")
    db.save_registry(bid, {"entities": []})
    for idx in (4, 7, 9):
        db.add_page(bid, idx, 0, f"P{idx}", "text", "somewhere", "a brief", [], "")
    db.bps_init(bid, [4, 7, 9])
    return bb, db, bid


class _Run:
    """The minimum of batch_bake.PageRun that _finalise_page touches."""

    def __init__(self, idx, cand, draft):
        self.idx, self.cand, self.attempt, self.gen_id = idx, cand, 2, 1
        self.trace = {}
        self.state = {"best": None, "draft": draft, "cands": []}
        self.ctx = {"page": {"brief": "a brief"}, "states": {}}


def test_an_unscored_page_keeps_the_candidate_it_drew(bake, monkeypatch):
    bb, db, bid = bake
    monkeypatch.setattr(bb, "_compress", lambda data, w, q: data)
    bb._finalise_page(bid, _Run(4, cand=b"drawn-image", draft=None))
    row = db.bps_get(bid, 4)
    assert (row["status"], row["done"], row["best_score"]) == ("done", 1, None)


def test_a_revise_that_drew_nothing_keeps_the_page_it_was_revising(bake):
    """The page already has an accepted picture -- the one the revise was seeded from.
    Marking it failed strands it: bps_actionable skips failed pages for good."""
    bb, db, bid = bake
    pr = _Run(7, cand=None, draft=b"the picture being revised")
    bb._finalise_page(bid, pr)
    row = db.bps_get(bid, 7)
    assert row["status"] == "done" and row["done"] == 1
    assert row["best_score"] is None
    assert 7 not in db.bps_actionable(bid)
    assert "revise seed" in pr.trace["fallback"]


def test_a_page_that_never_drew_anything_is_still_a_failure(bake):
    bb, db, bid = bake
    bb._finalise_page(bid, _Run(9, cand=None, draft=None))
    assert db.bps_get(bid, 9)["status"] == "failed"
