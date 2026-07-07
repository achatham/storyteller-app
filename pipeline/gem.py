"""Thin Gemini wrappers: structured text, image generation, vision critique.

A single shared Budget caps the number of image-generation calls across the
whole run (the user asked for a hard ceiling of 100 candidates).

Batch helpers (batch_submit / batch_poll) drive Google's Batch API, which bills
at a flat 50% of interactive pricing but runs asynchronously (target <=24h). They
back the "illustrate the whole book" bake in the webapp.
"""
import base64
import io
import json
import os
import tempfile
import time
import threading
from pathlib import Path

from google import genai
from google.genai import types

from . import costs
from .config import (IMAGE_SIZE, IMAGE_MODEL, TEXT_MODEL, SHEET_IMAGE_MODEL,
                     PAGE_IMAGE_MODEL, CRITIQUE_MODEL, WEBP_QUALITY)

ROOT = Path(__file__).resolve().parent.parent


def _record_usage(resp, model: str, kind: str, images: int = 0, batch: bool = False):
    """Log one call's token usage + cost to the cumulative SQLite DB. Never let
    accounting break a generation. batch=True prices at the 50% Batch-API rate."""
    try:
        u = getattr(resp, "usage_metadata", None)
        pin = (getattr(u, "prompt_token_count", 0) or 0) if u else 0
        pout = (getattr(u, "candidates_token_count", 0) or 0) if u else 0
        tot = (getattr(u, "total_token_count", 0) or 0) if u else 0
        costs.record(model, kind, pin, pout, tot or (pin + pout), images=images, batch=batch)
    except Exception as e:  # noqa: BLE001
        print(f"  [costs] record failed: {type(e).__name__}: {str(e)[:100]}", flush=True)


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# TEXT_MODEL / IMAGE_MODEL come from config (env-overridable); imported above.


