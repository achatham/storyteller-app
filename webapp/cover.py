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

* The art director picks each character's LOOK, not just who appears. It is shown
  every variant we have a sheet for with its chapter range, its share of that
  character's page time and how far into the book it starts (`_variant_menu`), and
  returns a look id per character. The registry gives a one-scene infant variant
  the same standing as the look 228 pages use, and the art direction and the
  attached sheet must agree -- when they don't, the sheet wins and you get a baby
  on the cover of a boarding-school book. `_dominant_variants` is the fallback when
  that call fails or names a look that doesn't exist.

* Every cover is critiqued and rerolled (`COVER_TRIES`), like the page scenes and
  the roster sheets. Nothing else catches a bad cover: it is generated once and
  shown for the life of the book, so the only alternative is a human noticing.

No text is baked into the image: the library card and the EPUB title page already
render the title/author as real text, and a misspelled title rendered into the art
is not fixable without a redraw.

CLI:  python -m webapp.cover            # backfill every eligible book without one
      python -m webapp.cover <id> [...] # (re)draw specific books
"""
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

from pipeline import costs, gem
from pipeline.config import COVER_IMAGE_MODEL, TEXT_MODEL

from . import db, scene

# A cover is one image per book, so generate it large and keep it larger than a
# page scene: it is also the EPUB cover, which readers show full-screen.
COVER_SIZE = os.environ.get("STORY_COVER_SIZE", "2K")
COVER_MAXW = int(os.environ.get("STORY_COVER_MAXW", "1024"))
COVER_QUALITY = int(os.environ.get("STORY_COVER_QUALITY", "82"))
# How many characters may appear on the cover. Above ~3 the faces get small and
# identity drifts from the sheets; a cover is a hero shot, not a group photo.
COVER_CAST = int(os.environ.get("STORY_COVER_CAST", "3"))
# Draw/critique attempts. Every other image in this pipeline is critiqued and
# rerolled (SCENE_TRIES, SHEET_TRIES); the cover is drawn once per book and is the
# most-looked-at image in the app, so it gets the same treatment. A cover that
# passes first time costs one extra text pass, not an extra image.
COVER_TRIES = int(os.environ.get("STORY_COVER_TRIES", "3"))
COVER_PASS = int(os.environ.get("STORY_COVER_PASS", "4"))

# Statuses where the roster is settled enough to draw a cover from. Anything else
# (queued/extracting/registry/segmenting/warming/baking) is either still deciding
# what the cast IS or is drawing those sheets in a batch right now.
COVER_OK_STATUS = ("ready", "roster_review")

COVER_BRIEF = """You are art-directing the front cover of an illustrated read-aloud \
edition of this book, for a child of about {age}.

BOOK: {title}{by}

The characters in this edition's reference roster. You may ONLY use these -- they are \
the only ones drawn consistently. Each character lists the LOOKS we have a canonical \
reference sheet for: its id, when in the book it applies, and how much of that \
character's page time it accounts for:
{roster}

Some early illustration briefs from the book, for tone and setting:
{briefs}

Write the cover illustration.

Rules:
- Pick at most {n_cast} characters, using their EXACT names from the roster above. \
Prefer the protagonist(s). Fewer is better; one hero is a fine cover.
- For each character you pick, also choose the LOOK to draw them in, by its exact \
`look` id. Choose the look a reader would recognise them by -- normally the one they \
wear for most of the book. Do NOT pick a look that would spoil the story (an \
end-state, a transformation, a costume from the climax, an epilogue-only look), and \
do NOT pick a one-scene cameo (an infant/flashback look for a character who is a \
child or adult for the rest of the book) just because it comes first.
- Choose an iconic, inviting moment. NO late-book spoilers: nothing that gives away \
a twist, a death, or the ending.
- Age-appropriate and warm. No gore, no peril, no distress.
- Describe the composition for a PORTRAIT (tall) cover: what the characters are \
doing, where they are, the light and mood, and what fills the top and bottom of \
the frame. Leave the composition uncluttered near the top.
- Do NOT ask for any text, title, lettering, logo or signature in the picture.
- In `scene`, write plain prose: use each character's plain name, with no bracketed \
tags, ids or annotations.

