"""Live progress + ETA for a whole-book bake (webapp/batch_bake).

The bake worker reports what it is doing through `report(book_id, **fields)`, which
merges into the batch_bake.progress JSON; the server turns that row (plus the page
counts, the round's batch jobs and recent job durations) into one `status()` dict
for the settings page and the library card: a headline ("Round 2 · drawing 174
pages · batch job running 6 min"), the page counts, and a rough time-left range.

Keys the worker writes (all optional; a resumed bake starts from whatever is there):

    started_at     when this worker started
    stages         {"plan": bool, "picture": bool}  which continuity reviews will run
    phase          plan | draw | tail | finalise | review | cover | done
    phase_since    when the phase began
    pass           main | redraw  -- draw phases: the first pass, or the continuity redraws
    round          current round (the global counter; see batch_bake)
    round_since    when the round began
    open           pages drawn this round
    attempts_left  most further rounds any open page can still need after this one
    step           generate | score
    step_since     when the step began
    scored         [done, total]  critique progress within the round
    rounds         [{r, open, passed, gen_s, score_s}]  finished rounds of this run
    admitted       pages admitted so far (the rest wait for their roster sheets)
    roster         {total, drawn, wave, attempt, step}  the background roster draw
    tail           [done, total]  the interactive tail
    judge          pages going to the best-of judge
    review         [windows_done, windows_total]  the running continuity review
    staged         pages the continuity review staged for redrawing

The ETA is a RANGE, and deliberately rough: a batch job's wall time is mostly queue
time on Google's side (4-24 minutes for anything from 3 to 400 images, varying by
time of day), so the estimate leans on the durations of recently finished jobs --
this bake's own once it has two, the last twenty overall before that -- and on
per-step rates measured on earlier bakes (below).
"""
import math
import time

from . import db

# Measured on the Sep-2026 bakes (Chamber of Secrets, Fablehaven roster):
SCORE_S_PER_PAGE = (0.6, 1.0)      # critique of a round's drafts, 8 workers, s/page
REVIEW_S_PER_WINDOW = (35.0, 60.0)  # picture continuity review, 3 workers, wall s per 5-page run
PLAN_S_PER_WINDOW = (15.0, 40.0)    # plan (text-only) review, same shape
TAIL_S_PER_PAGE = (15.0, 40.0)      # interactive tail, 4 workers, wall s/page
INTERACTIVE_SHEET_S = (20.0, 45.0)  # a roster sheet drawn interactively (force-admit)
FINALISE_S = (30.0, 180.0)
COVER_S = (30.0, 120.0)
DEFAULT_JOB_S = (8 * 60.0, 20 * 60.0)   # (median, p90) when no job history at all
DEFAULT_PASS_RATE = 0.5             # share of a round's drafts that pass the critic
REVIEW_WINDOW = 5


def report(book_id, **fields):
    """Merge progress fields (see module doc). Thin wrapper so the bake's callers
    don't depend on the storage."""
    return db.bake_progress(book_id, **fields)


