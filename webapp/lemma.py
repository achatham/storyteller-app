"""The headword a saved vocabulary word is learned and reviewed under.

A long-press saves whatever form is on the page -- "devoured", "attentively",
"leaves" -- but the thing to learn is the word itself: "devour", "attentive",
and "leaf" or "leave" depending on the sentence. Without this, every tense and
plural would become its own card and its own review. Rules can't do it well
(irregular verbs, homographs, -ly words whose base means something else, like
"hardly"), so the text model picks the headword, reading the sentence.

The headword is what a word is deduplicated on (one per kid, see db.vocab_add)
and what gets defined and reviewed; every form actually seen is kept beside it
(vocab.forms) so the reader can still highlight the words as printed. If the
model call fails the form itself is used, so saving a word never fails on this.
"""
from pipeline import costs, gem
from pipeline.config import DEFINE_MODEL

from . import db

# The reader waits on this before it highlights the word, so a slow model must not
# hold the save: past this the printed form is used. One attempt, and 10s is the
# shortest deadline the Gemini API accepts (it 400s anything lower).
HEADWORD_TIMEOUT = 10.0

SCHEMA = {"type": "object", "properties": {"headword": {"type": "string"}},
          "required": ["headword"]}

PROMPT = """A child reading a book met the word "{form}" in this sentence: "{context}"

Give the headword the child should learn and review: the dictionary word for the
meaning used here.
- Undo inflections: plurals, verb tenses and participles, comparatives
  (cutlasses -> cutlass, devoured -> devour, gloomier -> gloomy, leaves -> leaf
  or leave, whichever this sentence means).
- Also drop a derivational ending when the base word means the same thing and is
  the more basic word to learn, e.g. an -ly adverb from an adjective
  (attentively -> attentive).
- Keep the word as it is when the shorter form would change the meaning (hardly
  is not "hard") or isn't a common word, and when it is a form of a different,
  very common word (better, worse, went stay as they are).
- Leave names and words made up for the story as they are.
Reply with the single headword in lowercase.
"""


def _clean(raw: str) -> str | None:
    w = str(raw or "").strip().lower().strip(".,;:!?\"“”'’")
    if not w or len(w) > 64 or not w[0].isalpha():
        return None
    if not all(ch.isalpha() or ch in "'’-" for ch in w):   # one word, no phrases
        return None
    return w


def headword(form: str, context: str | None = None) -> str:
    """The headword for `form` as used in `context`; `form` itself on failure."""
    try:
        with costs.run_as("vocab"):
            out = gem.text_json(PROMPT.format(form=form, context=context or form), SCHEMA,
                                model=DEFINE_MODEL, thinking_level="low",
                                timeout=HEADWORD_TIMEOUT, tries=1)
        return _clean(out.get("headword")) or form
    except Exception as e:  # noqa: BLE001 -- never let this block a save
        print(f"  [lemma] {form!r} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return form


def backfill() -> int:
    """Give headwords to words saved before they existed (vocab.forms is NULL):
    the saved word becomes its first form, the model's headword becomes the word,
    and a headword the kid already has merges into that row. Safe to re-run;
    returns how many words were looked at."""
    rows = db.vocab_without_forms()
    for w in rows:
        db.vocab_set_headword(w["id"], headword(w["word"], w["context"]))
    if rows:
        print(f"  [lemma] backfilled headwords for {len(rows)} word(s)", flush=True)
    return len(rows)
