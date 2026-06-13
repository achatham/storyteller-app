"""Generate ONE page's scene illustration on demand, decoupled from the
env-frozen config so the long-lived server can render any book in its own style.

Reuses the pipeline's prompt builders + Gemini wrappers, but takes the art style
as a parameter (config.ART_STYLE is frozen at import and would pin every book to
one style). Reference sheets come from the DB (roster), with any still-missing
sheet generated and cached on first use. Result image bytes are stored in the DB.
"""
import json
import tempfile
import threading
from pathlib import Path

from pipeline import gem, costs
from pipeline.config import (STYLES, SHEET_IMAGE_MODEL, PAGE_IMAGE_MODEL, MAX_REFS)
from pipeline.run import (resolve_cast, scene_members, build_scene_prompt,
                          SCENE_CRITIQUE, PASS_THRESHOLD)
from . import db

SCENE_TRIES = 2  # keep page latency low; prefetch hides the rest
REVISE_AVG = 3.5  # if a failed candidate averages this high, edit it (img2img) vs restart
SHEET_TRIES = 2  # reference sheets are drawn once + cached, so a reroll is cheap insurance
SHEET_PASS = 4   # min(clean_sheet, match) needed to accept a sheet

SHEET_CRITIQUE = """You are reviewing a CANONICAL REFERENCE SHEET for a children's book.
It should depict, on a plain neutral background, this single subject:
{desc}

Return JSON only:
{{
  "clean_sheet": <1-5: is there EXACTLY ONE subject, drawn ONCE? Score 1 if it is duplicated,
                  mirrored or doubled (e.g. two heads/faces, a twin copy, the same feature at
                  both ends), or there are extra subjects, text labels or a busy background>,
  "match": <1-5: does it match the description above?>,
  "issues": ["..."],
  "fix_hint": "<one sentence telling the artist how to fix the biggest problem>"
}}"""


