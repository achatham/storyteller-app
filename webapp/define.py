"""Child-friendly definitions for vocabulary words, generated on demand and cached.

A definition is for a word *as it was used* -- "spot" in "the black spot" is not
"spot" in "a spot of tea" -- so the cache key is the word plus a hash of the
sentence it was saved from (and PROMPT_VERSION, so rewording the prompt starts a
fresh cache rather than mixing old and new definitions). The same word saved from
a different sentence gets its own definition; at a fraction of a cent each that is
cheaper than ever showing a child the wrong meaning.

The cache lives in Firestore when STORY_FIRESTORE_PROJECT is set (credentials via
GOOGLE_APPLICATION_CREDENTIALS), so it survives this box and is shared by anything
else that wants it. Without it, the same records go to a table in the app's SQLite
DB, so the feature works before Firestore is set up. A Firestore outage never fails
a request: the definition is generated and served, and caching is retried next time.
"""
import hashlib
import os
import threading
import time

from pipeline import costs, gem
from pipeline.config import DEFINE_MODEL

from . import db

PROMPT_VERSION = 1
FIRESTORE_PROJECT = os.environ.get("STORY_FIRESTORE_PROJECT", "")
FIRESTORE_DATABASE = os.environ.get("STORY_FIRESTORE_DATABASE", "(default)")
FIRESTORE_COLLECTION = os.environ.get("STORY_FIRESTORE_COLLECTION", "vocab_definitions")

SCHEMA = {
    "type": "object",
    "properties": {
        "definition": {"type": "string"},
        "part_of_speech": {"type": "string"},
        "example": {"type": "string"},
    },
    "required": ["definition", "part_of_speech", "example"],
}

PROMPT = """A parent is reading a book aloud to a young child (about 5 to 8 years
old) and the child asked what a word means. Explain it the way a kind, clear
grown-up would.

Word: {word}
The sentence it appeared in{book}: "{context}"

Give the meaning the word has IN THAT SENTENCE (if it has several meanings, pick
the one used there).

- definition: one or two short sentences a six-year-old understands. Use plain,
  everyday words and something concrete they know. Do not use the word itself or
  any harder word to explain it. Only call a word old-fashioned ("an old way of
  saying ...") if people today hardly ever use it.
- If the word is about something frightening, violent or sad, explain it honestly
  but gently, without gory or scary detail.
- part_of_speech: noun, verb, adjective, adverb, etc., for how it is used here.
- example: a new short sentence using the word the same way, about everyday
  kid life -- not a sentence from the book. For a frightening or violent word,
  don't put it in a child's own life or a cartoon; use a calm, factual sentence
  instead (e.g. about history, the news, or a story's rules).
"""


def cache_key(word: str, context: str | None) -> str:
    """Firestore document id / SQLite key. Readable word first; doc ids may not
    contain '/', and saved words are letters, digits, apostrophes and hyphens."""
    ctx = " ".join((context or "").split())
    h = hashlib.sha256(f"v{PROMPT_VERSION}\n{ctx}".encode()).hexdigest()[:16]
    return f"{word}~{h}"


# ---- cache backends ----

class SqliteCache:
    def get(self, key):
        return db.definition_get(key)

    def put(self, key, record):
        db.definition_put(key, record)


class FirestoreCache:
    def __init__(self, project, database, collection):
        from google.cloud import firestore   # only needed when configured
        self._col = firestore.Client(project=project, database=database).collection(collection)

    def get(self, key):
        snap = self._col.document(key).get()
        return snap.to_dict() if snap.exists else None

    def put(self, key, record):
        self._col.document(key).set(record)


_cache = None
_cache_lock = threading.Lock()


def cache():
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = (FirestoreCache(FIRESTORE_PROJECT, FIRESTORE_DATABASE, FIRESTORE_COLLECTION)
                      if FIRESTORE_PROJECT else SqliteCache())
        return _cache


def backend() -> str:
    return "firestore" if FIRESTORE_PROJECT else "sqlite"


# ---- lookup ----

# One generation per key at a time: the vocab page asks for several words at
# once and a reader can long-press the same word twice before the first answer.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _lock_for(key):
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _generate(word, context, title):
    prompt = PROMPT.format(word=word, context=context or word,
                           book=f' (from the book "{title}")' if title else "")
    with costs.run_as("vocab"):
        out = gem.text_json(prompt, SCHEMA, model=DEFINE_MODEL, thinking_level="low")
    return {
        "word": word,
        "context": context or "",
        "definition": str(out.get("definition", "")).strip(),
        "part_of_speech": str(out.get("part_of_speech", "")).strip(),
        "example": str(out.get("example", "")).strip(),
        "model": DEFINE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "created_at": time.time(),
    }


def _cache_call(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 -- a cache problem must not fail the lookup
        print(f"  [define] {backend()} cache {fn.__name__} failed: "
              f"{type(e).__name__}: {str(e)[:160]}", flush=True)
        return None


def define(word: str, context: str | None = None, title: str | None = None) -> dict:
    """The cached definition of `word` as used in `context`, generating (and
    caching) it on first request. Adds `cached`: whether it came from the cache."""
    key = cache_key(word, context)
    with _lock_for(key):
        c = _cache_call(cache)
        hit = c and _cache_call(c.get, key)
        if hit and hit.get("definition"):
            return {**hit, "cached": True}
        rec = _generate(word, context, title)
        if not rec["definition"]:
            raise ValueError("empty definition")
        if c:
            _cache_call(c.put, key, rec)
        return {**rec, "cached": False}
