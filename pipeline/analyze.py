"""Turn the chapter text into a 'story bible': global style, character roster
with neutral physical descriptions (for character sheets), and a segmentation
into read-aloud spreads each with an illustration brief.
"""
import json

from . import gem
from .config import (OUT, LABEL, ART_STYLE, VIOLENCE_POLICY, REGISTRY,
                     BOOK_REF, TITLE_OUT, AUDIENCE_AGE, WORDS_PER_PAGE)

ANALYZE_PROMPT = """You are the art director and adapter for an illustrated read-aloud edition of \
{book_ref} (section: {label}), for a {age}-year-old audience.

You are given a passage of the book's raw text. NOTE: it was extracted from a PDF and is \
MISSING MANY SPACES (e.g. "hiseyes" = "his eyes", "theone" = "the one"). When you \
quote text, RESTORE correct spacing and punctuation, but do not otherwise change \
the author's wording.

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
      "read_text": "<the COMPLETE, VERBATIM run of original text this page covers, spacing/punctuation restored. About {words_per_page} words. Do NOT summarize, paraphrase, or omit -- this is read aloud as the real book. Consecutive pages must join back into the full passage with no gaps and no overlaps.>",
      "cast": [
        {{"entity_id": "<id from this section's cast>", "variant_id": "<its variant id>"}}
      ],
      "setting": "<where/when, concrete>",
      "illustration_brief": "<a vivid, specific description of the single illustration: composition, who is doing what, expressions, camera angle, mood, key background details. Refer to characters by name. Apply the content policy. Do NOT include the global art style here.>",
      "content_note": "<exactly one of: safe OR softened>"
    }}
  ]
}}

Guidance:
- Build this section's "cast": every illustration-relevant character (and, if useful, key setting/prop) appearing in THIS passage.
  * If it matches a registry entity -> from_registry=true, reuse its id, choose the best variant_id, and leave appearance/sheet_prompt empty.
  * If it is a real character in this passage that is NOT in the registry (a locally-important minor character), from_registry=false, give it a new id, variant_id "default", and fill in appearance + sheet_prompt yourself.
- Each spread's "cast" lists the ids+variants actually visible in that illustration (a subset of the section cast).
- CADENCE: split the passage into about {target_pages} pages of ~{words_per_page} words each (one illustration per page), in reading order, tiling the ENTIRE passage start to finish with no gaps/overlaps. Choose page breaks at natural beats so each page is one strong, distinct, illustratable moment, but keep every page close to the target length.
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
        vs = "; ".join(f"{v['id']} ({v.get('label','')}, {v.get('when','')})"
                       for v in e.get("variants", [])) or "(no variants)"
        aka = (" aka " + "/".join(e["aliases"])) if e.get("aliases") else ""
        lines.append(f"- [{e['type']}] {e['id']}: {e['name']}{aka}\n    variants: {vs}")
    return "\n".join(lines)


def build_bible(chapter_text: str, registry: dict) -> dict:
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
    return gem.text_json(prompt)


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
