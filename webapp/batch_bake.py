"""Illustrate a whole book with the Batch API (~50% cheaper than the interactive
per-page path), run as its own subprocess:

    python -m webapp.batch_bake <book_id>

Batch mode is asynchronous, so the tight per-image critique/revise loop of
webapp/scene.py becomes ROUNDS across every page:

    prepare   -> build each page's scene context (draws any missing roster sheets)
    round r   -> GENERATE batch (image model)   : one draft per still-open page
                 CRITIQUE batch (text model)     : score + verdict per draft
                 VERIFY   batch (text model)     : carry-forward fix check (revises)
                 apply the SAME accept/revise/regenerate bookkeeping as the lazy path
    finalise  -> best-of judge for pages that never passed; store every page image

All decision logic is shared with the interactive path via webapp/scene.py
(build_scene_context / build_round_request / apply_verdict / ...), so a batched
page converges exactly like an interactively-rendered one. Resumable: submitted
jobs live in the batch_jobs table, so a restarted bake reattaches instead of
resubmitting, and finished pages are stored as they complete.
"""
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from pipeline import gem, costs
from pipeline.config import CRITIQUE_MODEL, IMAGE_SIZE

from . import db, scene
from .scene import (build_scene_context, build_round_request, apply_verdict,
                    new_scene_state, critique_prompt, _attempt_trace, _compress,
                    SCENE_TRIES, PASS_THRESHOLD, DEBUG_MAXW, DEBUG_QUALITY,
                    SCENE_MAXW, JUDGE_BEST, SCENE_CRITIQUE_SCHEMA,
                    FIX_VERIFY, FIX_VERIFY_SCHEMA)
from pipeline.config import WEBP_QUALITY

POLL_SECONDS = int(os.environ.get("STORY_BATCH_POLL", "20"))
PREPARE_WORKERS = int(os.environ.get("STORY_BATCH_PREPARE_WORKERS", "6"))
# Stragglers the batch critic never scored can be escalated to the interactive
# render. OFF by default: measured on Harry Potter, the critique block is a hard,
# persistent refusal (batch AND interactive), so escalation recovers ~1-2 of a
# dozen pages while paying ~3 full-price regenerations each -- the unscored fallback
# already gives every page an image. Enable per-book with STORY_BAKE_ESCALATE=1.
ESCALATE_INTERACTIVE = os.environ.get("STORY_BAKE_ESCALATE", "0") == "1"
INTERACTIVE_WORKERS = int(os.environ.get("STORY_BAKE_INTERACTIVE_WORKERS", "4"))
MAX_ROUNDS = SCENE_TRIES

JUDGE_SCHEMA = {"type": "object", "properties": {
    "best": {"type": "integer"}, "why": {"type": "string"}}, "required": ["best"]}


def log(msg):
    print(f"[bake] {msg}", flush=True)


# ---------------- per-page runtime ----------------

class PageRun:
    """In-memory state for one page across rounds. The durable subset is mirrored to
    batch_page_state so a restart can rebuild this."""

    def __init__(self, idx):
        self.idx = idx
        self.ctx = None
        self.state = new_scene_state()
        self.gen_id = None
        self.trace = None
        self.name_cache = {}
        self.attempt = 0
        # transient within a round: the request/candidate awaiting a verdict
        self.req = None
        self.cand = None      # candidate webp bytes generated this round
        self.mode = "fresh"

    # -- persistence --
    def carry(self) -> str:
        s = self.state
        best_actionable = bool(s["best_key"][0] == 0) if s["best_key"] else True
        return json.dumps({"mode": s["mode"], "pending_defect": s["pending_defect"],
                           "escalate": s["escalate"], "edit_instr": s["edit_instr"],
                           "ref_chars": s["ref_chars"], "best_actionable": best_actionable})

    def save(self, status):
        s = self.state
        best_blob = s["best"][0] if s["best"] else None
        best_score = s["best"][1] if s["best"] else None
        best_attempt = s["best"][2] if s["best"] else None
        db.bps_save(self.book_id, self.idx,
                    status=status, attempt=self.attempt, gen_id=self.gen_id,
                    done=1 if status == "done" else 0, best_score=best_score,
                    best_attempt=best_attempt, best_blob=best_blob,
                    draft_blob=s["draft"], carry_json=self.carry())

    def restore(self, row):
        s = self.state
        self.attempt = row["attempt"] or 0
        self.gen_id = row["gen_id"]
        s["draft"] = row["draft_blob"]
        if row["best_blob"] is not None:
            s["best"] = (row["best_blob"], row["best_score"], row["best_attempt"])
        carry = json.loads(row["carry_json"]) if row["carry_json"] else {}
        s["mode"] = carry.get("mode", "fresh")
        s["pending_defect"] = carry.get("pending_defect", "")
        s["escalate"] = carry.get("escalate", False)
        s["edit_instr"] = carry.get("edit_instr", "")
        s["ref_chars"] = carry.get("ref_chars", [])
        if s["best"]:
            actionable = carry.get("best_actionable", True)
            s["best_key"] = (0 if actionable else 1, s["best"][1])


