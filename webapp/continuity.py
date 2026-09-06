"""Continuity review: a critic looks at a RUN of consecutive illustrated pages
together -- the pictures and the text they accompany -- and recommends edits.

The per-page critic (pipeline/run.py SCENE_CRITIQUE) judges one picture in
isolation, so it cannot see the kind of drift that shows up only across pages:
the same room drawn three different ways, a character whose clothes change
between panels the story keeps them in, a prop that changes colour, a bed that
moves. Most of that drift has a *plan* cause rather than an image cause -- a
setting no reference sheet exists for, a variant the registry never made, a
cast list missing the thing that should anchor the look -- so the review is
allowed to propose NEW registry entities (settings, props, characters) and NEW
variants of existing ones, plus per-page brief/cast corrections and image edits.

Usage:
    python -m webapp.continuity <book_id> [--pages 382-391] [--window 5]
                                [--apply [--no-draw]] [--json]

A review never changes anything by itself; `apply_review` writes the accepted
recommendations into the registry + pages and returns the redraw plan (the
caller decides whether to spend the image generations).
"""
import argparse
import json
import re
import sys
import time

from pipeline import gem, costs, markup
from pipeline.config import TEXT_MODEL, STYLES
from . import db

WINDOW = 5            # pages per review call
MAX_SHEETS = 8        # reference sheets attached per window (identity anchors)
SRC_CHARS = 2600      # per-page source text cap
REVIEW_MODEL = TEXT_MODEL

REVIEW_PROMPT = """You are the continuity editor for a children's read-aloud picture-book \
edition of a novel (audience: {age} years old). You are shown {n} CONSECUTIVE pages -- for each, \
the source text the child hears, the plan the illustration was drawn from (setting, brief, cast), \
and the illustration itself -- followed by the canonical reference sheets the illustrator was given.

Your job is NOT to grade each picture alone (a separate critic already did). Look at the run of \
pages AS A SEQUENCE and find what breaks the spell for a child turning the pages:
- the same place drawn as a different room from one page to the next (layout, furniture, windows, \
colours, time of day) when the story has not moved;
- a character whose clothes, hair, age, bandages or held objects change between pages the text keeps \
them the same, or who does NOT change when the text says they did;
- a prop or creature that changes shape/colour/size, or is present on one page and unexplained on the \
next;
- a figure that is clearly a different person from the same-named figure on a neighbouring page;
- wrong or missing things the SOURCE TEXT states plainly (who is present, what they wear, what they \
hold, where they are) -- the source text is the only ground truth; the brief and cast were generated \
from it and can be wrong;
- a picture that spoils something the text has not reached yet.

For EVERY problem, find the ROOT CAUSE in the plan, because redrawing against the same plan just \
reproduces the drift:
- missing_setting: the pages take place somewhere that has NO entry in the registry below, so every \
page invents it afresh. Propose a NEW setting entity (a canonical look + a neutral reference-sheet \
prompt) and put it in those pages' cast.
- missing_prop / missing_character: a recurring object or person with no registry entry.
- wrong_variant: a page uses the wrong existing variant of a character (e.g. school uniform while the \
text has them in bed). Fix the page's cast.
- missing_variant: the text keeps a character in a look the registry has no variant for (pyjamas, a \
bandaged arm, a costume worn for a whole stretch). Propose a NEW variant of that existing entity and \
use it in the affected pages' cast. A variant is a DURABLE look across several pages, never a one-page \
moment.
- brief: the brief asks for, or omits, something the source contradicts. Rewrite the brief in full.
- cast: the page's cast list omits an entity that should anchor the look (or lists one that should not \
be there). Give the corrected full cast.
- image_noise: the plan is fine and the picture just came out wrong. Say whether an in-place edit \
(revise) can fix it or it must be redrawn (regenerate).

YOU ARE EXPECTED TO CHANGE THE PLAN, not just patch pictures. A revise that leaves the plan wrong is \
drawn against the same wrong plan next time and drifts straight back. So:
- Whenever a page's root cause is wrong_variant, missing_variant, missing_setting, missing_prop, \
missing_character or cast, that page's edit MUST include the complete corrected "cast" -- the right \
existing variant id, or the new variant/entity you are proposing -- not only an edit_instruction.
- Propose a NEW variant whenever a character keeps a look across two or more pages that no existing \
variant matches: soot-streaked clothes and broken glasses after a fireplace accident, wet or muddy \
clothes, a bandaged arm, casual summer clothes for a child who has not started school yet, pyjamas \
for a night sequence. Give it the pages it applies to. Do not wait for a later window to do it and do \
not describe the look only in edit_instruction. (A look must still be drawable for a children's \
book: describe rumpled/dirty clothes and a calm face, never a hurt or lifeless child.)
- If a variant proposed by an earlier window (listed below) fits, USE it in the cast of every page \
here where it applies -- that is what it was made for.

Rules for proposals:
- REUSE existing registry ids and variant ids exactly as written below. Only propose a new entity or \
variant when nothing existing fits. Never propose one that duplicates an earlier proposal listed below \
-- reuse its id instead.
- New ids are snake_case. Appearances are CONCRETE (materials, colours, layout, build, clothing), \
describe ONE look at ONE moment, and never mention an art style or medium. sheet_prompt describes a \
neutral reference image of the subject alone (a character: full body, front view, plain off-white \
background; a setting: a clean establishing view with no people; a prop: an isolated product-style \
view) -- no art-style words.
- Be proportionate. A page whose picture is fine gets action "keep" and no rewrite. Do not invent \
problems, and do not rewrite a brief that already matches the text.
- edit_instruction (when action is "revise") is a complete instruction for an image-to-image edit of \
THAT page's picture: first say what to keep unchanged (composition, characters, poses, setting, style), \
then the single precise change. When the fix depends on a character's canonical look, name them in \
reference_characters exactly as they appear in the cast.
- When a page's cast changes, return the COMPLETE corrected cast for that page (every entity that \
should anchor the picture: the setting, the people present, any key prop), not just the delta. \
Leave "cast" empty to keep the page's cast as it is. Likewise leave "brief" / "setting" empty to keep them.

THE INTENDED ART STYLE (for judging the pictures, not for writing into prompts):
{style}

THE REGISTRY -- every entity the illustrator can be given a reference sheet for, with its variants \
(id, label, when it applies). "views drawn" lists named spots of a setting that already have their \
own reference (a page's cast entry may name one of these in "view"):
{registry}
{prior}
THE PAGES, in reading order:
{pages}

Now judge the sequence. Return JSON only:
{{
  "summary": "<two or three sentences: what the run of pages looks like as a sequence, and the main thing that drifts>",
  "continuity_issues": [
    {{"pages": [<page numbers involved>], "issue": "<what is inconsistent or wrong, concretely>",
      "root_cause": "<missing_setting | missing_prop | missing_character | wrong_variant | missing_variant | brief | cast | image_noise | other>",
      "severity": <1 minor, 2 noticeable, 3 breaks the story for a child>}}
  ],
  "new_entities": [
    {{"id": "<snake_case>", "type": "<setting | prop | character>", "name": "<display name>",
      "importance": <1-5>, "summary": "<one line: what it is and where it appears>",
      "appearance": "<rich concrete canonical look>",
      "sheet_prompt": "<neutral reference-sheet image prompt, no style words>",
      "pages": [<pages that should list it in their cast>], "why": "<the drift it fixes>"}}
  ],
  "new_variants": [
    {{"entity_id": "<existing registry id>", "id": "<new snake_case variant id>",
      "kind": "<outfit | age | state | other>", "label": "<short label>",
      "when": "<where in the book it applies>", "delta": "<what differs from the entity's other looks>",
      "appearance": "<the FULL look with the delta applied, one figure at one moment>",
      "sheet_prompt": "<neutral reference-sheet prompt for this look, no style words>",
      "pages": [<pages that should use it>], "why": "<the drift it fixes>"}}
  ],
  "page_edits": [
    {{"idx": <page number>, "action": "<keep | revise | regenerate>",
      "problems": ["<concrete problems with THIS page's picture or plan>"],
      "edit_instruction": "<required when action is revise, else empty>",
      "reference_characters": ["<cast names whose sheet the edit needs>"],
      "brief": "<full corrected brief, or empty to keep>",
      "setting": "<corrected one-line setting, or empty to keep>",
      "cast": [{{"entity_id": "<id>", "variant_id": "<variant id, or 'default' if the entity has no variants>", "view": "<named spot within a setting, or empty>"}}]}}
  ]
}}
Include a page_edits entry for EVERY page shown, in order, even when its action is keep."""

