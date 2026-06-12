"""Per-section configuration, driven by environment variables so the same
pipeline can target any book / page range / output directory / art style.

  Section / output:
    STORY_OUT      output directory (default output/enders_game)
    STORY_PAGES    inclusive 1-based PDF page range for THIS section, "35,54"
    STORY_LABEL    human label for the section, e.g. "1. Third"

  Which book (so the prompts never hard-code a specific title):
    STORY_PDF      path to the source PDF
    STORY_BOOK     display title (blank => prompts stay generic, "this novel")
    STORY_AUTHOR   author name (used in prompts + the book footer)
    STORY_BODY     whole-book body page range for the registry, "start,end".
                   end may be <=0 to offset from the last page (e.g. -3 trims
                   trailing ad pages).
    STORY_REGISTRY path to the book's entity registry json
    STORY_ASSETS   dir of the book's shared canonical reference sheets

  Look:
    STORY_STYLE    art-style key (see STYLES below), default "watercolor"
    STORY_AGE      read-aloud audience age, default "5"
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("STORY_OUT", str(ROOT / "output" / "enders_game")))
_p = os.environ.get("STORY_PAGES", "35,54").split(",")
PAGES = (int(_p[0]), int(_p[1]))
LABEL = os.environ.get("STORY_LABEL", "1. Third")

# ---- which book ----
# STORY_PDF may point at a .pdf or a (decrypted) .epub; the format is detected
# from the suffix. PDFs are sectioned by page range (STORY_PAGES / STORY_BODY);
# epubs are sectioned by chapter index (STORY_CHAPTERS, 1-based inclusive).
PDF = Path(os.environ.get("STORY_PDF", str(ROOT / "stories" / "Ender's Game.pdf")))
IS_EPUB = PDF.suffix.lower() == ".epub"
BOOK_TITLE = os.environ.get("STORY_BOOK", "")
BOOK_AUTHOR = os.environ.get("STORY_AUTHOR", "")
_b = os.environ.get("STORY_BODY", "35,-3").split(",")
BODY_PAGES = (int(_b[0]), int(_b[1]))
_c = os.environ.get("STORY_CHAPTERS", "1,1").split(",")
CHAPTERS = (int(_c[0]), int(_c[1]))

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "candidates").mkdir(exist_ok=True)
(OUT / "characters").mkdir(exist_ok=True)
(OUT / "scenes").mkdir(exist_ok=True)

# Book-level registry + shared canonical sheets live outside any one section.
# Per-book so switching books does not mix one book's sheets into another.
REGISTRY = Path(os.environ.get("STORY_REGISTRY", str(ROOT / "output" / "registry.json")))
ASSETS = Path(os.environ.get("STORY_ASSETS", str(ROOT / "output" / "assets" / "sheets")))
ASSETS.mkdir(parents=True, exist_ok=True)

# A book-agnostic reference phrase for prompts. If a title is configured the
# book is named; otherwise prompts stay generic ("this novel") so the image /
# text model does not latch onto any particular known book or its cover art.
if BOOK_TITLE and BOOK_AUTHOR:
    BOOK_REF = f"{BOOK_AUTHOR}'s *{BOOK_TITLE}*"
elif BOOK_TITLE:
    BOOK_REF = f"*{BOOK_TITLE}*"
else:
    BOOK_REF = "this novel"
TITLE_OUT = BOOK_TITLE or "Untitled"

# ---- selectable art styles ----
# Each is a complete style prefix prepended to every image prompt. Pick with
# STORY_STYLE=<key>. Add new looks here; the rest of the pipeline is agnostic.
STYLES = {
    "watercolor": (
        "Warm classic children's picture-book watercolor: soft washes, gentle "
        "outlines, cozy lighting, slightly muted but rich palette, visible paper "
        "texture, expressive friendly faces. Timeless storybook feel, NOT "
        "cartoonish, NOT photorealistic, NOT 3D render."
    ),
    "ink_and_wash": (
        "Classic golden-age storybook illustration: fine pen-and-ink linework with "
        "delicate watercolor washes laid over it, warm and detailed, an antique "
        "fairy-tale book feel. Expressive faces, rich but gentle color. NOT "
        "cartoonish, NOT photorealistic, NOT 3D render."
    ),
    "bold_picturebook": (
        "Bold modern picture-book illustration: clean confident shapes, thick "
        "gentle outlines, flat saturated cheerful colors, simple expressive faces, "
        "playful and graphic. Friendly and contemporary. NOT photorealistic, NOT "
        "3D render."
    ),
    "oil_painterly": (
        "Rich painterly storybook illustration with an oil-paint feel: visible "
        "brush strokes, warm dramatic lighting, deep luminous color, a sense of "
        "grandeur and wonder. Classic and timeless. NOT cartoonish, NOT "
        "photorealistic, NOT 3D render."
    ),
    "soft_pastel": (
        "Soft chalk-pastel children's illustration: dreamy diffuse edges, gentle "
        "grainy texture, light airy palette, tender cozy mood. Calm and comforting. "
        "NOT cartoonish, NOT photorealistic, NOT 3D render."
    ),
    "vintage_midcentury": (
        "Mid-century retro children's-book illustration: limited flat palette of a "
        "few warm hues, textured screen-print look, stylized simplified shapes, "
        "charming and nostalgic. NOT photorealistic, NOT 3D render."
    ),
}
STYLE = os.environ.get("STORY_STYLE", "watercolor")
if STYLE not in STYLES:
    raise SystemExit(
        f"Unknown STORY_STYLE={STYLE!r}. Choose one of: {', '.join(STYLES)}")
ART_STYLE = STYLES[STYLE]

# ---- image models + output resolution ----
# Two image models by role: the roster/character reference SHEETS use the
# higher-fidelity "pro" model (canonical consistency matters most there), while
# the many per-page SCENE illustrations use the cheaper "flash" model.
SHEET_IMAGE_MODEL = os.environ.get("STORY_SHEET_IMAGE_MODEL", "gemini-3-pro-image-preview")
PAGE_IMAGE_MODEL = os.environ.get("STORY_PAGE_IMAGE_MODEL", "gemini-3.1-flash-image")
# general default (used when a caller doesn't specify) = the page model
IMAGE_MODEL = os.environ.get("STORY_IMAGE_MODEL", PAGE_IMAGE_MODEL)
TEXT_MODEL = os.environ.get("STORY_TEXT_MODEL", "gemini-3.5-flash")
MAX_REFS = int(os.environ.get("STORY_MAX_REFS", "4"))
# Gemini image_size is a discrete enum: "1K", "2K", "4K". Each step doubles
# each dimension. "1K" (~1280px long edge) keeps files small for remote loading.
IMAGE_SIZE = os.environ.get("STORY_IMAGE_SIZE", "1K")

# ---- illustration cadence ----
# Roughly one illustration (one read-aloud "page") per this many words of text.
# Lower = more pictures, more often (good for little kids: ~100); higher = fewer
# (older kids: ~300-500). Drives how finely analyze segments a section.
WORDS_PER_PAGE = int(os.environ.get("STORY_WORDS_PER_PAGE", "200"))

# ---- audience / content ----
AUDIENCE_AGE = os.environ.get("STORY_AGE", "5")
VIOLENCE_POLICY = (
    f"This book is read aloud to a {AUDIENCE_AGE}-year-old. SOFTEN all violence: "
    "depict tension, confrontation, and emotion (a standoff, a brave stance, "
    "worried faces) but NEVER show graphic injury, blood, or a child being "
    "struck. Imply conflict rather than depicting it."
)
