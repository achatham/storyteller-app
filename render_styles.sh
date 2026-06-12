#!/usr/bin/env bash
# Reusable: build the full-book registry + a section's bible ONCE, then render
# that section in many art styles CONCURRENTLY (each style is an independent
# process sharing only the read-only registry + bible).
#
# Required env (same knobs as config.py): STORY_PDF, STORY_BOOK, STORY_AUTHOR,
# and either STORY_CHAPTERS (epub) or STORY_PAGES (pdf), plus STORY_LABEL.
# Tunables: OUT_ROOT (output dir), STYLES (space-separated), MAXJOBS (parallel
# styles; default 3 -> 3 styles x 4 workers = ~12 concurrent API calls).
set -uo pipefail
cd "$(dirname "$0")"

OUT_ROOT="${OUT_ROOT:-output/dawn_treader}"
STYLES="${STYLES:-watercolor ink_and_wash bold_picturebook oil_painterly soft_pastel vintage_midcentury}"
MAXJOBS="${MAXJOBS:-3}"

export STORY_REGISTRY="${STORY_REGISTRY:-$OUT_ROOT/registry.json}"
BASE="$OUT_ROOT/_base"
mkdir -p "$BASE"

# ---- 1. full-book summary (registry), once ----
if [ ! -f "$STORY_REGISTRY" ]; then
  echo "=== [1/3] building full-book registry (once) ==="
  STORY_OUT="$BASE" python3 -m pipeline.registry
else
  echo "=== [1/3] reusing registry $STORY_REGISTRY ==="
fi

# ---- 2. section segmentation (bible), once ----
if [ ! -f "$BASE/bible.json" ]; then
  echo "=== [2/3] extracting + analyzing section (once) ==="
  STORY_OUT="$BASE" python3 -m pipeline.extract
  STORY_OUT="$BASE" python3 -m pipeline.analyze
else
  echo "=== [2/3] reusing bible $BASE/bible.json ==="
fi

# ---- 3. render each style, up to MAXJOBS at a time ----
echo "=== [3/3] rendering [$STYLES] with MAXJOBS=$MAXJOBS ==="
render_one() {
  local s="$1" out="$OUT_ROOT/$1"
  mkdir -p "$out"
  cp -f "$BASE/chapter.txt" "$BASE/bible.json" "$out/"
  if STORY_OUT="$out" STORY_STYLE="$s" STORY_ASSETS="$OUT_ROOT/sheets/$s" python3 -m pipeline.run \
     && STORY_OUT="$out" STORY_STYLE="$s" STORY_ASSETS="$OUT_ROOT/sheets/$s" python3 -m pipeline.build; then
    echo "########## $s DONE ##########"
  else
    echo "########## $s FAILED ##########"
  fi
}

for s in $STYLES; do
  render_one "$s" > "$OUT_ROOT/render_$s.log" 2>&1 &
  echo "  launched $s (pid $!)"
  while [ "$(jobs -rp | wc -l)" -ge "$MAXJOBS" ]; do wait -n; done
done
wait

echo ""
echo "=== ALL DONE ==="
ls -1 "$OUT_ROOT"/*/book.html
