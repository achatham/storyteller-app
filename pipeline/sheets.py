"""Shared canonical reference sheets, keyed by (entity, variant), cached once in
output/assets/sheets/ and reused by every section. This is what actually pins a
character/setting/prop's identity across the whole book.
"""
import shutil

from . import gem
from .config import ASSETS, ART_STYLE, SHEET_IMAGE_MODEL

PASS = 4
MAX_TRIES = 2

SHEET_CRITIQUE = """You are reviewing a CANONICAL REFERENCE SHEET for a children's book.
It should clearly depict this single entity, matching:
{desc}

The intended art style is:
{style}

Return JSON only:
{{
  "match": <1-5, matches the description?>,
  "clean_sheet": <1-5, single subject on a plain/neutral background, no text labels, no extra subjects?>,
  "style_ok": <1-5, matches the intended art style above?>,
  "issues": ["..."],
  "fix_hint": "<one sentence>"
}}"""


def sheet_path(entity_id: str, variant_id: str, base=ASSETS):
    return base / f"{entity_id}__{variant_id}.webp"


def ensure_sheet(entity: dict, variant: dict, budget: gem.Budget, base=ASSETS, log=print):
    """Generate (or reuse) the canonical sheet for one entity-variant. Returns
    the path, or None if it could not be made (e.g. budget exhausted).

    Registry entities use the shared ASSETS dir (generated once for the whole
    book); section-local characters pass base=<section>/characters.
    """
    eid, vid = entity["id"], variant["id"]
    path = sheet_path(eid, vid, base)
    if path.exists():
        log(f"[sheet:{eid}/{vid}] cached")
        return path

    desc = variant.get("appearance") or entity.get("base_appearance", "")
    base_prompt = variant.get("sheet_prompt") or entity.get("base_sheet_prompt", "")
    aspect = "2:3" if entity.get("type") == "character" else "3:2"

    best, fix = None, ""
    for attempt in range(1, MAX_TRIES + 1):
        if not budget.take():
            log(f"[sheet:{eid}/{vid}] budget exhausted")
            break
        prompt = f"{ART_STYLE}\n\n{base_prompt}\n\nCanonical look (match exactly): {desc}\n" \
                 "Single subject only, plain soft neutral background, even lighting, no text labels."
        if fix:
            prompt += f"\n\nIMPORTANT FIX FROM LAST ATTEMPT: {fix}"
        cand = ASSETS / f"_cand_{eid}__{vid}_try{attempt}.webp"
        gem.generate_image(prompt, out_path=cand, aspect=aspect, model=SHEET_IMAGE_MODEL)
        crit = gem.critique_image(cand, SHEET_CRITIQUE.format(desc=desc, style=ART_STYLE))
        score = min(crit.get("match", 0), crit.get("clean_sheet", 0), crit.get("style_ok", 0))
        log(f"[sheet:{eid}/{vid}] attempt {attempt} score={score} {crit.get('issues')}")
        if best is None or score > best[1]:
            best = (cand, score)
        if score >= PASS:
            break
        fix = crit.get("fix_hint", "")
    if best:
        shutil.copy(best[0], path)
        return path
    return None
