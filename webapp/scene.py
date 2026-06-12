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

from pipeline import gem
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


def _gen_to_bytes(prompt, refs, model, aspect) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "img.webp"
        gem.generate_image(prompt, refs=refs, out_path=out, aspect=aspect, model=model)
        return out.read_bytes()


def _ensure_sheet(book_id, member, style_text) -> bytes | None:
    """A reference sheet for one cast member: from the roster if present, else
    drawn now (in this book's style) and cached. Returns image bytes."""
    eid, vid = member["entity_id"], member["variant_id"]
    data = db.get_sheet(book_id, eid, vid)
    if data:
        return data
    appearance = member.get("appearance", "")
    sheet_prompt = member.get("sheet_prompt", "")
    if not (appearance or sheet_prompt):
        return None
    aspect = "2:3" if member.get("type") == "character" else "3:2"
    prompt = (f"{style_text}\n\n{sheet_prompt}\n\nCanonical look (match exactly): "
              f"{appearance}\nSingle subject only, plain soft neutral background, "
              "even lighting, no text labels.")
    with _entity_lock(book_id, eid):
        data = db.get_sheet(book_id, eid, vid)   # another thread may have just drawn it
        if data:
            return data
        try:
            with tempfile.TemporaryDirectory() as td:
                # pin identity: if another variant of THIS entity is already drawn,
                # attach it so the same face/build carries across the character's looks
                refs = None
                sib = db.get_any_sheet(book_id, eid, exclude_variant_id=vid)
                if sib:
                    sp = Path(td) / "sibling.webp"
                    sp.write_bytes(sib)
                    refs = [sp]
                    prompt += ("\n\nA reference image of THIS SAME character (a different "
                               "outfit/moment) is attached. Keep the SAME facial identity, "
                               "hair, and build; change only the clothing/age/form described above.")
                data = _gen_to_bytes(prompt, refs, SHEET_IMAGE_MODEL, aspect)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet {eid}/{vid} failed: {ex}", flush=True)
            return None
        db.save_sheet(book_id, eid, vid, data)
        return data


def generate_scene(book_id: int, idx: int) -> bytes:
    """Render page `idx`'s illustration, store it, and return the image bytes.
    Synchronous/blocking (the server runs it in a worker thread)."""
    book = db.get_book(book_id)
    page = db.get_page(book_id, idx)
    if not page:
        raise ValueError(f"no page {idx} for book {book_id}")
    registry = db.get_registry(book_id)
    chapter_cast = db.get_chapter_cast(book_id, page["chapter_idx"])
    style_text = _style_text(book["style"])

    cast_index = resolve_cast({"cast": chapter_cast}, registry)
    spread = {"illustration_brief": page["brief"], "setting": page["setting"],
              "cast": json.loads(page["cast_json"]) if page.get("cast_json") else []}
    members = scene_members(spread, cast_index)

    with tempfile.TemporaryDirectory() as td:
        ref_members, ref_paths = [], []
        for m in members:
            if len(ref_members) >= MAX_REFS:
                break
            data = _ensure_sheet(book_id, m, style_text)
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
