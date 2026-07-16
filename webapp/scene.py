"""Generate ONE page's scene illustration on demand, decoupled from the
env-frozen config so the long-lived server can render any book in its own style.

Reuses the pipeline's prompt builders + Gemini wrappers, but takes the art style
as a parameter (config.ART_STYLE is frozen at import and would pin every book to
one style). Reference sheets come from the DB (roster), with any still-missing
sheet generated and cached on first use. Result image bytes are stored in the DB.
"""
import io
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from PIL import Image

from pipeline import gem, costs, analyze
from pipeline.config import (STYLES, SHEET_IMAGE_MODEL, PAGE_IMAGE_MODEL, ROSTER_IMAGE_MODEL,
                             LITE_IMAGE_MODEL, MAX_REFS,
                             ANALYZE_MODEL, WEBP_QUALITY, SCENE_MAXW)

# The image models offered for a manual roster-sheet correction, keyed by the short
# name the roster UI sends. "pro" is the high-fidelity sheet model; "lite" is the new
# cheap/fast "nano banana lite". Order here is the order the radio buttons render in.
EDIT_MODELS = {
    "pro": SHEET_IMAGE_MODEL,     # gemini-3-pro-image-preview  (Nano Banana Pro)
    "flash": PAGE_IMAGE_MODEL,    # gemini-3.1-flash-image
    "lite": LITE_IMAGE_MODEL,     # gemini-3.1-flash-lite-image (Nano Banana Lite)
}

# Debug-history candidates are review-only -> compress them harder than display art.
DEBUG_MAXW = int(os.environ.get("STORY_DEBUG_MAXW", "960"))
DEBUG_QUALITY = int(os.environ.get("STORY_DEBUG_QUALITY", "52"))


def _compress(data: bytes, max_w: int, quality: int) -> bytes:
    """Re-encode webp bytes for storage: downscale to max_w (0 = keep) and apply a
    lossy quality. Returns the original if anything goes wrong or it wouldn't shrink."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if max_w and im.width > max_w:
            im = im.resize((max_w, round(max_w * im.height / im.width)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=quality, method=6)
        b = out.getvalue()
        return b if len(b) < len(data) else data
    except Exception:  # noqa: BLE001
        return data


def recompress_book(book_id) -> dict:
    """Re-encode already-stored art to the current display settings, in place --
    scenes downscaled to SCENE_MAXW, sheets re-encoded at quality only (they feed
    generation, so keep their resolution). No regeneration / API cost."""
    before = after = changed = 0
    for idx, data in db.iter_scene_blobs(book_id):
        nb = _compress(data, SCENE_MAXW, WEBP_QUALITY)
        before += len(data); after += len(nb)
        if len(nb) < len(data):
            db.update_scene_blob(book_id, idx, nb); changed += 1
    for eid, vid in db.list_sheets(book_id):
        data = db.get_sheet(book_id, eid, vid)
        if not data:
            continue
        nb = _compress(data, 0, WEBP_QUALITY)
        before += len(data); after += len(nb)
        if len(nb) < len(data):
            db.save_sheet(book_id, eid, vid, nb); changed += 1
    return {"changed": changed, "before_kb": before // 1024, "after_kb": after // 1024,
            "saved_kb": (before - after) // 1024}
from pipeline.run import (resolve_cast, scene_members, build_scene_prompt,
                          roster_digest, SCENE_CRITIQUE, SCENE_CRITIQUE_SCHEMA,
                          PASS_THRESHOLD)
from . import db

SCENE_TRIES = int(os.environ.get("STORY_SCENE_TRIES", "3"))  # max image attempts per scene
# Critic sub-score weights in the averaged quality score: anatomical correctness
# and not-spoiling-later-in-the-chapter matter twice as much as the rest.
SCORE_WEIGHTS = {"physical": 2, "no_spoiler": 2}
# whether a failed candidate may be fixed in place (img2img revise) at all, or must
# always be redrawn from scratch. When on, the critic chooses revise vs regenerate.
SCENE_REVISE = os.environ.get("STORY_SCENE_REVISE", "1") != "0"

# Carry-forward re-check: after a revise aimed at a specific defect, confirm that
# EXACT defect is gone before trusting the (fresh, stochastic) holistic critique.
# The critic sometimes flips a hard veto to a pass on an img2img revise that barely
# changed the picture, so the flagged flaw (e.g. an extra arm) survives unnoticed.
FIX_VERIFY = """You are verifying ONE specific correction to a children's-book illustration.

The illustration was just revised to fix this specific problem:
"{defect}"

Look ONLY at whether THAT specific problem is now completely gone. Ignore every other \
aspect of the picture (style, mood, other characters). A common failure is an edit that \
barely changed the image, so the problem is still present.

