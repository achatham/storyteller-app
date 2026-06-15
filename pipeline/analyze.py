"""Turn the chapter text into a 'story bible': global style, character roster
with neutral physical descriptions (for character sheets), and a segmentation
into read-aloud spreads each with an illustration brief.
"""
import json
import re

from . import gem
from .config import (OUT, LABEL, ART_STYLE, VIOLENCE_POLICY, REGISTRY,
                     BOOK_REF, TITLE_OUT, AUDIENCE_AGE, WORDS_PER_PAGE,
                     ANALYZE_MODEL)

ANALYZE_PROMPT = """You are the art director and adapter for an illustrated read-aloud edition of \
{book_ref} (section: {label}), for a {age}-year-old audience.

You are given a passage of the book's raw text. NOTE: it may be missing some spaces \
(e.g. "hiseyes" = "his eyes"). You do NOT need to fix that: you only mark where each \
read-aloud page BEGINS, and the page's actual text is taken verbatim from the book by \
code. When you give a page's start_anchor, copy those words EXACTLY as they appear in \
the passage below (same spelling/spacing), so the code can find that spot.

GLOBAL ART STYLE (apply to every illustration):
{art_style}

CONTENT POLICY:
{violence_policy}

You also have the book's ENTITY REGISTRY: every recurring character/setting/prop in the \
whole novel, each with a stable id and one or more variant ids (an entity's look at a \
particular point -- a given age, outfit, injury, or state). Whenever a character/place/thing \
in THIS passage matches a registry entity, you MUST reuse its id and pick the single best-\
fitting variant id, so the look stays consistent with the rest of the book.

ENTITY REGISTRY:
{registry}

Produce a JSON object (and nothing else) with this exact shape:

{{
  "title": "{title}",
  "chapter": "{label}",
  "art_style": "<one concise paragraph restating the global style as an image-prompt prefix>",
  "palette": "<short description of the recurring color palette>",
  "cast": [
    {{
      "entity_id": "<registry id if this matches a registry entity, else a NEW snake_case id>",
      "variant_id": "<the chosen registry variant id, or 'default' for a new local character>",
      "name": "<display name as used in this section>",
      "from_registry": <true|false>,
      "appearance": "<ONLY for from_registry=false: a detailed, concrete, neutral physical description (age, build, hair, eyes, skin, clothing). Omit/empty for registry entities -- their look comes from the registry.>",
      "sheet_prompt": "<ONLY for from_registry=false: a full neutral reference-sheet image prompt (full body, front view, relaxed pose, plain background, no text). Omit/empty for registry entities.>"
    }}
  ],
  "spreads": [
    {{
      "id": 1,
      "title": "<2-5 word scene title>",
      "start_anchor": "<the FIRST 8-12 words at the start of this page, copied EXACTLY (verbatim, same spelling and spacing) from the passage below. This only marks where the page begins; its full text is sliced from the book automatically. The FIRST page's anchor MUST be the very first words of the passage. Anchors must be in reading order and each must be unique enough to locate.>",
      "cast": [
        {{"entity_id": "<id from this section's cast>", "variant_id": "<its variant id>"}}
      ],
      "setting": "<where/when, concrete>",
      "illustration_brief": "<a vivid, specific description of the single illustration: composition, who is doing what, expressions, camera angle, mood, key background details. Refer to characters by name. Apply the content policy. Do NOT include the global art style here.>",
      "image_anchor": "<a short verbatim phrase (5-10 words) copied EXACTLY from this page's text, marking WHERE to place the illustration in the flowing text: right AFTER the moment the picture depicts, so the picture never appears before the words reveal that moment (avoid spoilers). Usually at the end of the sentence describing the depicted action. Must be a phrase that actually occurs in this page's text.>",
      "content_note": "<exactly one of: safe OR softened>"
    }}
  ]
}}

Guidance:
- Build this section's "cast": every illustration-relevant character (and, if useful, key setting/prop) appearing in THIS passage.
  * If it matches a registry entity -> from_registry=true, reuse its id, choose the best variant_id, and leave appearance/sheet_prompt empty.
  * If it is a real character in this passage that is NOT in the registry (a locally-important minor character), from_registry=false, give it a new id, variant_id "default", and fill in appearance + sheet_prompt yourself.
- Each spread's "cast" lists the ids+variants actually visible in that illustration (a subset of the section cast).
- An entity's VARIANT can change PARTWAY through this passage -- a ship gets damaged, a character changes clothes, ages, or is injured. Choose each spread's variant by what the text shows AT THAT page, in reading order: keep the EARLIER variant for pages before the change, and switch to the new variant only from the page where the change actually happens onward. The registry's chapter spans ("when") are a rough hint only -- this page's own text wins, even if the span suggests the change already happened.
- CADENCE: split the passage into about {target_pages} pages of ~{words_per_page} words each (one illustration per page), in reading order, covering the ENTIRE passage start to finish with no gaps/overlaps. Give ONLY each page's start_anchor (not the page text). Choose page breaks at natural beats so each page is one strong, distinct, illustratable moment, but keep every page close to the target length.
- For passages with no clearly visible characters (interludes, disembodied dialogue between unseen adults), use an evocative NON-LITERAL illustration (atmospheric setting, meaningful object) and an empty or setting-only cast.
- This passage may start mid-story: just illustrate what happens here.

BOOK TEXT (this section):
\"\"\"
{chapter}
\"\"\"
"""


