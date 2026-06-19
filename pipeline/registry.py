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
  "base_appearance": "<rich, CONCRETE, neutral canonical description of ONE single look at ONE moment: build, hair, eyes, skin, default clothing, distinctive features (for a setting/prop: materials, shapes, colors, mood). Describe a SINGLE figure as they look at this one point -- do NOT narrate how they change over time, age, or 'later become' anything (those are separate variants). Stay consistent with canonical_details; invent reasonable specifics only where the text is silent.>",
  "base_sheet_prompt": "<a complete, style-AGNOSTIC image prompt for a NEUTRAL REFERENCE SHEET of this entity in its DEFAULT look: for a character, full body, front view, relaxed neutral pose, plain soft off-white background, even lighting, no props, no text; for a setting, a clean establishing view; for a prop, an isolated product-style view. Describe only the subject and framing -- no art-style words.>",
  "variants": [
    {{
      "id": "<echo the variant id you are resolving>",
      "appearance": "<the FULL resolved appearance for this variant: the base look with THIS variant's delta applied, describing the character as a SINGLE figure at THIS one point only. Do NOT mention any other age/state or how they look at other times, and do NOT say they later 'grow into' or 'become' anything -- that belongs to other variants. One figure, one look.>",
      "sheet_prompt": "<a complete, style-AGNOSTIC reference-sheet image prompt for THIS variant, same neutral framing rules as base, no art-style words>"
    }}
  ]
}}
ANATOMY & DISTINCTIVE FEATURES: If the entity has any UNUSUAL or non-human anatomy or a striking \
defining trait (e.g. one leg, three eyes, four arms, a tail, wings, an animal head, an odd size or \
shape), LEAD base_appearance and each variant appearance with that feature and state it EXPLICITLY \
and unambiguously, with exact COUNTS ("exactly ONE central leg, not two"; "three eyes"). Never phrase \
it in a way an artist would default to an ordinary human/animal. When a body part merely RESEMBLES an \
object, say it is an organic body part shaped LIKE that object, never just the object (write "one huge \
flat foot shaped like a canoe -- an organic foot, NOT an actual boat", not "a canoe-shaped foot"). The \
single most identity-defining trait comes FIRST, before clothing and incidental details.
Resolve EVERY variant listed on the entity (echo each variant id). If the entity has no variants, return an empty variants list.
"""


REPAIR_PROMPT = """You are refining a children's-book character registry so each character can be \
drawn correctly in EVERY scene. The whole-book discovery pass sometimes collapses a character \
into only their dominant look and misses how they appear at their FIRST appearance -- for \
example, ordinary real-world clothes in the opening before they later enter a different world or \
change into other dress.

Below is the BEGINNING of the book and the current character roster (each with its variant ids, \
the chapters each applies to, and what is different about each). For every character who actually \
APPEARS in this opening passage, check whether their variants already cover how they look HERE. \
If their opening look is NOT covered by an existing variant, ADD a variant for it. If an existing \
variant's "when" wrongly includes this opening (e.g. a later look tagged as starting at chapter 1), \
correct its span.

Only address looks actually visible in this opening; do not invent later looks. Reuse exact entity ids.

Return JSON only:
{{
  "additions": [
    {{"entity_id": "<existing id>", "id": "<new snake_case variant id>", "kind": "outfit|age|state|other", "label": "<short label>", "when": "<where it applies, e.g. 'Chapter 1'>", "delta": "<what is different about this look vs the character's other variants>"}}
  ],
  "fixes": [
    {{"entity_id": "<id>", "variant_id": "<existing variant id>", "when": "<corrected span>"}}
  ]
}}
If nothing needs changing, return empty lists.

CURRENT ROSTER:
{roster}

BEGINNING OF THE BOOK:
\"\"\"
{opening}
\"\"\"
"""

OPENING_WORDS = 3000


def repair_openings(entities: list[dict], book_text: str) -> list[dict]:
    """Safety net for the most common discovery miss: a character present in the
    book's opening in one look (often ordinary/real-world clothes) before changing
    later, where discover kept only the later look. Focused on the opening text so
    it is reliable and cheap (one extra call). Mutates + returns `entities`."""
    chars = [e for e in entities if e.get("type") == "character"]
    if not chars:
        return entities
    roster = [{"id": e["id"], "name": e.get("name", ""),
               "variants": [{"id": v.get("id"), "when": v.get("when", ""),
                             "delta": v.get("delta", "")} for v in e.get("variants", [])]}
              for e in chars]
    opening = " ".join(book_text.split()[:OPENING_WORDS])
    prompt = REPAIR_PROMPT.format(roster=json.dumps(roster, ensure_ascii=False),
                                  opening=opening)
    try:
        data = gem.text_json(prompt, model=REGISTRY_MODEL, thinking_level=REGISTRY_THINK)
    except Exception as ex:  # noqa: BLE001 -- repair is best-effort; never sink the build
        print(f"[registry] opening repair skipped: {type(ex).__name__}: {ex}", flush=True)
        return entities
    by_id = {e["id"]: e for e in entities}
    added = fixed = 0
    for a in data.get("additions", []):
        e = by_id.get(a.get("entity_id"))
        vid = a.get("id")
        if not e or not vid or any(v.get("id") == vid for v in e.get("variants", [])):
            continue
        e.setdefault("variants", []).insert(0, {
            "id": vid, "kind": a.get("kind", "outfit"), "label": a.get("label", ""),
            "when": a.get("when", ""), "delta": a.get("delta", "")})
        added += 1
    for f in data.get("fixes", []):
        e = by_id.get(f.get("entity_id"))
        if not e or not f.get("when"):
            continue
        for v in e.get("variants", []):
            if v.get("id") == f.get("variant_id"):
                v["when"] = f["when"]
                fixed += 1
    print(f"[registry] opening repair: +{added} first-appearance variants, "
          f"{fixed} span fixes", flush=True)
    return entities


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
    print(f"[registry] discovered {len(entities)} entities; repairing openings ...", flush=True)
    entities = repair_openings(entities, book)
    print(f"[registry] expanding {len(entities)} entities in parallel ...", flush=True)

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