_CAST_ITEM = {"type": "object", "properties": {
    "entity_id": {"type": "string"}, "variant_id": {"type": "string"}, "view": {"type": "string"}},
    "required": ["entity_id", "variant_id"]}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "continuity_issues": {"type": "array", "items": {"type": "object", "properties": {
            "pages": {"type": "array", "items": {"type": "integer"}},
            "issue": {"type": "string"},
            "root_cause": {"type": "string", "enum": [
                "missing_setting", "missing_prop", "missing_character", "wrong_variant",
                "missing_variant", "brief", "cast", "image_noise", "other"]},
            "severity": {"type": "integer"}},
            "required": ["pages", "issue", "root_cause", "severity"]}},
        "new_entities": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": ["setting", "prop", "character"]},
            "name": {"type": "string"}, "importance": {"type": "integer"},
            "summary": {"type": "string"}, "appearance": {"type": "string"},
            "sheet_prompt": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer"}},
            "why": {"type": "string"}},
            "required": ["id", "type", "name", "appearance", "sheet_prompt", "pages"]}},
        "new_variants": {"type": "array", "items": {"type": "object", "properties": {
            "entity_id": {"type": "string"}, "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["outfit", "age", "state", "other"]},
            "label": {"type": "string"}, "when": {"type": "string"}, "delta": {"type": "string"},
            "appearance": {"type": "string"}, "sheet_prompt": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer"}},
            "why": {"type": "string"}},
            "required": ["entity_id", "id", "kind", "label", "appearance", "sheet_prompt", "pages"]}},
        "page_edits": {"type": "array", "items": {"type": "object", "properties": {
            "idx": {"type": "integer"},
            "action": {"type": "string", "enum": ["keep", "revise", "regenerate"]},
            "problems": {"type": "array", "items": {"type": "string"}},
            "edit_instruction": {"type": "string"},
            "reference_characters": {"type": "array", "items": {"type": "string"}},
            "brief": {"type": "string"}, "setting": {"type": "string"},
            "cast": {"type": "array", "items": _CAST_ITEM}},
            "required": ["idx", "action", "problems"]}},
    },
    "required": ["summary", "continuity_issues", "new_entities", "new_variants", "page_edits"],
}


