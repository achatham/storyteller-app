"""Draw a book's whole roster of reference sheets via the Batch API (~50% cheaper
than drawing each one interactively), in two dependency waves so a character's
several variants stay one identity:

    wave 1: each entity's ANCHOR variant (the first real variant any page needs)
            plus every single-variant entity  -> one image batch.
    wave 2: the remaining variants, each generated with its entity's freshly-drawn
            anchor sheet attached as an identity reference (same face/build).

Each wave is GENERATE (one image batch) -> CRITIQUE (interactive single-subject
critic, in parallel) -> keep the best, with one reroll (SHEET_TRIES) for sheets
that miss the bar. A sheet is SAVED the moment it is settled -- it passed, or the
critic couldn't grade it (kept unscored rather than dropped, the same rule as
pages) -- so the roster page fills in as soon as the first image batch lands
instead of after the last straggler's reroll.

Only the image generation is batched. The critique used to be a second batch job
per attempt, which held every finished sheet in memory until a text job that saves
cents came back -- once for two hours.

Only real (character/prop/setting) sheets are batched here. 'View' sheets (a named
spot inside a setting, variant ids starting with '__') and any sheet the batch
can't produce are left to the interactive pass in scene.draw_all_sheets, which
draws whatever is still missing (build_scene_context skips already-cached sheets).

Run indirectly via scene.draw_all_sheets (gated by STORY_ROSTER_BATCH); not a
standalone subprocess -- it executes inside the book-processing worker, which is
already a long-running background process, and polls the batch jobs to completion.
"""
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from pipeline import gem, costs
from pipeline.config import ROSTER_IMAGE_MODEL, WEBP_QUALITY

from . import batchjob, db, scene

PLAN_WORKERS = int(os.environ.get("STORY_ROSTER_PLAN_WORKERS", "6"))

# Roster jobs share the page rounds' batch_jobs bookkeeping, under a round of their
# own so the (book, round, kind) key can't collide with a page round.
ROSTER_ROUND = -1


def _skey(eid: str, vid: str) -> str:
    return f"{eid}|{vid}"


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


def _save_slot(book_id, s) -> int:
    """Store a slot's best candidate as the sheet (+ its debug history). Returns 1
    if saved, 0 if the slot never produced an image."""
    if s["best"] is None:
        return 0
    m = s["m"]
    data = scene._compress(s["best"][0], 0, WEBP_QUALITY)   # sheets feed gen -> keep res
    db.save_sheet(book_id, m["entity_id"], m["variant_id"], data)
    scene._save_sheet_history(book_id, m["entity_id"], m["variant_id"],
                              m.get("appearance", ""),
                              {"attempts": s["attempts"], "chosen": s["best"][2]})
    s["saved"] = True
    return 1


def _draw_wave(book_id, w, wave, style_text, style_ref_bytes, use_anchor, log) -> int:
    """Generate + critique + save one wave of sheets, keeping the best of up to
    SHEET_TRIES attempts each. `use_anchor` attaches a same-entity sibling sheet as
    an identity reference (for wave 2, whose entities were drawn in wave 1). `w` is
    the wave number, which with the attempt names the job for resume."""
    if not wave:
        return 0
    slots: dict = {}
    for m in wave:
        slots[_skey(m["entity_id"], m["variant_id"])] = {
            "m": m, "best": None, "attempts": [], "fix": "", "saved": False,
            "safe_prompt": "", "safety_tries": 0}
    saved = 0

    # extra attempts beyond SHEET_TRIES cover slots whose draw was policy-refused and
    # rewritten (a refused attempt produced no candidate, so it shouldn't cost a try).
    for attempt in range(1, scene.SHEET_TRIES + scene.SAFETY_REWRITES + 1):
        todo = [k for k, s in slots.items() if not s["saved"]]
        if not todo:
            break

        # --- GENERATE (image batch) ---
        reqs = []
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
            reqs.append({"key": k, "prompt": prompt, "ref_bytes": r["ref_bytes"],
                         "aspect": r["aspect"]})
        if not reqs:
            break
        results = batchjob.run_image_batch(book_id, ROSTER_ROUND, f"w{w}:gen:a{attempt}", reqs,
                                           ROSTER_IMAGE_MODEL, f"roster b{book_id} gen a{attempt}",
                                           log=lambda m: log(f"[roster] {m}"))
        if results is None:
            log("[roster] gen batch did not succeed; stopping wave")
            break
        cands = {}
        for k, res in results.items():
            # A reattached job answers the request set it was submitted with, which a
            # resume can have re-planned around (sheets saved since are dropped from
            # the wave). Ignore anything we're no longer drawing.
            s = slots.get(k)
            if s is None or res is None:
                continue
            if isinstance(res, (bytes, bytearray)):
                cands[k] = bytes(res)
            elif res.policy and s["safety_tries"] < scene.SAFETY_REWRITES:
                # policy refusal: rewrite this slot's prompt so the next attempt
                # regenerates a policy-safe version (e.g. a distressed child).
                s["safe_prompt"] = gem.rewrite_prompt_safely(s["_prompt"], res.reason)
                s["safety_tries"] += 1
                log(f"[roster] {k}: image blocked [{res.reason}] -- rewrote prompt for next attempt")

        # --- CRITIQUE (interactive, parallel) ---
        crits = batchjob.run_text_parallel(
            book_id, cands, lambda k, img: scene.critique_sheet(img, slots[k]["_desc"], style_text),
            log=lambda m: log(f"[roster] {m}"), what="sheet critique")

        # --- APPLY: score, keep best, save what is settled ---
        for k, img in cands.items():
            s = slots[k]
            crit = crits.get(k)
            score = crit["score"] if crit else None   # None: unscorable -> keep, don't reroll
            if crit:
                s["fix"] = crit.get("fix_hint", "")
            s["attempts"].append({"attempt": attempt, "prompt": s["_prompt"], "data": img,
                                  "critique": crit, "min": score,
                                  "avg": crit["avg"] if crit else None})
            if s["best"] is None or (score or 0) > (s["best"][1] or -1):
                s["best"] = (img, score, attempt)
            if score is None or score >= scene.SHEET_PASS:
                saved += _save_slot(book_id, s)

    # --- SAVE the best of every slot that never settled ---
    for k, s in slots.items():
        if s["saved"]:
            continue
        if s["best"] is None:
            log(f"[roster] {k}: no candidate produced -- left for interactive fallback")
            continue
        saved += _save_slot(book_id, s)
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
        n = _draw_wave(book_id, 1, wave1, style_text, style_ref, use_anchor=False, log=log)
        n += _draw_wave(book_id, 2, wave2, style_text, style_ref, use_anchor=True, log=log)
        log(f"[roster] batch drew {n}/{len(needed)} sheets")
        return n
