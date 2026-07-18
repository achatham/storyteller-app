"""Illustrate a whole book with the Batch API (~50% cheaper than the interactive
per-page path), run as its own subprocess:

    python -m webapp.batch_bake <book_id>

Batch mode is asynchronous, so the tight per-image critique/revise loop of
webapp/scene.py becomes ROUNDS across every page:

    roster    -> draw the reference sheets in the BACKGROUND (batch_roster), saving
                 wave 1 (anchors) before wave 2 (variants) so sheets land mid-draw
    admit     -> each round, admit any page whose OWN sheets are all present (so it
                 starts illustrating without waiting for the whole roster); once the
                 roster finishes, force-admit the rest (missing sheets drawn interactively)
    round r   -> GENERATE batch (image model)   : one draft per still-open page
                 CRITIQUE batch (text model)     : score + verdict per draft
                 VERIFY   batch (text model)     : carry-forward fix check (revises)
                 apply the SAME accept/revise/regenerate bookkeeping as the lazy path
    tail      -> once the roster is drawn and < INTERACTIVE_TAIL pages remain, finish
                 them with the interactive renderer (batch per-round latency isn't worth
                 it for a handful of pages)
    finalise  -> best-of judge for pages that never passed; store every page image

Pages stream in as their sheets become ready rather than waiting for a full roster
phase, so the reader (which shows pages progressively during a bake) gets its first
illustrations sooner. Each page still gets up to SCENE_TRIES attempts regardless of
which round it joined -- the attempt cap is per-page, not the global round counter.

All decision logic is shared with the interactive path via webapp/scene.py
(build_scene_context / build_round_request / apply_verdict / ...), so a batched
page converges exactly like an interactively-rendered one. Resumable: submitted
jobs live in the batch_jobs table, so a restarted bake reattaches instead of
resubmitting, and finished pages are stored as they complete.
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from pipeline import gem, costs
from pipeline.config import CRITIQUE_MODEL, IMAGE_SIZE

from . import batch_roster, db, scene
from .scene import (build_scene_context, build_round_request, apply_verdict,
                    new_scene_state, critique_prompt, critique_prompt_lite,
                    _attempt_trace, _compress,
                    SCENE_TRIES, SAFETY_REWRITES, PASS_THRESHOLD, DEBUG_MAXW, DEBUG_QUALITY,
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
# Once the roster is drawn and fewer than this many pages remain actionable, stop
# running batch rounds and finish the tail with the INTERACTIVE renderer. A batch
# round costs minutes-per-job latency across several serial jobs (gen/critique/verify),
# which isn't worth it for a handful of pages -- interactive draws them in parallel far
# faster (full price, full critique/revise loop). 0 disables the cutover (batch to the end).
INTERACTIVE_TAIL = int(os.environ.get("STORY_BAKE_INTERACTIVE_TAIL", "20"))
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
                           "ref_chars": s["ref_chars"], "best_actionable": best_actionable,
                           "safe_prompt": s["safe_prompt"], "safety_tries": s["safety_tries"]})

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
        s["safe_prompt"] = carry.get("safe_prompt", "")
        s["safety_tries"] = carry.get("safety_tries", 0)
        if s["best"]:
            actionable = carry.get("best_actionable", True)
            s["best_key"] = (0 if actionable else 1, s["best"][1])


# ---------------- seed / roster / admission ----------------

def _seed(book_id):
    """Seed per-page bake state and drop pages that already have an illustration (drawn
    lazily as-read, or by a prior bake) so the bake only fills pages without an image.
    Skipped pages are marked done up front: out of the actionable set, still counted."""
    db.bps_init(book_id, [p["idx"] for p in db.get_pages(book_id)])
    skipped = db.bps_skip_illustrated(book_id)
    if skipped:
        log(f"skipping {skipped} page(s) that already have an illustration")


class _RosterThread:
    """Draws the batch roster in the BACKGROUND so page rounds overlap it (~50% cheaper
    than interactive, and pages don't wait for a full roster phase). draw_roster saves
    wave 1 (anchors) before wave 2 (variants), so pages needing only anchors go ready --
    and start generating -- while wave 2 is still drawing. `finished` flips true on
    completion OR failure; that's when the run loop force-admits any remaining pages,
    letting build_scene_context draw whatever the batch couldn't produce interactively.
    Idempotent on resume: draw_roster skips already-cached sheets. Daemon: a cancelled
    bake exits the process, killing the thread."""

    def __init__(self, book_id):
        self.book_id = book_id
        self.finished = False
        self._t = None

    def start(self):
        def _go():
            try:
                n = batch_roster.draw_roster(self.book_id, log=log)
                log(f"batch roster drew {n} sheets")
            except Exception as ex:  # noqa: BLE001 -- fall back to interactive per-page draws
                log(f"batch roster failed ({type(ex).__name__}: {ex}); "
                    "remaining sheets drawn interactively")
            finally:
                self.finished = True
        self._t = threading.Thread(target=_go, name=f"roster-{self.book_id}", daemon=True)
        self._t.start()

    def join(self):
        if self._t:
            self._t.join()
        self.finished = True


def _build_page_run(book_id, idx):
    """Build one page's PageRun (scene context + debug gen id + trace), restoring prior
    state for a resumed/continued page. Returns None if the context can't be built (the
    page is marked failed). build_scene_context draws any still-missing sheet for this
    page interactively as a side effect -- the fallback for sheets the batch skipped."""
    pr = PageRun(idx)
    pr.book_id = book_id
    row = db.bps_get(book_id, idx)
    try:
        pr.ctx = build_scene_context(book_id, idx)
    except Exception as ex:  # noqa: BLE001 -- a bad page shouldn't sink the bake
        log(f"page {idx} context failed: {ex}")
        db.bps_save(book_id, idx, status="failed")
        return None
    if row and row["gen_id"]:      # resume: keep the same debug gen + prior state
        pr.gen_id = row["gen_id"]
        pr.restore(row)
    else:
        pr.gen_id = db.next_gen_id(book_id, idx)
    pr.trace = {"states": pr.ctx["states"], "max_tries": MAX_ROUNDS, "attempts": []}
    return pr


def _page_ready(book_id, idx, plan_cache) -> bool:
    """True once every real (non-view) roster sheet page `idx` references is drawn, so
    the page can generate without waiting for the rest of the roster. View sheets ('__')
    are drawn interactively by build_scene_context and don't gate readiness. The sheet
    plan is page-stable, so it's cached across rounds (a cheap DB-only resolve)."""
    plan = plan_cache.get(idx)
    if plan is None:
        try:
            plan = scene.plan_page_sheets(book_id, idx)
        except Exception:  # noqa: BLE001 -- treat as not-ready; a later force-admit covers it
            return False
        plan_cache[idx] = plan
    return all(db.has_sheet(book_id, m["entity_id"], m["variant_id"])
               for m in plan if not m["variant_id"].startswith("__"))


