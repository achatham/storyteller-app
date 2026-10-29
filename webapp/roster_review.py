"""Roster review: one whole-book look at the cast plan, after segmentation and
BEFORE any reference sheet is drawn.

Every other planning step sees part of the book. The registry is built from the
whole source file (which can include sequel excerpts and back matter), and each
chapter's analyze pass invents its own section-local minor characters without
knowing what the other chapters named theirs. What that produces, seen on The
Golden Compass:
- the same minor character under two ids in two chapters (`annie` / `annie_captive`),
  so they get two different sheets and look like two different children;
- one scene's bear guard under three ids across four pages;
- registry entities from OTHER books in the series (Will Parry, Cittagazze) that no
  page uses;
- a prop entity that duplicates what a character variant already wears (Iorek's
  armor prop next to his armored variant), using up one of the page's MAX_REFS reference slots.

So a stronger model gets the whole roster at once -- every registry entity and
variant with how many pages use it, every chapter-local character with its
chapters and look, AND every reference sheet already drawn (it is shown the
images, so it can catch a sheet that contradicts its description or the story,
and pick which of two duplicate looks to keep) -- and proposes fixes. Code
validates every id it returns and only applies the safe ones (a drop must have
zero page uses; a merge must name real locals; redraws are capped).

In batch mode it runs after the roster is drawn (so it sees every sheet) and
before the review gate; in lazy mode it runs before warming, on the text plus
whatever sheets exist.

Usage:
    python -m webapp.roster_review <book_id> [--apply] [--json]
"""
import argparse
import collections
import json
import os
import re
import sys
import io
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from pipeline import gem
from pipeline.config import MAX_REFS, ROSTER_REVIEW_MODEL
from . import db

ENABLED = os.environ.get("STORY_ROSTER_REVIEW", "1") != "0"
PROMOTED_IMPORTANCE = 2   # a merged minor character: above a local, below the leads
MAX_REDRAWS = int(os.environ.get("STORY_ROSTER_REVIEW_REDRAWS", "12"))  # image-cost cap
THUMB_PX = 640            # sheets are shown downscaled: enough to judge a look, cheap in tokens

