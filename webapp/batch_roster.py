"""Draw a book's whole roster of reference sheets via the Batch API (~50% cheaper
than drawing each one interactively), in two dependency waves so a character's
several variants stay one identity:

    wave 1: each entity's ANCHOR variant (the first real variant any page needs)
            plus every single-variant entity  -> one image batch.
    wave 2: the remaining variants, each generated with its entity's freshly-drawn
            anchor sheet attached as an identity reference (same face/build).

Each wave is GENERATE (image batch) -> CRITIQUE (single-subject text batch) -> keep
the best, with one reroll (SHEET_TRIES) for sheets that miss the bar. A blocked or
empty critique is treated as "keep the drawn sheet unscored" rather than dropping
it, so a sheet the critic won't grade still lands (the same failure that stranded
46 Harry-Potter pages in the first bake).

Only real (character/prop/setting) sheets are batched here. 'View' sheets (a named
spot inside a setting, variant ids starting with '__') and any sheet the batch
can't produce are left to the interactive pass in scene.draw_all_sheets, which
draws whatever is still missing (build_scene_context skips already-cached sheets).

Run indirectly via scene.draw_all_sheets (gated by STORY_ROSTER_BATCH); not a
standalone subprocess -- it executes inside the book-processing worker, which is
already a long-running background process, and polls the batch jobs to completion.
"""
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from pipeline import gem, costs
from pipeline.config import IMAGE_SIZE, CRITIQUE_MODEL, ROSTER_IMAGE_MODEL, WEBP_QUALITY

from . import db, scene

POLL_SECONDS = int(os.environ.get("STORY_BATCH_POLL", "20"))
PLAN_WORKERS = int(os.environ.get("STORY_ROSTER_PLAN_WORKERS", "6"))

SHEET_CRITIQUE_SCHEMA = {"type": "object", "properties": {
    "clean_sheet": {"type": "integer"}, "match": {"type": "integer"},
    "issues": {"type": "array", "items": {"type": "string"}},
    "fix_hint": {"type": "string"}}, "required": ["clean_sheet", "match"]}


def _skey(eid: str, vid: str) -> str:
    return f"{eid}|{vid}"


def _await(job_name: str) -> str:
    while True:
        st = gem.batch_state(job_name)
        if st in gem.BATCH_TERMINAL:
            return st
        time.sleep(POLL_SECONDS)


def _collect(book_id, log) -> dict:
    """The union of real (non-view, not-yet-drawn) reference sheets every page will
    reference: {(entity_id, variant_id): member}. Planning is read-only (no draws)."""
    idxs = [p["idx"] for p in db.get_pages(book_id)]

    def plan(idx):
        try:
            return scene.plan_page_sheets(book_id, idx)
        except Exception as ex:  # noqa: BLE001 -- a bad page shouldn't sink the roster
            log(f"[roster] plan page {idx} failed: {ex}")
            return []

    needed: dict = {}
    with ThreadPoolExecutor(max_workers=PLAN_WORKERS) as ex:
        for members in ex.map(plan, idxs):
            for m in members:
                eid, vid = m["entity_id"], m["variant_id"]
                if vid.startswith("__"):          # view/synthetic -> interactive fallback
                    continue
                if (eid, vid) in needed:
                    continue
                if not (m.get("appearance") or m.get("sheet_prompt")):
                    continue
                needed[(eid, vid)] = m
    # skip sheets already cached (e.g. drawn on a prior interrupted run, or edited)
    return {k: m for k, m in needed.items() if not db.has_sheet(book_id, k[0], k[1])}


