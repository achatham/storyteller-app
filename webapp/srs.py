"""Spaced repetition for vocabulary words, on FSRS (the `fsrs` package).

Reviews happen at reading time with a child, so at most about once a day. FSRS's
default minute-scale learning steps ("see it again in 10 minutes") make no sense
there, so they are off: the very first grade already schedules in days. With the
default parameters that is: knew it -> 2 days, then ~11, ~46, ...; didn't know it
-> tomorrow, and a miss later on shortens the interval again.

Two buttons, mapped onto FSRS's four ratings: "knew it" = Good, "didn't know" =
Again. Each word's card is stored as JSON on its vocab row, and every review also
goes to vocab_reviews, so per-child parameters can later be fitted with the FSRS
optimizer once there is enough history.
"""
import json
import threading
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

from . import db

SCHEDULER = Scheduler(learning_steps=(), relearning_steps=())
_lock = threading.Lock()   # read-modify-write of a word's card


def review(vocab_id: int, knew: bool, now: datetime | None = None) -> dict | None:
    """Grade one word and reschedule it. Returns the word's new schedule, or None
    if there is no such word."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        w = db.vocab_get(vocab_id)
        if not w:
            return None
        card = Card.from_json(w["srs"]) if w["srs"] else Card(card_id=vocab_id)
        rating = Rating.Good if knew else Rating.Again
        card, log = SCHEDULER.review_card(card, rating, now)
        due = card.due.timestamp()
        db.vocab_record_review(vocab_id, w["book_id"], card.to_json(), due, int(rating),
                               json.dumps(log.to_dict()), now.timestamp())
    return {"id": vocab_id, "due_at": due, "reviewed_at": now.timestamp(),
            "reviews": (w["reviews"] or 0) + 1,
            "interval_days": round((due - now.timestamp()) / 86400, 1)}
