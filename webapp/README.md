# Storyteller web app

A backend + reader UI on top of the illustration pipeline. Upload a book (PDF or
EPUB), it builds the character roster and splits the book into read-aloud pages;
then you read it page-by-page with illustrations drawn lazily as you go. All
state — books, page text, roster art, and page images — lives in one SQLite DB.

## Run with Docker

```sh
echo "GEMINI_API_KEY=your-key-here" > .env      # if you don't already have one
docker compose up --build
# open http://localhost:8000
```

`./data` (a mounted volume) holds the SQLite DB (`storyteller.db`), per-book
scratch dirs, and processing logs, so uploaded books and generated art survive
restarts.

## HTTPS on phones/tablets (PWA install + keep-awake)

Installing to the home screen and the screen wake-lock require a **secure
context** — HTTPS or `localhost`. Over the LAN by IP (plain HTTP) browsers
disable both. If the host machine is on Tailscale, expose the app over a trusted
**private** HTTPS name with one command (no public exposure, no per-device cert):

```sh
# one-time in the Tailscale admin console: enable Serve, MagicDNS, and HTTPS Certificates
sudo tailscale serve --bg --https=443 http://127.0.0.1:8200   # 8200 = the published host port
# then, on any device on your tailnet, open  https://<host>.<tailnet>.ts.net  ->  Install
```

Keep it on **Serve**, not Funnel — Funnel would expose it to the public internet.

## Run without Docker

```sh
pip install -r requirements.txt
uvicorn webapp.server:app --port 8000
```

Reads `GEMINI_API_KEY` from `.env` (same as the CLI pipeline).

## How it works

- **Upload** (`POST /api/books`) stores the file in SQLite and spawns
  `python -m webapp.process <id>`, which reuses the env-driven pipeline to:
  extract text → build the entity **registry** (the character/variant catalogue,
  on the strong text model at `thinking_level=high`) → **segment** each chapter
  into read-aloud pages. Finally it **warms the first `STORY_WARM_PAGES` page
  images** (default 2) so the book opens instantly. Status is shown live on the hub.
- **Roster reference sheets are drawn lazily** (pro image model), the first time a
  page needs a given character/variant — so a variant no read page uses is never
  drawn. When a character already has one sheet, a sibling variant is attached as a
  reference when drawing another, keeping the same face/build across their looks.
- **Reading** the page image endpoint generates each scene on demand (flash image
  model, using the roster sheets as references) and **prefetches the next
  `STORY_PREFETCH` pages** (default 4) in the background; the reader also pre-loads
  those into the browser cache, so turning the page is instant. Duplicate work is
  coalesced by a per-page lock; `STORY_GEN_CONCURRENCY` (default 3) caps it.
- **Progress** is stored server-side per book (`PUT /api/books/{id}/progress`), so
  opening a book picks up where you left off on any device.

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `GEMINI_API_KEY` | — | required |
| `STORY_APP_DB` | `output/storyteller.db` | main SQLite DB (books + art) |
| `STORY_WARM_PAGES` | `2` | first pages drawn during import (instant open) |
| `STORY_PREFETCH` | `4` | pages drawn ahead while reading |
| `STORY_GEN_CONCURRENCY` | `3` | max simultaneous image generations |
| `STORY_SHEET_IMAGE_MODEL` | `gemini-3-pro-image-preview` | roster sheets |
| `STORY_PAGE_IMAGE_MODEL` | `gemini-3.1-flash-image` | page scenes |
| `STORY_TEXT_MODEL` | `gemini-3.5-flash` | default for all text steps |
| `STORY_ANALYZE_MODEL` | = text model | segmentation (page anchors + briefs) |
| `STORY_REGISTRY_MODEL` | = text model | entity discovery/expansion (identity-critical) |
| `STORY_REGISTRY_THINK` | `high` | thinking level for the registry step (minimal/low/medium/high) |
| `STORY_CRITIQUE_MODEL` | = text model | image quality scoring (low-sensitivity) |
| `STORY_CHAPTER_MODEL` | = text model | chapter skeleton classification (low-sensitivity) |

Per-book choices (art style, illustration cadence, audience age) are set in the
upload form. Token/image costs are recorded to `costs.db` (`python -m pipeline.costs`).

**Cost note.** Segmentation no longer makes the model retype the book: it returns
only each page's *start anchor* (a short verbatim snippet) and the page text is
sliced from the source in code (exact, no hallucination). Because that step is now
low-stakes, the low-sensitivity text steps can run on a cheaper model — e.g. set
`STORY_ANALYZE_MODEL`, `STORY_CRITIQUE_MODEL`, and `STORY_CHAPTER_MODEL` to
`gemini-3.1-flash-lite` for ~80% off the text spend. Keep `STORY_REGISTRY_MODEL`
on the stronger model (it pins character identity for the whole book).