Return JSON: {{"characters": [{{"name": "exact roster name", "look": "exact look id"}}], \
"scene": "the illustration description, 2-4 sentences"}}"""

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "look": {"type": "string"}},
                "required": ["name", "look"],
            },
        },
        "scene": {"type": "string"},
    },
    "required": ["characters", "scene"],
}

COVER_CRITIQUE = """You are a strict art director reviewing the finished FRONT COVER of \
an illustrated children's read-aloud edition (audience: about {age}).

BOOK: {title}

THE COVER SHOULD DEPICT:
{scene}

THE CHARACTERS ON IT, and the look each one was supposed to be drawn in:
{cast}

THE INTENDED ART STYLE IS:
{style}

The canonical reference sheet for each character is attached after the cover, labelled \
with that character's name and intended look. Judge the figures against them.

Judge the attached cover. Return JSON only:
{{
  "physical": <1-5: anatomy and physical correctness. Correct number of fingers, hands, \
limbs and eyes on every figure, no fused/extra/missing/malformed parts, no melted or \
distorted faces, nothing floating or badly out of scale. 1-2 for any clear defect, 5 \
only when everything is well-formed.>,
  "figure_match": <1-5: is each figure the RIGHT individual, matching its reference \
sheet -- face, hair, build, species, clothing?>,
  "age_ok": <1-5: is each character drawn at the AGE their reference sheet shows? An \
infant or toddler where the sheet shows a school-age child, or an adult where the sheet \
shows a child, scores 1.>,
  "no_text": <1-5: 5 only if there is NO text anywhere -- no title, lettering, words, \
numbers, logo, signature or watermark, and no book mockup/spine/frame/border.>,
  "no_spoiler": <1-5: does it avoid giving away an ending, death, twist or \
transformation? A character shown in a clearly end-of-story state scores low.>,
  "style_ok": <1-5, does it match the intended art style above?>,
  "appeal": <1-5: does this work as a cover a child would want to pick up -- a clear \
subject, readable at thumbnail size, uncluttered near the top?>,
  "issues": [<short strings naming what is wrong, empty if nothing>],
  "fix_hint": "<one sentence telling the illustrator what to change on the next \
attempt, empty if it passes>"
}}"""

COVER_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "physical": {"type": "integer"}, "figure_match": {"type": "integer"},
        "age_ok": {"type": "integer"}, "no_text": {"type": "integer"},
        "no_spoiler": {"type": "integer"}, "style_ok": {"type": "integer"},
        "appeal": {"type": "integer"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "fix_hint": {"type": "string"},
    },
    "required": ["physical", "figure_match", "age_ok", "no_text", "no_spoiler",
                 "style_ok", "appeal", "issues"],
}
COVER_SCORES = ("physical", "figure_match", "age_ok", "no_text", "no_spoiler",
                "style_ok", "appeal")

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
    out = ["- " + " ".join(b.split())[:220] for b in db.page_briefs(book_id, n)]
    return "\n".join(out) or "(none)"


# ---- which LOOK of each character the cover draws -----------------------------
# The art director chooses this (it sees the menu _variant_menu builds, with page
# shares and chapter ranges, and can weigh "recognisable" against "spoiler" on a
# book nobody has tuned for). Everything below is the FALLBACK for when that call
# fails or returns a look that isn't real: counted rules with constants fitted to
# the eight books that were in the library when this was written, so treat them as
# a floor, not as the primary mechanism.
#
# A look first cast this far into the book is an END STATE, not something to put on
# a cover.
LATE_VARIANT_FRAC = float(os.environ.get("STORY_COVER_LATE_FRAC", "0.6"))
# A look worn for at least this share of a character's pages is their look, full
# stop -- take it even if something briefer came first.
MAJORITY_SHARE = float(os.environ.get("STORY_COVER_MAJORITY", "0.5"))
# Below the majority, a look still has to be more than a cameo to be the cover look.
SUBSTANTIAL_SHARE = float(os.environ.get("STORY_COVER_SUBSTANTIAL", "0.2"))


def _signature_variant(mine: dict, cutoff) -> str | None:
    """Which of a character's looks belongs on the cover, given
    {variant_id: {"pages", "first"}}: the one they wear for MOST of the book, else
    the FIRST one they wear substantially.

    Both halves are needed, and each fixes the other's failure:

      * majority alone -> Jim Hawkins marooned-and-wounded (48%, p191 of 327) and
        Johannes the liberated coyote (42%) -- plurality end states, and spoilers.
      * earliest-substantial alone -> Harry in Dudley's hand-me-downs (31%, p24)
        over the school robes he wears for 66% of the book.

    Cameos are what make "earliest" unusable on its own: infant Harry is 3 pages of
    347, and picking him is how a boarding-school book got a baby on its cover."""
    early = {vid: r for vid, r in mine.items() if cutoff is None or r["first"] <= cutoff}
    pool = early or mine          # a character who only ever appears late: use it all
    if not pool:
        return None
    total = sum(r["pages"] for r in mine.values()) or 1
    ranked = sorted(pool.items(), key=lambda kv: -kv[1]["pages"])
    top_vid, top = ranked[0]
    if top["pages"] / total >= MAJORITY_SHARE:
        return top_vid
    substantial = [(vid, r) for vid, r in pool.items()
                   if r["pages"] / total >= SUBSTANTIAL_SHARE]
    if substantial:
        return min(substantial, key=lambda kv: kv[1]["first"])[0]
    return top_vid


def _dominant_variants(book_id, registry) -> dict:
    """{entity_id: the variant dict the cover should use}.

    This has to be counted, not guessed. Taking whichever sheet happened to be drawn
    first put INFANT Harry on the cover of a book that is 228 pages of school-robes
    Harry, and adult-epilogue Turtle on The Westing Game. Both the art direction and
    the attached reference sheet read this, so the brief and the sheet can never
    describe two different people."""
    usage = db.variant_usage(book_id)
    n_pages = (db.get_book(book_id) or {}).get("num_pages") or 0
    cutoff = n_pages * LATE_VARIANT_FRAC if n_pages else None
    out = {}
    for e in _characters(registry):
        eid = e["id"]
        variants = [v for v in e.get("variants", [])
                    if not str(v.get("id", "")).startswith("__")]
        mine = {vid: rec for (e_id, vid), rec in usage.items() if e_id == eid}
        best = _signature_variant(mine, cutoff)
        var = next((v for v in variants if v.get("id") == best), None)
        if var is None:
            # No page-cast evidence (or the winner isn't a registry variant): fall
            # back to the entity's default, then to whatever it does define.
            var = next((v for v in variants if v.get("id") == "default"), None) \
                or (variants[0] if variants else {"id": best or "default"})
        out[eid] = var
    return out


def _strip_label(text: str) -> str:
    """Drop the '[Look]' tags the planner's roster uses, and tidy the spacing."""
    return " ".join(re.sub(r"\s*\[[^\]]*\]", "", text or "").split()).strip()


