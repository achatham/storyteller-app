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
import re

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
- A VARIANT IS A DURABLE LOOK, NOT A MOMENT. Only make a variant for how someone looks across a whole stretch of the story. A one-off item worn for a single scene -- a party hat, a costume, a paper crown, a single evening's finery -- is NOT a variant: it belongs to that one page's illustration, and baking it into a variant would put it on every page that variant covers. Never write a "delta" that hedges ("occasionally", "sometimes", "at one point", "for one scene"): if you find yourself hedging, the feature is a moment, so leave it out of the delta entirely and describe only what is true for the WHOLE span.
- A VARIANT IS NEVER A CONDITION OF HARM OR DISTRESS. Being unconscious, petrified, injured, bleeding, sick, crying, terrified, captive or dead is a MOMENT for that page's illustration, not a look: do NOT make a variant for it and do not put such words in any delta. A reference sheet of a lifeless or hurt child cannot be drawn at all (the image generator refuses it), and then every page needing it loses the character's reference. Only a lasting, VISIBLE wardrobe or body change that persists across scenes qualifies -- a plaster cast worn for chapters, a scar, an eyepatch, torn or dusty clothes -- described neutrally ("left arm in a white plaster cast"); "kind: injury" and "kind: state" are for those alone.
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
      "appearance": "<the FULL resolved appearance for this variant: the base look with THIS variant's delta applied, describing the character as a SINGLE figure at THIS one point only. Do NOT mention any other age/state or how they look at other times, and do NOT say they later 'grow into' or 'become' anything -- that belongs to other variants. One figure, one look. This appearance is drawn onto EVERY page the variant covers, so include ONLY what is true for the variant's whole span: if the delta mentions something occasional or worn for a single scene (a party hat, a costume, a one-evening accessory), LEAVE IT OUT of both appearance and sheet_prompt and describe the character's usual look instead.>",
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
SAFE TO DRAW: every appearance and sheet_prompt must describe a calm, unhurt figure in a neutral \
relaxed pose. An image generator REFUSES a reference sheet of a child who is lifeless, limp, \
motionless, wounded, bleeding, bruised, deathly pale, grimy with tear tracks, terrified or \
restrained -- and the character then has no reference at all. If a variant's delta describes such \
a condition, write that variant's appearance as the WARDROBE and hair of that span only (rumpled \
robes, a loosened tie, tousled dusty hair are fine) with a calm, composed expression and open \
eyes, standing; the condition itself belongs to the individual page illustrations.
Resolve EVERY variant listed on the entity (echo each variant id). If the entity has no variants, return an empty variants list.
"""


WARDROBE_PROMPT = """You are the costume designer for a children's picture-book edition of \
{book_ref}. For the character below, write the WARDROBE AND GROOMING reference for each of their \
listed looks, so the same outfit is drawn identically on every page it covers.

Describe ONLY clothing, footwear, accessories and hairstyle -- garment by garment, with fabrics, \
cuts, fastenings and colours, and say how worn or neat each is. Do NOT describe the body, build, \
face, skin, age or expression: those are fixed elsewhere and must not be repeated here. No art \
style, medium or rendering words.

Write flowing descriptive prose, not a labelled list -- this text is pasted straight into an \
illustration prompt, so "a loose unbleached wool tunic over grey woollen leggings, with scuffed \
brown leather boots" reads correctly and "Top: tunic. Legs: leggings." does not.

An outfit is what the character wears for the WHOLE span of that look. A costume worn for a single \
scene does not belong in any of these descriptions, and neither does anything torn, bloodied or \
disarranged by one event: describe the clothes as they are normally worn across the span.

CHARACTER:
{entity}

Return JSON only:
{{"base_outfit": "<the character's default everyday outfit and hairstyle>",
  "variants": [{{"id": "<echo the variant id>", "outfit": "<the full outfit and hairstyle for THIS look>"}}]}}
Resolve EVERY variant listed, echoing each id.
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


def neutral_sheet_prompt(name: str, desc: str) -> str:
    """A reference-sheet prompt built straight from a description, for when the expand
    pass didn't write one."""
    return (f"A neutral full reference view of {name}: {desc} "
            "Plain soft off-white background, even lighting, no text.")