Return JSON only:
{{"resolved": <true only if the described problem is clearly and fully gone; false if any \
trace of it remains -- e.g. an extra limb is still visible>,
  "still_present": "<if false, one sentence on what still remains; else empty>"}}"""

FIX_VERIFY_SCHEMA = {"type": "object", "properties": {
    "resolved": {"type": "boolean"}, "still_present": {"type": "string"}},
    "required": ["resolved"]}


def _verify_fix(cand_path, defect: str) -> dict:
    """Focused vision check: was `defect` actually removed from the revised image?
    Best-effort -- on any failure assume resolved (don't block on the checker)."""
    try:
        return gem.critique_image(cand_path, FIX_VERIFY.format(defect=defect),
                                  schema=FIX_VERIFY_SCHEMA)
    except Exception as ex:  # noqa: BLE001
        print(f"[scene] fix-verify failed: {ex}", flush=True)
        return {"resolved": True}


# When no attempt clears the bar, a vision critic picks the best of the candidates.
JUDGE_BEST = """You are an art director choosing the BEST of several candidate illustrations for one \
page of a children's picture book. None is perfect -- pick the single candidate that best depicts the \
scene, matches the characters, and is most usable (fewest/least serious problems).

THE SCENE SHOULD SHOW:
{brief}

Return JSON only: {{"best": <the 1-based number of the best candidate>, "why": "<short reason>"}}"""
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


def _draw_sheet(prompt, refs, aspect, desc):
    """Draw a reference sheet with a single-subject critic + retry, keeping the best
    of SHEET_TRIES attempts. Catches the model's habit of mirroring/doubling a subject
    (e.g. a figurehead at both ends -> 'two heads'). Returns (best_bytes, trace) where
    trace = {"attempts": [...], "chosen": n} for the debug history."""
    with tempfile.TemporaryDirectory() as td:
        best, fix, attempts = None, "", []
        for attempt in range(1, SHEET_TRIES + 1):
            p = prompt + (f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {fix}" if fix else "")
            cand = Path(td) / f"cand{attempt}.webp"
            gem.generate_image(p, refs=refs, out_path=cand, aspect=aspect, model=ROSTER_IMAGE_MODEL)
            data = cand.read_bytes()
            try:
                crit = gem.critique_image(cand, SHEET_CRITIQUE.format(desc=desc or "(the subject)"))
                cs, mt = crit.get("clean_sheet", 0), crit.get("match", 0)
                score, avg = min(cs, mt), round((cs + mt) / 2, 2)
            except Exception as ex:  # noqa: BLE001 -- never fail a sheet on a critic error
                print(f"[scene] sheet critique failed: {ex}", flush=True)
                attempts.append({"attempt": attempt, "prompt": p, "data": data,
                                 "critique": None, "min": None, "avg": None})
                return data, {"attempts": attempts, "chosen": attempt}
            attempts.append({"attempt": attempt, "prompt": p, "data": data,
                             "critique": crit, "min": score, "avg": avg})
            if best is None or score > best[1]:
                best = (data, score, attempt)
            if score >= SHEET_PASS:
                break
            fix = crit.get("fix_hint", "")
        return best[0], {"attempts": attempts, "chosen": best[2]}


def _save_sheet_history(book_id, eid, vid, desc, trace):
    """Persist a sheet generation's attempts (prompt + candidate image + critique)."""
    try:
        gen_id = db.next_sheet_gen_id(book_id, eid, vid)
        for a in trace["attempts"]:
            db.sheet_attempt_add(book_id, eid, vid, gen_id, a["attempt"], a["prompt"],
                                 _compress(a["data"], DEBUG_MAXW, DEBUG_QUALITY),
                                 json.dumps(a["critique"]) if a["critique"] else None,
                                 a["min"], a["avg"])
        chosen = trace["chosen"]
        final = next((a["min"] for a in trace["attempts"] if a["attempt"] == chosen), None)
        db.sheet_gen_add(book_id, eid, vid, gen_id, (desc or "")[:600], chosen, final)
    except Exception as ex:  # noqa: BLE001 -- history is best-effort
        print(f"[scene] sheet history save failed: {ex}", flush=True)

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
    "sunset; the child points up at a butterfly. Warm, gentle, full of wonder. "
    "ONE single seamless illustration that fills the whole frame -- it is NOT a book: "
    "no open pages, no centre fold, gutter, crease or seam, and no border, frame or "
    "panel divisions. Horizontal storybook composition.")


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


def _sheet_base_prompt(style_text, sheet_prompt, appearance) -> str:
    """The shared 'draw one reference sheet from a text description' prompt, used both
    by the automatic first-draw and by a manual from-scratch redraw so the two stay
    identical apart from the (editable) subject text."""
    return (f"{style_text}\n\n{sheet_prompt}\n\nCanonical look (match exactly): "
            f"{appearance}\nEXACTLY ONE figure / a single subject -- never two people, "
            "and never the same character shown at two ages or in two outfits. Plain soft "
            "neutral background, even lighting, no text labels.")


_STYLE_REF_NOTE = ("Image {n} is a STYLE REFERENCE: match its artistic style, medium, "
                   "brush/line work and colour palette EXACTLY -- but do NOT copy its "
                   "subject or scene.")


def sheet_gen_request(member, style_text, style_ref_bytes=None, sibling_bytes=None) -> dict | None:
    """Build the image-generation request for ONE roster sheet from bytes (no temp
    files, no DB) so the batch roster draw can assemble a JSONL batch. Mirrors the
    prompt/refs _ensure_sheet builds: a style anchor first, then (for a non-anchor
    variant) a sibling sheet of the same entity to hold identity. Returns
    {prompt, ref_bytes, aspect, desc} or None if the member has no describable look."""
    appearance = member.get("appearance", "")
    sheet_prompt = member.get("sheet_prompt", "")
    if not (appearance or sheet_prompt):
        return None
    aspect = "2:3" if member.get("type") == "character" else "3:2"
    base = _sheet_base_prompt(style_text, sheet_prompt, appearance)
    refs, notes = [], []
    if style_ref_bytes:
        refs.append(style_ref_bytes)
        notes.append(_STYLE_REF_NOTE.format(n=len(refs)))
    if sibling_bytes:
        refs.append(sibling_bytes)
        notes.append(f"Image {len(refs)} shows THIS SAME character in a different outfit/moment: "
                     "keep the SAME facial identity, hair and build; change only the "
                     "clothing/age/form described above.")
    prompt = base + ("\n\n" + " ".join(notes) if notes else "")
    return {"prompt": prompt, "ref_bytes": refs, "aspect": aspect,
            "desc": appearance or sheet_prompt}


def style_anchor_bytes(book) -> bytes | None:
    """The book's style-sample image bytes (generating it if needed), used as a
    shared STYLE reference when drawing every roster sheet. None if unavailable."""
    key = book.get("style")
    data = db.get_style_sample(key)
    if data:
        return data
    try:
        return generate_style_sample(key)
    except Exception:  # noqa: BLE001
        return None


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
    base = _sheet_base_prompt(style_text, sheet_prompt, appearance)
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
                    notes.append(_STYLE_REF_NOTE.format(n=len(refs)))
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
                data, trace = _draw_sheet(prompt, refs or None, img_aspect, appearance)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet {eid}/{vid} failed: {ex}", flush=True)
            return None
        db.save_sheet(book_id, eid, vid, data)
        _save_sheet_history(book_id, eid, vid, appearance, trace)
        return data


def _view_slug(view: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (view or "").strip().lower()).strip("_")
    return s[:28] or "inside"


def _view_prompt(name, appearance, view):
    """A book-agnostic reference prompt for ONE specific place within/on a setting,
    named by the story (e.g. 'lobby', 'rooftop', 'the deck'). Generic -- no ship or
    building assumptions baked in; the entity's own appearance drives the look."""
    what = (f"the '{view}' of {name} -- the specific spot inside or on {name} that the story calls "
            f"its {view}. Show ONLY what is actually visible at that spot: its local surfaces, "
            f"fittings, furniture and architecture, true to what {name} is. Do NOT show the whole "
            f"exterior of {name}, its overall outer shape, or any exterior-only identifying feature "
            f"of {name} -- not even through a window or doorway. Draw it ONCE, no mirrored or doubled "
            f"copy.")
    return (f"A neutral REFERENCE of {what}\n"
            f"For context, {name} as a whole is (use only to get its materials, palette and era right; "
            f"do NOT depict its exterior here): {appearance}\n"
            f"No people, even lighting, single clean reference.")


def _ensure_view_sheet(book_id, member, style_text, view, style_ref=None) -> bytes | None:
    """A reference for ONE named place within/on a setting (view = a short story label
    like 'lobby' or 'deck'), so scenes set there don't get the whole exterior pasted
    in. One per (entity, view), keyed under a synthetic '__v_<slug>' variant; NOT
    seeded from the exterior sheet. Falls back to the normal sheet on failure."""
    eid = member["entity_id"]
    vid = "__v_" + _view_slug(view)
    data = db.get_sheet(book_id, eid, vid)
    if data:
        return data
    appearance = member.get("appearance", "")
    prompt = f"{style_text}\n\n" + _view_prompt(member.get("name", eid), appearance, view)
    desc = _view_prompt(member.get("name", eid), "", view)
    with _entity_lock(book_id, eid + ":" + vid):
        data = db.get_sheet(book_id, eid, vid)
        if data:
            return data
        try:
            refs = None
            if style_ref and style_ref.exists():
                refs = [style_ref]
                prompt += ("\n\nThe attached image is a STYLE REFERENCE: match its art style, "
                           "medium and colour palette exactly, but do NOT copy its subject.")
            data, trace = _draw_sheet(prompt, refs, "3:2", desc)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] view '{view}' sheet {eid} failed: {ex}", flush=True)
            return _ensure_sheet(book_id, member, style_text, style_ref)   # fallback
        db.save_sheet(book_id, eid, vid, data)
        _save_sheet_history(book_id, eid, vid, desc, trace)
        return data