# ---------------- prepare ----------------

def prepare(book_id) -> dict:
    """Build a PageRun (with scene context + gen_id + trace) for every page, drawing
    any missing roster sheets on the way. Runs page contexts in parallel (each is an
    independent text pass + cached sheet draws). Restores state for a resumed bake."""
    db.bps_init(book_id, [p["idx"] for p in db.get_pages(book_id)])
    # Only build contexts for pages that still need work: a fresh bake seeds every
    # page 'pending' (so this is all of them), while a resume or a retry-failed run
    # reopens just the handful still open -- no point re-preparing finished pages.
    idxs = db.bps_actionable(book_id)
    runs: dict = {}

    def one(idx):
        pr = PageRun(idx)
        pr.book_id = book_id
        row = db.bps_get(book_id, idx)
        try:
            pr.ctx = build_scene_context(book_id, idx)
        except Exception as ex:  # noqa: BLE001 -- a bad page shouldn't sink the bake
            log(f"page {idx} context failed: {ex}")
            db.bps_save(book_id, idx, status="failed")
            return idx, None
        if row and row["gen_id"]:      # resume: keep the same debug gen + prior state
            pr.gen_id = row["gen_id"]
            pr.restore(row)
        else:
            pr.gen_id = db.next_gen_id(book_id, idx)
        pr.trace = {"states": pr.ctx["states"], "max_tries": MAX_ROUNDS, "attempts": []}
        return idx, pr

    with ThreadPoolExecutor(max_workers=PREPARE_WORKERS) as ex:
        for idx, pr in ex.map(one, idxs):
            if pr is not None:
                runs[idx] = pr
    log(f"prepared {len(runs)}/{len(idxs)} page contexts")
    return runs


# ---------------- batch job helpers ----------------

def _submit_or_reattach(book_id, r, kind, model, reqs, display):
    """Reuse an already-submitted job for this (round, kind) if present (resume),
    else submit a new one. Returns the job name."""
    existing = db.bjob_get(book_id, r, kind)
    if existing and existing["job_name"]:
        log(f"r{r} {kind}: reattaching {existing['job_name']} ({existing['state']})")
        return existing["job_name"]
    job = gem.batch_submit(reqs, model=model, display_name=display)
    db.bjob_upsert(book_id, r, kind, job, "JOB_STATE_PENDING")
    log(f"r{r} {kind}: submitted {job} ({len(reqs)} reqs, model={model})")
    return job


def _await(book_id, r, kind, job_name) -> str:
    """Poll a job to a terminal state, mirroring the state into batch_jobs."""
    while True:
        st = gem.batch_state(job_name)
        db.bjob_set_state(book_id, r, kind, st)
        if st in gem.BATCH_TERMINAL:
            return st
        time.sleep(POLL_SECONDS)


def _cancelled(book_id) -> bool:
    row = db.bake_get(book_id)
    return bool(row and row["status"] == "cancelled")


# ---------------- one round ----------------

def _run_generate(book_id, r, runs, open_idxs):
    """GENERATE step: one image batch per model (fresh + escalated-revise pages can
    need different models). Fills pr.cand / pr.req / pr.mode for each open page."""
    groups: dict = {}
    for idx in open_idxs:
        pr = runs[idx]
        pr.req = build_round_request(pr.ctx, pr.state, pr.name_cache)
        pr.mode = pr.req["mode"]
        pr.attempt += 1
        pr.cand = None
        groups.setdefault(pr.req["model"], []).append(idx)

    for model, idxs in groups.items():
        short = model.split("-image")[0].split("-")[-1] or "img"
        kind = f"gen:{short}"
        reqs = [{"key": str(idx),
                 "parts": [gem.text_part(runs[idx].req["prompt"])]
                          + [gem.image_part(b) for b in runs[idx].req["ref_bytes"]],
                 "generation_config": gem.image_gen_config(aspect="3:2", size=IMAGE_SIZE)}
                for idx in idxs]
        job = _submit_or_reattach(book_id, r, kind, model, reqs, f"bake b{book_id} r{r} {kind}")
        st = _await(book_id, r, kind, job)
        if st != gem.BATCH_DONE:
            log(f"r{r} {kind}: {st} -- pages retry next round")
            continue
        results = gem.batch_results(job)
        for idx in idxs:
            resp = results.get(str(idx))
            if resp is None:
                continue
            gem.record_batch_usage(resp, model, "image", images=1)
            img = gem.response_image_bytes(resp)
            if img:
                runs[idx].cand = img


