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

CHARACTERS THAT SHOULD APPEAR (must match these descriptions):
{chars}

THE INTENDED ART STYLE IS:
{style}

Judge the attached image. Return JSON only:
{{
  "consistency": <1-5, do characters match their descriptions and look like coherent recurring characters?>,
  "accuracy": <1-5, does the image faithfully depict the intended moment (per the brief AND the source \
text), including every concrete physical state or action true at that moment -- BOUND / roped / hands \
tied, kneeling, holding a named object, a stated number of people? Be strict: if such a detail is \
stated in the brief or source but missing or wrong in the image, score at most 2 even if the picture \
is otherwise nice.>,
  "kid_appropriate": <1-5, warm, non-scary, no graphic violence/blood, young-child friendly?>,
  "style_ok": <1-5, matches the intended art style above?>,
  "issues": ["<short concrete problems>"],
  "fix_hint": "<one actionable sentence naming the single most important missing/wrong element to add or fix>"
}}"""


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
    char_desc = "\n".join(f"- {m['name']}: {m['appearance']}" for m in members)
    prompt = (
        f"{art_style}\n\n"
        f"Illustrate this scene for a children's picture book:\n{spread['illustration_brief']}\n\n"
        f"Setting: {spread.get('setting','')}\n\n"
        f"Characters present and their canonical looks:\n{char_desc}\n\n"
    )
    if ref_members:
        labels = ", ".join(f"image {i+1} = {m['name']}" for i, m in enumerate(ref_members))
        prompt += (f"Reference images are attached ({labels}). Keep each one's face, hair, "
                   "and clothing CONSISTENT with their reference image. ")
    prompt += ("Horizontal storybook composition. Keep it warm, gentle, and age-5 "
               "appropriate: no blood, no graphic violence.")
    if fix:
        prompt += f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {fix}"
    return prompt


def gen_scene(spread: dict, cast_index: dict, art_style: str, budget: gem.Budget) -> dict:
    sid = spread["id"]
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
                                        chars=char_desc or "(none)", style=ART_STYLE))
        score = min(crit.get("consistency", 0), crit.get("accuracy", 0),
                    crit.get("kid_appropriate", 0), crit.get("style_ok", 0))
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
        futs = {ex.submit(gen_scene, s, cast_index, art_style, budget): s["id"] for s in pending}
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