def _member_for(registry, entity_id, variant_id):
    """Build the member dict for one entity/variant from the registry (for an
    on-demand single-sheet redraw). Synthetic view variants (__interior/__surface)
    use the base entity's look."""
    e = next((x for x in registry.get("entities", []) if x.get("id") == entity_id), None)
    if not e:
        return None
    var = None if variant_id.startswith("__") else \
        next((v for v in e.get("variants", []) if v.get("id") == variant_id), None)
    base_vid = (e.get("variants") or [{}])[0].get("id", "default") \
        if variant_id.startswith("__") else variant_id
    return {"entity_id": entity_id, "variant_id": base_vid, "name": e.get("name", entity_id),
            "appearance": (var or {}).get("appearance") or e.get("base_appearance", ""),
            "sheet_prompt": (var or {}).get("sheet_prompt") or e.get("base_sheet_prompt", ""),
            "type": e.get("type", "character")}


def regenerate_sheet(book_id, entity_id, variant_id) -> dict:
    """Force-redraw one roster sheet (clears the cached one) so it regenerates with
    a fresh debug-history trace. Tagged to the book for cost accounting."""
    book = db.get_book(book_id)
    registry = db.get_registry(book_id)
    if not book or not registry:
        return {"ok": False, "error": "no book or registry"}
    member = _member_for(registry, entity_id, variant_id)
    if not member:
        return {"ok": False, "error": "no such entity in registry"}
    style_text = _style_text(book["style"])
    db.delete_sheet(book_id, entity_id, variant_id)
    with costs.run_as(f"book:{book_id}"), tempfile.TemporaryDirectory() as td:
        style_ref = _style_anchor_path(book, td)
        if variant_id.startswith("__v_"):                 # a named view -> recover the label
            data = _ensure_view_sheet(book_id, member, style_text,
                                      variant_id[4:].replace("_", " "), style_ref=style_ref)
        elif variant_id.startswith("__"):                 # legacy __interior/__surface
            data = _ensure_view_sheet(book_id, member, style_text, variant_id[2:], style_ref=style_ref)
        else:
            data = _ensure_sheet(book_id, member, style_text, style_ref=style_ref)
    return {"ok": bool(data)}


def get_sheet_prompt(book_id, entity_id, variant_id) -> dict:
    """The editable text that produced (or would produce) this sheet: the roster's
    `sheet_prompt` (reference framing) and `appearance` (canonical look). Powers the
    roster UI's 'regenerate from scratch' editor."""
    registry = db.get_registry(book_id)
    member = _member_for(registry, entity_id, variant_id) if registry else None
    if not member:
        return {"ok": False, "error": "no such entity in registry"}
    return {"ok": True, "name": member.get("name", entity_id),
            "type": member.get("type", "character"),
            "sheet_prompt": member.get("sheet_prompt", ""),
            "appearance": member.get("appearance", ""),
            "editable": not variant_id.startswith("__")}


def _persist_sheet_prompt(registry, entity_id, variant_id, sheet_prompt, appearance) -> bool:
    """Write an edited prompt back onto the registry variant so future redraws/bakes
    use it too. Synthetic view variants (__...) have no variant object -> skip."""
    if variant_id.startswith("__"):
        return False
    e = next((x for x in registry.get("entities", []) if x.get("id") == entity_id), None)
    if not e:
        return False
    v = next((x for x in e.get("variants", []) if x.get("id") == variant_id), None)
    if not v:
        return False
    v["sheet_prompt"] = sheet_prompt
    v["appearance"] = appearance
    return True


