"""Orchestrate a section's illustrated-book run on top of the Entity Registry.

  1. Sheets  -- ensure a canonical reference sheet exists for every (entity,
                variant) in this section's cast. Registry entities are cached
                once in the shared assets dir and reused across sections;
                section-local characters are generated into the section dir.
  2. Scenes  -- per spread, attach the highest-ranked references (capped to
                MAX_REFS, since the image model takes only so many inputs),
                generate, critique, and retry. Resumable + crash-resilient.

A single Budget caps TOTAL image candidates (default 100).
"""
import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import gem, sheets, costs
from .config import OUT, REGISTRY, ASSETS, ART_STYLE, LABEL
from .config import MAX_REFS, PAGE_IMAGE_MODEL  # noqa: E402

CAND = OUT / "candidates"
CHARS = OUT / "characters"
SCENES = OUT / "scenes"

MAX_CANDIDATES = 100
MAX_TRIES_SCENE = 3
PASS_THRESHOLD = 4
# MAX_REFS imported from config (STORY_MAX_REFS) -- pro takes more refs than flash
WORKERS = 4
LOCAL_IMPORTANCE = 3  # default rank for section-local characters

SCENE_CRITIQUE = """You are a strict art director reviewing one illustration for a children's \
read-aloud picture book (audience: 5 years old).

THE ILLUSTRATION SHOULD DEPICT (the intended illustration for this moment):
{brief}

THE SOURCE TEXT this illustration accompanies (ground truth for the story moment -- the picture shows \
ONE moment from it). Use it to catch concrete, VISIBLE details the brief may have dropped (e.g. a \
character bound or roped, holding something, a wound, who is present). Do NOT penalise the image for \
not showing dialogue, inner thoughts, or other moments elsewhere on the page:
{source}

WHAT HAPPENS LATER IN THIS CHAPTER (the text that comes AFTER this moment -- the young reader has \
NOT reached it yet; it tells you WHERE in the chapter this moment sits). The illustration accompanies \
ONLY the moment above and must NOT depict, reveal or foreshadow anything that happens solely in the text \
below -- doing so SPOILS a later surprise, reveal, arrival, death or transformation for the child:
{chapter_ahead}

CHARACTERS THAT SHOULD APPEAR (must match these descriptions):
{chars}

EVERY NAMED CHARACTER IN THIS BOOK (use ONLY to recognise background or secondary figures -- do NOT \
expect them all to appear; most scenes show just the cast above). If a figure ANYWHERE in the picture, \
including the background, is clearly meant to be one of these but gets their core identity wrong -- wrong \
species/creature, or a defining feature missing or changed (e.g. Reepicheep is a Talking Mouse, NOT a \
cat) -- that is a real error:
{roster}

THE INTENDED ART STYLE IS:
{style}

Canonical reference sheets for some characters MAY be attached after the image (each labelled with the \
character's name). When they are, use them to judge `figure_match` below.

Judge the attached image. Return JSON only:
{{
  "physical": <1-5: ANATOMICAL AND PHYSICAL CORRECTNESS -- the single most important check. Is every \
figure (people AND animals/creatures) well-formed -- correct number of fingers, hands, limbs and eyes, no \
fused/extra/missing/malformed parts, no melted or distorted faces, no impossible joints or merged bodies -- \
AND does the scene obey physical reality, with every figure and object properly supported and positioned \
(nothing floating, defying gravity, badly out of scale, or passing through solid objects)? Score 1-2 for \
any clear anatomical or physical defect, even on a small/background figure; 4 for minor awkwardness or \
stiffness; 5 only when everything is well-formed and believably placed.>,
  "consistency": <1-5, do characters match their descriptions and look like coherent recurring characters?>,
  "figure_match": <1-5: are the characters in the image the RIGHT individuals? Compare the foreground cast \
to their attached reference sheets, AND check any background or secondary figure that is clearly meant to \
be one of the named book characters listed above. Judge IDENTITY/species only, not minor variation: pose, \
expression, camera angle, crop, lighting or slight shading is FINE and must still score 5. Score 1-2 if \
ANY figure -- foreground OR background -- that is clearly a specific named character is the WRONG \
person/creature or has a wrong core identity (e.g. a Talking Mouse drawn as a cat, a defining feature \
missing/changed). Do NOT penalise generic unnamed extras (a random sailor, a crowd) who are not a specific \
named character, and do NOT penalise minor differences. If there are no reference sheets AND no \
recognisable named characters, score 5.>,
  "accuracy": <1-5, does the image faithfully depict the intended moment (per the brief AND the source \
text), including every concrete physical state or action true at that moment -- BOUND / roped / hands \
tied, kneeling, holding a named object, a stated number of people? Be strict: if such a detail is \
stated in the brief or source but missing or wrong in the image, score at most 2 even if the picture \
is otherwise nice.>,
  "style_ok": <1-5, matches the intended art style above?>,
  "no_stray_text": <1-5, is the image FREE of unwanted text? Score 5 if there is NO text, OR the only \
text is something the scene genuinely calls for (a sign, a shop name, a book cover, a labelled object \
that the brief or source text actually describes). Score 1-2 if there is gibberish lettering, a \
watermark, floating words, captions, or fragments of the description/prompt rendered as text that the \
story does not call for.>,
  "no_spoiler": <1-5: does the image avoid SPOILING anything that only happens LATER in this chapter \
(see "WHAT HAPPENS LATER" above)? Score 5 if the picture depicts only the CURRENT moment and gives away \
nothing from the text that comes after it. Score 1-2 if it depicts, reveals or foreshadows a later event, \
reveal, a character's fate, a hidden identity, a transformation, or the arrival/appearance of someone or \
something not yet present at THIS moment. If there is no later text, score 5.>,
  "wrong_figures": ["<the NAME of any character -- foreground OR background -- drawn as the WRONG \
person/creature or with a wrong core identity (use the reference-sheet label, or the book character name \
for a background figure); empty list if every recognisable figure is correct>"],
  "drop_figures": ["<the NAME of any figure that is wrong or unwanted and would be EASIER to simply REMOVE \
from the picture than to fix -- typically a background character NOT required by the brief or source text. \
NEVER list anyone the brief or source actually calls for; leave empty unless removing the figure is clearly \
the simpler fix>"],
  "issues": ["<short concrete problems>"],
  "fix_hint": "<one actionable sentence naming the single most important missing/wrong element to add or fix>"
}}"""