# ---------------- assembling one window ----------------

def window_groups(idxs: list[int], window: int = WINDOW) -> list[list[int]]:
    """Split page numbers into consecutive runs of `window`. A tail of one or two
    pages is folded into the previous run rather than reviewed alone -- a critic
    can't judge continuity from a single page."""
    idxs = sorted(idxs)
    groups = [idxs[i:i + window] for i in range(0, len(idxs), window)]
    if len(groups) > 1 and len(groups[-1]) < 3:
        groups[-2].extend(groups.pop())
    return groups


def _snip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def registry_digest(registry: dict, sheets: list) -> str:
    """Every registry entity WITH its ids and variant ids (unlike run.roster_digest,
    which is names-only and characters-only) so the critic can reference and reuse
    them exactly. `sheets` = db.list_sheets rows, used to list the named views a
    setting already has references for."""
    views: dict = {}
    for eid, vid in sheets:
        if vid.startswith("__v_"):
            views.setdefault(eid, []).append(vid[4:].replace("_", " "))
    ents = sorted(registry.get("entities", []),
                  key=lambda e: (e.get("type", "character") != "character",
                                 -e.get("importance", 0), e.get("id", "")))
    lines = []
    for e in ents:
        head = (f"- {e.get('id')} [{e.get('type', 'character')}, importance "
                f"{e.get('importance', '?')}] \"{e.get('name', e.get('id'))}\": "
                f"{_snip(e.get('base_appearance') or e.get('canonical_details') or '', 160)}")
        vs = e.get("variants") or []
        if vs:
            head += "\n    variants: " + "; ".join(
                f"{v.get('id')} \"{v.get('label', '')}\" ({v.get('when', '')})" for v in vs)
        else:
            head += "\n    variants: (none -- use variant_id 'default')"
        if e.get("id") in views:
            head += "\n    views drawn: " + ", ".join(sorted(set(views[e["id"]])))
        lines.append(head)
    return "\n".join(lines) or "(empty)"


def _cast_label(cc: dict, reg_by_id: dict, local_by_id: dict) -> str:
    eid, vid = cc.get("entity_id", "?"), cc.get("variant_id") or "default"
    e = reg_by_id.get(eid)
    name = (e or local_by_id.get(eid) or {}).get("name") or eid
    s = f"{eid}/{vid} ({name}"
    if e:
        var = next((v for v in e.get("variants", []) if v.get("id") == vid), None)
        if var and var.get("label"):
            s += f", {var['label']}"
        elif vid != "default" and not var:
            s += ", UNKNOWN VARIANT"
    elif not local_by_id.get(eid):
        s += ", NOT IN REGISTRY"
    s += ")"
    if cc.get("view"):
        s += f" view='{cc['view']}'"
    return s


def build_window(book_id: int, idxs: list[int]) -> dict:
    """Everything one review call needs for pages `idxs`: per-page text/plan/image,
    the registry digest, and the reference sheets for the window's cast."""
    book = db.get_book(book_id)
    registry = db.get_registry(book_id)
    reg_by_id = {e["id"]: e for e in registry.get("entities", [])}
    sheets = db.list_sheets(book_id)
    pages, missing_img = [], []
    local_by_id: dict = {}
    for idx in idxs:
        page = db.get_page(book_id, idx)
        if not page:
            continue
        for cc in db.get_chapter_cast(book_id, page["chapter_idx"]):
            if cc.get("entity_id") and cc["entity_id"] not in reg_by_id:
                local_by_id[cc["entity_id"]] = cc
        cast = json.loads(page["cast_json"]) if page.get("cast_json") else []
        img = db.scene_data(book_id, idx)
        if not img:
            missing_img.append(idx)
        pages.append({"idx": idx, "page": page, "cast": cast, "image": img})
    # reference sheets: the window's cast, characters first, most-used first
    want: list = []
    for p in pages:
        for cc in p["cast"]:
            eid, vid = cc.get("entity_id"), cc.get("variant_id") or "default"
            if not eid:
                continue
            key = (eid, ("__v_" + _view_slug(cc["view"])) if cc.get("view") else vid)
            if key not in want:
                want.append(key)
    def _rank(k):
        e = reg_by_id.get(k[0], {})
        return (0 if e.get("type", "character") == "character" else 1, -e.get("importance", 0))
    ref_sheets = []
    for eid, vid in sorted(want, key=_rank):
        if len(ref_sheets) >= MAX_SHEETS:
            break
        data = db.get_sheet(book_id, eid, vid) or (
            db.get_any_sheet(book_id, eid) if not vid.startswith("__v_") else None)
        if not data:
            continue
        e = reg_by_id.get(eid) or local_by_id.get(eid) or {}
        label = e.get("name") or eid
        if vid.startswith("__v_"):
            label += f" -- view: {vid[4:].replace('_', ' ')}"
        else:
            var = next((v for v in e.get("variants", []) if v.get("id") == vid), None)
            if var and var.get("label"):
                label += f" ({var['label']})"
        ref_sheets.append({"entity_id": eid, "variant_id": vid, "label": label, "data": data})
    return {"book": book, "registry": registry, "reg_by_id": reg_by_id,
            "local_by_id": local_by_id, "pages": pages, "missing_images": missing_img,
            "registry_text": registry_digest(registry, sheets), "ref_sheets": ref_sheets}