def _draw_sheet(prompt, refs, aspect, desc) -> bytes:
    """Draw a reference sheet with a single-subject critic + retry, keeping the
    best of SHEET_TRIES attempts. Catches the model's habit of mirroring/doubling
    a subject (e.g. a figurehead at both ends -> 'two heads')."""
    with tempfile.TemporaryDirectory() as td:
        best, fix = None, ""
        for attempt in range(1, SHEET_TRIES + 1):
            p = prompt + (f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {fix}" if fix else "")
            cand = Path(td) / f"cand{attempt}.webp"
            gem.generate_image(p, refs=refs, out_path=cand, aspect=aspect, model=SHEET_IMAGE_MODEL)
            data = cand.read_bytes()
            try:
                crit = gem.critique_image(cand, SHEET_CRITIQUE.format(desc=desc or "(the subject)"))
                score = min(crit.get("clean_sheet", 0), crit.get("match", 0))
            except Exception as ex:  # noqa: BLE001 -- never fail a sheet on a critic error
                print(f"[scene] sheet critique failed: {ex}", flush=True)
                return data
            if best is None or score > best[1]:
                best = (data, score)
            if score >= SHEET_PASS:
                break
            fix = crit.get("fix_hint", "")
        return best[0]

# Serialize sheet generation per (book, entity): the first variant drawn becomes
# the identity anchor, and concurrent scenes that need the same character's sheets
# coalesce on it (no duplicate pro draws, and later variants reference the anchor).
# Different characters still draw fully in parallel.
_entity_locks: dict[tuple, threading.Lock] = {}
_entity_locks_guard = threading.Lock()


def _entity_lock(book_id, entity_id) -> threading.Lock:
    key = (book_id, entity_id)
    with _entity_locks_guard:
        lk = _entity_locks.get(key)
        if lk is None:
            lk = _entity_locks[key] = threading.Lock()
        return lk


def _style_text(style_key: str) -> str:
    return STYLES.get(style_key) or next(iter(STYLES.values()))


# A fixed, evocative scene used to render one preview thumbnail per art style so
# they can be compared side by side in the upload gallery.
SAMPLE_SCENE = (
    "Illustrate this scene for a children's picture book: a young child and a small "
    "friendly red fox sit together on a grassy hill beneath a big oak tree at golden "
    "sunset; the child holds an open storybook and points up at a butterfly. Warm, "
    "gentle, full of wonder. Horizontal storybook composition.")


def generate_style_sample(style_key: str) -> bytes | None:
    """Render (once, cached in the DB) a preview thumbnail for one art style."""
    style = STYLES.get(style_key)
    if not style:
        return None
    data = db.get_style_sample(style_key)
    if data:
        return data
    with _entity_lock(0, "sample:" + style_key):   # coalesce concurrent requests
        data = db.get_style_sample(style_key)
        if data:
            return data
        prompt = f"{style}\n\n{SAMPLE_SCENE}"
        with costs.run_as("style_sample"):
            data = _gen_to_bytes(prompt, None, PAGE_IMAGE_MODEL, "3:2")
        db.save_style_sample(style_key, data)
        return data


def _gen_to_bytes(prompt, refs, model, aspect) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "img.webp"
        gem.generate_image(prompt, refs=refs, out_path=out, aspect=aspect, model=model)
        return out.read_bytes()


def _style_anchor_path(book, td) -> "Path | None":
    """A temp file holding the book's style sample image, used as a STYLE
    reference when drawing roster sheets. Text alone under-determines the style
    (two "oil" sheets can diverge); anchoring every sheet to one concrete sample
    keeps them in a single look, which then carries into the scenes that reference
    them. Generates the sample if it does not exist yet."""
    key = book.get("style")
    data = db.get_style_sample(key)
    if not data:
        try:
            data = generate_style_sample(key)
        except Exception:  # noqa: BLE001
            return None
    if not data:
        return None
    p = Path(td) / "styleref.webp"
    p.write_bytes(data)
    return p


def _ensure_sheet(book_id, member, style_text, style_ref=None) -> bytes | None:
    """A reference sheet for one cast member: from the roster if present, else
    drawn now (in this book's style) and cached. `style_ref` is a style-anchor
    image so all sheets share one concrete look. Returns image bytes."""
    eid, vid = member["entity_id"], member["variant_id"]
    data = db.get_sheet(book_id, eid, vid)
    if data:
        return data
    appearance = member.get("appearance", "")
    sheet_prompt = member.get("sheet_prompt", "")
    if not (appearance or sheet_prompt):
        return None
    img_aspect = "2:3" if member.get("type") == "character" else "3:2"
    base = (f"{style_text}\n\n{sheet_prompt}\n\nCanonical look (match exactly): "
            f"{appearance}\nEXACTLY ONE figure / a single subject -- never two people, "
            "and never the same character shown at two ages or in two outfits. Plain soft "
            "neutral background, even lighting, no text labels.")
    with _entity_lock(book_id, eid):
        data = db.get_sheet(book_id, eid, vid)   # another thread may have just drawn it
        if data:
            return data
        try:
            with tempfile.TemporaryDirectory() as td:
                refs, notes = [], []
                # 1) a STYLE reference so this sheet matches the book's one look
                if style_ref and style_ref.exists():
                    refs.append(style_ref)
                    notes.append(f"Image {len(refs)} is a STYLE REFERENCE: match its artistic "
                                 "style, medium, brush/line work and colour palette EXACTLY -- "
                                 "but do NOT copy its subject or scene.")
                # 2) identity: another variant of THIS entity, to keep the same face/build
                sib = db.get_any_sheet(book_id, eid, exclude_variant_id=vid)
                if sib:
                    sp = Path(td) / "sibling.webp"
                    sp.write_bytes(sib)
                    refs.append(sp)
                    notes.append(f"Image {len(refs)} shows THIS SAME character in a different "
                                 "outfit/moment: keep the SAME facial identity, hair and build; "
                                 "change only the clothing/age/form described above.")
                prompt = base + ("\n\n" + " ".join(notes) if notes else "")
                data = _draw_sheet(prompt, refs or None, img_aspect, appearance)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet {eid}/{vid} failed: {ex}", flush=True)
            return None
        db.save_sheet(book_id, eid, vid, data)
        return data


def _view_prompt(name, appearance, view):
    """A general (book-agnostic) reference prompt for a non-exterior view of an
    object -- described by the camera's relationship to it, not by named parts."""
    if view == "interior":
        what = (f"the INTERIOR of {name}, seen from INSIDE it -- ONLY its interior surfaces, "
                f"materials and architecture (e.g. for a ship: wood-panelled cabin walls, beams, a "
                f"porthole, lanterns; for a building: a room within). Do NOT show the exterior of "
                f"{name}, its overall outer shape, or any exterior-only identifying feature (a "
                f"figurehead, the whole hull or outer silhouette) -- not even through a window.")
    else:  # surface
        what = (f"the open outer SURFACE / top of {name}, seen by someone standing ON it and looking "
                f"along it (for a ship: its deck with the base of the mast, rigging, rails, hatches and "
                f"cabin door; for a building: its rooftop), with only the structures that rise directly "
                f"from that surface. Do NOT show a separate whole copy of {name} in the distance, and do "
                f"NOT show its exterior-only identifying features (a figurehead or prow ornament, the "
                f"full outer hull or silhouette). Draw each feature EXACTLY ONCE -- never a mirrored, "
                f"doubled or twin copy at both ends.")
    return (f"A neutral REFERENCE of {what}\nUse this only for the palette, wood tones and materials "
            f"(do NOT add exterior features from it): {appearance}\n"
            f"No people, even lighting, single clean reference.")


def _ensure_view_sheet(book_id, member, style_text, view, style_ref=None) -> bytes | None:
    """A non-exterior reference (view = 'surface' or 'interior') for a setting/prop
    so scenes ON or INSIDE it don't get the whole exterior pasted in. One per
    (entity, view), keyed under a synthetic '__surface'/'__interior' variant; NOT
    seeded from the exterior sheet. Falls back to the normal sheet on failure."""
    eid = member["entity_id"]
    vid = "__" + view
    data = db.get_sheet(book_id, eid, vid)
    if data:
        return data
    prompt = f"{style_text}\n\n" + _view_prompt(member.get("name", eid),
                                                member.get("appearance", ""), view)
    with _entity_lock(book_id, eid + ":" + view):
        data = db.get_sheet(book_id, eid, vid)
        if data:
            return data
        try:
            refs = None
            if style_ref and style_ref.exists():
                refs = [style_ref]
                prompt += ("\n\nThe attached image is a STYLE REFERENCE: match its art style, "
                           "medium and colour palette exactly, but do NOT copy its subject.")
            desc = f"{_view_prompt(member.get('name', eid), '', view)}"
            data = _draw_sheet(prompt, refs, "3:2", desc)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] {view} sheet {eid} failed: {ex}", flush=True)
            return _ensure_sheet(book_id, member, style_text, style_ref)   # fallback
        db.save_sheet(book_id, eid, vid, data)
        return data