def roster_digest(registry: dict, max_each: int = 140) -> str:
    """A compact text list of EVERY named character in the book (name + a short
    canonical appearance), for the scene critic to recognise background/secondary
    figures that aren't in this page's reference set -- so it can flag e.g. a
    background Reepicheep drawn as a cat. Characters only (not settings/props),
    ranked by importance. Cheap: text, no images."""
    ents = [e for e in registry.get("entities", []) if e.get("type", "character") == "character"]
    ents.sort(key=lambda e: -e.get("importance", 0))
    lines = []
    for e in ents:
        app = " ".join((e.get("base_appearance") or "").split())
        if len(app) > max_each:
            app = app[:max_each].rsplit(" ", 1)[0] + "…"
        name = e.get("name") or e.get("id")
        lines.append(f"- {name}: {app}" if app else f"- {name}")
    return "\n".join(lines) or "(none listed)"


def log(msg):
    print(msg, flush=True)


# ---------------- cast resolution ----------------

def resolve_cast(bible: dict, registry: dict) -> dict:
    """Map "entity_id/variant_id" -> resolved member with name, appearance,
    sheet location, type, importance, and source."""
    reg = {e["id"]: e for e in registry.get("entities", [])}
    out = {}
    for m in bible.get("cast", []):
        eid, vid = m["entity_id"], m.get("variant_id", "default")
        key = f"{eid}/{vid}"
        if m.get("from_registry") and eid in reg:
            e = reg[eid]
            var = next((v for v in e.get("variants", []) if v["id"] == vid), None) or {}
            out[key] = {
                "entity_id": eid, "variant_id": vid, "name": m.get("name", e["name"]),
                "appearance": var.get("appearance") or e.get("base_appearance", ""),
                "sheet_prompt": var.get("sheet_prompt") or e.get("base_sheet_prompt", ""),
                "type": e.get("type", "character"), "importance": e.get("importance", 3),
                "base": ASSETS, "source": "registry",
            }
        else:  # section-local
            out[key] = {
                "entity_id": eid, "variant_id": vid, "name": m.get("name", eid),
                "appearance": m.get("appearance", ""), "sheet_prompt": m.get("sheet_prompt", ""),
                "type": "character", "importance": LOCAL_IMPORTANCE,
                "base": CHARS, "source": "local",
            }
    return out


def member_sheet(member: dict):
    return sheets.sheet_path(member["entity_id"], member["variant_id"], member["base"])


# ---------------- scenes ----------------