# --- making narrative text drawable ---------------------------------------
#
# `canonical_details` is prose ABOUT a character, written to be read; an appearance is
# a description of one figure, written to be drawn. The expand pass converts one into
# the other. When it is refused (see expand_one) the fallback has to do that conversion
# itself, because whatever it produces is pasted verbatim into a reference-sheet prompt
# and into the cast block of every page the variant covers. Three things in narrative
# prose are actively harmful there, and all three shipped in Princess Academy:
#   "Her laugh is low and throaty"          -- nothing to draw; noise in an image prompt
#   "round, FRIGHTENED dark eyes"           -- bakes an emotion into every page
#   "Often rubs her forehead when perplexed" -- a habit drawn as a permanent pose
# and one thing is missing: an age. "Tiny from birth, noticeably smaller than girls
# years younger than her" with no age anchor got the protagonist drawn as a toddler.

# A sentence is non-visual if it describes sound, manner or temperament, or claims a
# likeness to someone we are not drawing -- UNLESS it also carries a visual fact, in
# which case dropping it would lose that fact.
NON_VISUAL_RE = re.compile(r"\b(laugh|laughter|voice|accent|tone|sings?|speaks?|smells?|"
                           r"temper|temperament|personality|spitting image|image of her|"
                           r"image of his|looks? just like|takes after)\b", re.I)
VISUAL_RE = re.compile(r"\b(hair|eyes?|skin|face|freckl|scar|beard|braid|cheeks?|build|"
                       r"tall|short|small|tiny|thin|stout|wears?|wearing|dressed|clothe|"
                       r"boots?|coat|dress|gown|robes?|shirt|trousers|apron|hat|colou?r)\b", re.I)
# Distress worn on the face. The expand pass has a SAFE TO DRAW clause for exactly this;
# the fallback needs its own, or a reference sheet of a frightened child either gets
# refused by the image model or becomes the character's permanent expression.
DISTRESS_RE = re.compile(r"\b(frightened|terrified|fearful|scared|anxious|weeping|tearful|"
                         r"tear-streaked|haunted|grief-stricken|panicked|miserable|"
                         r"sickly|wretched)\b[,\s]*", re.I)

_AGE_WORDS = {w: i for i, w in enumerate(
    ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
     "fourteen fifteen sixteen seventeen eighteen nineteen").split())}
AGE_RE = re.compile(r"\b(\d{1,2}|" + "|".join(_AGE_WORDS) + r")[-\s]year[s]?[-\s]old\b"
                    r"|\b(?:age[d]?|turns|turning)\s+(\d{1,2}|" + "|".join(_AGE_WORDS) + r")\b"
                    r"|\b(\d{1,2}|" + "|".join(_AGE_WORDS) + r")\s+years?\s+old\b", re.I)


def find_age(*texts: str) -> int | None:
    """The character's age in years, from the first text that states one outright."""
    for t in texts:
        m = AGE_RE.search(t or "")
        if not m:
            continue
        raw = next(g for g in m.groups() if g)
        age = int(raw) if raw.isdigit() else _AGE_WORDS.get(raw.lower())
        if age is not None and 0 < age < 20:
            return age
    return None


def drawable(text: str) -> str:
    """Keep only what an illustrator could put on the page: drop clauses that carry no
    visual fact, and strip distress worn as an expression.

    Judged clause by clause, not sentence by sentence, because deltas habitually join
    an event to a look with a semicolon ("Turns fifteen at midsummer; the same wool
    shirt, but new thick-soled boots") and only the second half can be drawn. A clause
    is dropped only when it BOTH matches something unillustratable and contains no
    visual anchor of its own -- an age statement is unillustratable on its own, but
    `find_age` has already hoisted it into the leading noun phrase by this point.
    """
    # split keeping the separators, so text that loses nothing comes back unchanged
    parts = re.split(r"((?<=[.!?])\s+|;\s*)", (text or "").strip())
    out = ""
    for i in range(0, len(parts), 2):
        seg, sep = parts[i].strip(), (parts[i + 1] if i + 1 < len(parts) else "")
        if not seg:
            continue
        if not VISUAL_RE.search(seg) and (NON_VISUAL_RE.search(seg) or HEDGE_RE.search(seg)
                                          or AGE_RE.search(seg)):
            continue
        seg = DISTRESS_RE.sub("", seg).strip()
        if not seg:
            continue
        out += (seg if out else seg[0].upper() + seg[1:]) + sep
    out = out.strip().rstrip(";").strip()
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return out


