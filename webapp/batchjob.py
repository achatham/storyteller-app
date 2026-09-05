"""One image-generation batch, end to end, with resume bookkeeping.

Both batched drawers (webapp/batch_bake for pages, webapp/batch_roster for reference
sheets) need the same thing: submit a set of image requests to the Batch API -- or
reattach to the job a previous run of this same step already submitted -- wait for
it, and collect the images. `run_image_batch` is that step. Everything else about
a bake (critique, verify, judge) is interactive -- `run_text_parallel` -- because
text is cheap enough that queueing it would only add latency (a 114-minute critique
queue once saved six cents).

Resume: a batch job runs server-side, so a killed worker (restart, redeploy) doesn't
cancel it. Each step is keyed (book, round, kind) in the batch_jobs table; a rerun
finds the recorded job and reattaches instead of paying for the same batch twice. A
job that ended FAILED/CANCELLED/EXPIRED has no results to collect, so the only useful
move is a fresh submit.
"""
import os
from concurrent.futures import ThreadPoolExecutor

from pipeline import gem, costs

from . import db


def run_image_batch(book_id: int, round: int, kind: str, reqs: list[dict], model: str,
                    display: str, log=print) -> dict | None:
    """Generate `reqs` (see gem.batch_generate_images) with `model` in one batch and
    return {key: bytes | gem.ImageRefused | None} (see gem.batch_image_results), or
    None if the job ended in a non-success state (the caller retries next step)."""
    tag = f"r{round} {kind}" if round >= 0 else kind
    existing = db.bjob_get(book_id, round, kind)
    if existing and existing["job_name"] and existing["state"] in gem.BATCH_TERMINAL \
            and existing["state"] != gem.BATCH_DONE:
        log(f"{tag}: last job {existing['job_name']} ended {existing['state']} "
            "-- submitting a fresh one")
        existing = None
    if existing and existing["job_name"]:
        job = existing["job_name"]
        log(f"{tag}: reattaching {job} ({existing['state']})")
    else:
        job = gem.batch_generate_images(reqs, model=model, display_name=display)
        db.bjob_upsert(book_id, round, kind, job, "JOB_STATE_PENDING")
        log(f"{tag}: submitted {job} ({len(reqs)} reqs, model={model})")
    # recorded (or re-asserted on reattach) for the outstanding-requests indicator
    db.batch_req_add(book_id, job, len(reqs))
    st = gem.batch_wait(job, on_state=lambda s: db.bjob_set_state(book_id, round, kind, s))
    if st != gem.BATCH_DONE:
        log(f"{tag}: {st}")
        return None
    return gem.batch_image_results(job, model)


# Interactive text calls (critique / verify / judge) issued by the batched drawers
# run in parallel: each is a few seconds, so a whole round scores in a minute or two.
TEXT_WORKERS = int(os.environ.get("STORY_TEXT_WORKERS", "8"))


def run_text_parallel(book_id: int, items: dict, fn, log=print, what: str = "critique") -> dict:
    """{key: fn(key, item)} over `items` in parallel interactive calls, each attributed
    to the book's cost run (costs.run_as is thread-local, so the pool re-enters it). A
    call that raises is logged and its key left out -- callers treat a missing verdict
    as "unscored", never as a failure of the whole step."""
    if not items:
        return {}

    def one(kv):
        k, v = kv
        with costs.run_as(f"book:{book_id}"):
            try:
                return k, fn(k, v)
            except Exception as ex:  # noqa: BLE001 -- one unscorable item mustn't sink the step
                log(f"{what} failed for {k}: {str(ex)[:160]}")
                return k, None

    out = {}
    with ThreadPoolExecutor(max_workers=TEXT_WORKERS) as ex:
        for k, res in ex.map(one, items.items()):
            if res is not None:
                out[k] = res
    return out
