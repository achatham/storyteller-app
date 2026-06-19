# Regenerating & redeploying the static demo

How to rebuild the self-contained static book viewer and publish it to the live
GitHub Pages demo at <https://achatham.github.io/storyteller-app/>.

The exporter (`webapp/export.py`) writes a single `index.html` with the book text
inlined and the illustrations as `images/*.webp` — no backend needed to read it.
Pages serves the **root of the `gh-pages` branch**, deployed from a separate clone.

---

## 1. (Optional) Edit the intro / welcome screen

The viewer's welcome card lives in the `INTRO` block near the top of the `<script>`
in `webapp/static/export.html`. Edit `heading`, the `paragraphs` array, and the
`github` link there. These tokens are filled in per book at view time:

| token       | becomes                                   |
|-------------|-------------------------------------------|
| `{title}`   | the book's title                          |
| `{style}`   | the book's art style (underscores → spaces) |
| `{project}` | a link using the `github: { url, label }` config |

`export.html` is a **template** — changes only take effect when you re-export (step 2).

## 2. Export the book from the database

The app DB is `data/storyteller.db` (the default export path points elsewhere, so set
`STORY_APP_DB` explicitly):

```bash
STORY_APP_DB=data/storyteller.db \
  python3 -m webapp.export <book_id> output/<name> \
  --title "<Title>" --author "<Author>"
```

Notes:
- Book titles/authors are usually **blank in the DB**, so pass `--title`/`--author`.
- Add `--pages N` to export only the first N pages (a short demo slice).
- `output/` is git-ignored — it's just a scratch build dir.
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
  python3 -m webapp.export 16 output/carol-pages \
  --title "A Christmas Carol" --author "Charles Dickens"
```

(Book `16`, style `crayon_childlike`, 152 illustrated pages.)

## 3. Publish to GitHub Pages

Pages is served from the **`gh-pages` branch root**, deployed from a dedicated clone
at `/tmp/ghp` (a full separate clone, *not* a worktree of the main repo). If it's
missing, recreate it with `git clone <repo> /tmp/ghp && git -C /tmp/ghp checkout gh-pages`.

Mirror the fresh export into the clone (the `--delete` drops files no longer
exported; `--exclude=.git` protects the repo metadata), then commit and push:

```bash
git -C /tmp/ghp pull --ff-only                     # sync with origin first
rsync -a --delete --exclude='.git' output/carol-pages/ /tmp/ghp/
git -C /tmp/ghp add -A
git -C /tmp/ghp status --short                      # sanity-check the diff
git -C /tmp/ghp commit -m "Redeploy A Christmas Carol"
git -C /tmp/ghp push origin gh-pages
```

Pages rebuilds within a minute or so. Verify the deploy:

```bash
gh api repos/achatham/storyteller-app/pages          # status should be "built"
```

The export writes `.nojekyll`, so GitHub Pages serves the files verbatim (no Jekyll).

---

## Quick reference

| Thing | Value |
|-------|-------|
| App DB | `data/storyteller.db` (set `STORY_APP_DB`) |
| Exporter | `python3 -m webapp.export <id> <out> [--title --author --pages]` |
| Intro text | `INTRO` block in `webapp/static/export.html` |
| Deploy clone | `/tmp/ghp` (branch `gh-pages`, served at root) |
| Live demo | <https://achatham.github.io/storyteller-app/> |
| Carol demo | book id `16`, style `crayon_childlike`, 152 pages |