def _figure_noun(entity: dict, age: int | None) -> str:
    """A leading noun phrase for a character with a known age, so the image model has
    something concrete to size the figure against. The gendered noun comes from the
    pronouns the book itself uses about this character -- never from the name."""
    if age is None or entity.get("type") != "character":
        return ""
    text = f"{entity.get('canonical_details', '')} {entity.get('summary', '')}"
    she = len(re.findall(r"\b(she|her|hers|daughter|girl|sister|mother)\b", text, re.I))
    he = len(re.findall(r"\b(he|him|his|son|boy|brother|father)\b", text, re.I))
    who = "girl" if she > he else "boy" if he > she else "child"
    if age > 15:
        who = {"girl": "young woman", "boy": "young man", "child": "youth"}[who]
    return f"A {age}-year-old {who}."


def fill_variant_fallback(entity: dict, variant: dict) -> None:
    """Give one variant whatever appearance/sheet_prompt the expand pass didn't:
    the entity's base look with THIS variant's delta appended.

    Folding the delta in is the whole point. Every variant of an entity shares a base
    look, so a fallback of the base alone gives all of them byte-identical text --
    every reference sheet is then drawn from the same prompt and the roster shows the
    same picture two or three times, with nothing on screen to say why. "In this look:"
    marks the delta as the override, so a delta that contradicts the base (shorn hair
    over a base that says long hair) reads as the later state rather than a conflict.

    The age is resolved per variant: a variant whose delta says "turns fifteen" must
    lead with fifteen even though the entity's own details say fourteen.
    """
    base = (entity.get("base_appearance") or "").strip()
    delta = (variant.get("delta") or "").strip()
    if not variant.get("appearance"):
        base_age, delta_txt, lead = find_age(base), drawable(delta), ""
        age = find_age(delta) or base_age or find_age(entity.get("summary", ""))
        if base_age is None:
            lead = _figure_noun(entity, age)           # nothing states an age yet
        elif age != base_age:
            # the base already names an age and this look changes it -- say so in the
            # delta, where "In this look:" already marks it as the override, rather
            # than opening with a second age that contradicts the sentence after it
            delta_txt = " ".join(x for x in (f"now {age} years old;", delta_txt) if x)
        desc = " ".join(x for x in (lead, base,
                                    f"In this look: {delta_txt}" if delta_txt else "") if x)
    else:
        desc = variant["appearance"]
    variant["appearance"] = desc
    if not variant.get("sheet_prompt"):
        variant["sheet_prompt"] = neutral_sheet_prompt(
            entity.get("name", "the subject"), desc)


def expand_wardrobe(entity: dict) -> dict:
    """The rescue pass for an entity the full expand refused: ask for CLOTHING AND
    HAIR ONLY, and take the body from the book's own words.

    Measured against the live API on the two entities that were refused in Princess
    Academy (book 26): the block is not about the entity, the wording, or the call's
    shape -- grouping several entities into one call is refused too, and takes the
    healthy ones down with it, and anonymising the child changes nothing. It is about
    being asked to describe a child's BODY. Drop build/face/skin from the request and
    the same model, same entity, same moment answers in full detail.

    That is most of what was lost anyway: the outfit is what actually differs between
    a character's variants, and it is what a reference sheet is FOR. The physical facts
    come from `canonical_details` (which the book stated), cleaned by `drawable`.
    """
    stub = {k: entity.get(k) for k in ("id", "name", "summary", "canonical_details")}
    stub["variants"] = [{k: v.get(k) for k in ("id", "label", "when", "delta")}
                        for v in entity.get("variants", [])]
    rich = gem.text_json(
        WARDROBE_PROMPT.format(book_ref=BOOK_REF, entity=json.dumps(stub, ensure_ascii=False)),
        model=REGISTRY_MODEL, thinking_level=REGISTRY_THINK)
    out = dict(entity)
    body = drawable(entity.get("canonical_details", "") or entity.get("summary", ""))
    outfits = {v.get("id"): (v.get("outfit") or "").strip()
               for v in rich.get("variants", []) if isinstance(v, dict)}

    body_age = find_age(body)

    def compose(outfit: str, age: int | None) -> str:
        # the book's own words often already give the age ("Barely twelve years old");
        # only add the leading noun phrase when nothing else states one
        lead = _figure_noun(entity, age) if body_age is None else ""
        older = f"Now {age} years old." if body_age is not None and age != body_age else ""
        return " ".join(x for x in (lead, body, older,
                                    f"Wearing: {outfit}" if outfit else "") if x)

    base_age = find_age(entity.get("canonical_details", ""), entity.get("summary", ""))
    out["base_appearance"] = compose((rich.get("base_outfit") or "").strip(), base_age)
    out["base_sheet_prompt"] = neutral_sheet_prompt(
        entity.get("name", "the subject"), out["base_appearance"])
    for v in out.get("variants", []):
        outfit = outfits.get(v["id"], "")
        if outfit:
            v["appearance"] = compose(outfit, find_age(v.get("delta", "")) or base_age)
            v["sheet_prompt"] = neutral_sheet_prompt(
                entity.get("name", "the subject"), v["appearance"])
        fill_variant_fallback(out, v)      # the model skipped this variant id
        flag_momentary_variant(out, v)
    out["wardrobe_only"] = True
    return out


