"""Build a book-wide Entity Registry so illustrations stay consistent across the
whole book, not just within one section.

Two passes (keeps every model OUTPUT small -- the 64k output cap, not the 1M
input window, is the real limit):

  1. DISCOVER  -- one read over the whole book -> a thin index of every
                  illustration-worthy entity (characters, settings, props) with
                  stable ids, aliases, importance, a one-line summary, the
                  GROUNDING appearance facts stated in the text, and the list of
                  VARIANTS the entity needs (age / outfit / injury / state ...).
                  Output is bounded (~ids + short fields), so it never truncates.

  2. EXPAND    -- per entity, in parallel, turn the stub + grounding facts into a
                  rich canonical appearance + a neutral reference-sheet image
                  prompt, plus a resolved appearance + sheet prompt for EACH
                  variant. Each call's output is tiny, so it is reliable and fast.

The result, registry.json, is the backbone the per-section analyze + the shared
canonical sheets both build on.
"""
import json

from . import gem
from .extract import full_story_text
from .config import REGISTRY, BOOK_REF, BOOK_TITLE, REGISTRY_MODEL, REGISTRY_THINK

DISCOVER_PROMPT = """You are cataloguing every illustration-worthy entity in {book_ref}, \
to keep a fully-illustrated children's read-aloud edition visually consistent.

You are given the (PDF-extracted, some spaces missing) full text of the book.

Return JSON only: {{"entities": [ ... ]}}. Each entity:
{{
  "id": "<stable snake_case id, e.g. red_haired_boy, old_lighthouse, brass_lantern>",
  "type": "character | setting | prop",
  "name": "<canonical display name>",
  "aliases": ["<every other name/epithet the text uses for this same entity>"],
  "importance": <1-5, how often/centrally it appears and how much it matters visually>,
  "summary": "<one line: who/what this is and its role>",
  "canonical_details": "<appearance facts ACTUALLY stated or strongly implied in the text: age, size, hair, distinctive marks, typical clothing, look of a place/object. Quote/paraphrase the book; do not invent. If the text says little, say so.>",
  "variants": [
    {{
      "id": "<snake_case, unique within this entity, e.g. age_six, winter_coat, bandaged_arm>",
      "kind": "age | outfit | injury | state | other",
      "label": "<short human label>",
      "when": "<where in the book this variant applies, e.g. 'Ch 1-2' or 'the final voyage'>",
      "delta": "<what is DIFFERENT in this variant vs the entity's default look>"
    }}
  ]
}}

Rules:
- DEDUPLICATE aggressively: one entity per real person/place/thing. Fold every alias in (e.g. a full name, a nickname, and a descriptive epithet that all refer to the same person) -- do not emit duplicates.
- VARIANTS = the distinct LOOKS an entity has across the book. Reason through the story in order and segment each character's appearance into the separate looks they actually have: start a new variant wherever their clothing, gear/weapons, age, grooming, or physical condition changes enough that drawing them the SAME in both places would be wrong. Give each variant a "when" span (chapters/scenes) and make the spans cover the whole book in reading order WITHOUT overlapping or being misattributed -- a look that only applies later must not claim the opening, and vice-versa.
- The first look is simply the earliest segment; capture it like any other. Common triggers for a new variant: moving between different worlds or settings (e.g. everyday real-world clothes before entering a fantasy/secondary world, then that world's dress), donning a uniform/armor/disguise, aging, or a lasting injury.
- Do not collapse genuinely different looks into one variant, and do not split looks that are essentially the same. A character whose appearance never meaningfully changes can have a single variant; a constant setting/prop may have an empty variants list.
- Aim for completeness on importance>=3 entities; you may include minor ones at importance 1-2 but do not pad.
- Keep each text field short; richness is added in a later pass.

FULL BOOK TEXT:
\"\"\"
{book}
\"\"\"
"""