def _view_slug(view: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (view or "").strip().lower()).strip("_")
    return s[:28] or "inside"


def _prior_text(prior: dict | None) -> str:
    if not prior or not (prior.get("new_entities") or prior.get("new_variants")):
        return ""
    lines = ["\nALREADY PROPOSED BY EARLIER WINDOWS OF THIS REVIEW (reuse these ids; do not propose "
             "them again -- you MAY list them in a page's cast):"]
    for e in prior.get("new_entities", []):
        lines.append(f"- NEW {e.get('type')} {e.get('id')} \"{e.get('name')}\": "
                     f"{_snip(e.get('appearance', ''), 140)}")
    for v in prior.get("new_variants", []):
        lines.append(f"- NEW variant {v.get('entity_id')}/{v.get('id')} \"{v.get('label')}\": "
                     f"{_snip(v.get('delta') or v.get('appearance', ''), 140)}")
    return "\n".join(lines) + "\n"


def review_contents(win: dict, prior: dict | None = None) -> list:
    """The ordered text/image parts for one review call."""
    book = win["book"]
    style = STYLES.get(book.get("style")) or next(iter(STYLES.values()))
    page_blocks = []
    for p in win["pages"]:
        pg, cast = p["page"], p["cast"]
        cast_txt = "; ".join(_cast_label(c, win["reg_by_id"], win["local_by_id"]) for c in cast) \
            or "(none)"
        page_blocks.append(
            f"=== PAGE {p['idx']}: \"{pg.get('title') or ''}\" ===\n"
            f"SETTING (planned): {pg.get('setting') or '(none)'}\n"
            f"CAST (planned, entity_id/variant_id): {cast_txt}\n"
            f"BRIEF (planned): {pg.get('brief') or '(none)'}\n"
            f"SOURCE TEXT:\n{_snip(markup.plain(pg.get('read_text') or ''), SRC_CHARS)}\n"
            + ("" if p["image"] else "(this page has NO illustration yet -- judge its plan only)\n"))
    prompt = REVIEW_PROMPT.format(
        age=book.get("age") or "5", n=len(win["pages"]), style=style,
        registry=win["registry_text"], prior=_prior_text(prior), pages="\n".join(page_blocks))
    contents: list = [prompt, "\nTHE ILLUSTRATIONS, in the same order:"]
    for p in win["pages"]:
        if p["image"]:
            contents.append(f"--- Illustration for page {p['idx']} ---")
            contents.append(p["image"])
    if win["ref_sheets"]:
        contents.append("\nCANONICAL REFERENCE SHEETS the illustrator was given (what each named "
                        "entity is SUPPOSED to look like on every page):")
        for r in win["ref_sheets"]:
            contents.append(f"--- Reference sheet: {r['label']} ---")
            contents.append(r["data"])
    return contents


def _clean_review(rv: dict, idxs: list[int]) -> dict:
    """Normalise a raw verdict: keep only edits for pages in the window, sane ids."""
    out = {"summary": (rv.get("summary") or "").strip(),
           "continuity_issues": [i for i in rv.get("continuity_issues", []) if isinstance(i, dict)],
           "new_entities": [], "new_variants": [], "page_edits": []}
    for e in rv.get("new_entities", []) or []:
        if isinstance(e, dict) and e.get("id") and e.get("appearance"):
            e["id"] = _slug(e["id"])
            out["new_entities"].append(e)
    for v in rv.get("new_variants", []) or []:
        if isinstance(v, dict) and v.get("entity_id") and v.get("id") and v.get("appearance"):
            v["id"] = _slug(v["id"])
            out["new_variants"].append(v)
    seen = set()
    for pe in rv.get("page_edits", []) or []:
        if not isinstance(pe, dict) or pe.get("idx") not in idxs or pe["idx"] in seen:
            continue
        seen.add(pe["idx"])
        pe["action"] = pe.get("action") if pe.get("action") in ("keep", "revise", "regenerate") \
            else "keep"
        pe["cast"] = [c for c in (pe.get("cast") or []) if isinstance(c, dict) and c.get("entity_id")]
        out["page_edits"].append(pe)
    out["page_edits"].sort(key=lambda e: e["idx"])
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")[:48]


def review_window(book_id: int, idxs: list[int], prior: dict | None = None,
                  thinking_level: str | None = "medium") -> dict:
    """One review call over pages `idxs`. Returns the cleaned verdict plus the
    window it covered ("pages") and which pages had no picture."""
    win = build_window(book_id, idxs)
    if not win["pages"]:
        raise ValueError(f"no pages in {idxs}")
    contents = review_contents(win, prior)
    with costs.run_as(f"book:{book_id}"):
        raw = gem.vision_json(contents, schema=REVIEW_SCHEMA, model=REVIEW_MODEL,
                              thinking_level=thinking_level, kind="critique")
    rv = _clean_review(raw, [p["idx"] for p in win["pages"]])
    rv["pages"] = [p["idx"] for p in win["pages"]]
    rv["missing_images"] = win["missing_images"]
    rv["model"] = REVIEW_MODEL
    return rv