def _run_critique(book_id, r, runs, open_idxs) -> dict:
    """CRITIQUE step: one text batch judging each freshly generated draft against its
    brief + reference sheets. Returns {idx: critique dict}."""
    reqs = []
    for idx in open_idxs:
        pr = runs[idx]
        if pr.cand is None:
            continue
        reqs.append({"key": str(idx),
                     "parts": gem.critique_parts(critique_prompt(pr.ctx), pr.cand,
                                                 ref_bytes=pr.ctx["ref_bytes"],
                                                 ref_labels=pr.ctx["ref_labels"]),
                     "generation_config": gem.json_config(SCENE_CRITIQUE_SCHEMA, temperature=0.3)})
    if not reqs:
        return {}
    job = _submit_or_reattach(book_id, r, "critique", CRITIQUE_MODEL, reqs,
                              f"bake b{book_id} r{r} critique")
    st = _await(book_id, r, "critique", job)
    if st != gem.BATCH_DONE:
        log(f"r{r} critique: {st}")
        return {}
    out = {}
    for idx, resp in gem.batch_results(job).items():
        if resp is None:
            continue
        gem.record_batch_usage(resp, CRITIQUE_MODEL, "critique")
        try:
            out[int(idx)] = gem._coerce_json(resp.text, gem._block_reason(resp))
        except Exception as ex:  # noqa: BLE001
            log(f"r{r} critique parse failed for page {idx}: {ex}")
    return out


def _run_verify(book_id, r, runs, open_idxs, crits) -> dict:
    """VERIFY step: for pages whose current attempt was a revise targeting a specific
    defect, confirm that defect is gone. Returns {idx: fix_ok bool} (default True)."""
    reqs = []
    for idx in open_idxs:
        pr = runs[idx]
        if pr.cand is None or idx not in crits:
            continue
        if pr.mode == "revise" and pr.state["pending_defect"]:
            reqs.append({"key": str(idx),
                         "parts": [gem.text_part(FIX_VERIFY.format(defect=pr.state["pending_defect"])),
                                   gem.image_part(pr.cand)],
                         "generation_config": gem.json_config(FIX_VERIFY_SCHEMA, temperature=0.3)})
    if not reqs:
        return {}
    job = _submit_or_reattach(book_id, r, "verify", CRITIQUE_MODEL, reqs,
                              f"bake b{book_id} r{r} verify")
    st = _await(book_id, r, "verify", job)
    if st != gem.BATCH_DONE:
        return {}
    out = {}
    for idx, resp in gem.batch_results(job).items():
        if resp is None:
            continue
        gem.record_batch_usage(resp, CRITIQUE_MODEL, "critique")
        try:
            v = gem._coerce_json(resp.text, gem._block_reason(resp))
            out[int(idx)] = bool(v.get("resolved", True))
            runs[int(idx)]._verify = v
        except Exception:  # noqa: BLE001
            out[int(idx)] = True
    return out


def _apply_round(book_id, r, runs, open_idxs, crits, verifies):
    """Fold this round's critiques into each page's state, record the attempt to the
    debug history, and finalise any page that just passed (store its image now so the
    reader can show it while the bake continues).

    Non-done pages' advanced state is persisted only AFTER the whole round applies
    (and the caller then bumps the resume-round pointer), so a crash mid-round makes
    the resume re-run this round cleanly from each page's pre-round state instead of
    double-applying a verdict to a page that already advanced. Done pages are stored
    immediately (terminal + idempotent)."""
    to_save = []
    for idx in open_idxs:
        pr = runs[idx]
        if pr.cand is None or idx not in crits:
            continue   # no usable candidate this round -> leave prior saved state, retry next round
        crit = crits[idx]
        fix_ok = verifies.get(idx, True)
        if pr.mode == "revise" and pr.state["pending_defect"] and idx in verifies:
            v = getattr(pr, "_verify", {})
            crit["fix_verified"] = {"defect": pr.state["pending_defect"], "resolved": fix_ok,
                                    "still_present": v.get("still_present", "")}
        res = apply_verdict(pr.state, crit, pr.cand, pr.attempt, fix_ok)
        pr.trace["attempts"].append(_attempt_trace(pr.attempt, pr.mode, res, crit, fix_ok))
        db.scene_attempt_add(book_id, idx, pr.gen_id, pr.attempt, pr.mode, pr.req["prompt"],
                             _compress(pr.cand, DEBUG_MAXW, DEBUG_QUALITY),
                             json.dumps(crit), res["min"], res["avg"])
        if res["done"]:
            _finalise_page(book_id, pr)
        else:
            to_save.append(pr)
    for pr in to_save:
        pr.save(status="revising" if pr.state["draft"] is not None else "pending")


