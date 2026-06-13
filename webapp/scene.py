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
                data = _gen_to_bytes(prompt, refs or None, SHEET_IMAGE_MODEL, img_aspect)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet {eid}/{vid} failed: {ex}", flush=True)
            return None
        db.save_sheet(book_id, eid, vid, data)
        return data


ABOARD_VID = "__aboard"


def _ensure_aboard_sheet(book_id, member, style_text, style_ref=None) -> bytes | None:
    """An interior / on-board reference for a setting or prop (e.g. a ship's deck,
    a room inside a building), so scenes set INSIDE/ON it don't get the whole
    exterior pasted in. One per entity, keyed under a synthetic '__aboard' variant.
    Deliberately NOT seeded from the exterior sheet -- that would put the whole
    object back. Falls back to the normal sheet if it can't be made."""
    eid = member["entity_id"]
    data = db.get_sheet(book_id, eid, ABOARD_VID)
    if data:
        return data
    name = member.get("name", eid)
    appearance = member.get("appearance", "")
    prompt = (f"{style_text}\n\nA neutral REFERENCE of the INTERIOR / ON-BOARD view of {name} -- "
              f"what you would see when INSIDE or ABOARD it (for a ship: its open wooden deck with "
              f"the base of the mast, rigging and side rails; for a building: a room within), in the "
              f"same materials, colours and style as: {appearance}\n"
              f"Show ONLY the interior/on-board structure -- do NOT show the whole exterior of "
              f"{name}. No people, even lighting, single clean reference.")
    with _entity_lock(book_id, eid + ":aboard"):
        data = db.get_sheet(book_id, eid, ABOARD_VID)
        if data:
            return data
        try:
            refs = None
            if style_ref and style_ref.exists():
                refs = [style_ref]
                prompt += ("\n\nThe attached image is a STYLE REFERENCE: match its art style, "
                           "medium and colour palette exactly, but do NOT copy its subject.")
            data = _gen_to_bytes(prompt, refs, SHEET_IMAGE_MODEL, "3:2")
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] aboard sheet {eid} failed: {ex}", flush=True)
            return _ensure_sheet(book_id, member, style_text, style_ref)   # fallback
        db.save_sheet(book_id, eid, ABOARD_VID, data)
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
    spread = {"illustration_brief": page["brief"], "setting": page["setting"], "cast": page_cast}
    members = scene_members(spread, cast_index)
    # aspect per entity (set on settings/props by the prop pass): exterior vs aboard
    aspect = {c.get("entity_id"): c.get("aspect") for c in page_cast if c.get("aspect")}

    with tempfile.TemporaryDirectory() as td:
        style_ref = _style_anchor_path(book, td)   # one concrete look for every sheet
        ref_members, ref_paths = [], []
        # an aboard setting/prop (the deck/room the scene is in) is prioritized into
        # the reference set -- the location matters as much as the cast for coherence
        ordered = sorted(members, key=lambda m: 0 if (
            m.get("type") in ("setting", "prop")
            and aspect.get(m["entity_id"]) == "aboard") else 1)
        for m in ordered:
            if len(ref_members) >= MAX_REFS:
                break
            # for a setting/prop the characters are INSIDE/ON, use its interior
            # reference (a deck/room view) instead of the whole-exterior sheet,
            # so the whole object isn't pasted into an on-board scene
            if m.get("type") in ("setting", "prop") and aspect.get(m["entity_id"]) == "aboard":
                data = _ensure_aboard_sheet(book_id, m, style_text, style_ref=style_ref)
            else:
                data = _ensure_sheet(book_id, m, style_text, style_ref=style_ref)
            if data:
                p = Path(td) / f"{m['entity_id']}__{m['variant_id']}.webp"
                p.write_bytes(data)
                ref_members.append(m)
                ref_paths.append(p)

        char_desc = "\n".join(f"- {m['name']}: {m['appearance']}" for m in members)
        best, fix = None, ""
        for attempt in range(1, SCENE_TRIES + 1):
            prompt = build_scene_prompt(spread, members, ref_members, style_text, fix=fix)
            cand = Path(td) / f"cand{attempt}.webp"
            gem.generate_image(prompt, refs=ref_paths, out_path=cand, aspect="3:2",
                               model=PAGE_IMAGE_MODEL)
            crit = gem.critique_image(cand, SCENE_CRITIQUE.format(
                brief=page["brief"], chars=char_desc or "(none)", style=style_text))
            score = min(crit.get("consistency", 0), crit.get("accuracy", 0),
                        crit.get("kid_appropriate", 0), crit.get("style_ok", 0))
            data = cand.read_bytes()
            if best is None or score > best[1]:
                best = (data, score)
            if score >= PASS_THRESHOLD:
                break
            fix = crit.get("fix_hint", "")

    data, score = best
    db.scene_store(book_id, idx, data, score)
    return data