PROMPT = """You are the casting editor for an illustrated read-aloud edition of a novel{book}. \
The illustrations are drawn from a ROSTER: one canonical reference sheet per character / setting / \
prop look, reused on every page that look appears on. Before any sheet is drawn, review the whole \
roster for problems that would make the book look inconsistent or waste reference slots. After this text you are shown the reference sheets already drawn, each labelled with its entity_id/variant_id -- judge them as pictures, not just their descriptions.

How the roster was built (and why it goes wrong):
- REGISTRY entities were catalogued from the whole source FILE, which can include excerpts of \
sequels/prequels, previews, and back matter. The illustrated pages cover only the story itself.
- Each chapter was planned separately, and invented its own CHAPTER-LOCAL minor characters \
without seeing the other chapters. The same person can appear under different ids in different \
chapters (or twice in one), and a local can duplicate a registry entity.
- Each page gets at most {max_refs} reference sheets, so a prop that merely repeats what a \
character variant already wears (armor, a uniform) wastes a slot and can contradict that variant.

Return ONLY problems you are confident about:
1. same_as_registry -- a chapter-local id that is really a registry entity (and which variant).
2. merge_locals -- groups of chapter-local ids that are the SAME individual (same person, same \
creature, same object). Also list a single local id here if it recurs in several chapters and \
should get ONE consistent look. Do NOT merge different individuals of the same kind (two \
different guards who appear together are two people). Give the group a stable snake_case \
new_id, a display name, and keep_id: the member whose look (and sheet, if drawn) best fits \
the story -- the others adopt it.
3. drop -- registry entities or variants that do not belong to THIS story (from another book in \
the series, an epilogue excerpt, a far-future age) or are otherwise never needed. Only things \
with 0 page uses can be dropped; variant_id "" drops the whole entity.
4. redundant_props -- a registry prop that is fully part of a character variant's look \
(e.g. armor the armored variant already wears), listing the variants that cover it. The prop \
is removed only from pages where one of those variants is present.
5. redraw -- a DRAWN sheet that is wrong: contradicts its own description or the story (wrong \
species, age, colouring, outfit, wrong number of limbs, a companion animal missing or wrong), \
or is so unlike the same character's other looks that it reads as a different person. Give the \
problem and a one-sentence correction stating what the sheet must show. Only clear errors; at \
most {max_redraws}, worst first.
6. notes -- anything else worth a human's attention that you cannot fix with the above \
(a look that seems wrong for the story, a recurring character with no entity at all). Short.

=== REGISTRY (id, type, name, aliases, importance; then variants with page uses) ===
{registry}

=== CHAPTER-LOCAL CAST (id, name, chapters, page uses, look) ===
{locals}

=== CHAPTERS ===
{chapters}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "same_as_registry": {"type": "array", "items": {
            "type": "object",
            "properties": {"local_id": {"type": "string"}, "entity_id": {"type": "string"},
                           "variant_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["local_id", "entity_id", "variant_id", "reason"]}},
        "merge_locals": {"type": "array", "items": {
            "type": "object",
            "properties": {"local_ids": {"type": "array", "items": {"type": "string"}},
                           "new_id": {"type": "string"}, "name": {"type": "string"},
                           "keep_id": {"type": "string"},
                           "type": {"type": "string", "enum": ["character", "prop", "setting"]},
                           "reason": {"type": "string"}},
            "required": ["local_ids", "new_id", "name", "keep_id", "type", "reason"]}},
        "drop": {"type": "array", "items": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}, "variant_id": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["entity_id", "variant_id", "reason"]}},
        "redundant_props": {"type": "array", "items": {
            "type": "object",
            "properties": {"prop_id": {"type": "string"},
                           "covered_by": {"type": "array", "items": {
                               "type": "object",
                               "properties": {"entity_id": {"type": "string"},
                                              "variant_id": {"type": "string"}},
                               "required": ["entity_id", "variant_id"]}},
                           "reason": {"type": "string"}},
            "required": ["prop_id", "covered_by", "reason"]}},
        "redraw": {"type": "array", "items": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}, "variant_id": {"type": "string"},
                           "problem": {"type": "string"}, "correction": {"type": "string"}},
            "required": ["entity_id", "variant_id", "problem", "correction"]}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["same_as_registry", "merge_locals", "drop", "redundant_props", "redraw",
                 "notes"],
}


# ---------------- gathering ----------------

def gather(book_id: int) -> dict:
    """Everything the review (and its validation) needs: the registry, per-look page
    uses, and the chapter-local cast with where each local appears."""
    registry = db.get_registry(book_id)
    reg_ids = {e["id"] for e in registry.get("entities", [])}
    chapters = db.get_chapters(book_id)
    uses = collections.Counter()
    local_pages = collections.defaultdict(list)
    for idx, cast in db.page_casts(book_id):
        seen = set()
        for cc in cast:
            k = (cc.get("entity_id"), cc.get("variant_id") or "default")
            if k in seen:
                continue
            seen.add(k)
            uses[k] += 1
            if k[0] not in reg_ids:
                local_pages[k[0]].append(idx)
    locals_ = {}          # id -> {name, chapters: [idx], defs: [chapter cast entry]}
    for ch in chapters:
        for cc in db.get_chapter_cast(book_id, ch["idx"]):
            eid = cc.get("entity_id")
            if not eid or eid in reg_ids:   # NOT from_registry: chapters mis-flag ids the
                continue                    # registry lacks, and those need mapping most
            loc = locals_.setdefault(eid, {"name": cc.get("name") or eid, "chapters": [], "defs": []})
            loc["chapters"].append(ch["idx"])
            loc["defs"].append(cc)
    for eid, loc in locals_.items():
        loc["pages"] = local_pages.get(eid, [])
    sheets = sorted(k for k in db.list_sheets(book_id) if not k[1].startswith("__"))
    return {"registry": registry, "chapters": chapters, "uses": uses, "locals": locals_,
            "sheets": sheets}


def _thumb(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.thumbnail((THUMB_PX, THUMB_PX))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "WEBP", quality=75)
    return buf.getvalue()


def _sheet_label(g: dict, eid: str, vid: str) -> str:
    e = next((x for x in g["registry"].get("entities", []) if x["id"] == eid), None)
    if e:
        v = next((v for v in e.get("variants") or [] if v["id"] == vid), None)
        look = (v or {}).get("appearance") or e.get("base_appearance", "")
        name = e.get("name", eid) + (f" ({v['label']})" if v and v.get("label") else "")
    else:
        loc = g["locals"].get(eid) or {}
        name = loc.get("name", eid) + " [chapter-local]"
        look = ((loc.get("defs") or [{}])[0]).get("appearance", "")
    return f"SHEET {eid}/{vid} -- {name}. Described as: {look[:400]}"


def _entity_uses(e: dict, uses) -> int:
    return sum(n for (eid, _), n in uses.items() if eid == e["id"])


def digest(g: dict) -> dict:
    reg_lines = []
    for e in g["registry"].get("entities", []):
        al = ", ".join(e.get("aliases") or [])
        reg_lines.append(f"- {e['id']} [{e.get('type')}] \"{e.get('name')}\""
                         + (f" aka {al}" if al else "") + f" imp={e.get('importance')}"
                         + f" -- {(e.get('summary') or '')[:160]}")
        vs = e.get("variants") or []
        if not vs:
            reg_lines.append(f"    default: {g['uses'][(e['id'], 'default')]} pages")
        for v in vs:
            reg_lines.append(f"    {v['id']} \"{v.get('label', '')}\" ({v.get('when', '')}): "
                             f"{g['uses'][(e['id'], v['id'])]} pages")
    loc_lines = []
    for eid, loc in sorted(g["locals"].items()):
        look = (loc["defs"][0].get("appearance") or "")[:220]
        pg = loc["pages"]
        loc_lines.append(f"- {eid} \"{loc['name']}\" chapters={loc['chapters']} "
                         f"pages={len(pg)} ({pg[0]}-{pg[-1]}) -- {look}" if pg else
                         f"- {eid} \"{loc['name']}\" chapters={loc['chapters']} pages=0 -- {look}")
    ch_lines = [f"{c['idx']}: {c['title']} (from page {c['first_page']})" for c in g["chapters"]]
    return {"registry": "\n".join(reg_lines) or "(empty)",
            "locals": "\n".join(loc_lines) or "(none)",
            "chapters": "\n".join(ch_lines)}


# ---------------- review ----------------

def review(book_id: int, model: str = ROSTER_REVIEW_MODEL) -> dict:
    """Ask the model for roster fixes; returns them validated against the real ids
    (anything unsafe or unknown is moved to `rejected` with the reason)."""
    g = gather(book_id)
    book = db.get_book(book_id) or {}
    title = book.get("title") or ""
    by = f" by {book['author']}" if book.get("author") else ""
    prompt = PROMPT.format(book=f" ({title}{by})" if title else "", max_refs=MAX_REFS,
                           max_redraws=MAX_REDRAWS, **digest(g))
    contents = [prompt]
    for eid, vid in g["sheets"]:
        data = db.get_sheet(book_id, eid, vid)
        if data:
            contents += [_sheet_label(g, eid, vid), _thumb(data)]
    if len(contents) == 1:
        contents.append("(no reference sheets drawn yet)")
    raw = gem.vision_json(contents, schema=SCHEMA, model=model, thinking_level="high",
                          kind="text", temperature=0.4)
    return validate(raw, g)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def validate(raw: dict, g: dict) -> dict:
    reg = {e["id"]: e for e in g["registry"].get("entities", [])}
    locals_, uses = g["locals"], g["uses"]
    out = {"same_as_registry": [], "merge_locals": [], "drop": [], "redundant_props": [],
           "redraw": [], "notes": [n for n in raw.get("notes") or [] if n], "rejected": []}
    claimed = set()        # each local is touched by at most one fix

    def reject(kind, item, why):
        out["rejected"].append({"kind": kind, "item": item, "why": why})

    def real_variant(eid, vid):
        e = reg.get(eid)
        if not e:
            return False
        vs = e.get("variants") or []
        return vid in {v["id"] for v in vs} if vs else vid in ("default", "")

    for it in raw.get("same_as_registry") or []:
        lid, eid = it.get("local_id"), it.get("entity_id")
        vid = it.get("variant_id") or "default"
        if lid not in locals_:
            reject("same_as_registry", it, "not a chapter-local id")
        elif not real_variant(eid, vid):
            reject("same_as_registry", it, "no such registry entity/variant")
        elif lid in claimed:
            reject("same_as_registry", it, "local already claimed by another fix")
        else:
            claimed.add(lid)
            out["same_as_registry"].append({**it, "variant_id": vid})

    for it in raw.get("merge_locals") or []:
        ids = [i for i in dict.fromkeys(it.get("local_ids") or [])]
        new_id = _slug(it.get("new_id") or it.get("name"))
        bad = [i for i in ids if i not in locals_]
        n_defs = sum(len(locals_[i]["chapters"]) for i in ids if i in locals_)
        if bad:
            reject("merge_locals", it, f"not chapter-local ids: {bad}")
        elif n_defs < 2:
            reject("merge_locals", it, "nothing to merge (one id in one chapter)")
        elif claimed & set(ids):
            reject("merge_locals", it, "local already claimed by another fix")
        elif not new_id or new_id in reg:
            reject("merge_locals", it, f"new_id {new_id!r} empty or already a registry id")
        else:
            claimed.update(ids)
            keep = it.get("keep_id") if it.get("keep_id") in ids else \
                max(ids, key=lambda i: len(locals_[i]["pages"]))
            out["merge_locals"].append({**it, "local_ids": ids, "new_id": new_id, "keep_id": keep})

    for it in raw.get("drop") or []:
        eid, vid = it.get("entity_id"), it.get("variant_id") or ""
        e = reg.get(eid)
        if not e:
            reject("drop", it, "no such registry entity")
        elif vid and not real_variant(eid, vid):
            reject("drop", it, "no such variant")
        elif (uses[(eid, vid)] if vid else _entity_uses(e, uses)):
            reject("drop", it, "still used by pages")
        else:
            out["drop"].append({**it, "variant_id": vid})

    for it in raw.get("redundant_props") or []:
        pid = it.get("prop_id")
        cov = [(c.get("entity_id"), c.get("variant_id") or "default")
               for c in it.get("covered_by") or []]
        cov = [c for c in cov if real_variant(*c) and c[0] != pid]
        if reg.get(pid, {}).get("type") != "prop":
            reject("redundant_props", it, "not a registry prop")
        elif not cov:
            reject("redundant_props", it, "no valid covering variants")
        else:
            out["redundant_props"].append({**it, "covered_by": [list(c) for c in cov]})

    drawn = set(g["sheets"])
    for it in raw.get("redraw") or []:
        k = (it.get("entity_id"), it.get("variant_id") or "default")
        if k not in drawn:
            reject("redraw", it, "no such drawn sheet")
        elif k[0] not in reg:
            reject("redraw", it, "chapter-local sheet (merge it to fix its look)")
        elif not (it.get("correction") or "").strip():
            reject("redraw", it, "no correction given")
        elif len(out["redraw"]) >= MAX_REDRAWS:
            reject("redraw", it, f"over the {MAX_REDRAWS}-redraw cap")
        else:
            out["redraw"].append({**it, "variant_id": k[1]})
    return out


# ---------------- applying ----------------

def apply(book_id: int, rv: dict, log=print, draw: bool = True) -> dict:
    """Write a validated review into the registry, chapter casts and page casts, then
    (draw=True) redraw the sheets it flagged. Merged locals become one registry entity
    whose look (and, if already drawn, sheet) is the keep_id member's, so a merge
    itself needs no redraw."""
    g = gather(book_id)
    registry = g["registry"]
    reg = {e["id"]: e for e in registry.get("entities", [])}
    remap = {}          # old local id -> (entity_id, variant_id)
    summary = collections.Counter()

    for it in rv.get("same_as_registry", []):
        remap[it["local_id"]] = (it["entity_id"], it["variant_id"])
        summary["mapped"] += 1

    for it in rv.get("merge_locals", []):
        ids = it["local_ids"]
        src = it["keep_id"]
        d = g["locals"][src]["defs"][0]
        others = sorted({g["locals"][i]["name"] for i in ids} - {it["name"]})
        reg[it["new_id"]] = {
            "id": it["new_id"], "type": it.get("type") or "character", "name": it["name"],
            "aliases": others, "importance": PROMOTED_IMPORTANCE,
            "summary": it.get("reason", ""), "canonical_details": "", "variants": [],
            "base_appearance": d.get("appearance", ""),
            "base_sheet_prompt": d.get("sheet_prompt", ""), "added_by": "roster_review"}
        registry["entities"].append(reg[it["new_id"]])
        sheet = db.get_sheet(book_id, src, "default")
        if sheet and src != it["new_id"]:   # the sheet must match the look we kept
            db.save_sheet(book_id, it["new_id"], "default", sheet)
        for i in ids:
            remap[i] = (it["new_id"], "default")
            if i != it["new_id"]:
                db.delete_sheet(book_id, i, "default")
        summary["merged"] += 1

    dropped_entities, dropped_variants = set(), set()
    for it in rv.get("drop", []):
        if it["variant_id"]:
            dropped_variants.add((it["entity_id"], it["variant_id"]))
        else:
            dropped_entities.add(it["entity_id"])
    if dropped_entities or dropped_variants:
        kept = []
        for e in registry["entities"]:
            if e["id"] in dropped_entities:
                summary["dropped"] += 1
                continue
            vs = e.get("variants") or []
            nv = [v for v in vs if (e["id"], v["id"]) not in dropped_variants]
            summary["dropped"] += len(vs) - len(nv)
            if vs and not nv:      # never leave an entity with its last variant gone
                nv = vs
            e["variants"] = nv
            kept.append(e)
        registry["entities"] = kept

    redundant = {it["prop_id"]: {tuple(c) for c in it["covered_by"]}
                 for it in rv.get("redundant_props", [])}

    # chapter casts: retarget remapped locals to registry entries, drop dropped ids
    for ch in g["chapters"]:
        cast = db.get_chapter_cast(book_id, ch["idx"])
        new, seen = [], set()
        for cc in cast:
            eid = cc.get("entity_id")
            if eid in remap:
                ne, nv = remap[eid]
                cc = {"entity_id": ne, "variant_id": nv, "name": reg[ne]["name"],
                      "from_registry": True}
            elif eid in dropped_entities or (eid, cc.get("variant_id")) in dropped_variants:
                continue
            k = (cc["entity_id"], cc.get("variant_id") or "default")
            if k in seen:
                continue
            seen.add(k)
            new.append(cc)
        if new != cast:
            db.add_chapter(book_id, ch["idx"], ch["title"], ch["first_page"], new)

    # page casts: same retargeting, plus redundant props where a covering variant is on the page
    for idx, cast in db.page_casts(book_id):
        keys = {(cc.get("entity_id"), cc.get("variant_id") or "default") for cc in cast}
        keys |= {remap[k[0]] for k in keys if k[0] in remap}
        new, seen = [], set()
        for cc in cast:
            eid = cc.get("entity_id")
            if eid in remap:
                cc = {**cc, "entity_id": remap[eid][0], "variant_id": remap[eid][1]}
            elif eid in redundant and redundant[eid] & keys:
                summary["prop_refs_removed"] += 1
                continue
            k = (cc["entity_id"], cc.get("variant_id") or "default")
            if k in seen:
                continue
            seen.add(k)
            new.append(cc)
        if new != cast:
            db.update_page_plan(book_id, idx, cast=new)
            summary["pages_changed"] += 1

    # flagged sheets: fold the correction into what the sheet (and pages) draw from
    redraws = []
    for it in rv.get("redraw", []):
        e = reg.get(it["entity_id"])
        if not e:
            continue
        v = next((v for v in e.get("variants") or [] if v["id"] == it["variant_id"]), None)
        tgt, pre = (v, "") if v else (e, "base_")
        note = f" Correction: {it['correction'].strip()}"
        for f in ("appearance", "sheet_prompt"):
            if tgt.get(pre + f) is not None or f == "appearance":
                tgt[pre + f] = (tgt.get(pre + f) or "") + note
        redraws.append((it["entity_id"], it["variant_id"]))

    registry["roster_review"] = {"at": time.time(), "notes": rv.get("notes", []),
                                 "applied": dict(summary)}
    db.save_registry(book_id, registry)

    if draw and redraws:
        from . import scene

        def one(k):
            try:
                return scene.regenerate_sheet(book_id, *k).get("ok")
            except Exception as ex:  # noqa: BLE001 -- the old sheet stays
                log(f"[roster_review] redraw {k[0]}/{k[1]} failed: {ex}")
                return False

        with ThreadPoolExecutor(max_workers=4) as ex:
            summary["redrawn"] = sum(bool(r) for r in ex.map(one, redraws))
    summary["redraws_flagged"] = len(redraws)
    log(f"[roster_review] book {book_id}: applied {dict(summary)}")
    return dict(summary)


def fill_missing(book_id: int, log=print, workers: int = 6) -> int:
    """Draw any sheet a page now plans for but the roster lacks (e.g. a registry look a
    local was mapped onto that no page used before). Only pages with a gap are built,
    so this is free when the review changed nothing. Returns pages touched."""
    from . import scene
    gaps = [idx for idx, _ in db.page_casts(book_id)
            if any(not db.has_sheet(book_id, *scene.sheet_key(m))
                   for m in scene.plan_page_sheets(book_id, idx))]

    def one(idx):
        try:
            scene.build_scene_context(book_id, idx)
        except Exception as ex:  # noqa: BLE001
            log(f"[roster_review] page {idx}: sheet fill failed: {ex}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, gaps))
    if gaps:
        log(f"[roster_review] drew missing sheets for {len(gaps)} pages")
    return len(gaps)


def run(book_id: int, log=print, draw: bool = True) -> dict | None:
    """The process-pipeline hook: review + apply. Never fatal -- a failed review
    leaves the roster exactly as segmentation produced it."""
    if not ENABLED:
        return None
    try:
        rv = review(book_id)
        log("[roster_review] " + format_review(rv).replace("\n", "\n[roster_review] "))
        summary = apply(book_id, rv, log=log, draw=draw)
        if draw:                    # batch mode: the drawn roster must stay complete
            fill_missing(book_id, log=log)
        return summary
    except Exception as ex:  # noqa: BLE001 -- an optional polish step
        log(f"[roster_review] book {book_id}: skipped ({type(ex).__name__}: {ex})")
        return None


def format_review(rv: dict) -> str:
    lines = []
    for it in rv.get("same_as_registry", []):
        lines.append(f"map    {it['local_id']} -> {it['entity_id']}/{it['variant_id']}: {it['reason']}")
    for it in rv.get("merge_locals", []):
        lines.append(f"merge  {', '.join(it['local_ids'])} -> {it['new_id']} "
                     f"\"{it['name']}\" (look of {it['keep_id']}): {it['reason']}")
    for it in rv.get("drop", []):
        lines.append(f"drop   {it['entity_id']}{'/' + it['variant_id'] if it['variant_id'] else ''}: "
                     f"{it['reason']}")
    for it in rv.get("redundant_props", []):
        cov = ", ".join("/".join(c) for c in it["covered_by"])
        lines.append(f"prop   {it['prop_id']} covered by {cov}: {it['reason']}")
    for it in rv.get("redraw", []):
        lines.append(f"redraw {it['entity_id']}/{it['variant_id']}: {it['problem']} "
                     f"=> {it['correction']}")
    for n in rv.get("notes", []):
        lines.append(f"note   {n}")
    for r in rv.get("rejected", []):
        lines.append(f"REJECT {r['kind']}: {r['why']} -- {json.dumps(r['item'], ensure_ascii=False)[:200]}")
    return "\n".join(lines) or "(no changes proposed)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("book_id", type=int)
    ap.add_argument("--apply", action="store_true", help="write the fixes (default: dry run)")
    ap.add_argument("--no-draw", action="store_true", help="with --apply: skip sheet redraws")
    ap.add_argument("--from-json", help="apply a saved --json review instead of calling the model")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--model", default=ROSTER_REVIEW_MODEL)
    a = ap.parse_args(argv)
    if a.from_json:
        with open(a.from_json) as f:
            rv = json.load(f)
    else:
        rv = review(a.book_id, model=a.model)
    print(json.dumps(rv, indent=1, ensure_ascii=False) if a.json else format_review(rv))
    if a.apply:
        apply(a.book_id, rv, draw=not a.no_draw)


if __name__ == "__main__":
    sys.exit(main())