def review_range(book_id: int, start: int, end: int, window: int = WINDOW,
                 store: bool = True, log=print, on_window=None) -> list[dict]:
    """Review pages start..end (inclusive) in consecutive windows. Each window is
    told what earlier windows already proposed, so a setting invented for pages
    1-5 is reused (not re-proposed under another id) for pages 6-10. Each verdict
    is stored as its own row (id in "review_id") so it can be applied separately.
    `on_window(rv)` is called after each window; returning False stops the run."""
    idxs = [p["idx"] for p in db.get_pages(book_id) if start <= p["idx"] <= end]
    if not idxs:
        raise ValueError(f"book {book_id} has no pages in {start}-{end}")
    prior = {"new_entities": [], "new_variants": []}
    out = []
    for grp in window_groups(idxs, window):
        log(f"[continuity] book {book_id}: reviewing pages {grp[0]}-{grp[-1]}")
        rv = review_window(book_id, grp, prior)
        if store:
            rv["review_id"] = db.review_add(book_id, grp[0], grp[-1], rv)
        out.append(rv)
        for e in rv["new_entities"]:
            if not any(x["id"] == e["id"] for x in prior["new_entities"]):
                prior["new_entities"].append(e)
        for v in rv["new_variants"]:
            if not any(x["entity_id"] == v["entity_id"] and x["id"] == v["id"]
                       for x in prior["new_variants"]):
                prior["new_variants"].append(v)
        log(f"[continuity]   {len(rv['continuity_issues'])} issues, "
            f"{sum(1 for e in rv['page_edits'] if e['action'] != 'keep')} page edits, "
            f"+{len(rv['new_entities'])} entities, +{len(rv['new_variants'])} variants")
        if on_window is not None and on_window(rv) is False:
            log("[continuity] stopped early by the caller")
            break
    return out


def merge_reviews(reviews: list[dict]) -> dict:
    """Fold several windows' verdicts into one: proposals de-duplicated by id (their
    page lists unioned), page edits concatenated in order."""
    ents: dict = {}
    vars_: dict = {}
    issues, edits = [], []
    for rv in reviews:
        for e in rv.get("new_entities", []):
            cur = ents.get(e["id"])
            if cur:
                cur["pages"] = sorted(set(cur.get("pages", [])) | set(e.get("pages", [])))
            else:
                ents[e["id"]] = dict(e)
        for v in rv.get("new_variants", []):
            k = (v["entity_id"], v["id"])
            cur = vars_.get(k)
            if cur:
                cur["pages"] = sorted(set(cur.get("pages", [])) | set(v.get("pages", [])))
            else:
                vars_[k] = dict(v)
        issues.extend(rv.get("continuity_issues", []))
        edits.extend(rv.get("page_edits", []))
    return {"summary": "\n".join(r.get("summary", "") for r in reviews if r.get("summary")),
            "continuity_issues": issues, "new_entities": list(ents.values()),
            "new_variants": list(vars_.values()),
            "page_edits": sorted(edits, key=lambda e: e["idx"]),
            "pages": sorted({i for r in reviews for i in r.get("pages", [])})}


# ---------------- applying a review ----------------

# Root causes that live in the PLAN: redrawing against the same plan reproduces the
# drift, so a page touched by one of these is worth an image generation even when the
# picture itself is only mildly off. Everything else is image_noise / other.
PLAN_CAUSES = {"missing_setting", "missing_prop", "missing_character", "wrong_variant",
               "missing_variant", "brief", "cast"}
SERIOUS_SEVERITY = 3


def serious_pages(review: dict, min_severity: int = SERIOUS_SEVERITY,
                  plan_causes: set = PLAN_CAUSES) -> set:
    """Pages whose problems justify spending an image generation: touched by an issue
    that breaks the story for a child (severity >= min_severity -- a spoiler, a
    contradiction of the text) or one rooted in the plan (a wrong brief or cast, a
    missing variant). Cosmetic drift below that bar -- a bench changing colour, a
    wardrobe on the other side of a door -- is what the per-page critic already
    tolerates; on the first Chamber of Secrets sample it made up two thirds of the
    critic's revise/regenerate verdicts (15 of 30 pages), so chasing it page by page
    is where the cost goes."""
    out = set()
    for i in review.get("continuity_issues", []):
        try:
            sev = int(i.get("severity") or 0)
        except (TypeError, ValueError):
            sev = 0
        if sev >= min_severity or i.get("root_cause") in plan_causes:
            out.update(p for p in i.get("pages", []) if isinstance(p, int))
    return out