class Budget:
    """Thread-safe cap on total image-generation candidates."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def remaining(self) -> int:
        with self._lock:
            return self.limit - self.used


def _retry(fn, tries=4, base=4.0, what="call"):
    last = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            wait = base * (2 ** attempt)
            print(f"  [retry] {what} failed ({type(e).__name__}: {msg[:120]}); "
                  f"sleeping {wait:.0f}s")
            time.sleep(wait)
    raise last


def _coerce_json(raw: str | None) -> dict:
    """Parse a JSON object from a model response, raising (so the caller's
    retry kicks in) when the response is empty/blocked/truncated."""
    if not raw or not raw.strip():
        raise ValueError("empty model response (no text — blocked or truncated?)")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e == -1 or e < s:
            raise ValueError(f"no JSON object in response: {raw[:160]!r}")
        return json.loads(raw[s:e + 1])


def text_json(prompt: str, schema: dict | None = None, model: str = TEXT_MODEL,
              thinking_level: str | None = None) -> dict:
    """Call the text model and parse a JSON object out of the response.
    Empty/garbage responses are retried (not crash-on-None).

    thinking_level (Gemini 3 models): minimal|low|medium|high. None = model
    default (dynamic). Raise it for reasoning-heavy steps like the registry."""
    kwargs = dict(response_mime_type="application/json", response_schema=schema,
                  temperature=0.6)
    if thinking_level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
    cfg = types.GenerateContentConfig(**kwargs)

    def _go():
        resp = _client.models.generate_content(model=model, contents=prompt, config=cfg)
        _record_usage(resp, model, "text")
        return _coerce_json(resp.text)

    return _retry(_go, what="text_json")


def generate_image(prompt: str, refs: list[Path] | None = None, out_path: Path | None = None,
                   aspect: str = "3:2", size: str = IMAGE_SIZE,
                   model: str = IMAGE_MODEL) -> Path | None:
    """Generate one image with `model`. `refs` are reference images (sheets)."""
    from PIL import Image
    contents: list = [prompt]
    for r in (refs or []):
        contents.append(Image.open(r))
    cfg = types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size),
    )

    def _go():
        resp = _client.models.generate_content(model=model, contents=contents, config=cfg)
        _record_usage(resp, model, "image", images=1)
        return resp

    resp = _retry(_go, what="generate_image")
    import io
    for part in resp.parts:
        if part.inline_data is not None:
            if out_path is None:
                return part.as_image()
            pil = Image.open(io.BytesIO(part.inline_data.data))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.suffix.lower() == ".webp":
                pil.convert("RGB").save(str(out_path), "WEBP", quality=WEBP_QUALITY, method=6)
            else:
                pil.save(str(out_path))
            return out_path
    return None


def critique_image(image_path: Path, brief: str, refs: list[Path] | None = None,
                   ref_labels: list[str] | None = None, schema: dict | None = None,
                   model: str = CRITIQUE_MODEL) -> dict:
    """Vision-model critique of a generated image against its brief.

    If `refs` are given they are attached AFTER the judged image as the canonical
    reference sheets for the named characters (labelled, in order via `ref_labels`),
    so the critic can check whether the figures in the image are actually the RIGHT
    people -- catching a totally-wrong face -- rather than only matching them against
    a text description."""
    from PIL import Image
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.3,
    )

    def _build(use_refs: bool) -> list:
        contents = [brief]
        if use_refs and refs:
            contents.append("THE IMAGE TO JUDGE:")
        contents.append(Image.open(image_path))
        if use_refs and refs:
            contents.append(
                "CANONICAL CHARACTER REFERENCE SHEETS follow -- each shows what one named "
                "character is supposed to look like. Compare the figures in the image above "
                "against them to judge `figure_match`:")
            for i, r in enumerate(refs):
                label = ref_labels[i] if ref_labels and i < len(ref_labels) else f"character {i + 1}"
                contents.append(f"--- Reference: {label} ---")
                contents.append(Image.open(r))
        return contents

    def _go(contents):
        resp = _client.models.generate_content(model=model, contents=contents, config=cfg)
        _record_usage(resp, model, "critique")
        return _coerce_json(resp.text)

    try:
        return _retry(lambda: _go(_build(True)), what="critique")
    except Exception:
        # A multi-image critique (judged image + several reference sheets) occasionally
        # comes back empty/blocked where the single-image call succeeds. Rather than
        # fail the page, fall back to judging the image alone -- we lose only the
        # reference-based `figure_match` fidelity (it scores leniently without sheets).
        if not refs:
            raise
        print(f"[gem] critique with {len(refs)} refs failed after retries; "
              "retrying image-only", flush=True)
        return _retry(lambda: _go(_build(False)), what="critique(image-only)")


def judge_images(image_paths: list[Path], prompt: str, schema: dict | None = None,
                 model: str = CRITIQUE_MODEL) -> dict:
    """Show the model several candidate images (labelled Candidate 1..N) alongside
    `prompt`, and return its JSON verdict (e.g. which candidate is best)."""
    from PIL import Image
    contents = [prompt]
    for i, p in enumerate(image_paths):
        contents.append(f"--- Candidate {i + 1} ---")
        contents.append(Image.open(p))
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0.2)

    def _go():
        resp = _client.models.generate_content(model=model, contents=contents, config=cfg)
        _record_usage(resp, model, "critique")
        return _coerce_json(resp.text)

    return _retry(_go, what="judge_images")


# ---------------- batch API ----------------
# The Batch API takes a JSONL file of {"key","request"} lines and, asynchronously,
# produces a JSONL file of {"key","response"} lines. We reconstruct each response
# into a normal SDK GenerateContentResponse so downstream code (image extraction,
# _coerce_json, usage recording) is identical to the interactive path.

def image_part(src) -> dict:
    """A Gemini content Part carrying an inline image, for a batch request.
    `src` is raw bytes or a Path/str to a webp/png/jpeg file."""
    data = bytes(src) if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
    return {"inline_data": {"mime_type": "image/webp",
                            "data": base64.b64encode(data).decode("ascii")}}


def text_part(text: str) -> dict:
    return {"text": text}


def image_gen_config(aspect: str = "3:2", size: str = IMAGE_SIZE) -> dict:
    """generation_config for an image request in a batch (mirrors generate_image)."""
    return {"response_modalities": ["TEXT", "IMAGE"],
            "image_config": {"aspect_ratio": aspect, "image_size": size}}


def json_config(schema: dict | None = None, temperature: float = 0.3) -> dict:
    """generation_config for a JSON (critique/verify/judge) request in a batch."""
    cfg = {"response_mime_type": "application/json", "temperature": temperature}
    if schema:
        cfg["response_schema"] = schema
    return cfg


def batch_submit(requests: list[dict], model: str,
                 display_name: str = "storyteller-batch") -> str:
    """Submit a batch job and return its job name. Each request is a dict:
        {"key": str, "parts": [<part>...], "generation_config": {...}}
    where parts come from text_part()/image_part(). Uses a JSONL file upload so
    large inline-image payloads (a whole book of scenes) are not capped."""
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as tf:
        path = tf.name
        for r in requests:
            line = {"key": str(r["key"]),
                    "request": {"contents": [{"parts": r["parts"]}],
                                "generation_config": r.get("generation_config") or {}}}
            tf.write(json.dumps(line) + "\n")

    def _go():
        f = _client.files.upload(file=path, config={"mime_type": "jsonl"})
        job = _client.batches.create(model=model, src=f.name,
                                     config={"display_name": display_name})
        return job.name

    try:
        return _retry(_go, what="batch_submit")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# Terminal batch states (no further polling will change them).
BATCH_DONE = "JOB_STATE_SUCCEEDED"
BATCH_TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
                  "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


def batch_state(job_name: str) -> str:
    """Current state string of a batch job (e.g. JOB_STATE_RUNNING)."""
    return _client.batches.get(name=job_name).state.name


class _Blob:
    def __init__(self, d: dict):
        self.mime_type = d.get("mimeType") or d.get("mime_type")
        raw = d.get("data")
        self.data = base64.b64decode(raw) if isinstance(raw, str) else raw


class _Part:
    def __init__(self, p: dict):
        self.text = p.get("text")
        idata = p.get("inlineData") or p.get("inline_data")
        self.inline_data = _Blob(idata) if idata else None


class _Usage:
    def __init__(self, u: dict | None):
        u = u or {}
        self.prompt_token_count = u.get("promptTokenCount") or u.get("prompt_token_count") or 0
        self.candidates_token_count = (u.get("candidatesTokenCount")
                                       or u.get("candidates_token_count") or 0)
        self.total_token_count = u.get("totalTokenCount") or u.get("total_token_count") or 0


class BatchResponse:
    """A minimal stand-in for an SDK GenerateContentResponse, built from a batch
    result-file JSON object (REST camelCase). Exposes exactly the attributes the
    rest of gem.py reads (.parts, .text, .usage_metadata) so batch results flow
    through the same image-extraction / _coerce_json / usage-recording code paths.
    Hand-rolled rather than model_validate() so a newly-added API field (e.g.
    usageMetadata.serviceTier) never breaks parsing."""

    def __init__(self, obj: dict):
        cands = obj.get("candidates") or []
        self.parts = []
        if cands:
            content = cands[0].get("content") or {}
            self.parts = [_Part(p) for p in (content.get("parts") or [])]
        self.usage_metadata = _Usage(obj.get("usageMetadata") or obj.get("usage_metadata"))

    @property
    def text(self):
        joined = "".join(p.text for p in self.parts if p.text)
        return joined or None


def _response_from_json(obj: dict) -> "BatchResponse":
    """Wrap a result-file JSON response so callers use .text / .parts /
    .usage_metadata exactly as for an interactive response."""
    return BatchResponse(obj)


def batch_results(job_name: str) -> dict:
    """{key: GenerateContentResponse | None} for a SUCCEEDED job. Handles both the
    file destination (our submit path) and an inline destination. A per-request
    error yields None for that key. Call only once batch_state() is SUCCEEDED."""
    job = _client.batches.get(name=job_name)
    if job.state.name != BATCH_DONE:
        raise RuntimeError(f"batch {job_name} not done: {job.state.name}")
    out: dict = {}
    dest = job.dest
    inlined = getattr(dest, "inlined_responses", None)
    if inlined:
        for i, r in enumerate(inlined):
            key = (r.metadata or {}).get("key") if r.metadata else None
            out[key or str(i)] = None if getattr(r, "error", None) else r.response
        return out
    file_name = getattr(dest, "file_name", None)
    if not file_name:
        raise RuntimeError(f"batch {job_name} has no results destination")
    raw = _client.files.download(file=file_name)
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        key = str(obj.get("key"))
        if obj.get("error") or obj.get("response", {}).get("error"):
            out[key] = None
        else:
            try:
                out[key] = _response_from_json(obj["response"])
            except Exception as e:  # noqa: BLE001
                print(f"  [batch] parse failed for key {key}: {type(e).__name__}: {str(e)[:120]}",
                      flush=True)
                out[key] = None
    return out


def response_image_bytes(resp, quality: int = WEBP_QUALITY) -> bytes | None:
    """Extract the first inline image from a response and return WebP bytes
    (same encoding generate_image uses). None if the response carries no image."""
    from PIL import Image
    if resp is None:
        return None
    for part in resp.parts or []:
        if getattr(part, "inline_data", None) is not None:
            pil = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, "WEBP", quality=quality, method=6)
            return buf.getvalue()
    return None


def record_batch_usage(resp, model: str, kind: str, images: int = 0):
    """Record one batch response's token usage at the 50% batch rate."""
    _record_usage(resp, model, kind, images=images, batch=True)


def critique_parts(brief: str, image: bytes, ref_bytes: list | None = None,
                   ref_labels: list | None = None) -> list:
    """Content parts for a batch scene-critique, matching critique_image's layout:
    the judged image first, then the labelled canonical reference sheets. Keep in
    sync with critique_image._build()."""
    parts = [text_part(brief)]
    if ref_bytes:
        parts.append(text_part("THE IMAGE TO JUDGE:"))
    parts.append(image_part(image))
    if ref_bytes:
        parts.append(text_part(
            "CANONICAL CHARACTER REFERENCE SHEETS follow -- each shows what one named "
            "character is supposed to look like. Compare the figures in the image above "
            "against them to judge `figure_match`:"))
        for i, b in enumerate(ref_bytes):
            label = ref_labels[i] if ref_labels and i < len(ref_labels) else f"character {i + 1}"
            parts.append(text_part(f"--- Reference: {label} ---"))
            parts.append(image_part(b))
    return parts


def judge_parts(prompt: str, images: list) -> list:
    """Content parts for a batch best-of judge (matches judge_images layout)."""
    parts = [text_part(prompt)]
    for i, b in enumerate(images):
        parts.append(text_part(f"--- Candidate {i + 1} ---"))
        parts.append(image_part(b))
    return parts