def _variant_label(entity: dict, variant: dict) -> str:
    lbl = (variant.get("label") or "").strip()
    return lbl if lbl and variant.get("id") != "default" else ""


def _variant_menu(registry, usage: dict, n_pages: int, max_each: int = 130) -> str:
    """The roster the art director chooses from: every character, and under each the
    LOOKS we can actually draw, annotated with the evidence needed to tell a
    signature look from a cameo or an end state -- when it applies, what share of the
    character's page time it is, and how far into the book it starts.

    Giving the model the evidence is the point. A counted rule with hand-set
    thresholds can only encode the cases its author happened to look at; the model
    can read "Infant in Blankets, Chapter 1, 1% of pages" against "Hogwarts School
    Robes, Chapters 6-17, 66%" and reach the same conclusion on a book nobody has
    seen."""
    lines = []
    for e in _characters(registry):
        eid = e["id"]
        lines.append(f"- {_entity_name(e)}")
        mine = {vid: r for (e_id, vid), r in usage.items() if e_id == eid}
        total = sum(r["pages"] for r in mine.values()) or 1
        variants = [v for v in e.get("variants", [])
                    if not str(v.get("id", "")).startswith("__")]
        if not variants:
            variants = [{"id": "default"}]
        for v in sorted(variants, key=lambda v: mine.get(v.get("id"), {}).get("first", 10**9)):
            vid = v.get("id", "default")
            rec = mine.get(vid)
            app = " ".join((v.get("appearance") or e.get("base_appearance") or "").split())
            if len(app) > max_each:
                app = app[:max_each].rsplit(" ", 1)[0] + "…"
            bits = [f"look={vid}"]
            if v.get("label"):
                bits.append(f'"{v["label"]}"')
            if v.get("when"):
                bits.append(str(v["when"]))
            if rec:
                pct = round(100 * rec["pages"] / total)
                at = f", starts {round(100 * rec['first'] / n_pages)}% in" if n_pages else ""
                bits.append(f"{pct}% of their pages{at}")
            else:
                bits.append("never cast in this edition")
            lines.append(f"    · {'; '.join(bits)}" + (f" -- {app}" if app else ""))
    return "\n".join(lines) or "(none listed)"