def run_round(book_id, r, runs) -> int:
    """Run one full round over the still-open pages. Returns how many remain open."""
    open_idxs = [i for i in db.bps_actionable(book_id) if i in runs]
    if not open_idxs:
        return 0
    log(f"round {r}: {len(open_idxs)} pages open")
    # resume pointer = r while this round runs; a crash resumes and re-runs round r
    db.bake_upsert(book_id, "baking", round=r,
                   done_pages=db.bps_counts(book_id).get("done", 0))
    _run_generate(book_id, r, runs, open_idxs)
    crits = _run_critique(book_id, r, runs, open_idxs)
    verifies = _run_verify(book_id, r, runs, open_idxs, crits)
    _apply_round(book_id, r, runs, open_idxs, crits, verifies)
    # round fully applied: advance the resume pointer so a later restart won't redo it
    db.bake_upsert(book_id, "baking", round=r + 1,
                   done_pages=db.bps_counts(book_id).get("done", 0))
    return len(db.bps_actionable(book_id))


# ---------------- finalise ----------------

def _finalise_page(book_id, pr, judged=None):
    """Store a page's chosen image + debug gen row. `judged` = {"attempt","why"} when
    a best-of judge overrode the min-score pick."""
    s = pr.state
    if s["best"] is None:
        # No critique ever scored this page. If generation nonetheless produced an
        # image (the failure mode that stranded 46 HP pages: the batch critic returned
        # empty/blocked responses every round), keep the last drawn candidate unscored
        # rather than dropping the page -- an unscored illustration beats a blank.
        if pr.cand is not None:
            data = _compress(pr.cand, SCENE_MAXW, WEBP_QUALITY)
            pr.trace["chosen"] = pr.attempt
            pr.trace["fallback"] = "kept last candidate (critique never scored this page)"
            db.scene_store(book_id, pr.idx, data, None, trace=json.dumps(pr.trace))
            db.scene_gen_add(book_id, pr.idx, pr.gen_id, pr.ctx["page"]["brief"],
                             json.dumps(pr.ctx["states"]), pr.attempt, None)
            db.bps_save(book_id, pr.idx, status="done", done=1, best_score=None,
                        best_attempt=pr.attempt)
            return
        db.bps_save(book_id, pr.idx, status="failed")
        return
    data, score, chosen = s["best"]
    if judged:
        for c in s["cands"]:
            if c["n"] == judged["attempt"]:
                data, score, chosen = c["data"], c["score"], c["n"]
                pr.trace["judge_pick"] = {"attempt": chosen, "why": judged.get("why", "")}
                break
    data = _compress(data, SCENE_MAXW, WEBP_QUALITY)
    pr.trace["chosen"] = chosen
    db.scene_store(book_id, pr.idx, data, score, trace=json.dumps(pr.trace))
    db.scene_gen_add(book_id, pr.idx, pr.gen_id, pr.ctx["page"]["brief"],
                     json.dumps(pr.ctx["states"]), chosen, score)
    # done=1 marks the page finished for bps_actionable / bps_counts (not just the
    # status string), so a resume won't re-process it and progress counts correctly.
    db.bps_save(book_id, pr.idx, status="done", done=1, best_score=score,
                best_attempt=chosen)