EXPAND_PROMPT = """You are the character/scene designer for a children's picture-book \
edition of {book_ref}. Design the CANONICAL LOOK of one entity so it can be drawn \
identically every time it appears.

The art style/medium is chosen separately and applied at render time, so describe only
the SUBJECT itself -- do NOT mention any art style, medium, or rendering technique.

ENTITY (from the book catalogue):
{entity}

Return JSON only:
{{
  "base_appearance": "<rich, CONCRETE, neutral canonical description: build, hair, eyes, skin, default clothing, distinctive features (for a setting/prop: materials, shapes, colors, mood). Stay consistent with canonical_details; invent reasonable specifics only where the text is silent.>",
  "base_sheet_prompt": "<a complete, style-AGNOSTIC image prompt for a NEUTRAL REFERENCE SHEET of this entity in its DEFAULT look: for a character, full body, front view, relaxed neutral pose, plain soft off-white background, even lighting, no props, no text; for a setting, a clean establishing view; for a prop, an isolated product-style view. Describe only the subject and framing -- no art-style words.>",
  "variants": [
    {{
      "id": "<echo the variant id you are resolving>",
      "appearance": "<the FULL resolved appearance for this variant (base look + the variant's delta applied)>",
      "sheet_prompt": "<a complete, style-AGNOSTIC reference-sheet image prompt for THIS variant, same neutral framing rules as base, no art-style words>"
    }}
  ]
}}
Resolve EVERY variant listed on the entity (echo each variant id). If the entity has no variants, return an empty variants list.
"""


def discover(book_text: str) -> list[dict]:
    prompt = DISCOVER_PROMPT.format(book_ref=BOOK_REF, book=book_text)
    data = gem.text_json(prompt, model=REGISTRY_MODEL, thinking_level=REGISTRY_THINK)
    return data.get("entities", [])


def expand_one(entity: dict) -> dict:
    stub = {k: entity.get(k) for k in
            ("id", "type", "name", "summary", "canonical_details", "variants")}
    prompt = EXPAND_PROMPT.format(
        book_ref=BOOK_REF, entity=json.dumps(stub, ensure_ascii=False))
    out = dict(entity)
    try:
        rich = gem.text_json(prompt, model=REGISTRY_MODEL, thinking_level=REGISTRY_THINK)
    except Exception as e:  # noqa: BLE001 -- never let one entity sink the whole summary
        print(f"[registry] expand FAILED for {entity.get('id')}: "
              f"{type(e).__name__}: {str(e)[:120]} -- using canonical_details fallback", flush=True)
        fallback = entity.get("canonical_details", "") or entity.get("summary", "")
        out["base_appearance"] = fallback
        out["base_sheet_prompt"] = (
            f"A neutral full reference view of {entity.get('name','the subject')}: "
            f"{fallback} Plain soft off-white background, even lighting, no text.")
        out["expand_failed"] = True
        for v in out.get("variants", []):
            v.setdefault("appearance", fallback)
            v.setdefault("sheet_prompt", out["base_sheet_prompt"])
        return out
    out["base_appearance"] = rich.get("base_appearance", "")
    out["base_sheet_prompt"] = rich.get("base_sheet_prompt", "")
    # merge resolved appearance/sheet_prompt back onto the matching variant
    resolved = {v.get("id"): v for v in rich.get("variants", [])}
    for v in out.get("variants", []):
        r = resolved.get(v["id"], {})
        v["appearance"] = r.get("appearance", "")
        v["sheet_prompt"] = r.get("sheet_prompt", "")
    return out


def build(max_workers: int = 6) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    print("[registry] reading full book ...", flush=True)
    book = full_story_text()
    print(f"[registry] discover pass over ~{len(book)//4} tokens ...", flush=True)
    entities = discover(book)
    entities.sort(key=lambda e: (-e.get("importance", 0), e.get("id", "")))
    print(f"[registry] discovered {len(entities)} entities; expanding in parallel ...", flush=True)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        expanded = list(ex.map(expand_one, entities))

    registry = {"book": BOOK_TITLE or "untitled",
                "art_style": "(style-neutral; applied per-render)", "entities": expanded}
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    return registry


def main():
    reg = build()
    ents = reg["entities"]
    print(f"\n[registry] wrote {REGISTRY}  ({len(ents)} entities)")
    by_type = {}
    for e in ents:
        by_type.setdefault(e["type"], []).append(e)
    for t in ("character", "setting", "prop"):
        items = by_type.get(t, [])
        print(f"\n=== {t.upper()} ({len(items)}) ===")
        for e in items:
            vs = ", ".join(v["id"] for v in e.get("variants", []))
            print(f"  [{e.get('importance')}] {e['id']:<16} {e['name']:<26} "
                  f"aka={e.get('aliases')}  variants=[{vs}]")


if __name__ == "__main__":
    main()