def _admit(book_id, runs, plan_cache, force) -> int:
    """Admit still-actionable pages into `runs`. While the roster is drawing, only pages
    whose sheets are all present are admitted (they start generating early); once the
    roster is done, `force` admits the rest. Contexts build in parallel (independent
    text passes). Returns how many pages were newly admitted."""
    targets = [idx for idx in db.bps_actionable(book_id)
               if idx not in runs and (force or _page_ready(book_id, idx, plan_cache))]
    if not targets:
        return 0
    added = 0
    with ThreadPoolExecutor(max_workers=PREPARE_WORKERS) as ex:
        for pr in ex.map(lambda i: _build_page_run(book_id, i), targets):
            if pr is not None:
                runs[pr.idx] = pr
                added += 1
    if added:
        log(f"admitted {added} page(s) ({'roster done' if force else 'sheets ready'})")
    return added


# ---------------- batch job helpers ----------------

def _submit_or_reattach(book_id, r, kind, model, reqs, display):
    """Reuse an already-submitted job for this (round, kind) if present (resume),
    else submit a new one. Returns the job name."""
    coarse = "image" if kind.startswith("gen:") else "text"
    existing = db.bjob_get(book_id, r, kind)
    if existing and existing["job_name"]:
        db.batch_req_add(book_id, existing["job_name"], coarse, len(reqs))
        log(f"r{r} {kind}: reattaching {existing['job_name']} ({existing['state']})")
        return existing["job_name"]
    job = gem.batch_submit(reqs, model=model, display_name=display)
    db.bjob_upsert(book_id, r, kind, job, "JOB_STATE_PENDING")
    db.batch_req_add(book_id, job, coarse, len(reqs))
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
                continue
            # No image: a blocked/empty generation. On a content-policy refusal, rewrite
            # the prompt so the NEXT round regenerates a policy-safe version -- via the
            # same state["safe_prompt"] field build_round_request reads on both paths.
            # Persisted to carry_json so a crash-resume keeps the rewrite. A transient
            # empty just leaves cand=None and retries the same prompt next round.
            reason = gem._block_reason(resp)
            st = runs[idx].state
            if gem.is_policy_refusal(reason) and st["safety_tries"] < SAFETY_REWRITES:
                st["safe_prompt"] = gem.rewrite_prompt_safely(runs[idx].req["prompt"], reason)
                st["safety_tries"] += 1
                runs[idx].save(status="pending")
                log(f"page {idx}: image blocked [{reason}] -- rewrote prompt for next round")


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
    # LITE re-critique: pages that drew an image but got no parseable critique are
    # usually a PROHIBITED_CONTENT block on the embedded story text -- retry them in
    # one more batch with that text stripped + image only (no child reference
    # sheets), which reliably clears the child-safety filter and gets a real score.
    blocked = [idx for idx in open_idxs if runs[idx].cand is not None and idx not in out]
    if blocked:
        lite_reqs = [{"key": str(idx),
                      "parts": gem.critique_parts(critique_prompt_lite(runs[idx].ctx), runs[idx].cand),
                      "generation_config": gem.json_config(SCENE_CRITIQUE_SCHEMA, temperature=0.3)}
                     for idx in blocked]
        ljob = _submit_or_reattach(book_id, r, "critique_lite", CRITIQUE_MODEL, lite_reqs,
                                   f"bake b{book_id} r{r} critique-lite")
        if _await(book_id, r, "critique_lite", ljob) == gem.BATCH_DONE:
            n = 0
            for idx, resp in gem.batch_results(ljob).items():
                if resp is None:
                    continue
                gem.record_batch_usage(resp, CRITIQUE_MODEL, "critique")
                try:
                    out[int(idx)] = gem._coerce_json(resp.text, gem._block_reason(resp))
                    n += 1
                except Exception as ex:  # noqa: BLE001
                    log(f"r{r} lite critique still failed for page {idx}: {ex}")
            log(f"r{r} lite critique recovered {n}/{len(blocked)} blocked page(s)")
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


