"""The bake worker's progress reporting, with the model calls stubbed out."""
import importlib


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    import webapp.bake_progress as bp
    importlib.reload(bp)
    import webapp.batch_bake as bb
    importlib.reload(bb)
    bid = db.create_book("Title", "Author", "book.pdf", "watercolor", 200, "5",
                         "application/pdf", b"%PDF-test")
    return db, bp, bb, bid


def test_counting_log_counts_finished_review_windows(monkeypatch, tmp_path, capsys):
    db, bp, bb, bid = setup(monkeypatch, tmp_path)
    db.bake_upsert(bid, "baking", total_pages=10)
    log = bb._counting_log(bid, "review", 2)
    log("[continuity] book 1: reviewing pages 0-4")          # start of a run: not counted
    assert "review" not in db.bake_get(bid)["progress"]
    log("[continuity]   pages 0-4: 3 issues, 2 page edits, +0 entities, +0 variants")
    assert db.bake_get(bid)["progress"]["review"] == [1, 2]
    log("[continuity]   pages 5-9: 0 issues, 0 page edits, +0 entities, +0 variants")
    assert db.bake_get(bid)["progress"]["review"] == [2, 2]
    assert "reviewing pages 0-4" in capsys.readouterr().out   # still logged through


def test_run_round_reports_steps_and_round_history(monkeypatch, tmp_path):
    db, bp, bb, bid = setup(monkeypatch, tmp_path)
    db.bake_upsert(bid, "baking", total_pages=4, round=0)
    db.bps_init(bid, [0, 1, 2, 3])
    runs = {i: bb.PageRun(i) for i in range(4)}
    for pr in runs.values():
        pr.book_id = bid
    seen = []

    def fake_generate(book_id, r, runs_, open_idxs):
        seen.append(("generate", dict(db.bake_get(bid)["progress"])))
        for i in open_idxs:
            runs_[i].attempt += 1

    def fake_score(book_id, r, runs_, open_idxs):
        seen.append(("score", dict(db.bake_get(bid)["progress"])))
        return {}

    def fake_apply(book_id, r, runs_, open_idxs, scored):
        for i in open_idxs[:3]:   # three of four pass this round
            db.bps_save(book_id, i, status="done", done=1)

    monkeypatch.setattr(bb, "_run_generate", fake_generate)
    monkeypatch.setattr(bb, "_score_round", fake_score)
    monkeypatch.setattr(bb, "_apply_round", fake_apply)

    left = bb.run_round(bid, 0, runs, [0, 1, 2, 3])
    assert left == 1
    gen = seen[0][1]
    assert gen["phase"] == "draw" and gen["round"] == 0 and gen["open"] == 4
    assert gen["step"] == "generate" and gen["attempts_left"] == bb.MAX_ROUNDS - 1
    # the score step is reported by _score_round itself (stubbed here), so the
    # progress at that point still says generate; the round's end resets it
    p = db.bake_get(bid)["progress"]
    assert p["round"] == 1 and p["open"] == 0 and "step" not in p
    assert p["rounds"] == [{"r": 0, "open": 4, "passed": 3,
                            "gen_s": p["rounds"][0]["gen_s"], "score_s": p["rounds"][0]["score_s"]}]
    assert db.bake_get(bid)["round"] == 1
    # the status the UI polls reads it back with a learned pass rate
    st = bp.status(bid)
    assert st["done_pages"] == 3 and st["headline"] == "Round 2 · starting"