def _draw_wave(book_id, wave, style_text, style_ref_bytes, use_anchor, log) -> int:
    """Generate + critique + save one wave of sheets, keeping the best of up to
    SHEET_TRIES attempts each. `use_anchor` attaches a same-entity sibling sheet as
    an identity reference (for wave 2, whose entities were drawn in wave 1)."""
    if not wave:
        return 0
    slots: dict = {}
    for m in wave:
        slots[_skey(m["entity_id"], m["variant_id"])] = {
            "m": m, "best": None, "attempts": [], "fix": "",
            "safe_prompt": "", "safety_tries": 0}

    # extra attempts beyond SHEET_TRIES cover slots whose draw was policy-refused and
    # rewritten (a refused attempt produced no candidate, so it shouldn't cost a try).
    for attempt in range(1, scene.SHEET_TRIES + scene.SAFETY_REWRITES + 1):
        todo = [k for k, s in slots.items()
                if s["best"] is None or (s["best"][1] or 0) < scene.SHEET_PASS]
        if not todo:
            break

        # --- GENERATE (image batch) ---
        gen_reqs = []
        for k in todo:
            s = slots[k]
            m = s["m"]
            sib = (db.get_any_sheet(book_id, m["entity_id"], exclude_variant_id=m["variant_id"])
                   if use_anchor else None)
            r = scene.sheet_gen_request(m, style_text, style_ref_bytes, sib)
            if not r:
                continue
            # a prior draw was refused on policy -> use the rewritten, policy-safe prompt
            base = s["safe_prompt"] or r["prompt"]
            prompt = base + (f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {s['fix']}"
                             if s["fix"] else "")
            s["_prompt"], s["_desc"] = prompt, r["desc"]
            gen_reqs.append({"key": k,
                             "parts": [gem.text_part(prompt)]
                                      + [gem.image_part(b) for b in r["ref_bytes"]],
                             "generation_config": gem.image_gen_config(aspect=r["aspect"],
                                                                       size=IMAGE_SIZE)})
        if not gen_reqs:
            break
        gjob = gem.batch_submit(gen_reqs, model=ROSTER_IMAGE_MODEL,
                                display_name=f"roster b{book_id} gen a{attempt}")
        db.batch_req_add(book_id, gjob, "image", len(gen_reqs))
        log(f"[roster] gen a{attempt}: submitted {gjob} ({len(gen_reqs)} sheets)")
        if _await(gjob) != gem.BATCH_DONE:
            log(f"[roster] gen batch {gjob} did not succeed; stopping wave")
            break
        cands = {}
        for k, resp in gem.batch_results(gjob).items():
            if resp is None:
                continue
            gem.record_batch_usage(resp, ROSTER_IMAGE_MODEL, "image", images=1)
            img = gem.response_image_bytes(resp)
            if img:
                cands[k] = img
                continue
            # blocked/empty draw: on a policy refusal, rewrite this slot's prompt so the
            # next attempt regenerates a policy-safe version (e.g. a distressed child).
            reason = gem._block_reason(resp)
            s = slots.get(k)
            if s is not None and gem.is_policy_refusal(reason) and s["safety_tries"] < scene.SAFETY_REWRITES:
                s["safe_prompt"] = gem.rewrite_prompt_safely(s.get("_prompt", ""), reason)
                s["safety_tries"] += 1
                log(f"[roster] {k}: image blocked [{reason}] -- rewrote prompt for next attempt")

        # --- CRITIQUE (single-subject text batch) ---
        crits = {}
        crit_reqs = [{"key": k,
                      "parts": [gem.text_part(scene.SHEET_CRITIQUE.format(
                                    desc=slots[k].get("_desc") or "(the subject)")),
                                gem.image_part(cands[k])],
                      "generation_config": gem.json_config(SHEET_CRITIQUE_SCHEMA, temperature=0.3)}
                     for k in cands]
        if crit_reqs:
            cjob = gem.batch_submit(crit_reqs, model=CRITIQUE_MODEL,
                                    display_name=f"roster b{book_id} crit a{attempt}")
            db.batch_req_add(book_id, cjob, "text", len(crit_reqs))
            if _await(cjob) == gem.BATCH_DONE:
                for k, resp in gem.batch_results(cjob).items():
                    if resp is None:
                        continue
                    gem.record_batch_usage(resp, CRITIQUE_MODEL, "critique")
                    try:
                        crits[k] = gem._coerce_json(resp.text, gem._block_reason(resp))
                    except Exception as ex:  # noqa: BLE001 -- blocked/empty: keep sheet unscored
                        log(f"[roster] critique parse failed for {k}: {ex}")

        # --- APPLY: score, keep best, set the reroll fix hint ---
        for k, img in cands.items():
            s = slots[k]
            crit = crits.get(k)
            if crit is not None:
                cs, mt = crit.get("clean_sheet", 0), crit.get("match", 0)
                score, avg = min(cs, mt), round((cs + mt) / 2, 2)
                s["fix"] = crit.get("fix_hint", "")
            else:
                score, avg = None, None   # unscored: don't reroll harder, just keep it
            s["attempts"].append({"attempt": attempt, "prompt": s.get("_prompt", ""),
                                  "data": img, "critique": crit, "min": score, "avg": avg})
            if s["best"] is None or (score or 0) > (s["best"][1] or -1):
                s["best"] = (img, score, attempt)

    # --- SAVE best of each slot ---
    saved = 0
    for k, s in slots.items():
        if s["best"] is None:
            log(f"[roster] {k}: no candidate produced -- left for interactive fallback")
            continue
        m = s["m"]
        data = scene._compress(s["best"][0], 0, WEBP_QUALITY)   # sheets feed gen -> keep res
        db.save_sheet(book_id, m["entity_id"], m["variant_id"], data)
        scene._save_sheet_history(book_id, m["entity_id"], m["variant_id"],
                                  m.get("appearance", ""),
                                  {"attempts": s["attempts"], "chosen": s["best"][2]})
        saved += 1
    return saved


def draw_roster(book_id, log=print) -> int:
    """Draw the bulk of a book's roster sheets in two batched waves. Returns how many
    sheets were saved (the interactive pass in draw_all_sheets covers the remainder)."""
    book = db.get_book(book_id)
    if not book:
        return 0
    with costs.run_as(f"book:{book_id}"):
        style_text = scene._style_text(book["style"])
        style_ref = scene.style_anchor_bytes(book)
        needed = _collect(book_id, log)
        if not needed:
            log("[roster] nothing to batch (all needed sheets already present)")
            return 0
        # Split by entity: an entity with no drawn sheet yet contributes an anchor to
        # wave 1 and its other variants to wave 2; an entity that already has a sheet
        # (drawn earlier / edited) sends all its needed variants straight to wave 2.
        by_ent = defaultdict(list)
        for (eid, vid), m in needed.items():
            by_ent[eid].append((vid, m))
        wave1, wave2 = [], []
        for eid, items in by_ent.items():
            items.sort(key=lambda t: t[0])
            if db.get_any_sheet(book_id, eid) is not None:
                wave2 += [m for _, m in items]
            else:
                wave1.append(items[0][1])
                wave2 += [m for _, m in items[1:]]
        log(f"[roster] {len(needed)} sheets to batch (wave1={len(wave1)} anchors, "
            f"wave2={len(wave2)}), model={ROSTER_IMAGE_MODEL}")
        n = _draw_wave(book_id, wave1, style_text, style_ref, use_anchor=False, log=log)
        n += _draw_wave(book_id, wave2, style_text, style_ref, use_anchor=True, log=log)
        log(f"[roster] batch drew {n}/{len(needed)} sheets")
        return n
