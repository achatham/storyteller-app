"""Draw ONE cover illustration per book -- the image the library grid and the
downloadable EPUB lead with.

Two things make a cover different from a page scene, and both are deliberate:

* It is drawn with the PRO image model (`config.COVER_IMAGE_MODEL`). Every
  interior page uses flash because there are hundreds of them; there is exactly
  one cover per book and it is the most-looked-at image in the app, so ~$0.15 of
  pro is worth it here where it is not worth it per page.

* It REQUIRES the roster first. A cover whose hero doesn't look like the hero on
  page 7 is worse than no cover, so `ensure_cover` resolves the cover's cast to
  real roster sheets and draws any that are missing (via `scene._ensure_sheet`,
  which caches them for the interior art to reuse) BEFORE generating. It refuses
  to run at all while a book is still importing or mid-bake, because the batch
  roster is drawing those same sheets right then and racing it would both
  duplicate spend and risk two different faces for one character.

No text is baked into the image: the library card and the EPUB title page already
render the title/author as real text, and a misspelled title rendered into the art
is not fixable without a redraw.

CLI:  python -m webapp.cover            # backfill every eligible book without one
      python -m webapp.cover <id> [...] # (re)draw specific books
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

from pipeline import costs, gem
from pipeline.config import COVER_IMAGE_MODEL, TEXT_MODEL
from pipeline.run import roster_digest

from . import db, scene

# A cover is one image per book, so generate it large and keep it larger than a
# page scene: it is also the EPUB cover, which readers show full-screen.
COVER_SIZE = os.environ.get("STORY_COVER_SIZE", "2K")
COVER_MAXW = int(os.environ.get("STORY_COVER_MAXW", "1024"))
COVER_QUALITY = int(os.environ.get("STORY_COVER_QUALITY", "82"))
# How many characters may appear on the cover. Above ~3 the faces get small and
# identity drifts from the sheets; a cover is a hero shot, not a group photo.
COVER_CAST = int(os.environ.get("STORY_COVER_CAST", "3"))

# Statuses where the roster is settled enough to draw a cover from. Anything else
# (queued/extracting/registry/segmenting/warming/baking) is either still deciding
# what the cast IS or is drawing those sheets in a batch right now.
COVER_OK_STATUS = ("ready", "roster_review")

COVER_BRIEF = """You are art-directing the front cover of an illustrated read-aloud \
edition of this book, for a child of about {age}.

BOOK: {title}{by}

The characters and places that exist in this edition's reference roster (you may \
ONLY use these -- they are the only ones drawn consistently):
{roster}

Some early illustration briefs from the book, for tone and setting:
{briefs}

Write the cover illustration.

Rules:
- Pick at most {n_cast} characters, using their EXACT names from the roster above. \
Prefer the protagonist(s). Fewer is better; one hero is a fine cover.
- Choose an iconic, inviting opening-of-the-story moment. NO late-book spoilers: \
nothing that gives away a twist, a death, or the ending.
- Age-appropriate and warm. No gore, no peril, no distress.
- Describe the composition for a PORTRAIT (tall) cover: what the characters are \
doing, where they are, the light and mood, and what fills the top and bottom of \
the frame. Leave the composition uncluttered near the top.
- Do NOT ask for any text, title, lettering, logo or signature in the picture.