def run_round(book_id, r, runs, open_idxs) -> int:
    """Run one full round over the given open pages (chosen by the caller: admitted,
    still actionable, attempts remaining). Returns how many pages remain actionable."""
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


def _drain_interactive(book_id):
    """Finish the remaining actionable pages with the INTERACTIVE renderer instead of
    more batch rounds -- the tail cutover (see INTERACTIVE_TAIL). Each page gets the full
    interactive critique/revise loop and is stored by generate_scene itself; we just mark
    its bake row done. Runs in parallel (full price, but a small tail). A page that still
    can't be drawn (every image blocked) is left actionable so finalise() can fall back to
    any batch candidate it accumulated. Cancellation-aware between pages."""
    targets = db.bps_actionable(book_id)
    if not targets:
        return
    log(f"interactive tail: drawing {len(targets)} remaining page(s)")

    def one(idx):
        if _cancelled(book_id):
            return False
        try:
            scene.generate_scene(book_id, idx)   # full critique/revise; stores the scene
            db.bps_save(book_id, idx, status="done", done=1)
            return True
        except Exception as ex:  # noqa: BLE001 -- leave actionable for finalise's fallback
            log(f"interactive tail render failed for page {idx}: {ex}")
            return False

    with ThreadPoolExecutor(max_workers=INTERACTIVE_WORKERS) as ex:
        n = sum(1 for ok in ex.map(one, targets) if ok)
    db.bake_upsert(book_id, "baking", done_pages=db.bps_counts(book_id).get("done", 0))
    log(f"interactive tail drew {n}/{len(targets)} page(s)")


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
        _seed(book_id)
        total = len(db.get_pages(book_id))
        # Draw the roster in the BACKGROUND so a page starts illustrating as soon as ITS
        # OWN sheets are ready, instead of waiting for the whole roster (better time to
        # first illustrated page -- the reader shows pages progressively during a bake).
        roster = _RosterThread(book_id)
        if scene.ROSTER_BATCH:
            roster.start()
        else:
            roster.finished = True   # no batch roster: force-admit draws sheets interactively
        runs: dict = {}
        plan_cache: dict = {}
        # resume from the last-unfinished round (0 on a fresh bake); a completed round
        # advanced the pointer to r+1 so it is not redone.
        start_round = (db.bake_get(book_id) or {}).get("round") or 0
        db.bake_upsert(book_id, "baking", total_pages=total,
                       done_pages=db.bps_counts(book_id).get("done", 0))
        if start_round:
            log(f"resuming at round {start_round}")
        # The round counter is just a batch-job namespace + resume pointer now; the real
        # stop condition is per-page (a page leaves the open set after SCENE_TRIES
        # attempts). A late-admitted page can push total iterations past MAX_ROUNDS, so
        # cap generously -- every open page still increments its attempt each round, so
        # once the roster is done the loop drains in <= MAX_ROUNDS more iterations.
        r = start_round
        max_round = start_round + MAX_ROUNDS * 4
        tail = False
        while r < max_round:
            if _cancelled(book_id):
                log("cancelled")
                db.set_status(book_id, "roster_review", "bake cancelled — review or re-illustrate")
                return
            _admit(book_id, runs, plan_cache, force=roster.finished)
            outstanding = db.bps_actionable(book_id)
            # Tail cutover: once the roster is drawn and only a small tail of pages
            # remains, stop batching and finish them interactively -- a batch round's
            # minutes-per-job latency isn't worth it for a handful of pages.
            if INTERACTIVE_TAIL and roster.finished and 0 < len(outstanding) < INTERACTIVE_TAIL:
                log(f"{len(outstanding)} page(s) left (< {INTERACTIVE_TAIL}) -> interactive tail")
                tail = True
                break
            open_idxs = [i for i in outstanding
                         if i in runs and runs[i].attempt < MAX_ROUNDS]
            if not open_idxs:
                if roster.finished:
                    break                     # roster done and nothing left to generate
                time.sleep(POLL_SECONDS)       # sheets still drawing -- wait, then re-admit
                continue
            run_round(book_id, r, runs, open_idxs)
            r += 1
        roster.join()
        if _cancelled(book_id):
            db.set_status(book_id, "roster_review", "bake cancelled — review or re-illustrate")
            return
        if tail:
            _drain_interactive(book_id)       # finish the last few pages interactively
        finalise(book_id, runs)
        done = db.bps_counts(book_id).get("done", 0)
        db.bake_upsert(book_id, "done", round=r, done_pages=done)
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
