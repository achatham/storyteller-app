# Regenerating & redeploying the static demo

How to rebuild the self-contained static book viewer and publish it to the live
GitHub Pages site at <https://achatham.github.io/storyteller-app/>.

Each book is served at a **stable, book-specific URL**:

```
https://achatham.github.io/storyteller-app/books/<slug>/
```

The site **root** is an auto-generated landing page that lists every published
book and links to it. Adding a new book never disturbs the URLs of existing ones.

The exporter (`webapp/export.py`) writes a single `index.html` per book with the
text inlined and illustrations as `images/*.webp` — no backend needed. All paths
inside a book are relative, so the book dir can live at any subpath. Pages serves
the **root of the `gh-pages` branch**, deployed from a separate clone.

---

## TL;DR — one command

```bash
./deploy.sh <book_id> <slug> "<Title>" "<Author>" [extra export args...]
# e.g.
./deploy.sh 16 a-christmas-carol "A Christmas Carol" "Charles Dickens"
```

`deploy.sh` exports the book, mirrors it into `books/<slug>/` on the deploy clone,
rebuilds the root landing page from **all** deployed books, and commits + pushes.
The manual steps below are what it automates, in case you need to do them by hand.

---

## 1. (Optional) Edit the intro / welcome screen

The viewer's welcome card lives in the `INTRO` block near the top of the `<script>`
in `webapp/static/export.html`. Edit `heading`, the `paragraphs` array, and the
`github` link there. These tokens are filled in per book at view time:

| token       | becomes                                          |
|-------------|--------------------------------------------------|
| `{title}`   | the book's title, rendered in *italics*           |
| `{style}`   | the book's art style (underscores → spaces), in “quotes” |
| `{project}` | a link using the `github: { url, label }` config |

`export.html` is a **template** — changes only take effect when you re-export (step 2).
The root landing page is a separate template, `webapp/static/index.html`.

## 2. Export the book from the database

The app DB is `data/storyteller.db` (the default export path points elsewhere, so set
`STORY_APP_DB` explicitly). Export each book into its own scratch dir, and give it a
URL slug (defaults to a slugified title if omitted):

```bash
STORY_APP_DB=data/storyteller.db \
  python3 -m webapp.export <book_id> output/<slug> \
  --slug <slug> --title "<Title>" --author "<Author>"
```

Notes:
- Book titles/authors are usually **blank in the DB**, so pass `--title`/`--author`.
- `--slug` sets the published URL (`books/<slug>/`). Keep it stable — it's the
  permanent link to the book.
- Add `--pages N` to export only the first N pages (a short demo slice).
- `output/` is git-ignored — it's just a scratch build dir.
- Each export also writes `book.json` (title, author, style, slug, cover) — the
  manifest the landing page is built from. `cover` is the first illustrated page.
- The command prints a summary, e.g. `{'chapters': 11, 'images': 152, 'pages_missing_image': []}`.
  A non-empty `pages_missing_image` means those pages haven't been illustrated yet.

Find a book's id / style with:

```bash
STORY_APP_DB=data/storyteller.db python3 - <<'PY'
import sqlite3
c = sqlite3.connect("data/storyteller.db"); c.row_factory = sqlite3.Row
for r in c.execute("select id,filename,style,num_pages,status from books order by id"):
    n = c.execute("select count(distinct idx) from scenes where book_id=?", (r["id"],)).fetchone()[0]
    print(f'id={r["id"]:>3}  illus={n}/{r["num_pages"]}  {r["status"]:<8} {r["style"]:<18} {r["filename"]}')
PY
```

**Example — the full A Christmas Carol demo:**

```bash
STORY_APP_DB=data/storyteller.db \
  python3 -m webapp.export 16 output/a-christmas-carol \
  --slug a-christmas-carol --title "A Christmas Carol" --author "Charles Dickens"
```

(Book `16`, style `crayon_childlike`, 152 illustrated pages.)

## 3. Publish to GitHub Pages

Pages is served from the **`gh-pages` branch root**, deployed from a dedicated clone
at `/tmp/ghp` (a full separate clone, *not* a worktree of the main repo). If it's
missing, recreate it with `git clone <repo> /tmp/ghp && git -C /tmp/ghp checkout gh-pages`.

Mirror the fresh export into `books/<slug>/`. The `--delete` is **scoped to that one
book's dir**, so it prunes stale files from this book without touching other books:

```bash
git -C /tmp/ghp pull --ff-only                     # sync with origin first
mkdir -p /tmp/ghp/books/a-christmas-carol
rsync -a --delete output/a-christmas-carol/ /tmp/ghp/books/a-christmas-carol/
```

Rebuild the root landing page from every book deployed under `books/*/`:

```bash
python3 -m webapp.build_index /tmp/ghp
```

Then commit and push:

```bash
git -C /tmp/ghp add -A
git -C /tmp/ghp status --short                      # sanity-check the diff
git -C /tmp/ghp commit -m "Deploy A Christmas Carol (books/a-christmas-carol)"
git -C /tmp/ghp push origin gh-pages
```

Pages rebuilds within a minute or so. Verify the deploy:

```bash
gh api repos/achatham/storyteller-app/pages          # status should be "built"
```

`build_index` writes `.nojekyll` at the root (and each export writes one too), so
GitHub Pages serves the files verbatim (no Jekyll).

---

## Migrating the old root URL

The old layout served A Christmas Carol at the bare root
(`…/storyteller-app/`). Under the new layout the root is the landing page and Carol
lives at `…/storyteller-app/books/a-christmas-carol/`. To migrate, deploy Carol with
the command above; the first deploy leaves the old root files in place until you
clear them. To fully switch over, delete the stale book files from the root of the
deploy clone (keep `books/`, `index.html`, `.nojekyll`) and push. If you want the
bare root URL to keep working, drop a redirecting `index.html` — but the generated
landing page is the intended root going forward.

---

## Quick reference

| Thing | Value |
|-------|-------|
| One-command deploy | `./deploy.sh <id> <slug> "<Title>" "<Author>"` |
| App DB | `data/storyteller.db` (set `STORY_APP_DB`) |
| Exporter | `python3 -m webapp.export <id> <out> --slug <slug> [--title --author --pages]` |
| Index builder | `python3 -m webapp.build_index <site_dir>` |
| Book URL | `…/storyteller-app/books/<slug>/` |
| Viewer template | `export.html` (`INTRO` block) |
| Landing template | `webapp/static/index.html` |
| Per-book manifest | `book.json` (title, author, style, slug, cover) |
| Deploy clone | `/tmp/ghp` (branch `gh-pages`, served at root) |
| Live site | <https://achatham.github.io/storyteller-app/> |
| Carol demo | book id `16`, style `crayon_childlike`, 152 pages |
