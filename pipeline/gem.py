"""Thin Gemini wrappers: structured text, image generation, vision critique.

A single shared Budget caps the number of image-generation calls across the
whole run (the user asked for a hard ceiling of 100 candidates).
"""
import json
import os
import time
import threading
from pathlib import Path

from google import genai
from google.genai import types

from . import costs
from .config import (IMAGE_SIZE, IMAGE_MODEL, TEXT_MODEL, SHEET_IMAGE_MODEL,
                     PAGE_IMAGE_MODEL, CRITIQUE_MODEL, WEBP_QUALITY)

ROOT = Path(__file__).resolve().parent.parent


def _record_usage(resp, model: str, kind: str, images: int = 0):
    """Log one call's token usage + cost to the cumulative SQLite DB. Never let
    accounting break a generation."""
    try:
        u = getattr(resp, "usage_metadata", None)
        pin = (getattr(u, "prompt_token_count", 0) or 0) if u else 0
        pout = (getattr(u, "candidates_token_count", 0) or 0) if u else 0
        tot = (getattr(u, "total_token_count", 0) or 0) if u else 0
        costs.record(model, kind, pin, pout, tot or (pin + pout), images=images)
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


def critique_image(image_path: Path, brief: str, schema: dict | None = None,
                   model: str = CRITIQUE_MODEL) -> dict:
    """Vision-model critique of a generated image against its brief."""
    from PIL import Image
    contents = [brief, Image.open(image_path)]
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.3,
    )

    def _go():
        resp = _client.models.generate_content(model=model, contents=contents, config=cfg)
        _record_usage(resp, model, "critique")
        return _coerce_json(resp.text)

    return _retry(_go, what="critique")


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