def apply_review(book_id: int, review: dict, log=print, serious_only: bool = False) -> dict:
    """Write a review's recommendations into the book's PLAN: new entities and
    variants into the registry, corrected brief/setting/cast onto the pages. Nothing
    is drawn here -- the returned "redraws" list ([{idx, mode, seed}]) is the plan for
    the caller to execute (regenerate = clear + redraw; revise = img2img edit of the
    current picture, seeded with the critic's instruction). Only pages the critic
    marked revise/regenerate are in it. New sheets are drawn lazily the first time a
    redrawn page references them.

    serious_only: still write EVERY plan correction (registry additions, briefs, casts
    -- they are free and fix any later redraw), but plan a redraw only for pages in
    serious_pages(review); the critic's other revise/regenerate verdicts are kept as
    they are and listed in "skipped"."""
    keep_for = serious_pages(review) if serious_only else None
    registry = db.get_registry(book_id)
    reg_by_id = {e["id"]: e for e in registry.get("entities", [])}
    rep = {"entities_added": [], "variants_added": [], "pages_updated": [],
           "redraws": [], "skipped": [], "notes": []}
    changed = False
    for e in review.get("new_entities", []):
        eid = _slug(e.get("id", ""))
        if not eid or not e.get("appearance"):
            continue
        if eid in reg_by_id:
            rep["notes"].append(f"entity {eid} already exists -- not re-added")
            continue
        ent = {"id": eid, "type": e.get("type") or "setting", "name": e.get("name") or eid,
               "aliases": [], "importance": int(e.get("importance") or 3),
               "summary": e.get("summary", ""),
               "canonical_details": e.get("why", ""),
               "variants": [], "base_appearance": e["appearance"],
               "base_sheet_prompt": e.get("sheet_prompt") or (
                   f"A neutral full reference view of {e.get('name') or eid}: {e['appearance']} "
                   "Plain soft off-white background, even lighting, no text."),
               "origin": "continuity_review"}
        registry.setdefault("entities", []).append(ent)
        reg_by_id[eid] = ent
        rep["entities_added"].append(eid)
        changed = True
    for v in review.get("new_variants", []):
        eid, vid = v.get("entity_id"), _slug(v.get("id", ""))
        e = reg_by_id.get(eid)
        if not e or not vid or not v.get("appearance"):
            rep["notes"].append(f"variant {eid}/{vid}: no such entity or empty -- skipped")
            continue
        if any(x.get("id") == vid for x in e.get("variants", [])):
            rep["notes"].append(f"variant {eid}/{vid} already exists -- not re-added")
            continue
        e.setdefault("variants", []).append({
            "id": vid, "kind": v.get("kind") or "outfit", "label": v.get("label") or vid,
            "when": v.get("when", ""), "delta": v.get("delta", ""),
            "appearance": v["appearance"],
            "sheet_prompt": v.get("sheet_prompt") or e.get("base_sheet_prompt", ""),
            "origin": "continuity_review"})
        rep["variants_added"].append(f"{eid}/{vid}")
        changed = True
    if changed:
        db.save_registry(book_id, registry)

    # A proposal names the pages it is for. The critic often marks those pages revise
    # with only an edit_instruction, so fold the proposal into their casts here --
    # otherwise the picture is patched while the plan still points at the old look
    # and the next draw drifts straight back. A page edit that gives its own cast wins.
    retag: dict = {}    # idx -> list of (entity_id, variant_id)
    in_review = set(review.get("pages") or [pe.get("idx") for pe in review.get("page_edits", [])])
    for v in review.get("new_variants", []):
        eid, vid = v.get("entity_id"), _slug(v.get("id", ""))
        if eid in reg_by_id and any(x.get("id") == vid for x in reg_by_id[eid].get("variants", [])):
            for idx in v.get("pages", []) or []:
                if idx in in_review:
                    retag.setdefault(idx, []).append((eid, vid))
    for e in review.get("new_entities", []):
        eid = _slug(e.get("id", ""))
        if eid in reg_by_id:
            for idx in e.get("pages", []) or []:
                if idx in in_review:
                    retag.setdefault(idx, []).append((eid, "default"))

    for pe in review.get("page_edits", []):
        idx = pe.get("idx")
        page = db.get_page(book_id, idx) if idx is not None else None
        if not page:
            continue
        chapter_cast = db.get_chapter_cast(book_id, page["chapter_idx"])
        local_ids = {c.get("entity_id") for c in chapter_cast}
        old_cast = json.loads(page["cast_json"]) if page.get("cast_json") else []
        new_cast = None
        if pe.get("cast"):
            new_cast = _validate_cast(pe["cast"], old_cast, reg_by_id, local_ids, rep["notes"], idx)
        elif idx in retag:
            new_cast = _retag_cast(old_cast, retag[idx])
            rep["notes"].append(f"page {idx}: cast retagged to proposed "
                                + ", ".join(f"{e}/{v}" for e, v in retag[idx]))
        brief = (pe.get("brief") or "").strip() or None
        setting = (pe.get("setting") or "").strip() or None
        if brief == page.get("brief"):
            brief = None
        if setting == page.get("setting"):
            setting = None
        if new_cast is not None and new_cast == old_cast:
            new_cast = None
        if brief or setting or new_cast is not None:
            db.update_page_plan(book_id, idx, brief=brief, setting=setting, cast=new_cast)
            rep["pages_updated"].append({"idx": idx, "brief": bool(brief),
                                         "setting": bool(setting), "cast": new_cast is not None})
        action = pe.get("action")
        if action in ("revise", "regenerate") and keep_for is not None and idx not in keep_for:
            rep["skipped"].append(idx)     # cosmetic drift: not worth an image generation
            continue
        if action == "regenerate" or (action == "revise" and
                                      not (pe.get("edit_instruction") or "").strip()):
            rep["redraws"].append({"idx": idx, "mode": "regenerate"})
        elif action == "revise":
            rep["redraws"].append({
                "idx": idx, "mode": "revise",
                "seed": {"instruction": pe["edit_instruction"].strip(),
                         "ref_chars": [r for r in pe.get("reference_characters", [])
                                       if isinstance(r, str)],
                         "defect": "; ".join(pe.get("problems") or []),
                         "source": "continuity review"}})
        # A "keep" page whose cast/setting was retagged is NOT redrawn: the critic
        # judged the picture fine, and the corrected plan only matters if it is ever
        # drawn again. Spending an image generation on it would buy nothing.
    log(f"[continuity] applied: +{len(rep['entities_added'])} entities, "
        f"+{len(rep['variants_added'])} variants, {len(rep['pages_updated'])} pages updated, "
        f"{len(rep['redraws'])} redraws planned"
        + (f", {len(rep['skipped'])} cosmetic verdicts skipped" if rep["skipped"] else ""))
    return rep