def job_stats(book_id) -> tuple[float, float, str]:
    """(median, p90, basis) of recent SUCCEEDED batch job durations in seconds:
    this bake's own jobs once it has two, else the last twenty across all books."""
    own = db.batch_job_durations(book_id, limit=10)
    if len(own) >= 2:
        d, basis = own, "this bake's jobs"
    else:
        d = db.batch_job_durations(None, limit=20)
        basis = "recent jobs"
    if not d:
        return (*DEFAULT_JOB_S, "no history")
    s = sorted(d)
    med = s[len(s) // 2]
    p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
    return med, max(p90, med), basis


def _score_s(n):
    return (n * SCORE_S_PER_PAGE[0], n * SCORE_S_PER_PAGE[1])


def _pass_rate(progress) -> float:
    hist = progress.get("rounds") or []
    open_ = sum(h.get("open", 0) for h in hist)
    passed = sum(h.get("passed", 0) for h in hist)
    if open_ < 20:
        return DEFAULT_PASS_RATE
    return min(0.8, max(0.2, passed / open_))


def _draw_pass(progress, med, p90, now, extra_pages=0) -> tuple[float, float]:
    """Time left in the current draw pass: the rest of this round, then the further
    rounds the still-open pages can need, each round smaller by the pass rate."""
    n = progress.get("open") or 0
    step = progress.get("step")
    since = progress.get("step_since") or now
    elapsed = max(0.0, now - since)
    low = high = 0.0
    if n and step == "generate":
        low += max(0.0, med - elapsed)
        high += max(p90 - elapsed, 120.0)   # past p90: still a couple of minutes, not zero
        lo, hi = _score_s(n)
        low, high = low + lo, high + hi
    elif n and step == "score":
        done = (progress.get("scored") or [0, n])[0]
        lo, hi = _score_s(max(0, n - done))
        low, high = low + lo, high + hi
    rate = _pass_rate(progress)
    n_next = n * (1 - rate) + extra_pages
    for _ in range(progress.get("attempts_left") or 0):
        if n_next < 0.5:
            break
        lo, hi = _score_s(n_next)
        low += med + lo
        high += p90 + hi
        n_next *= (1 - rate)
    return low, high


def _review_s(windows_left, per_window) -> tuple[float, float]:
    return (windows_left * per_window[0], windows_left * per_window[1])


def _after_main(progress, total_pages, med, p90) -> tuple[float, float]:
    """Everything after the main draw pass: judge, the picture review, its redraws
    (one round to two, on few pages), the cover."""
    low, high = FINALISE_S
    if (progress.get("stages") or {}).get("picture", True):
        windows = math.ceil((total_pages or 0) / REVIEW_WINDOW)
        lo, hi = _review_s(windows, REVIEW_S_PER_WINDOW)
        low, high = low + lo, high + hi
        low += med + 30
        high += 2 * (p90 + 60)
    return low + COVER_S[0], high + COVER_S[1]


def estimate(bake: dict, counts: dict, now: float | None = None, max_rounds: int = 3) -> dict | None:
    """Rough seconds-left range for a running bake, or None if there is nothing to go
    on yet. {"low_s", "high_s", "basis"}."""
    if not bake or bake.get("status") != "baking":
        return None
    p = bake.get("progress") or {}
    now = now or time.time()
    total = bake.get("total_pages") or 0
    med, p90, basis = job_stats(bake["book_id"])
    phase = p.get("phase")
    todo = max(0, total - counts.get("done", 0))
    if phase == "plan":
        done, n = (p.get("review") or [0, math.ceil(total / REVIEW_WINDOW)])
        low, high = _review_s(max(0, n - done), PLAN_S_PER_WINDOW)
        # then the whole main pass, from scratch
        n_pages = float(todo)
        for _ in range(max_rounds):
            lo, hi = _score_s(n_pages)
            low += med + lo
            high += p90 + hi
            n_pages *= (1 - DEFAULT_PASS_RATE)
        lo, hi = _after_main(p, total, med, p90)
        return _out(low + lo, high + hi, basis)
    if phase == "draw":
        not_admitted = max(0, todo - (p.get("admitted") or 0)) if not _roster_done(p) else 0
        low, high = _draw_pass(p, med, p90, now, extra_pages=not_admitted)
        if not_admitted and not p.get("open"):
            # waiting on the roster: the next round hasn't started; count one full one
            lo, hi = _score_s(not_admitted)
            low += med + lo
            high += p90 + hi
        if p.get("pass") == "redraw":
            return _out(low + COVER_S[0], high + COVER_S[1], basis)
        lo, hi = _after_main(p, total, med, p90)
        return _out(low + lo, high + hi, basis)
    if phase == "tail":
        done, n = p.get("tail") or [0, todo]
        left = max(0, n - done)
        low, high = left * TAIL_S_PER_PAGE[0], left * TAIL_S_PER_PAGE[1]
        if p.get("pass") == "redraw":
            return _out(low + COVER_S[0], high + COVER_S[1], basis)
        lo, hi = _after_main(p, total, med, p90)
        return _out(low + lo, high + hi, basis)
    if phase == "finalise":
        low, high = FINALISE_S
        if p.get("pass") == "redraw":
            return _out(low + COVER_S[0], high + COVER_S[1], basis)
        lo, hi = _after_main(p, total, med, p90)
        return _out(low + lo - FINALISE_S[0], high + hi - FINALISE_S[1], basis)
    if phase == "review":
        done, n = p.get("review") or [0, math.ceil(total / REVIEW_WINDOW)]
        low, high = _review_s(max(0, n - done), REVIEW_S_PER_WINDOW)
        low += med + 30 + COVER_S[0]
        high += 2 * (p90 + 60) + COVER_S[1]
        return _out(low, high, basis)
    if phase == "cover":
        return _out(COVER_S[0], COVER_S[1], basis)
    return None


def _roster_done(p) -> bool:
    r = p.get("roster")
    return bool(r) and r.get("step") == "done"


def _out(low, high, basis):
    return {"low_s": int(max(0, low)), "high_s": int(max(60, high)), "basis": basis}


def _mins(s: float) -> str:
    m = int(round(s / 60))
    return f"{m} min" if m < 90 else f"{m // 60}h{m % 60:02d}"


def headline(bake: dict, counts: dict, jobs: list[dict], now: float | None = None) -> str:
    """One line saying what the bake is doing right now."""
    p = bake.get("progress") or {}
    now = now or time.time()
    phase = p.get("phase")
    total = bake.get("total_pages") or 0
    if phase == "plan":
        done, n = p.get("review") or [0, math.ceil(total / REVIEW_WINDOW)]
        return f"Reviewing the plan before drawing · {done}/{n} five-page runs"
    if phase == "draw":
        n = p.get("open") or 0
        prefix = "Continuity redraws · " if p.get("pass") == "redraw" else ""
        rnd = f"Round {(p.get('round') or 0) + 1}"
        if not n:
            r = p.get("roster") or {}
            if r and r.get("step") != "done":
                return (f"{prefix}Waiting for reference sheets · roster "
                        f"{r.get('drawn', 0)}/{r.get('total', 0)} drawn")
            return f"{prefix}{rnd} · starting"
        if p.get("step") == "generate":
            live = [j for j in jobs if j.get("state") not in db_terminal()]
            age = max((now - j["created_at"] for j in live), default=now - (p.get("step_since") or now))
            state = "queued" if any(j.get("state") == "JOB_STATE_PENDING" for j in live) \
                and not any(j.get("state") == "JOB_STATE_RUNNING" for j in live) else "running"
            return (f"{prefix}{rnd} · drawing {n} page{'s' if n != 1 else ''} · "
                    f"batch job {state} {_mins(age)}")
        if p.get("step") == "score":
            done = (p.get("scored") or [0, n])[0]
            return f"{prefix}{rnd} · scoring drafts {done}/{n}"
        return f"{prefix}{rnd}"
    if phase == "tail":
        done, n = p.get("tail") or [0, 0]
        return f"Finishing the last {n} page{'s' if n != 1 else ''} interactively · {done}/{n}"
    if phase == "finalise":
        j = p.get("judge") or 0
        return (f"Choosing the best draft for {j} page{'s' if j != 1 else ''} that never passed"
                if j else "Storing the finished pages")
    if phase == "review":
        done, n = p.get("review") or [0, math.ceil(total / REVIEW_WINDOW)]
        return f"Continuity review across pages · {done}/{n} five-page runs"
    if phase == "cover":
        return "Drawing the cover"
    if phase == "done":
        return "Finished"
    return bake.get("detail") or "Starting…"


def db_terminal():
    from pipeline import gem
    return gem.BATCH_TERMINAL


def status(book_id: int, now: float | None = None) -> dict:
    """Everything the UI shows about a bake, in one dict (see server.api_bake_status)."""
    bake = db.bake_get(book_id)
    if not bake:
        return {"status": None}
    now = now or time.time()
    counts = db.bps_counts(book_id)
    p = bake.get("progress") or {}
    jobs = []
    if bake["status"] == "baking" and p.get("phase") == "draw" and p.get("step") == "generate":
        jobs = [{"kind": j["kind"], "state": j["state"], "n_reqs": j["n_reqs"] or 0,
                 "age_s": int(now - (j["created_at"] or now)), "created_at": j["created_at"]}
                for j in db.bjobs_for_round(book_id, bake["round"])]
    out = {"status": bake["status"], "round": bake["round"],
           "total_pages": bake["total_pages"], "done_pages": counts.get("done", 0),
           "detail": bake["detail"], "counts": counts, "progress": p,
           "jobs": [{k: v for k, v in j.items() if k != "created_at"} for j in jobs]}
    if bake["status"] == "baking":
        started = p.get("started_at") or bake.get("created_at")
        out["elapsed_s"] = int(now - started) if started else None
        out["headline"] = headline(bake, counts, jobs, now)
        out["eta"] = estimate(bake, counts, now)
    return out