def redraw_sheet_from_prompt(book_id, entity_id, variant_id, sheet_prompt,
                             appearance="", persist=True) -> dict:
    """Draw one roster sheet FROM SCRATCH using a user-edited prompt (not an img2img
    tweak of the current image), replacing the cached sheet. Uses the same style
    anchoring + single-subject critic/retry as the automatic first draw, but does NOT
    seed from a sibling sheet -- the point is to honour the new description. When
    `persist`, the edited text is saved back onto the registry variant."""
    sheet_prompt = (sheet_prompt or "").strip()
    appearance = (appearance or "").strip()
    if not (sheet_prompt or appearance):
        return {"ok": False, "error": "prompt required"}
    book = db.get_book(book_id)
    registry = db.get_registry(book_id)
    if not book or not registry:
        return {"ok": False, "error": "no book or registry"}
    member = _member_for(registry, entity_id, variant_id)
    if not member:
        return {"ok": False, "error": "no such entity in registry"}
    style_text = _style_text(book["style"])
    img_aspect = "2:3" if member.get("type") == "character" else "3:2"
    base = _sheet_base_prompt(style_text, sheet_prompt, appearance)
    with costs.run_as(f"book:{book_id}"), tempfile.TemporaryDirectory() as td:
        refs = []
        style_ref = _style_anchor_path(book, td)
        if style_ref and style_ref.exists():
            refs.append(style_ref)
            base += "\n\n" + _STYLE_REF_NOTE.format(n=len(refs))
        try:
            data, trace = _draw_sheet(base, refs or None, img_aspect, appearance or sheet_prompt)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet redraw {entity_id}/{variant_id} failed: {ex}", flush=True)
            return {"ok": False, "error": str(ex)}
    data = _compress(data, 0, WEBP_QUALITY)   # sheets feed generation -> keep resolution
    db.save_sheet(book_id, entity_id, variant_id, data)
    _save_sheet_history(book_id, entity_id, variant_id,
                        f"REDRAW: {appearance or sheet_prompt}", trace)
    if persist and _persist_sheet_prompt(registry, entity_id, variant_id, sheet_prompt, appearance):
        db.save_registry(book_id, registry)
    return {"ok": True}