def generate_scene(book_id: int, idx: int) -> bytes:
    """Render page `idx`'s illustration, store it, and return the image bytes.
    Synchronous/blocking (the server runs it in a worker thread). All API usage
    is tagged to this book for the per-book cost view."""
    with costs.run_as(f"book:{book_id}"):
        return _render_scene(book_id, idx)


def _render_scene(book_id: int, idx: int) -> bytes:
    book = db.get_book(book_id)
    page = db.get_page(book_id, idx)
    if not page:
        raise ValueError(f"no page {idx} for book {book_id}")
    registry = db.get_registry(book_id)
    chapter_cast = db.get_chapter_cast(book_id, page["chapter_idx"])
    style_text = _style_text(book["style"])

    cast_index = resolve_cast({"cast": chapter_cast}, registry)
    page_cast = json.loads(page["cast_json"]) if page.get("cast_json") else []
    spread = {"illustration_brief": page["brief"], "setting": page["setting"], "cast": page_cast,
              "read_text": page["read_text"]}
    members = scene_members(spread, cast_index)
    # aspect per entity (set on settings/props by the prop pass): exterior | surface | interior
    aspect = {c.get("entity_id"): c.get("aspect") for c in page_cast if c.get("aspect")}

    def _view(m):   # the non-exterior view for a SETTING you can stand on / go inside, or None
        # Only settings (a ship, a room, a building) have a surface or interior. A
        # prop -- a painting, a book, a knife -- is always shown whole; "the interior
        # of a picture" is incoherent and fuses the prop with the room around it.
        if m.get("type") == "setting":
            a = aspect.get(m["entity_id"])
            if a in ("surface", "interior"):
                return a
        return None

    # for INTERIOR/SURFACE scenes, rewrite the object's description so the prompt
    # never narrates its whole exterior -- else the model paints the figurehead
    # (and tends to mirror it to both edges -> a "double-headed ship" on deck)
    interior_names, surface_names = [], []
    for m in members:
        name = m.get("name", m["entity_id"])
        v = _view(m)
        if v == "interior":
            interior_names.append(name)
            m["appearance"] = (f"the interior of {name} -- show ONLY what is visible inside; do NOT "
                               "depict its exterior or exterior-only features, even through a window")
        elif v == "surface":
            surface_names.append(name)
            m["appearance"] = (f"the open deck / top surface of {name} -- show the deck underfoot and the "
                               f"masts, rigging and rails rising from it; do NOT include {name}'s "
                               "figurehead, prow ornament or whole outer hull")

    with tempfile.TemporaryDirectory() as td:
        style_ref = _style_anchor_path(book, td)   # one concrete look for every sheet
        ref_members, ref_paths = [], []
        # the location (a surface/interior setting/prop the scene is in) is prioritized
        # into the reference set -- it matters as much as the cast for coherence
        ordered = sorted(members, key=lambda m: 0 if _view(m) else 1)
        for m in ordered:
            if len(ref_members) >= MAX_REFS:
                break
            view = _view(m)
            if view:   # use the surface/interior reference, not the whole-exterior sheet
                data = _ensure_view_sheet(book_id, m, style_text, view, style_ref=style_ref)
            else:
                data = _ensure_sheet(book_id, m, style_text, style_ref=style_ref)
            if data:
                p = Path(td) / f"{m['entity_id']}__{m['variant_id']}.webp"
                p.write_bytes(data)
                ref_members.append(m)
                ref_paths.append(p)

        char_desc = "\n".join(f"- {m['name']}: {m['appearance']}" for m in members)
        place_note = ""
        if interior_names:
            names = ", ".join(interior_names)
            place_note = (f"\n\nThe scene takes place INSIDE {names}: show only its interior. Do NOT "
                             f"depict the whole exterior of {names} or its exterior-only features "
                             "(figureheads, the full outer hull/silhouette) anywhere -- not even through "
                             "a window or doorway.")
        if surface_names:
            names = ", ".join(surface_names)
            place_note += (f"\n\nThe scene takes place ON the open deck/surface of {names}: show the deck "
                              f"and the masts, rigging and rails rising from it. Do NOT show {names}'s "
                              "exterior-only identifying features (a figurehead or prow ornament, the full "
                              "outer hull or silhouette), and NEVER show a mirrored, doubled or twin copy of "
                              "any feature at both ends/edges of the picture.")
        best, fix, draft = None, "", None
        for attempt in range(1, SCENE_TRIES + 1):
            cand = Path(td) / f"cand{attempt}.webp"
            if draft is not None:
                # REVISE: the last attempt was broadly right with one flagged defect.
                # Edit that image (keep composition/characters/style, change only the
                # defect) instead of regenerating -- preserves what worked and lets us
                # actually land a specific fix (e.g. "their hands must be bound").
                edit_prompt = (f"{style_text}\n\nImage 1 is a DRAFT illustration that is almost right. "
                               f"Redraw it keeping its composition, characters, poses, setting and art "
                               f"style the SAME, and change ONLY this: {fix}\n\n"
                               f"The people must still match:\n{char_desc}{place_note}")
                refs = ([draft] + ref_paths)[:MAX_REFS]
                gem.generate_image(edit_prompt, refs=refs, out_path=cand, aspect="3:2",
                                   model=PAGE_IMAGE_MODEL)
            else:
                prompt = build_scene_prompt(spread, members, ref_members, style_text, fix=fix) + place_note
                gem.generate_image(prompt, refs=ref_paths, out_path=cand, aspect="3:2",
                                   model=PAGE_IMAGE_MODEL)
            crit = gem.critique_image(cand, SCENE_CRITIQUE.format(
                brief=page["brief"], chars=char_desc or "(none)", style=style_text,
                source=(page["read_text"] or "")[:1200] or "(not available)"))
            scores = [crit.get(k, 0) for k in ("consistency", "accuracy", "kid_appropriate", "style_ok")]
            score = min(scores)
            data = cand.read_bytes()
            if best is None or score > best[1]:
                best = (data, score)
            if score >= PASS_THRESHOLD:
                break
            fix = crit.get("fix_hint", "")
            # "very close" (broadly good, one weak spot) -> revise this image next;
            # otherwise discard it and regenerate the scene from scratch.
            draft = cand if (sum(scores) / len(scores)) >= REVISE_AVG else None

    data, score = best
    db.scene_store(book_id, idx, data, score)
    return data
