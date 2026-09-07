import importlib


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    import webapp.bake_progress as bp
    importlib.reload(bp)
    bid = db.create_book("Title", "Author", "book.pdf", "watercolor", 200,
                         "application/pdf", b"%PDF-test")
    return db, bp, bid


def test_progress_merges_per_key_and_none_removes(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    db.bake_upsert(bid, "baking", total_pages=10)
    bp.report(bid, phase="draw", round=0, open=10)
    bp.report(bid, step="generate")             # another key: the first ones survive
    bp.report(bid, roster={"drawn": 3, "total": 5})   # a second thread's key
    p = db.bake_get(bid)["progress"]
    assert p == {"phase": "draw", "round": 0, "open": 10, "step": "generate",
                 "roster": {"drawn": 3, "total": 5}}
    bp.report(bid, step=None)
    assert "step" not in db.bake_get(bid)["progress"]
    db.bake_progress_reset(bid)
    assert db.bake_get(bid)["progress"] == {}


def test_status_is_quiet_when_not_baking(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    assert bp.status(bid) == {"status": None}
    db.bake_upsert(bid, "roster_review", total_pages=10)
    st = bp.status(bid)
    assert st["status"] == "roster_review" and "eta" not in st and "headline" not in st


def _seed_jobs(db, bid, durations):
    """Finished batch jobs with known wall times, so the ETA has a basis."""
    for i, d in enumerate(durations):
        db.bjob_upsert(bid, 100 + i, "gen:flash", f"batches/j{i}", "JOB_STATE_PENDING")
        with db.conn() as c:
            c.execute("UPDATE batch_jobs SET created_at=?, updated_at=?, state=? WHERE job_name=?",
                      (1000.0, 1000.0 + d, "JOB_STATE_SUCCEEDED", f"batches/j{i}"))


def test_eta_counts_the_running_job_then_the_rounds_left(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    db.bake_upsert(bid, "baking", total_pages=100, round=0)
    db.bps_init(bid, list(range(100)))
    _seed_jobs(db, bid, [600, 600, 600])            # every job takes 10 minutes
    now = 5000.0
    bp.report(bid, started_at=now - 300, phase="draw", **{"pass": "main"}, round=0,
              open=100, attempts_left=2, step="generate", step_since=now - 240,
              stages={"plan": False, "picture": False},
              roster={"step": "done", "total": 0, "drawn": 0}, admitted=100)
    db.bjob_upsert(bid, 0, "gen:flash", "batches/live", "JOB_STATE_RUNNING")
    db.batch_req_add(bid, "batches/live", 100)
    st = bp.status(bid, now=now)
    assert st["headline"].startswith("Round 1 · drawing 100 pages · batch job running")
    assert st["jobs"] == [{"kind": "gen:flash", "state": "JOB_STATE_RUNNING", "n_reqs": 100,
                           "age_s": st["jobs"][0]["age_s"]}]
    assert st["elapsed_s"] == 300
    eta = st["eta"]
    assert eta["basis"] == "this bake's jobs"
    # low: 6 min left on this job + scoring 100 drafts (60s) + two more rounds of
    # 50 and 25 pages (10 min + 30s, 10 min + 15s) + finalise + cover, no reviews
    low = (600 - 240) + 60 + (600 + 30) + (600 + 15) + 30 + 30
    assert abs(eta["low_s"] - low) <= 2
    assert eta["high_s"] > eta["low_s"]

    # scoring step: only the drafts still unscored count
    bp.report(bid, step="score", step_since=now, scored=[40, 100], attempts_left=0)
    st = bp.status(bid, now=now)
    assert st["headline"] == "Round 1 · scoring drafts 40/100"
    assert abs(st["eta"]["low_s"] - (60 * 0.6 + 30 + 30)) <= 2


def test_eta_for_the_review_and_headlines_per_phase(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    db.bake_upsert(bid, "baking", total_pages=50)
    db.bps_init(bid, list(range(50)))
    now = 9000.0
    bp.report(bid, started_at=now - 1000, phase="review", review=[4, 10])
    st = bp.status(bid, now=now)
    assert st["headline"] == "Continuity review across pages · 4/10 five-page runs"
    # 6 windows left at 35-60s each, then one to two redraw rounds and the cover;
    # no job history at all -> default job times
    assert st["eta"]["basis"] == "no history"
    assert st["eta"]["low_s"] == int(6 * 35 + 480 + 30 + 30)

    bp.report(bid, phase="tail", tail=[2, 7])
    assert bp.status(bid, now=now)["headline"] == "Finishing the last 7 pages interactively · 2/7"
    bp.report(bid, phase="finalise", judge=3)
    assert bp.status(bid, now=now)["headline"].startswith("Choosing the best draft for 3 pages")
    bp.report(bid, phase="draw", **{"pass": "redraw"}, open=0, round=3,
              roster={"step": "generate", "total": 12, "drawn": 5})
    assert bp.status(bid, now=now)["headline"] == \
        "Continuity redraws · Waiting for reference sheets · roster 5/12 drawn"
    bp.report(bid, phase="plan", review=[1, 10])
    assert bp.status(bid, now=now)["headline"] == "Reviewing the plan before drawing · 1/10 five-page runs"


def test_pass_rate_learned_from_this_bake(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    assert bp._pass_rate({}) == bp.DEFAULT_PASS_RATE
    assert bp._pass_rate({"rounds": [{"open": 5, "passed": 5}]}) == bp.DEFAULT_PASS_RATE  # too few
    assert bp._pass_rate({"rounds": [{"open": 100, "passed": 70}]}) == 0.7
    assert bp._pass_rate({"rounds": [{"open": 100, "passed": 2}]}) == 0.2      # clamped


def test_job_durations_prefer_this_bake(monkeypatch, tmp_path):
    db, bp, bid = setup(monkeypatch, tmp_path)
    other = db.create_book("Other", "", "b.pdf", "watercolor", 200,
                           "application/pdf", b"%PDF-test")
    _seed_jobs(db, other, [100, 200, 300])
    med, p90, basis = bp.job_stats(bid)
    assert basis == "recent jobs" and med == 200
    _seed_jobs(db, bid, [900, 1000])
    med, p90, basis = bp.job_stats(bid)
    assert basis == "this bake's jobs" and med == 1000 and p90 == 1000