def _retag_cast(old_cast: list, changes: list) -> list:
    """`old_cast` with each (entity_id, variant_id) in `changes` applied: an entity
    already in the cast switches to that variant; a new one is appended."""
    cast = [dict(c) for c in old_cast]
    for eid, vid in changes:
        hit = next((c for c in cast if c.get("entity_id") == eid), None)
        if hit:
            hit["variant_id"] = vid
        else:
            cast.append({"entity_id": eid, "variant_id": vid, "view": ""})
    return cast


def _validate_cast(cast: list, old_cast: list, reg_by_id: dict, local_ids: set,
                   notes: list, idx: int) -> list:
    """Keep only cast entries that resolve to something drawable: a registry entity
    (with a real variant id, or 'default') or a chapter-local character. An unknown
    variant falls back to whatever variant the page used before for that entity, else
    the entity's first variant."""
    old_by_id = {c.get("entity_id"): c for c in old_cast}
    out, seen = [], set()
    for c in cast:
        eid = c.get("entity_id")
        vid = c.get("variant_id") or "default"
        if not eid or eid in seen:
            continue
        e = reg_by_id.get(eid)
        if e:
            vids = [v.get("id") for v in e.get("variants", [])]
            if vids and vid not in vids:
                fallback = old_by_id.get(eid, {}).get("variant_id")
                fallback = fallback if fallback in vids else vids[0]
                notes.append(f"page {idx}: {eid} has no variant '{vid}' -- using '{fallback}'")
                vid = fallback
            elif not vids:
                vid = "default"
        elif eid not in local_ids:
            notes.append(f"page {idx}: cast id '{eid}' is not in the registry -- dropped")
            continue
        entry = {"entity_id": eid, "variant_id": vid}
        view = (c.get("view") or "").strip()
        if e and e.get("type") == "setting":
            entry["view"] = view
        elif view:
            entry["view"] = view
        out.append(entry)
        seen.add(eid)
    return out


REDRAW_WORKERS = 3


def execute_redraws(book_id: int, redraws: list, log=print, workers: int = REDRAW_WORKERS) -> list:
    """Carry out a redraw plan (the CLI path), a few pages in parallel: regenerate =
    clear the scene and draw fresh; revise = seeded img2img of the current picture.
    Returns the indices that were redrawn."""
    from concurrent.futures import ThreadPoolExecutor
    from . import scene

    def one(r):
        idx = r["idx"]
        try:
            if r["mode"] == "revise":
                cur = db.scene_data(book_id, idx)
                if cur:
                    seed = dict(r["seed"], draft=cur)
                    log(f"[continuity] revising page {idx}: {seed['instruction'][:90]}…")
                    scene.generate_scene(book_id, idx, seed=seed)
                    return idx
            log(f"[continuity] redrawing page {idx} from scratch")
            db.delete_scene(book_id, idx)
            scene.generate_scene(book_id, idx)
            return idx
        except Exception as ex:  # noqa: BLE001 -- one bad page shouldn't stop the rest
            log(f"[continuity] page {idx} redraw failed: {type(ex).__name__}: {str(ex)[:160]}")
            return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return [i for i in ex.map(one, redraws) if i is not None]


def bake_redraws(book_id: int, redraws: list, log=print) -> int:
    """Carry out a redraw plan through the BATCH bake: stage every page (revise pages
    keep their picture on show and become img2img edits; regenerate pages are drawn
    fresh) and run the bake inline, blocking until it finishes. Half the image price
    of execute_redraws, at batch latency -- worth it once a book's review adds up to
    dozens of pages. If a bake is already running, waits for it first: its in-memory
    page contexts would race the staging. Returns how many pages were staged."""
    from . import batch_bake
    if not redraws:
        return 0
    while (db.bake_get(book_id) or {}).get("status") == "baking":
        log("[continuity] a bake is running -- waiting for it before staging the redraws")
        time.sleep(15)
    n = db.bake_stage_redraws(book_id, redraws)
    log(f"[continuity] staged {n} page(s) for the batch bake "
        f"({sum(1 for r in redraws if r['mode'] == 'revise')} revise, "
        f"{sum(1 for r in redraws if r['mode'] != 'revise')} regenerate)")
    db.set_illustration_mode(book_id, "batch")
    db.bake_upsert(book_id, "baking", round=0, total_pages=db.get_book(book_id)["num_pages"])
    db.set_status(book_id, "baking", "redrawing pages from the continuity review…")
    batch_bake.run(book_id)
    return n


# ---------------- CLI ----------------

