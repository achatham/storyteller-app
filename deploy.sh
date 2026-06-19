#!/usr/bin/env bash
# Export one book and (re)deploy the static GitHub Pages site, which serves each
# book at a stable, book-specific URL:
#
#     https://achatham.github.io/storyteller-app/books/<slug>/
#
# The site root is an auto-generated landing page listing every deployed book.
# Other books on gh-pages are left untouched, so this is safe to run per book.
#
#   ./deploy.sh <book_id> <slug> "<Title>" "<Author>" [extra export args...]
#
# Example:
#   ./deploy.sh 16 a-christmas-carol "A Christmas Carol" "Charles Dickens"
#
# Env overrides: STORY_APP_DB (default data/storyteller.db),
#   GHP (deploy clone dir, default /tmp/ghp), REPO_URL.
#
# See docs/static_regen.md for the full description.
set -euo pipefail

if [ "$#" -lt 4 ]; then
  sed -n '2,18p' "$0"; exit 2
fi

DB=${STORY_APP_DB:-data/storyteller.db}
GHP=${GHP:-/tmp/ghp}
REPO_URL=${REPO_URL:-https://github.com/achatham/storyteller-app.git}

book_id=$1; slug=$2; title=$3; author=$4; shift 4

# 1. Export the book into a scratch build dir (self-contained, relative paths).
out="output/$slug"
STORY_APP_DB="$DB" python3 -m webapp.export "$book_id" "$out" \
  --slug "$slug" --title "$title" --author "$author" "$@"

# 2. Ensure the gh-pages deploy clone exists and is current.
if [ ! -d "$GHP/.git" ]; then
  git clone "$REPO_URL" "$GHP"
  git -C "$GHP" checkout gh-pages
fi
git -C "$GHP" pull --ff-only

# 3. Mirror this book into books/<slug>/. The scoped --delete only prunes stale
#    files inside this one book's dir, never other books.
mkdir -p "$GHP/books/$slug"
rsync -a --delete "$out/" "$GHP/books/$slug/"

# 4. Rebuild the root landing page from every deployed book.
python3 -m webapp.build_index "$GHP"

# 5. Commit & push.
git -C "$GHP" add -A
git -C "$GHP" status --short
git -C "$GHP" commit -m "Deploy $title (books/$slug)"
git -C "$GHP" push origin gh-pages

echo
echo "Deployed: https://achatham.github.io/storyteller-app/books/$slug/"