def scene_members(spread: dict, cast_index: dict) -> list[dict]:
    """Resolved cast for a spread, ranked by importance (protagonists first)."""
    ms = []
    for c in spread.get("cast", []):
        key = f"{c['entity_id']}/{c.get('variant_id', 'default')}"
        if key in cast_index:
            ms.append(cast_index[key])
    ms.sort(key=lambda m: -m["importance"])
    return ms


def build_scene_prompt(spread: dict, members: list[dict], ref_members: list[dict],
                       art_style: str, fix: str = "") -> str:
    char_desc = "\n".join(
        f"- {m['name']}" + (f" (RIGHT NOW: {m['state']})" if m.get("state") else "")
        + f": {m['appearance']}" for m in members)
    has_state = any(m.get("state") for m in members)
    prompt = (
        f"{art_style}\n\n"
        f"Illustrate this scene for a children's picture book:\n{spread['illustration_brief']}\n\n"
        f"Setting: {spread.get('setting','')}\n\n"
    )
    src = (spread.get("read_text") or "").strip()
    if src:
        prompt += (
            "For accuracy, here is the passage this picture accompanies. Illustrate ONLY the single "
            "moment described in the brief above -- do NOT add other events from the passage. Use it "
            "just to get concrete, visible details right (who is present, what they hold or wear, "
            f"whether anyone is bound, hurt, or carrying something):\n\"{src[:800]}\"\n\n"
        )
    prompt += f"Characters present and their canonical looks:\n{char_desc}\n\n"
    if has_state:
        prompt += ("Each character's 'RIGHT NOW' note is that specific person's state at this moment "
                   "and must be shown ONLY on them -- do not apply one person's state (e.g. being "
                   "bound) to the others, and do not apply it to everyone.\n\n")
    if ref_members:
        labels = ", ".join(f"image {i+1} = {m['name']}" for i, m in enumerate(ref_members))
        prompt += (f"Reference images are attached ({labels}). Keep each one's face, hair, "
                   "and clothing CONSISTENT with their reference image. ")
    prompt += ("Horizontal storybook composition. Keep it warm, gentle, and age-5 "
               "appropriate: no blood, no graphic violence. Do NOT render any text, words, "
               "letters, captions or labels in the image unless the scene itself calls for it "
               "(e.g. a sign or book the story describes) -- never put the description into the picture.")
    if fix:
        prompt += f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {fix}"
    return prompt


def gen_scene(spread: dict, cast_index: dict, art_style: str, budget: gem.Budget,
              registry: dict | None = None) -> dict:
    sid = spread["id"]
    roster = roster_digest(registry or {"entities": []})
    members = scene_members(spread, cast_index)
    char_desc = "\n".join(f"- {m['name']}: {m['appearance']}" for m in members)
    # attach only the top-ranked references that actually have a sheet on disk
    ref_members = [m for m in members if member_sheet(m).exists()][:MAX_REFS]
    ref_paths = [member_sheet(m) for m in ref_members]
    dropped = [m["name"] for m in members if m not in ref_members and member_sheet(m).exists()]
    if dropped:
        log(f"[scene:{sid}] ref cap {MAX_REFS}: dropped refs {dropped} (kept in text)")

    best, fix = None, ""
    for attempt in range(1, MAX_TRIES_SCENE + 1):
        if not budget.take():
            log(f"[scene:{sid}] budget exhausted")
            break
        prompt = build_scene_prompt(spread, members, ref_members, art_style, fix=fix)
        cand = CAND / f"scene_{sid:02d}_try{attempt}.webp"
        gem.generate_image(prompt, refs=ref_paths, out_path=cand, aspect="3:2", model=PAGE_IMAGE_MODEL)
        log(f"[scene:{sid}] attempt {attempt} -> {cand.name} (budget {budget.remaining()} left)")
        crit = gem.critique_image(
            cand, SCENE_CRITIQUE.format(brief=spread["illustration_brief"],
                                        source=(spread.get("read_text") or "")[:1200] or "(not available)",
                                        chapter_ahead="(not available)",
                                        chars=char_desc or "(none)", style=ART_STYLE, roster=roster),
            refs=ref_paths, ref_labels=[m["name"] for m in ref_members])
        score = min(crit.get("physical", 0), crit.get("consistency", 0), crit.get("accuracy", 0),
                    crit.get("style_ok", 0), crit.get("no_stray_text", 5), crit.get("figure_match", 5),
                    crit.get("no_spoiler", 5))
        rec = {"path": cand, "score": score, "crit": crit, "attempt": attempt, "prompt": prompt}
        if best is None or score > best["score"]:
            best = rec
        log(f"[scene:{sid}]   score={score} issues={crit.get('issues')}")
        if score >= PASS_THRESHOLD:
            break
        fix = crit.get("fix_hint", "")

    chosen = SCENES / f"scene_{sid:02d}.webp"
    if best:
        shutil.copy(best["path"], chosen)
    return {"id": sid, "title": spread["title"], "scene": str(chosen),
            "score": best["score"] if best else 0, "crit": best["crit"] if best else None,
            "prompt": best["prompt"] if best else None, "attempt": best["attempt"] if best else None,
            "refs": [f"{m['entity_id']}/{m['variant_id']}" for m in ref_members],
            "cast": [f"{m['entity_id']}/{m['variant_id']}" for m in members]}