def format_review(rv: dict) -> str:
    L = [f"== pages {rv['pages'][0]}-{rv['pages'][-1]} ==", rv.get("summary", ""), ""]
    if rv.get("missing_images"):
        L.append(f"(no illustration yet: {rv['missing_images']})")
    for i in rv.get("continuity_issues", []):
        L.append(f"  [sev {i.get('severity')}] pages {i.get('pages')} {i.get('root_cause')}: "
                 f"{i.get('issue')}")
    for e in rv.get("new_entities", []):
        L.append(f"  + NEW {e.get('type')} {e['id']} \"{e.get('name')}\" pages {e.get('pages')}"
                 f" -- {e.get('why', '')}\n      {e.get('appearance', '')[:300]}")
    for v in rv.get("new_variants", []):
        L.append(f"  + NEW variant {v['entity_id']}/{v['id']} \"{v.get('label')}\" "
                 f"pages {v.get('pages')} -- {v.get('why', '')}\n      {v.get('appearance', '')[:300]}")
    for pe in rv.get("page_edits", []):
        L.append(f"  p{pe['idx']} {pe['action'].upper()}"
                 + (": " + " | ".join(pe.get("problems") or []) if pe.get("problems") else ""))
        if pe.get("edit_instruction"):
            L.append(f"      edit: {pe['edit_instruction']}")
        if pe.get("brief"):
            L.append(f"      brief: {pe['brief']}")
        if pe.get("setting"):
            L.append(f"      setting: {pe['setting']}")
        if pe.get("cast"):
            L.append("      cast: " + ", ".join(
                f"{c['entity_id']}/{c.get('variant_id', 'default')}"
                + (f"[{c['view']}]" if c.get("view") else "") for c in pe["cast"]))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_id", type=int)
    ap.add_argument("--pages", help="inclusive page range, e.g. 382-391 (default: whole book)")
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--apply", action="store_true",
                    help="write each window's recommendations into the registry/pages and "
                         "redraw, as soon as that window is reviewed")
    ap.add_argument("--no-draw", action="store_true", help="with --apply: skip the redraws")
    ap.add_argument("--batch", action="store_true",
                    help="with --apply: collect every window's redraws and run them as ONE "
                         "batch bake at the end (half the image price) instead of drawing "
                         "each window's pages interactively as it is reviewed")
    ap.add_argument("--serious-only", action="store_true",
                    help="with --apply: redraw only pages with a severity-3 issue or a plan-level "
                         "root cause (brief/cast/variant); still write every plan correction")
    ap.add_argument("--max-redraw-rate", type=float, default=None,
                    help="with --apply: stop (before applying more) once redraws / pages reviewed "
                         "exceeds this fraction, e.g. 0.25 -- the 'more than a quarter of the "
                         "book would be redrawn, rethink' guard")
    ap.add_argument("--prior", default="0/0", metavar="REDRAWN/REVIEWED",
                    help="with --max-redraw-rate: counts from earlier runs over this book, so a "
                         "relaunch judges the BOOK-wide rate rather than the chapter it resumes "
                         "in (a 15-page chapter tripped a 40%% guard at 60%% while the book stood "
                         "at 38%%)")
    ap.add_argument("--no-store", action="store_true", help="don't save the review rows")
    ap.add_argument("--json", action="store_true", help="print the raw JSON verdicts")
    a = ap.parse_args(argv)
    db.init()
    pages = db.get_pages(a.book_id)
    if not pages:
        sys.exit(f"book {a.book_id}: no pages")
    if a.pages:
        m = re.fullmatch(r"(\d+)-(\d+)", a.pages.strip())
        if not m:
            sys.exit("--pages must look like 382-391")
        start, end = int(m.group(1)), int(m.group(2))
    else:
        start, end = pages[0]["idx"], pages[-1]["idx"]

    m = re.fullmatch(r"(\d+)/(\d+)", a.prior.strip())
    if not m:
        sys.exit("--prior must look like 23/60 (redrawn/reviewed)")
    redrawn, reviewed = int(m.group(1)), int(m.group(2))
    deferred: list = []      # --batch: redraw plans collected for one bake at the end
    stopped = False

    def on_window(rv):
        """Applied per window (not once at the end) so a long run streams its fixes and
        the redraw-rate guard can stop it early instead of after the whole book."""
        nonlocal reviewed, redrawn
        print(json.dumps(rv, indent=1, ensure_ascii=False) if a.json else format_review(rv))
        print(flush=True)
        if not a.apply:
            return True
        rep = apply_review(a.book_id, rv, serious_only=a.serious_only)
        for n in rep["notes"]:
            print("  note:", n)
        if rv.get("review_id"):
            db.review_mark_applied(a.book_id, rv["review_id"], rep)
        reviewed += len(rv["pages"])
        redrawn += len(rep["redraws"])
        if not a.no_draw and rep["redraws"]:
            if a.batch:
                deferred.extend(rep["redraws"])
            else:
                with costs.run_as(f"book:{a.book_id}"):
                    execute_redraws(a.book_id, rep["redraws"])
        rate = redrawn / reviewed if reviewed else 0
        print(f"[continuity] running redraw rate: {redrawn}/{reviewed} = {rate:.0%}", flush=True)
        if a.max_redraw_rate is not None and reviewed >= 2 * a.window and rate > a.max_redraw_rate:
            nonlocal stopped
            stopped = True
            print(f"[continuity] STOP: redraw rate {rate:.0%} exceeds {a.max_redraw_rate:.0%} "
                  "-- rethink before continuing", flush=True)
            return False
        return True

    review_range(a.book_id, start, end, window=a.window, store=not a.no_store,
                 on_window=on_window)
    if deferred and stopped:
        # the guard fired: the plan corrections are written, but spend nothing on images
        # until someone has looked at why the rate is high
        print(f"[continuity] {len(deferred)} deferred redraw(s) NOT drawn (stopped by the "
              "redraw-rate guard): " + ", ".join(str(r["idx"]) for r in deferred), flush=True)
    elif deferred:
        with costs.run_as(f"book:{a.book_id}"):
            bake_redraws(a.book_id, deferred)


if __name__ == "__main__":
    main()