def expand_one(entity: dict) -> dict:
    stub = {k: entity.get(k) for k in
            ("id", "type", "name", "summary", "canonical_details", "variants")}
    prompt = EXPAND_PROMPT.format(
        book_ref=BOOK_REF, entity=json.dumps(stub, ensure_ascii=False))
    out = dict(entity)
    try:
        rich = gem.text_json(prompt, model=REGISTRY_MODEL, thinking_level=REGISTRY_THINK)
    except Exception as e:  # noqa: BLE001 -- never let one entity sink the whole summary
        # Reliably reached: Gemini's child-safety filter refuses to write a detailed
        # physical description of a named child (PROHIBITED_CONTENT on the *output*, so
        # gem's retries can't help). Every occurrence to date -- 11 across 11 books --
        # has been a girl child, twice the book's own protagonist. Retry for the
        # clothes alone, which is not refused; see expand_wardrobe.
        print(f"[registry] expand FAILED for {entity.get('id')}: "
              f"{type(e).__name__}: {str(e)[:120]} -- retrying wardrobe-only", flush=True)
        try:
            out = expand_wardrobe(entity)
            print(f"[registry] wardrobe-only expand rescued {entity.get('id')} "
                  f"({len(out.get('variants', []))} variants)", flush=True)
            return out
        except Exception as e2:  # noqa: BLE001
            print(f"[registry] wardrobe-only expand ALSO failed for {entity.get('id')}: "
                  f"{type(e2).__name__}: {str(e2)[:120]} -- using canonical_details fallback",
                  flush=True)
        fallback = drawable(entity.get("canonical_details", "") or entity.get("summary", ""))
        out["base_appearance"] = fallback
        out["base_sheet_prompt"] = neutral_sheet_prompt(
            entity.get("name", "the subject"), fallback)
        out["expand_failed"] = True
        for v in out.get("variants", []):
            fill_variant_fallback(out, v)
            flag_momentary_variant(out, v)
        return out
    out["base_appearance"] = rich.get("base_appearance", "")
    out["base_sheet_prompt"] = rich.get("base_sheet_prompt", "")
    # merge resolved appearance/sheet_prompt back onto the matching variant
    resolved = {v.get("id"): v for v in rich.get("variants", [])}
    for v in out.get("variants", []):
        r = resolved.get(v["id"], {})
        v["appearance"] = r.get("appearance") or ""
        v["sheet_prompt"] = r.get("sheet_prompt") or ""
        fill_variant_fallback(out, v)      # the model skipped this variant id
        flag_momentary_variant(out, v)
    return out


# A delta that hedges describes a MOMENT, not a look ("occasionally swapping his
# pointed hat for a flowered bonnet"). Resolved into an appearance it becomes
# unconditional, gets drawn onto the reference sheet, and then rides every page the
# variant covers -- and the scene critic scores that as correct, because the sheet
# says so. Cheap to detect, so say it loudly rather than silently shipping it.
HEDGE_RE = re.compile(r"\b(occasionally|sometimes|often|frequently|at one point|at times|"
                      r"momentarily|for one scene|on one occasion|on occasion|now and then|"
                      r"every so often|at the feast)\b", re.I)


def flag_momentary_variant(entity: dict, variant: dict) -> bool:
    """Warn when a variant's delta hedges but the hedge survived into the appearance
    the sheet is drawn from. Advisory only -- the roster is editable in the UI."""
    delta = variant.get("delta") or ""
    if not HEDGE_RE.search(delta):
        return False
    print(f"[registry] WARNING {entity.get('id')}/{variant.get('id')}: delta describes a "
          f"MOMENTARY look ({HEDGE_RE.search(delta).group(0)!r}) -- a one-scene item baked "
          f"into this variant will appear on every page it covers. delta: {delta[:160]}",
          flush=True)
    variant["momentary_warning"] = delta[:200]
    return True


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