Return JSON: {{"characters": [names from the roster], "scene": "the illustration \
description, 2-4 sentences"}}"""

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {"type": "array", "items": {"type": "string"}},
        "scene": {"type": "string"},
    },
    "required": ["characters", "scene"],
}

# Appended to the brief when the image is actually drawn.
COVER_FRAME = (
    "This is the FRONT COVER of a children's illustrated book: one single seamless "
    "portrait illustration that fills the whole frame. Absolutely NO text, title, "
    "lettering, words, numbers, logo, signature or watermark anywhere in the image. "
    "It is not a photograph of a book: no book, no open pages, no spine, no centre "
    "fold or crease, no border, frame, mockup or panel divisions.")


def _entity_name(e: dict) -> str:
    return e.get("name") or e.get("id") or ""


def _characters(registry: dict) -> list[dict]:
    ents = [e for e in registry.get("entities", [])
            if e.get("type", "character") == "character"]
    ents.sort(key=lambda e: -e.get("importance", 0))
    return ents


def _brief_samples(book_id, n: int = 6) -> str:
    """A few early illustration briefs, so the cover matches the book's actual
    opening tone rather than a generic reading of the title."""
    out = []
    for p in db.get_pages(book_id):
        b = (p.get("brief") or "").strip()
        if b:
            out.append("- " + " ".join(b.split())[:220])
        if len(out) >= n:
            break
    return "\n".join(out) or "(none)"


def _plan(book_id, book, registry) -> dict:
    """Ask the (cheap) text model which roster characters belong on the cover and
    what they are doing. Falls back to the most important character on any
    failure -- a cover is worth drawing even if the art direction step fails."""
    chars = _characters(registry)
    prompt = COVER_BRIEF.format(
        age=book.get("age") or "5", title=book.get("title") or "this novel",
        by=f"\nAUTHOR: {book['author']}" if book.get("author") else "",
        roster=roster_digest(registry), briefs=_brief_samples(book_id),
        n_cast=COVER_CAST)
    try:
        plan = gem.text_json(prompt, schema=COVER_SCHEMA, model=TEXT_MODEL)
    except Exception as ex:  # noqa: BLE001
        print(f"[cover] book {book_id}: art direction failed ({ex}) -- using a default",
              flush=True)
        plan = {}
    names = [n for n in (plan.get("characters") or []) if isinstance(n, str)]
    if not names and chars:
        names = [_entity_name(chars[0])]
    scene_text = (plan.get("scene") or "").strip()
    if not scene_text:
        lead = _entity_name(chars[0]) if chars else "the main character"
        scene_text = (f"{lead} at the very start of the story, in the book's main "
                      "setting, warm inviting light, looking out toward an adventure "
                      "about to begin.")
    return {"characters": names, "scene": scene_text, "prompt": prompt}


def _resolve_names(names: list[str], registry: dict) -> list[dict]:
    """Map the planner's names back onto registry entities (it is told to use exact
    roster names, but a stray 'Mr.' or a possessive shouldn't lose the character)."""
    chars = _characters(registry)
    by_name = {_entity_name(e).lower(): e for e in chars}
    picked, seen = [], set()
    for nm in names:
        key = nm.strip().lower()
        e = by_name.get(key)
        if not e:   # loose match: first roster name contained in / containing the ask
            e = next((c for c in chars
                      if key and (key in _entity_name(c).lower()
                                  or _entity_name(c).lower() in key)), None)
        if e and e["id"] not in seen:
            seen.add(e["id"])
            picked.append(e)
        if len(picked) >= COVER_CAST:
            break
    if not picked and chars:
        picked = chars[:1]
    return picked


def _anchor_variant(entity: dict, drawn: list[str]) -> dict | None:
    """Which variant of this character the cover should reference: one whose sheet
    is ALREADY drawn (no new spend, and it is the look the interior pages use),
    else the entity's default/first variant."""
    variants = [v for v in entity.get("variants", []) if not str(v.get("id", "")).startswith("__")]
    for vid in drawn:
        v = next((v for v in variants if v.get("id") == vid), None)
        if v:
            return v
        if vid == "default":
            return {"id": "default"}
    return next((v for v in variants if v.get("id") == "default"), None) \
        or (variants[0] if variants else {"id": "default"})


def _member(entity: dict, variant: dict) -> dict:
    return {
        "entity_id": entity["id"], "variant_id": variant.get("id", "default"),
        "name": _entity_name(entity), "type": entity.get("type", "character"),
        "appearance": variant.get("appearance") or entity.get("base_appearance", ""),
        "sheet_prompt": variant.get("sheet_prompt") or entity.get("base_sheet_prompt", ""),
    }


def _cast_sheets(book_id, entities, style_text, style_ref) -> list[tuple]:
    """(name, sheet bytes, entity_id, variant_id) for each cover character -- drawing
    and caching any sheet that doesn't exist yet, so the cover is always made from
    real roster art (and the interior pages then reuse whatever was drawn here)."""
    drawn = {}
    for eid, vid in db.list_sheets(book_id):
        if not str(vid).startswith("__"):
            drawn.setdefault(eid, []).append(vid)
    out = []
    for e in entities:
        var = _anchor_variant(e, drawn.get(e["id"], []))
        m = _member(e, var)
        data = db.get_sheet(book_id, m["entity_id"], m["variant_id"])
        if not data:
            print(f"[cover] book {book_id}: drawing missing sheet "
                  f"{m['entity_id']}/{m['variant_id']} for the cover", flush=True)
            data = scene._ensure_sheet(book_id, m, style_text, style_ref=style_ref)
        if data:
            out.append((m["name"], data, m["entity_id"], m["variant_id"]))
    return out