def _plan(book_id, book, registry, usage) -> dict:
    """Ask the (cheap) text model which roster characters belong on the cover, WHICH
    LOOK to draw each in, and what they are doing. Falls back to the most important
    character on any failure -- a cover is worth drawing even if art direction
    fails."""
    chars = _characters(registry)
    prompt = COVER_BRIEF.format(
        age=book.get("age") or "5", title=book.get("title") or "this novel",
        by=f"\nAUTHOR: {book['author']}" if book.get("author") else "",
        roster=_variant_menu(registry, usage, book.get("num_pages") or 0),
        briefs=_brief_samples(book_id), n_cast=COVER_CAST)
    try:
        plan = gem.text_json(prompt, schema=COVER_SCHEMA, model=TEXT_MODEL)
    except Exception as ex:  # noqa: BLE001
        print(f"[cover] book {book_id}: art direction failed ({ex}) -- using a default",
              flush=True)
        plan = {}
    picks = []
    for c in (plan.get("characters") or []):
        if isinstance(c, dict) and c.get("name"):
            picks.append({"name": _strip_label(c["name"]), "look": (c.get("look") or "").strip()})
        elif isinstance(c, str):          # tolerate the older bare-name shape
            picks.append({"name": _strip_label(c), "look": ""})
    if not picks and chars:
        picks = [{"name": _entity_name(chars[0]), "look": ""}]
    # Bracketed asides in an image prompt invite the model to render them as text.
    scene_text = _strip_label(plan.get("scene") or "")
    if not scene_text:
        lead = _entity_name(chars[0]) if chars else "the main character"
        scene_text = (f"{lead} at the very start of the story, in the book's main "
                      "setting, warm inviting light, looking out toward an adventure "
                      "about to begin.")
    return {"characters": picks, "scene": scene_text, "prompt": prompt}


def _resolve_cast(picks: list[dict], registry, fallback: dict, log=print) -> list[tuple]:
    """[(entity, variant)] for the art director's picks: its chosen look when that is
    a real variant of that character, else the counted fallback. It is told to use
    exact roster names/ids, but a stray 'Mr.' or a hallucinated look id shouldn't
    lose the character."""
    chars = _characters(registry)
    by_name = {_entity_name(e).lower(): e for e in chars}
    out, seen = [], set()
    for p in picks:
        key = p["name"].strip().lower()
        e = by_name.get(key)
        if not e:   # loose match: first roster name contained in / containing the ask
            e = next((c for c in chars
                      if key and (key in _entity_name(c).lower()
                                  or _entity_name(c).lower() in key)), None)
        if not e or e["id"] in seen:
            continue
        seen.add(e["id"])
        variants = [v for v in e.get("variants", [])
                    if not str(v.get("id", "")).startswith("__")]
        var = next((v for v in variants if v.get("id") == p["look"]), None)
        if var is None:
            var = fallback.get(e["id"]) or {"id": "default"}
            if p["look"]:
                log(f"[cover] look {p['look']!r} is not a variant of {e['id']} -- "
                    f"using {var.get('id')}")
        out.append((e, var))
        if len(out) >= COVER_CAST:
            break
    if not out and chars:
        out = [(chars[0], fallback.get(chars[0]["id"]) or {"id": "default"})]
    return out