def render_registry(registry: dict) -> str:
    """Compact roster string for the analyze prompt (ids + variant labels only)."""
    lines = []
    for e in registry.get("entities", []):
        vs = "; ".join(f"{v['id']} = {v.get('label','')} (approx when: {v.get('when','')})"
                       for v in e.get("variants", [])) or "(no variants)"
        aka = (" aka " + "/".join(e["aliases"])) if e.get("aliases") else ""
        lines.append(f"- [{e['type']}] {e['id']}: {e['name']}{aka}\n    variants: {vs}")
    return "\n".join(lines)


def _find_anchor(text: str, anchor: str, start: int) -> int | None:
    """Locate where `anchor` (a short verbatim snippet the model copied from the
    text) begins, at/after `start`. Matches on word tokens with flexible
    separators so odd whitespace/punctuation/case don't defeat it."""
    toks = re.findall(r"\w+", anchor)[:8]
    if not toks:
        return None
    pat = re.compile(r"\W+".join(re.escape(t) for t in toks), re.I)
    m = pat.search(text, start)
    return m.start() if m else None


def apply_anchors(chapter_text: str, bible: dict) -> dict:
    """Turn each spread's start_anchor into verbatim read_text by slicing the
    real chapter text between consecutive anchors. The whole chapter is always
    covered start-to-finish; a page whose anchor can't be found is merged into
    the previous page (so no text is ever dropped)."""
    spreads = bible.get("spreads") or []
    if not spreads:   # model gave no pages -> keep the chapter as a single page
        bible["spreads"] = [{"id": 1, "title": bible.get("chapter", ""),
                             "read_text": chapter_text.strip(), "cast": [],
                             "setting": "", "illustration_brief": "",
                             "content_note": "safe"}]
        return bible
    located: list[tuple[int, dict]] = []
    cursor = 0
    for s in spreads:
        if not located:                       # first page = start of the chapter
            located.append((0, s))
            continue
        off = _find_anchor(chapter_text, s.get("start_anchor", ""), cursor)
        if off is not None and off > cursor:
            # the anchor matches the first WORD; pull the break back over an
            # opening quote/bracket glued to it so dialogue keeps its quote mark
            while off > cursor + 1 and chapter_text[off - 1] in "“‘\"'(«¿¡":
                off -= 1
            located.append((off, s))
            cursor = off
        # else: anchor not found / out of order -> drop this break, merge forward
    for i, (off, s) in enumerate(located):
        end = located[i + 1][0] if i + 1 < len(located) else len(chapter_text)
        s["read_text"] = chapter_text[off:end].strip()
        s.pop("start_anchor", None)
    bible["spreads"] = [s for _, s in located]
    for i, s in enumerate(bible["spreads"], 1):
        s["id"] = i
    return bible


def build_bible(chapter_text: str, registry: dict, model: str = ANALYZE_MODEL) -> dict:
    words = len(chapter_text.split())
    target_pages = max(1, round(words / WORDS_PER_PAGE))
    prompt = ANALYZE_PROMPT.format(
        book_ref=BOOK_REF,
        title=TITLE_OUT,
        age=AUDIENCE_AGE,
        label=LABEL,
        art_style=ART_STYLE,
        violence_policy=VIOLENCE_POLICY,
        words_per_page=WORDS_PER_PAGE,
        target_pages=target_pages,
        registry=render_registry(registry),
        chapter=chapter_text,
    )
    bible = gem.text_json(prompt, model=model)
    return apply_anchors(chapter_text, bible)


def main():
    src = OUT / "chapter.txt"
    if not src.exists():  # back-compat with the original ch1 filename
        src = OUT / "chapter1.txt"
    chapter = src.read_text()
    registry = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"entities": []}
    bible = build_bible(chapter, registry)
    (OUT / "bible.json").write_text(json.dumps(bible, indent=2, ensure_ascii=False))
    print(f"cast: {len(bible.get('cast', []))}")
    for c in bible.get("cast", []):
        src_tag = "registry" if c.get("from_registry") else "LOCAL"
        print(f"  - {c['entity_id']}/{c.get('variant_id')}  ({src_tag})  {c.get('name')}")
    print(f"spreads: {len(bible.get('spreads', []))}")
    for s in bible.get("spreads", []):
        cast = ",".join(f"{m['entity_id']}/{m.get('variant_id')}" for m in s.get("cast", []))
        print(f"  {s['id']:>2}. {s['title']}  [{cast}]  ({s.get('content_note')})")


if __name__ == "__main__":
    main()