def ensure_cover(book_id: int, force: bool = False, log=print) -> bytes | None:
    """The book's cover image, drawn once and cached. Returns None (without raising)
    when the book isn't eligible yet or generation fails -- callers treat a cover as
    a nice-to-have and must never fail an import over one."""
    if not force:
        data = db.get_cover(book_id)
        if data:
            return data
    book = db.get_book(book_id)
    if not book:
        return None
    status = book.get("status")
    if status not in COVER_OK_STATUS:
        log(f"[cover] book {book_id}: status {status!r} -- roster not settled, skipping")
        return None
    registry = db.get_registry(book_id)
    if not registry.get("entities"):
        log(f"[cover] book {book_id}: no registry yet, skipping")
        return None

    # One label for the whole job (art direction, any sheet it has to draw, the
    # cover itself) so the book's cost report attributes all of it to the book.
    with scene._entity_lock(book_id, "__cover__"), costs.run_as(f"book:{book_id}"):
        if not force:
            data = db.get_cover(book_id)
            if data:
                return data
        style_text = scene._style_text(book["style"])
        plan = _plan(book_id, book, registry)
        entities = _resolve_names(plan["characters"], registry)
        with tempfile.TemporaryDirectory() as td:
            style_ref = scene._style_anchor_path(book, td)
            cast = _cast_sheets(book_id, entities, style_text, style_ref)
            refs, notes = [], []
            if style_ref and style_ref.exists():
                refs.append(style_ref)
                notes.append(scene._STYLE_REF_NOTE.format(n=len(refs)))
            for name, sheet, _eid, _vid in cast:
                p = Path(td) / f"ref{len(refs)}.webp"
                p.write_bytes(sheet)
                refs.append(p)
                notes.append(f"Image {len(refs)} is the canonical reference sheet for "
                             f"{name}: draw this character with EXACTLY that face, hair, "
                             "build, species and clothing.")
            who = ", ".join(n for n, _d, _e, _v in cast)
            prompt = "\n\n".join(filter(None, [
                style_text,
                plan["scene"],
                f"The characters shown are: {who}." if who else "",
                COVER_FRAME,
                " ".join(notes),
            ]))
            # PNG intermediate: generate_image would otherwise write WebP at the
            # page-art quality (72) and _compress would then re-encode that lossy
            # copy. The cover is downscaled from 2K and shown large, so it is worth
            # keeping the one round-trip lossless.
            out = Path(td) / "cover.png"
            try:
                gem.generate_image(prompt, refs=refs or None, out_path=out,
                                   aspect="2:3", size=COVER_SIZE,
                                   model=COVER_IMAGE_MODEL)
            except gem.ImageRefused as ex:
                if not gem.is_policy_refusal(str(ex)):
                    log(f"[cover] book {book_id}: generation failed ({ex})")
                    return None
                log(f"[cover] book {book_id}: refused ({ex}) -- rewriting prompt")
                try:
                    safe = gem.rewrite_prompt_safely(prompt, str(ex))
                    gem.generate_image(safe, refs=refs or None, out_path=out,
                                       aspect="2:3", size=COVER_SIZE,
                                       model=COVER_IMAGE_MODEL)
                    prompt = safe
                except Exception as ex2:  # noqa: BLE001
                    log(f"[cover] book {book_id}: still refused after rewrite ({ex2})")
                    return None
            except Exception as ex:  # noqa: BLE001
                log(f"[cover] book {book_id}: generation failed ({type(ex).__name__}: {ex})")
                return None
            data = scene._compress(out.read_bytes(), COVER_MAXW, COVER_QUALITY)
        db.save_cover(book_id, data, prompt=prompt,
                      cast=[{"name": n, "entity_id": e, "variant_id": v}
                            for n, _d, e, v in cast])
        log(f"[cover] book {book_id}: drawn ({len(data) // 1024}KB, "
            f"{COVER_IMAGE_MODEL}, cast={who or 'none'})")
        return data


def backfill(book_ids=None, force=False, log=print) -> dict:
    """Draw covers for every eligible book that hasn't got one (or the given ids)."""
    if book_ids:
        todo = list(book_ids)
    else:
        have = db.books_with_covers()
        todo = [b["id"] for b in db.list_books()
                if b["id"] not in have and b["status"] in COVER_OK_STATUS]
    log(f"[cover] backfill: {len(todo)} book(s) -> {todo}")
    made, skipped = [], []
    for bid in todo:
        try:
            if ensure_cover(bid, force=force, log=log):
                made.append(bid)
            else:
                skipped.append(bid)
        except Exception:  # noqa: BLE001 -- one bad book shouldn't stop the sweep
            traceback.print_exc()
            skipped.append(bid)
    log(f"[cover] backfill done: {len(made)} drawn, {len(skipped)} skipped")
    return {"drawn": made, "skipped": skipped}


def main():
    db.init()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv[1:]
    res = backfill([int(a) for a in args] or None, force=force)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