def _member(entity: dict, variant: dict) -> dict:
    return {
        "entity_id": entity["id"], "variant_id": variant.get("id", "default"),
        "name": _entity_name(entity), "type": entity.get("type", "character"),
        "appearance": variant.get("appearance") or entity.get("base_appearance", ""),
        "sheet_prompt": variant.get("sheet_prompt") or entity.get("base_sheet_prompt", ""),
    }


def _cast_sheets(book_id, cast, style_text, style_ref) -> list[dict]:
    """One entry per cover character: {name, label, sheet, entity_id, variant_id}.

    `cast` is the resolved [(entity, variant)] the art direction was written
    against, so the brief and the attached sheet always describe the same person.
    Draws + caches any sheet that doesn't exist yet, so the cover is made from real
    roster art and the interior pages reuse whatever gets drawn here."""
    out = []
    for e, var in cast:
        m = _member(e, var)
        data = db.get_sheet(book_id, m["entity_id"], m["variant_id"])
        if not data:
            print(f"[cover] book {book_id}: drawing missing sheet "
                  f"{m['entity_id']}/{m['variant_id']} for the cover", flush=True)
            data = scene._ensure_sheet(book_id, m, style_text, style_ref=style_ref)
        if data:
            out.append({"name": m["name"], "label": _variant_label(e, var), "sheet": data,
                        "entity_id": m["entity_id"], "variant_id": m["variant_id"]})
    return out


def _draw(book_id, prompt, refs, out: Path, attempt: int, log) -> tuple | None:
    """Draw one cover candidate. Returns (prompt actually used, path) or None if the
    attempt produced no image. A content-policy refusal gets the same one-shot
    prompt rewrite the page and sheet paths use."""
    try:
        gem.generate_image(prompt, refs=refs or None, out_path=out,
                           aspect="2:3", size=COVER_SIZE, model=COVER_IMAGE_MODEL)
        return prompt, out
    except gem.ImageRefused as ex:
        if not gem.is_policy_refusal(str(ex)):
            log(f"[cover] book {book_id}: attempt {attempt} produced no image ({ex})")
            return None
        log(f"[cover] book {book_id}: attempt {attempt} refused ({ex}) -- rewriting prompt")
        try:
            safe = gem.rewrite_prompt_safely(prompt, str(ex))
            gem.generate_image(safe, refs=refs or None, out_path=out,
                               aspect="2:3", size=COVER_SIZE, model=COVER_IMAGE_MODEL)
            return safe, out
        except Exception as ex2:  # noqa: BLE001
            log(f"[cover] book {book_id}: still refused after rewrite ({ex2})")
            return None
    except Exception as ex:  # noqa: BLE001
        log(f"[cover] book {book_id}: attempt {attempt} failed "
            f"({type(ex).__name__}: {ex})")
        return None