def edit_sheet(book_id, entity_id, variant_id, instruction, model_key="pro") -> dict:
    """Apply a user's written correction to one roster sheet as an img2img edit:
    keep the same subject/identity/style, change ONLY what the instruction asks, then
    replace the cached sheet. `model_key` picks the image model (see EDIT_MODELS)."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "empty instruction"}
    model = EDIT_MODELS.get(model_key)
    if not model:
        return {"ok": False, "error": f"unknown model '{model_key}'"}
    book = db.get_book(book_id)
    if not book:
        return {"ok": False, "error": "no such book"}
    current = db.get_sheet(book_id, entity_id, variant_id)
    if not current:
        return {"ok": False, "error": "sheet not drawn yet"}
    registry = db.get_registry(book_id)
    member = _member_for(registry, entity_id, variant_id) if registry else None
    style_text = _style_text(book["style"])
    is_char = (member or {}).get("type", "character") == "character"
    aspect = "2:3" if is_char else "3:2"
    appearance = (member or {}).get("appearance", "")
    # What to hold constant depends on the subject: a character must keep its facial
    # identity, but a prop/setting only needs the same art style & framing -- otherwise
    # a transformative request ("make it like a space rover") gets suppressed.
    keep = ("the SAME character and facial identity, art style, framing and plain "
            "neutral background") if is_char else \
           "the SAME art style, framing and plain neutral background"
    with costs.run_as(f"book:{book_id}"), tempfile.TemporaryDirectory() as td:
        ref = Path(td) / "current.webp"
        ref.write_bytes(current)
        prompt = (f"{style_text}\n\nImage 1 is the CURRENT reference sheet for this subject. "
                  f"Redraw it keeping {keep}. Apply this requested change fully and clearly, "
                  f"even if it is substantial: {instruction}"
                  + (f"\n\nFor reference, the subject is: {appearance}" if appearance else "")
                  + "\nKeep EXACTLY ONE subject, drawn once, even lighting, no text labels.")
        try:
            data = _gen_to_bytes(prompt, [ref], model, aspect)
        except Exception as ex:  # noqa: BLE001
            print(f"[scene] sheet edit {entity_id}/{variant_id} failed: {ex}", flush=True)
            return {"ok": False, "error": str(ex)}
    data = _compress(data, 0, WEBP_QUALITY)   # sheets feed generation -> keep resolution
    db.save_sheet(book_id, entity_id, variant_id, data)
    _save_sheet_history(book_id, entity_id, variant_id, f"EDIT ({model}): {instruction}",
                        {"attempts": [{"attempt": 1, "prompt": prompt, "data": data,
                                       "critique": None, "min": None, "avg": None}], "chosen": 1})
    return {"ok": True, "model": model}


def generate_scene(book_id: int, idx: int, fast_critique: bool = False) -> bytes:
    """Render page `idx`'s illustration, store it, and return the image bytes.
    Synchronous/blocking (the server runs it in a worker thread). All API usage
    is tagged to this book for the per-book cost view. `fast_critique` makes each
    critique a single no-backoff attempt (used by the bake's straggler escalation,
    where the critic is likely blocking and we have a fallback -- don't burn minutes
    of retry backoff per page)."""
    with costs.run_as(f"book:{book_id}"):
        return _render_scene(book_id, idx, fast_critique=fast_critique)


def _character_states(brief: str, source: str, members: list) -> dict:
    """A cheap text pass: each present character's PHYSICAL state/action at THIS one
    moment (bound, crying, holding X, kneeling). Lets the image model attach a state
    to the right person instead of applying one state to everyone -- which is how a
    "Caspian freed but the others still bound" moment gets each person right. Only
    runs when >=2 characters are present (the only case the distinction matters)."""
    names = [m["name"] for m in members if m.get("type") == "character" and m.get("name")]
    if len(names) < 2:
        return {}
    prompt = (
        "For ONE children's-book illustration, give each named character's PHYSICAL state or "
        "action at THIS single moment -- only what is visibly depictable (bound/roped, hands tied, "
        "kneeling, crying, holding or carrying X, wounded, pointing). If a character has nothing "
        "notable, use an empty string. Do not invent anything not supported by the text.\n\n"
        f"THE MOMENT (illustration brief):\n{brief}\n\n"
        f"SOURCE PASSAGE:\n{(source or '')[:1000]}\n\n"
        f"CHARACTERS PRESENT: {', '.join(names)}\n\n"
        'Return JSON only: {"states": {"<name>": "<short phrase or empty>"}}'
    )
    try:
        out = gem.text_json(prompt, model=ANALYZE_MODEL)
        st = out.get("states", {}) if isinstance(out, dict) else {}
        return {k: v.strip() for k, v in st.items() if isinstance(v, str) and v.strip()}
    except Exception as ex:  # noqa: BLE001 -- never fail a scene on the state pass
        print(f"[scene] character-state pass failed: {ex}", flush=True)
        return {}


def _chapter_ahead(book_id: int, page: dict) -> str:
    """The text of the rest of THIS chapter after the current page, plus where in
    the chapter we are -- given to the critic so it can flag an illustration that
    spoils a not-yet-reached reveal. Capped for the token budget."""
    chap = [p for p in db.get_pages(book_id) if p["chapter_idx"] == page["chapter_idx"]]
    chap.sort(key=lambda p: p["idx"])
    pos = next((k for k, p in enumerate(chap) if p["idx"] == page["idx"]), 0)
    ahead = "\n\n".join(p["read_text"] for p in chap
                        if p["idx"] > page["idx"] and p.get("read_text"))
    if not ahead.strip():
        return "(This is the final moment of the chapter -- there is nothing later to spoil.)"
    return f"(This is moment {pos + 1} of {len(chap)} in the chapter.)\n\n{ahead}"[:3200]


# ---------------- shared scene logic (lazy + batch) ----------------
# The per-page critique/decision machinery is factored out of _render_scene so the
# batch "illustrate the whole book" bake (webapp/batch_bake.py) drives the SAME
# accept/revise/regenerate + carry-forward bookkeeping across async rounds. Nothing
# here touches the DB or the network except sheet drawing in build_scene_context.

# Critic sub-scores that gate the image. The three lenient ones default to 5 when
# the critic omits them (absence != failure); the rest default to 0 (must be earned).
CRIT_KEYS = ("physical", "consistency", "accuracy", "style_ok",
             "no_stray_text", "figure_match", "no_spoiler")
CRIT_LENIENT = ("no_stray_text", "figure_match", "no_spoiler")


def score_critique(crit: dict) -> tuple[int, float, list]:
    """(min_score, weighted_avg, per-key scores) for a critique. min gates the
    image (a bad anatomy OR spoiler vetoes it); the weighted avg is the best-of
    tie-break (physical + no_spoiler count double, per SCORE_WEIGHTS)."""
    scores = [crit.get(k, 5 if k in CRIT_LENIENT else 0) for k in CRIT_KEYS]
    weights = [SCORE_WEIGHTS.get(k, 1) for k in CRIT_KEYS]
    avg = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    return min(scores), round(avg, 2), scores


def new_scene_state() -> dict:
    """Fresh per-page state for the attempt/round loop. `draft`/`best` hold image
    BYTES (not paths) so the state survives across async batch rounds/processes."""
    return {"mode": "fresh", "draft": None, "best": None, "best_key": None,
            "fix": "", "pending_defect": "", "escalate": False,
            "edit_instr": "", "ref_chars": [], "cands": []}


def apply_verdict(state: dict, crit: dict, data: bytes, attempt: int,
                  fix_ok: bool, revise_enabled: bool = SCENE_REVISE) -> dict:
    """Fold one critique (+ carry-forward fix-verify) into `state`, mutating it in
    place, and return {min, avg, scores, verdict, done}. `data` is the candidate's
    webp bytes. Sets the plan for the NEXT round (draft/escalate/edit_instr/
    ref_chars/pending_defect/fix) exactly as the interactive loop did."""
    mn, avg, scores = score_critique(crit)
    verdict = (crit.get("verdict") or "revise").strip()
    if not revise_enabled and verdict == "revise":
        verdict = "regenerate"     # global toggle: never img2img, always redraw
    edit_next = (crit.get("edit_instruction") or "").strip()
    refs_next = [r for r in crit.get("reference_characters", [])
                 if isinstance(r, str) and r.strip()]
    # A candidate is "done" only when accepted, its targeted defect (if any) verified
    # gone, and hard sub-scores clear the bar. Rank best by (actionable?, min score).
    actionable = verdict != "accept" or not fix_ok
    key = (0 if actionable else 1, mn)
    if state["best"] is None or key > state["best_key"]:
        state["best"], state["best_key"] = (data, mn, attempt), key
    state["cands"].append({"n": attempt, "data": data, "score": mn})
    done = verdict == "accept" and fix_ok and mn >= PASS_THRESHOLD
    if done:
        return {"min": mn, "avg": avg, "scores": scores, "verdict": verdict, "done": True}
    state["fix"] = crit.get("fix_hint", "")
    state["pending_defect"] = "; ".join(crit.get("issues", []) or []) or state["fix"]
    if state["mode"] == "revise" and not fix_ok:
        # targeted edit didn't land: escalate once (stronger model + forceful note),
        # keeping this otherwise-good draft; if already escalated, redraw from scratch.
        if state["escalate"]:
            state["draft"], state["escalate"] = None, False
        else:
            state["draft"], state["escalate"] = data, True
            state["edit_instr"] = edit_next or state["edit_instr"]
            state["ref_chars"] = refs_next or state["ref_chars"]
    elif verdict == "revise" and edit_next:
        state["draft"], state["escalate"] = data, False
        state["edit_instr"], state["ref_chars"] = edit_next, refs_next
    else:   # regenerate (or accept-but-below-bar / missing instruction)
        state["draft"], state["escalate"] = None, False
    state["mode"] = "revise" if state["draft"] is not None else "fresh"
    return {"min": mn, "avg": avg, "scores": scores, "verdict": verdict, "done": False}


def _resolved_members(book_id, page, registry, chapter_cast):
    """The ordered, importance-ranked cast for a page, including registry variants a
    page references that the chapter cast omits (e.g. a flashback age). Extracted
    verbatim from the old _render_scene setup so lazy + batch resolve cast identically."""
    # Repair invented per-variant ids (e.g. angela_wexler_injured -> angela_wexler)
    # at render time, so already-segmented books resolve the right variant + sheet
    # without re-running analyze. The remap also fixes the page cast below.
    remap = analyze.reconcile_cast_ids(chapter_cast, registry)
    cast_index = resolve_cast({"cast": chapter_cast}, registry)
    page_cast = json.loads(page["cast_json"]) if page.get("cast_json") else []
    for cc in page_cast:
        if cc.get("entity_id") in remap:
            cc["entity_id"] = remap[cc["entity_id"]]
    spread = {"illustration_brief": page["brief"], "setting": page["setting"],
              "cast": page_cast, "read_text": page["read_text"]}
    members = scene_members(spread, cast_index)
    have = {(m["entity_id"], m["variant_id"]) for m in members}
    reg_by_id = {e["id"]: e for e in registry.get("entities", [])}
    for cc in page_cast:
        eid, vid = cc.get("entity_id"), cc.get("variant_id") or "default"
        if not eid or (eid, vid) in have:
            continue
        e = reg_by_id.get(eid)
        if not e:
            print(f"[scene] book {book_id} page {page['idx']}: cast id '{eid}' not in "
                  "registry -- character omitted from the illustration", flush=True)
            continue
        var = next((v for v in e.get("variants", []) if v.get("id") == vid), None)
        if var is None and vid != "default":
            continue   # not a real variant of this entity
        appearance = (var or {}).get("appearance") or e.get("base_appearance", "")
        sheet_prompt = (var or {}).get("sheet_prompt") or e.get("base_sheet_prompt", "")
        if not (appearance or sheet_prompt):
            continue
        name = e.get("name", eid)
        if var and var.get("label"):
            name = f"{name} ({var['label']})"
        members.append({"entity_id": eid, "variant_id": vid, "name": name,
                        "appearance": appearance, "sheet_prompt": sheet_prompt,
                        "type": e.get("type", "character"), "importance": e.get("importance", 3)})
        have.add((eid, vid))
    members.sort(key=lambda m: -m.get("importance", 3))
    return spread, members, page_cast


def _members_with_views(book_id, page, registry, chapter_cast):
    """Resolve a page's ordered, importance-ranked cast and tag each member with its
    `view` -- a named spot inside/on a setting the story references (e.g. 'deck',
    'lobby', or the legacy surface/interior aspects), else None. Shared by
    build_scene_context (which then draws the sheets) and plan_page_sheets (which
    only enumerates them for the batch roster), so the two never disagree about
    which sheets a page needs."""
    spread, members, page_cast = _resolved_members(book_id, page, registry, chapter_cast)
    views_by_id = {}
    for c in page_cast:
        v = (c.get("view") or "").strip()
        if not v and c.get("aspect") in ("surface", "interior"):
            v = "deck" if c.get("aspect") == "surface" else "interior"
        if v:
            views_by_id[c.get("entity_id")] = v
    for m in members:
        m["view"] = views_by_id.get(m["entity_id"]) if m.get("type") == "setting" else None
    return spread, members, page_cast


def plan_page_sheets(book_id: int, idx: int) -> list[dict]:
    """The roster sheets page `idx` will reference: the same ordered, MAX_REFS-capped
    member set build_scene_context would draw, each annotated with `view`, but WITHOUT
    drawing anything. The batch roster unions this across all pages to know the full
    set of sheets to generate up front. Returns [] for a missing/bad page."""
    page = db.get_page(book_id, idx)
    if not page:
        return []
    registry = db.get_registry(book_id)
    chapter_cast = db.get_chapter_cast(book_id, page["chapter_idx"])
    _, members, _ = _members_with_views(book_id, page, registry, chapter_cast)
    plan = []
    for m in sorted(members, key=lambda m: 0 if m.get("view") else 1):
        if len(plan) >= MAX_REFS:
            break
        plan.append(m)
    return plan


def build_scene_context(book_id: int, idx: int) -> dict:
    """Everything needed to render page `idx`, computed once and shared by the lazy
    path and the batch bake: resolved cast, per-character states, the reference
    sheets (as BYTES, drawn/cached on demand), and the derived prompt text. This is
    the setup half of the old _render_scene, lifted out so both paths are identical."""
    book = db.get_book(book_id)
    page = db.get_page(book_id, idx)
    if not page:
        raise ValueError(f"no page {idx} for book {book_id}")
    registry = db.get_registry(book_id)
    chapter_cast = db.get_chapter_cast(book_id, page["chapter_idx"])
    style_text = _style_text(book["style"])
    spread, members, page_cast = _members_with_views(book_id, page, registry, chapter_cast)

    def _view(m):
        return m.get("view")

    states = _character_states(page["brief"], page["read_text"] or "", members)
    for m in members:
        m["state"] = states.get(m.get("name", ""), "")

    # reference sheets (bytes), location prioritised into the capped ref set
    with tempfile.TemporaryDirectory() as td:
        style_ref = _style_anchor_path(book, td)
        style_ref_bytes = style_ref.read_bytes() if style_ref and style_ref.exists() else None
        ref_members, ref_bytes = [], []
        for m in sorted(members, key=lambda m: 0 if _view(m) else 1):
            if len(ref_members) >= MAX_REFS:
                break
            view = _view(m)
            data = (_ensure_view_sheet(book_id, m, style_text, view, style_ref=style_ref)
                    if view else _ensure_sheet(book_id, m, style_text, style_ref=style_ref))
            if data:
                ref_members.append(m)
                ref_bytes.append(data)

    # rewrite view-settings' descriptions to focus on the named spot (not exterior)
    view_members = [m for m in members if _view(m)]
    for m in view_members:
        nm = m.get("name", m["entity_id"])
        m["appearance"] = (f"the {_view(m)} of {nm} -- show ONLY what is visible at that spot; do "
                           f"NOT depict the whole exterior of {nm} or its exterior-only features, "
                           "even through a window")
    char_desc = "\n".join(
        f"- {m['name']}" + (f" (RIGHT NOW: {m['state']})" if m.get("state") else "")
        + f": {m['appearance']}" for m in members)
    place_note = ""
    if view_members:
        spots = "; ".join(f"the {_view(m)} of {m.get('name', m['entity_id'])}" for m in view_members)
        place_note = (f"\n\nThe scene takes place at {spots}: show only that location. Do NOT depict "
                      "the whole exterior of those place(s) or their exterior-only identifying "
                      "features anywhere -- not even through a window or doorway, and never a "
                      "mirrored or doubled copy.")
    return {
        "book_id": book_id, "idx": idx, "book": book, "page": page, "registry": registry,
        "reg_by_name": {(e.get("name") or e.get("id")): e for e in registry.get("entities", [])},
        "spread": spread, "members": members, "states": states,
        "ref_members": ref_members, "ref_bytes": ref_bytes,
        "ref_labels": [m["name"] for m in ref_members],
        "char_desc": char_desc, "place_note": place_note,
        "chapter_ahead": _chapter_ahead(book_id, page), "roster": roster_digest(registry),
        "style_text": style_text, "style_ref_bytes": style_ref_bytes,
    }


ROSTER_BATCH = os.environ.get("STORY_ROSTER_BATCH", "1") != "0"


def draw_all_sheets(book_id, workers: int = 6, log=print) -> int:
    """Draw + cache every roster sheet the book's pages will reference (the union
    across all pages), so the full roster can be reviewed before a batch bake.

    Two-stage: first (when STORY_ROSTER_BATCH is on) the bulk of the real
    character/prop/setting sheets are drawn via the Batch API (~50% cheaper) in
    webapp/batch_roster; then an interactive pass over every page draws whatever is
    still missing -- the few 'view' sheets and any sheet the batch couldn't produce
    (build_scene_context skips sheets already cached, so nothing is redrawn).
    Returns the page count processed by the interactive pass."""
    from concurrent.futures import ThreadPoolExecutor
    if ROSTER_BATCH:
        try:
            from . import batch_roster
            n = batch_roster.draw_roster(book_id, log=log)
            log(f"[sheets] batch roster drew {n} sheets; interactive pass for the rest")
        except Exception as ex:  # noqa: BLE001 -- fall back to the all-interactive path
            log(f"[sheets] batch roster failed ({type(ex).__name__}: {ex}); drawing interactively")
    idxs = [p["idx"] for p in db.get_pages(book_id)]

    def one(idx):
        try:
            build_scene_context(book_id, idx)   # draws any missing sheets as a side effect
        except Exception as ex:  # noqa: BLE001 -- a bad page shouldn't block the roster
            log(f"[sheets] page {idx} failed: {ex}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, idxs))
    return len(idxs)


def resolve_name_sheet(ctx: dict, name: str, cache: dict):
    """(entity_id, sheet_bytes) for a character the critic named for a revise, drawing
    the default sheet on demand if needed. Lets a wrong BACKGROUND figure (not in the
    page's ref set) still be corrected. Mirrors the old _sheet_for_name, but bytes."""
    if name in cache:
        return cache[name]
    res = None
    for mm, data in zip(ctx["ref_members"], ctx["ref_bytes"]):
        if mm["name"] == name:
            res = (mm["entity_id"], data)
            break
    if res is None:
        e = ctx["reg_by_name"].get(name)
        if e:
            eid = e["id"]
            data = db.get_any_sheet(ctx["book_id"], eid)
            if not data:
                vid = (e.get("variants") or [{}])[0].get("id", "default")
                member = _member_for(ctx["registry"], eid, vid)
                if member:
                    with tempfile.TemporaryDirectory() as td:
                        sref = None
                        if ctx.get("style_ref_bytes"):
                            sref = Path(td) / "styleref.webp"
                            sref.write_bytes(ctx["style_ref_bytes"])
                        data = _ensure_sheet(ctx["book_id"], member, ctx["style_text"], style_ref=sref)
            if data:
                res = (eid, data)
    cache[name] = res
    return res


def build_round_request(ctx: dict, state: dict, name_cache: dict) -> dict:
    """The image-generation request for this round of one page. Returns
    {prompt, ref_bytes, model, mode}: `ref_bytes` are the ordered reference images
    (draft first for a revise), `model` the image model to use. Shared verbatim by
    the lazy loop and the batch bake so both build identical prompts."""
    style_text, char_desc, place_note = ctx["style_text"], ctx["char_desc"], ctx["place_note"]
    if state.get("draft") is not None:
        # REVISE (img2img): critic-authored edit_instruction; attach the sheets the
        # critic asked for + this page's cast, budgeted to MAX_REFS incl. the draft.
        sheet_refs, seen_ent = [], set()

        def _add(ent, path_bytes):
            if ent not in seen_ent and len(sheet_refs) < MAX_REFS - 1:
                seen_ent.add(ent)
                sheet_refs.append((ent, path_bytes))

        labels = []
        for nm in state.get("ref_chars", []):
            got = resolve_name_sheet(ctx, nm, name_cache)
            if got and got[0] not in seen_ent and len(sheet_refs) < MAX_REFS - 1:
                _add(got[0], got[1])
                labels.append(nm)
        for m, data in zip(ctx["ref_members"], ctx["ref_bytes"]):
            if m["entity_id"] not in seen_ent and len(sheet_refs) < MAX_REFS - 1:
                _add(m["entity_id"], data)
                labels.append(m["name"])
        ref_lbls = ", ".join(f"image {i + 2} = {nm}" for i, nm in enumerate(labels))
        esc_note = ("\n\nA PREVIOUS edit did NOT change the picture. You MUST actually redraw the "
                    "affected area this time -- do not return a near-identical image."
                    if state.get("escalate") else "")
        prompt = (f"{style_text}\n\nImage 1 is a DRAFT illustration to edit in place.\n\n"
                  f"{state.get('edit_instr','')}"
                  + (f"\n\nReference sheets are attached ({ref_lbls}); make each named "
                     "character match its sheet exactly (face, build, species, clothing)."
                     if ref_lbls else "")
                  + esc_note
                  + f"\n\nThe people must still match:\n{char_desc}{place_note}")
        ref_bytes = [state["draft"]] + [b for _, b in sheet_refs]
        model = SHEET_IMAGE_MODEL if state.get("escalate") else PAGE_IMAGE_MODEL
        return {"prompt": prompt, "ref_bytes": ref_bytes, "model": model, "mode": "revise"}
    prompt = build_scene_prompt(ctx["spread"], ctx["members"], ctx["ref_members"],
                                style_text, fix=state.get("fix", "")) + place_note
    return {"prompt": prompt, "ref_bytes": list(ctx["ref_bytes"]),
            "model": PAGE_IMAGE_MODEL, "mode": "fresh"}


def critique_prompt(ctx: dict) -> str:
    """The scene-critique prompt for a page (same inputs the lazy path used)."""
    page = ctx["page"]
    return SCENE_CRITIQUE.format(
        brief=page["brief"], chars=ctx["char_desc"] or "(none)", style=ctx["style_text"],
        roster=ctx["roster"], chapter_ahead=ctx["chapter_ahead"],
        source=(page["read_text"] or "")[:1200] or "(not available)")


def critique_prompt_lite(ctx: dict) -> str:
    """A critique prompt with the two embedded verbatim story passages (source +
    chapter-ahead) removed. Those passages, alongside the child imagery in the
    illustration + reference sheets, are what trip Gemini's PROHIBITED_CONTENT
    child-safety filter (empirically: removing either passage clears the block).
    Used only as critique_image's last fallback tier -- it loses spoiler-detection
    and source-detail grounding, but reliably gets a score for a page the full
    prompt won't grade, instead of leaving it unscored."""
    page = ctx["page"]
    return SCENE_CRITIQUE.format(
        brief=page["brief"], chars=ctx["char_desc"] or "(none)", style=ctx["style_text"],
        roster=ctx["roster"], chapter_ahead="(omitted)", source="(omitted)")


def _judge_best(cands: list, brief: str, td: Path) -> dict | None:
    """Vision critic picks the best of several candidates (bytes) when none cleared
    the bar. Returns {"best": idx0based, "why": str} or None. Shared with the bake."""
    if len(cands) <= 1:
        return None
    paths = []
    for c in cands:
        cp = Path(td) / f"judge{c['n']}.webp"
        cp.write_bytes(c["data"])
        paths.append(cp)
    try:
        verdict = gem.judge_images(paths, JUDGE_BEST.format(brief=brief),
                                   schema={"type": "object", "properties": {
                                       "best": {"type": "integer"}, "why": {"type": "string"}},
                                       "required": ["best"]})
        pick = int(verdict.get("best", 0)) - 1
        if 0 <= pick < len(cands):
            return {"best": pick, "why": verdict.get("why", "")}
    except Exception as ex:  # noqa: BLE001 -- fall back to the best-min-score
        print(f"[scene] best-of judge failed: {ex}", flush=True)
    return None


def _attempt_trace(attempt: int, mode: str, res: dict, crit: dict, fix_ok: bool) -> dict:
    """One attempt's row for the debug trace (shared by lazy + batch)."""
    return {
        "n": attempt, "mode": mode, "scores": dict(zip(CRIT_KEYS, res["scores"])),
        "min": res["min"], "avg": res["avg"], "fix_ok": fix_ok, "verdict": res["verdict"],
        "edit_instruction": (crit.get("edit_instruction") or "").strip(),
        "reference_characters": [r for r in crit.get("reference_characters", [])
                                 if isinstance(r, str) and r.strip()],
        "issues": crit.get("issues", []), "fix_hint": crit.get("fix_hint", ""),
    }


def _render_scene(book_id: int, idx: int, fast_critique: bool = False) -> bytes:
    """Render page `idx`'s illustration synchronously (the lazy read path), on top
    of the shared build_scene_context / build_round_request / apply_verdict helpers
    so it stays in lock-step with the batch bake."""
    crit_tries = 1 if fast_critique else 4
    ctx = build_scene_context(book_id, idx)
    page = ctx["page"]
    state = new_scene_state()
    name_cache: dict = {}
    gen_id = db.next_gen_id(book_id, idx)   # debug history: this generation run
    trace = {"states": ctx["states"], "max_tries": SCENE_TRIES, "attempts": []}

    with tempfile.TemporaryDirectory() as td:
        # critique always sees this page's cast sheets (to catch a wrong figure)
        crit_ref_paths = []
        for m, blob in zip(ctx["ref_members"], ctx["ref_bytes"]):
            p = Path(td) / f"ref_{m['entity_id']}__{m['variant_id']}.webp"
            p.write_bytes(blob)
            crit_ref_paths.append(p)

        last_cand = None   # newest drawn image, kept as an unscored fallback
        for attempt in range(1, SCENE_TRIES + 1):
            req = build_round_request(ctx, state, name_cache)
            mode = req["mode"]
            gen_ref_paths = []
            for i, b in enumerate(req["ref_bytes"]):
                gp = Path(td) / f"gen{attempt}_{i}.webp"
                gp.write_bytes(b)
                gen_ref_paths.append(gp)
            cand = Path(td) / f"cand{attempt}.webp"
            gem.generate_image(req["prompt"], refs=gen_ref_paths, out_path=cand,
                               aspect="3:2", model=req["model"])
            data = cand.read_bytes()
            last_cand = data
            try:
                crit = gem.critique_image(cand, critique_prompt(ctx), refs=crit_ref_paths,
                                          ref_labels=ctx["ref_labels"], schema=SCENE_CRITIQUE_SCHEMA,
                                          tries=crit_tries, lite_brief=critique_prompt_lite(ctx))
            except Exception as ex:  # noqa: BLE001 -- a blocked/empty critique must not sink
                # the page: keep this candidate as an unscored fallback and REGENERATE a
                # fresh image next attempt (a different image often isn't blocked).
                print(f"[scene] critique failed for page {idx} attempt {attempt}: {ex}", flush=True)
                state["mode"], state["draft"] = "fresh", None
                continue
            # carry-forward re-check: a revise targeting a specific defect must actually
            # clear it before we trust the (fresh, stochastic) verdict
            fix_ok = True
            pending_defect = state["pending_defect"]
            if mode == "revise" and pending_defect:
                v = _verify_fix(cand, pending_defect)
                fix_ok = bool(v.get("resolved", True))
                crit["fix_verified"] = {"defect": pending_defect, "resolved": fix_ok,
                                        "still_present": v.get("still_present", "")}
                if not fix_ok:
                    print(f"[scene] revise did not clear defect ({pending_defect!r}); "
                          f"still: {v.get('still_present','')!r}", flush=True)
            res = apply_verdict(state, crit, data, attempt, fix_ok)
            trace["attempts"].append(_attempt_trace(attempt, mode, res, crit, fix_ok))
            db.scene_attempt_add(book_id, idx, gen_id, attempt, mode, req["prompt"],
                                 _compress(data, DEBUG_MAXW, DEBUG_QUALITY),
                                 json.dumps(crit), res["min"], res["avg"])
            if res["done"]:
                break

        if state["best"] is not None:
            data, score, chosen = state["best"]
            # If nothing cleared the bar, let a vision critic pick the best candidate.
            if score < PASS_THRESHOLD:
                pick = _judge_best(state["cands"], page["brief"], Path(td))
                if pick:
                    c = state["cands"][pick["best"]]
                    data, score, chosen = c["data"], c["score"], c["n"]
                    trace["judge_pick"] = {"attempt": chosen, "why": pick["why"]}
        else:
            # every attempt's critique was blocked -> store the last drawn image unscored
            # (an illustration beats a blank; the reader can redraw it if it looks off).
            data, score, chosen = last_cand, None, SCENE_TRIES
            trace["fallback"] = "kept last candidate (critique blocked every attempt)"
        data = _compress(data, SCENE_MAXW, WEBP_QUALITY)   # downscale for display storage

    trace["chosen"] = chosen
    db.scene_store(book_id, idx, data, score, trace=json.dumps(trace))
    db.scene_gen_add(book_id, idx, gen_id, page["brief"], json.dumps(ctx["states"]), chosen, score)
    return data
