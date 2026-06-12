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

## Run without Docker

```sh
pip install -r requirements.txt
uvicorn webapp.server:app --port 8000
```

Reads `GEMINI_API_KEY` from `.env` (same as the CLI pipeline).

## How it works

- **Upload** (`POST /api/books`) stores the file in SQLite and spawns
  `python -m webapp.process <id>`, which reuses the env-driven pipeline to:
  extract text → build the entity **registry** → draw a canonical **roster**
  reference sheet per entity/variant (pro image model) → **segment** each chapter
  into read-aloud pages (roster and segmentation run concurrently). Finally it
  **warms the first `STORY_WARM_PAGES` page images** (default 2) so the book opens
  instantly. Status is written back to the DB and shown live on the hub.
- **Reading** the page image endpoint generates each scene on demand (flash image
  model, using the roster sheets as references) and **prefetches the next
  `STORY_PREFETCH` pages** (default 2) in the background, so turning the page is
  instant once you're moving. Duplicate work is coalesced by a per-page lock;
  `STORY_GEN_CONCURRENCY` (default 3) caps simultaneous generations.
- **Progress** is stored server-side per book (`PUT /api/books/{id}/progress`), so
  opening a book picks up where you left off on any device.

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `GEMINI_API_KEY` | — | required |
| `STORY_APP_DB` | `output/storyteller.db` | main SQLite DB (books + art) |
| `STORY_WARM_PAGES` | `2` | first pages drawn during import (instant open) |
| `STORY_PREFETCH` | `2` | pages drawn ahead while reading |
| `STORY_GEN_CONCURRENCY` | `3` | max simultaneous image generations |
| `STORY_SHEET_IMAGE_MODEL` | `gemini-3-pro-image-preview` | roster sheets |
| `STORY_PAGE_IMAGE_MODEL` | `gemini-3.1-flash-image` | page scenes |

Per-book choices (art style, illustration cadence, audience age) are set in the
upload form. Token/image costs are still recorded to `costs.db`
(`python -m pipeline.costs`).