def _escalate_interactive(book_id, stragglers, runs):
    """Pages the batch critic never scored (every round's image came back blocked/empty
    from the critic) are escalated to the INTERACTIVE render: it regenerates fresh
    images -- the actual recovery path, since a different image usually isn't blocked --
    and critiques them with robust per-call retries, so a page that never scored in the
    batch usually converges to a properly-scored one here. If it still can't be scored,
    _render_scene keeps its best candidate, so the page ends up illustrated either way.
    Interactive calls are full price (not batched); this runs only for the stragglers."""
    targets = [i for i in stragglers if runs[i].state["best"] is None]
    if not targets:
        return
    log(f"escalating {len(targets)} un-scored page(s) to interactive render")

    def one(idx):
        try:
            # fast_critique: single no-backoff critique per attempt -- these pages'
            # critiques are likely blocked, and regeneration (not retrying) is what
            # recovers them; the unscored fallback catches the rest.
            scene.generate_scene(book_id, idx, fast_critique=True)
            db.bps_save(book_id, idx, status="done", done=1)
            return True
        except Exception as ex:  # noqa: BLE001 -- leave it a straggler for the fallback
            log(f"interactive render failed for page {idx}: {ex}")
            return False

    with ThreadPoolExecutor(max_workers=INTERACTIVE_WORKERS) as ex:
        n = sum(1 for ok in ex.map(one, targets) if ok)
    log(f"interactive render resolved {n}/{len(targets)} page(s)")


def finalise(book_id, runs):
    """Finish every page that never passed. First escalate pages the batch critic never
    scored to the interactive render (fresh regeneration usually unblocks them); then a
    best-of judge picks the strongest candidate of each remaining scored straggler and
    stores it. Anything still un-scored falls back to its last drawn image in
    _finalise_page -- an illustration beats a blank. Passed pages were stored in-round."""
    stragglers = [i for i in db.bps_actionable(book_id) if i in runs]
    if ESCALATE_INTERACTIVE:
        _escalate_interactive(book_id, stragglers, runs)
        stragglers = [i for i in db.bps_actionable(book_id) if i in runs]   # drop the resolved
    judgeable = [i for i in stragglers if len(runs[i].state["cands"]) > 1]
    picks = {}
    if judgeable:
        reqs = [{"key": str(i),
                 "parts": gem.judge_parts(JUDGE_BEST.format(brief=runs[i].ctx["page"]["brief"]),
                                          [c["data"] for c in runs[i].state["cands"]]),
                 "generation_config": gem.json_config(JUDGE_SCHEMA, temperature=0.2)}
                for i in judgeable]
        job = _submit_or_reattach(book_id, MAX_ROUNDS, "judge", CRITIQUE_MODEL, reqs,
                                  f"bake b{book_id} judge")
        if _await(book_id, MAX_ROUNDS, "judge", job) == gem.BATCH_DONE:
            for idx, resp in gem.batch_results(job).items():
                if resp is None:
                    continue
                gem.record_batch_usage(resp, CRITIQUE_MODEL, "critique")
                try:
                    v = gem._coerce_json(resp.text, gem._block_reason(resp))
                    pick = int(v.get("best", 0)) - 1
                    cands = runs[int(idx)].state["cands"]
                    if 0 <= pick < len(cands):
                        picks[int(idx)] = {"attempt": cands[pick]["n"], "why": v.get("why", "")}
                except Exception:  # noqa: BLE001
                    pass
    for i in stragglers:
        _finalise_page(book_id, runs[i], judged=picks.get(i))


# ---------------- entry ----------------

def run(book_id: int):
    book = db.get_book(book_id)
    if not book:
        raise ValueError(f"no book {book_id}")
    with costs.run_as(f"book:{book_id}"):
        runs = prepare(book_id)
        total = len(db.get_pages(book_id))
        # resume from the last-unfinished round (0 on a fresh bake); a completed round
        # advanced the pointer to r+1 so it is not redone.
        start_round = (db.bake_get(book_id) or {}).get("round") or 0
        db.bake_upsert(book_id, "baking", total_pages=total,
                       done_pages=db.bps_counts(book_id).get("done", 0))
        if start_round:
            log(f"resuming at round {start_round}")
        for r in range(start_round, MAX_ROUNDS):
            if _cancelled(book_id):
                log("cancelled")
                db.set_status(book_id, "roster_review", "bake cancelled — review or re-illustrate")
                return
            if run_round(book_id, r, runs) == 0:
                break
        if _cancelled(book_id):
            db.set_status(book_id, "roster_review", "bake cancelled — review or re-illustrate")
            return
        finalise(book_id, runs)
        done = db.bps_counts(book_id).get("done", 0)
        db.bake_upsert(book_id, "done", round=MAX_ROUNDS, done_pages=done)
        db.set_status(book_id, "ready", f"{done} pages illustrated (batch)")
        log(f"book {book_id} bake done: {done}/{total} pages")


def main():
    book_id = int(sys.argv[1])
    try:
        run(book_id)
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        db.bake_upsert(book_id, "failed", detail=f"{type(ex).__name__}: {str(ex)[:300]}")
        db.set_status(book_id, "roster_review", f"bake failed: {str(ex)[:120]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