def _score_cover(path: Path, brief: str, sheet_refs, ref_labels, log) -> tuple:
    """(score, critique) for one candidate -- the weakest sub-score, as everywhere
    else in this pipeline, so one bad axis can't be averaged away.

    Returns (None, None) when the critic won't grade the image at all. Gemini's
    child-safety filter blocks some critiques of pictures of children outright, and
    that block is persistent per image; treating it as a failure would burn every
    reroll on an image that may be perfectly good. Unscored-but-drawn beats no
    cover, which is the same call the page bake makes."""
    try:
        crit = gem.critique_image(path, brief, refs=sheet_refs or None,
                                  ref_labels=ref_labels, schema=COVER_CRITIQUE_SCHEMA)
    except Exception as ex:  # noqa: BLE001
        log(f"[cover] critique unavailable ({str(ex)[:120]}) -- keeping the image unscored")
        return None, None
    return min(int(crit.get(k, 0)) for k in COVER_SCORES), crit


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
        usage = db.variant_usage(book_id)
        plan = _plan(book_id, book, registry, usage)
        picked = _resolve_cast(plan["characters"], registry,
                               _dominant_variants(book_id, registry), log=log)
        with tempfile.TemporaryDirectory() as td:
            style_ref = scene._style_anchor_path(book, td)
            cast = _cast_sheets(book_id, picked, style_text, style_ref)
            refs, notes = [], []
            if style_ref and style_ref.exists():
                refs.append(style_ref)
                notes.append(scene._STYLE_REF_NOTE.format(n=len(refs)))
            for m in cast:
                p = Path(td) / f"ref{len(refs)}.webp"
                p.write_bytes(m["sheet"])
                refs.append(p)
                # Name the variant in the ref note as well: without it the model has
                # to reconcile "young Harry on the platform" against a sheet it is
                # told to copy EXACTLY, and the sheet wins.
                who_ref = m["name"] + (f" as they appear here -- {m['label']}"
                                       if m["label"] else "")
                notes.append(f"Image {len(refs)} is the canonical reference sheet for "
                             f"{who_ref}: draw this character with EXACTLY that face, "
                             "hair, build, age, species and clothing.")
            who = ", ".join(m["name"] + (f" ({m['label']})" if m["label"] else "")
                            for m in cast)
            base_prompt = "\n\n".join(filter(None, [
                style_text,
                plan["scene"],
                f"The characters shown are: {who}." if who else "",
                COVER_FRAME,
                " ".join(notes),
            ]))
            crit_brief = COVER_CRITIQUE.format(
                age=book.get("age") or "5", title=book.get("title") or "this book",
                scene=plan["scene"], style=style_text,
                cast="\n".join(f"- {m['name']}" + (f" -- {m['label']}" if m["label"] else "")
                               for m in cast) or "(none)")
            ref_labels = [m["name"] + (f" ({m['label']})" if m["label"] else "")
                          for m in cast]

            best = None
            for attempt in range(1, COVER_TRIES + 1):
                prompt = base_prompt
                if best and best.get("fix"):
                    prompt += ("\n\nThe previous attempt was rejected: "
                               f"{best['fix']} Fix that in this new drawing.")
                # PNG intermediate: generate_image would otherwise write WebP at the
                # page-art quality (72) and _compress would then re-encode that lossy
                # copy. The cover is downscaled from 2K and shown large, so keep the
                # one round-trip lossless.
                out = Path(td) / f"cover{attempt}.png"
                got = _draw(book_id, prompt, refs, out, attempt, log)
                if not got:
                    continue
                prompt, path = got
                score, crit = _score_cover(path, crit_brief, refs[1:], ref_labels, log)
                rec = {"data": path.read_bytes(), "prompt": prompt, "score": score,
                       "crit": crit, "fix": (crit or {}).get("fix_hint", "")}
                if best is None or (score or 0) > (best["score"] or 0):
                    best = rec
                log(f"[cover] book {book_id}: attempt {attempt} score={score} "
                    f"issues={(crit or {}).get('issues')}")
                if score is None or score >= COVER_PASS:
                    break     # passed, or the critic won't grade it (an image beats none)
            if best is None:
                return None
            data = scene._compress(best["data"], COVER_MAXW, COVER_QUALITY)
        db.save_cover(book_id, data, prompt=best["prompt"],
                      cast=[{k: m[k] for k in ("name", "label", "entity_id", "variant_id")}
                            for m in cast])
        log(f"[cover] book {book_id}: drawn ({len(data) // 1024}KB, "
            f"{COVER_IMAGE_MODEL}, score={best['score']}, cast={who or 'none'})")
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