# ---------------- orchestration ----------------

def ensure_member_sheet(member, registry_by_id, budget):
    """Make sure one cast member's canonical sheet exists (shared or local)."""
    path = member_sheet(member)
    if path.exists():
        log(f"[sheet:{member['entity_id']}/{member['variant_id']}] cached ({member['source']})")
        return
    entity = {"id": member["entity_id"], "type": member["type"],
              "base_appearance": member["appearance"], "base_sheet_prompt": member["sheet_prompt"]}
    variant = {"id": member["variant_id"], "appearance": member["appearance"],
               "sheet_prompt": member["sheet_prompt"]}
    sheets.ensure_sheet(entity, variant, budget, base=member["base"], log=log)


def main():
    bible = json.loads((OUT / "bible.json").read_text())
    registry = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"entities": []}
    # Use the configured style (NOT the bible's restated one) so a single
    # style-neutral bible can be rendered in any selected art style.
    art_style = ART_STYLE
    budget = gem.Budget(MAX_CANDIDATES)
    cast_index = resolve_cast(bible, registry)
    reg_by_id = {e["id"]: e for e in registry.get("entities", [])}

    # --- stage 1: canonical sheets for every cast member (shared cache + locals) ---
    log(f"=== Stage 1: {len(cast_index)} cast sheets ===")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(lambda m: ensure_member_sheet(m, reg_by_id, budget), cast_index.values()))

    # --- stage 2: scenes (parallel; skip already-finalized; crash-resilient) ---
    pending = [s for s in bible["spreads"]
               if not (SCENES / f"scene_{s['id']:02d}.webp").exists()]
    log(f"\n=== Stage 2: {len(pending)}/{len(bible['spreads'])} scenes to make "
        f"(budget {budget.remaining()} left) ===")
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(gen_scene, s, cast_index, art_style, budget, registry): s["id"]
                for s in pending}
        for f in as_completed(futs):
            sid = futs[f]
            try:
                r = f.result()
                results.append(r)
                log(f"[scene:{r['id']:02d}] DONE score={r['score']}")
            except Exception as e:  # noqa: BLE001
                log(f"[scene:{sid:02d}] FAILED: {type(e).__name__}: {str(e)[:160]}")
                results.append({"id": sid, "title": "(failed)", "scene": None, "score": 0})

    done_ids = {r["id"] for r in results}
    for s in bible["spreads"]:
        if s["id"] not in done_ids and (SCENES / f"scene_{s['id']:02d}.webp").exists():
            members = scene_members(s, cast_index)
            ref_members = [m for m in members if member_sheet(m).exists()][:MAX_REFS]
            results.append({"id": s["id"], "title": s["title"],
                            "scene": str(SCENES / f"scene_{s['id']:02d}.webp"),
                            "score": "prev", "crit": None,
                            "prompt": build_scene_prompt(s, members, ref_members, art_style),
                            "refs": [f"{m['entity_id']}/{m['variant_id']}" for m in ref_members],
                            "cast": [f"{m['entity_id']}/{m['variant_id']}" for m in members]})
    results.sort(key=lambda r: r["id"])
    (OUT / "results.json").write_text(json.dumps(
        {"scenes": results, "candidates_used": budget.used}, indent=2))
    log(f"\n=== DONE. candidates used: {budget.used}/{MAX_CANDIDATES} ===")
    for r in results:
        log(f"  scene {r['id']:02d} {r['title']:<26} score={r['score']}")

    # cost accounting: this run (by label) + cumulative
    log("\n" + costs.report(run=LABEL))
    log("\n" + costs.report())


if __name__ == "__main__":
    main()
