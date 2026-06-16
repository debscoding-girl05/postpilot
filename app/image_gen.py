"""AI image generation, provider-aware.

Providers (free options need only a no-credit-card signup token):
  - huggingface   FLUX.1-schnell via HF Inference router  (free token: huggingface.co/settings/tokens)
  - together      FLUX.1-schnell-Free via Together         (free token: api.together.xyz)
  - xai           Grok grok-2-image                        (needs xAI credits)
  - pollinations  image.pollinations.ai                    (now gated/rate-limited; kept as fallback)

Active provider is chosen by IMAGE_PROVIDER, or auto-detected from whichever key
is present. Generated images are saved to data/media/ and returned as file paths.

Env vars:
  IMAGE_PROVIDER            "huggingface" | "together" | "xai" | "pollinations"

  HF_TOKEN / HUGGINGFACE_API_KEY    Hugging Face token
  HF_IMAGE_MODEL            default: black-forest-labs/FLUX.1-schnell

  TOGETHER_API_KEY          Together key
  TOGETHER_IMAGE_MODEL      default: black-forest-labs/FLUX.1-schnell-Free

  XAI_API_KEY / GROK_API_KEY        xAI key
  GROK_IMAGE_MODEL          default: grok-2-image

  POLLINATIONS_TOKEN        Pollinations token (legacy image host)
  POLLINATIONS_MODEL        default: flux
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import random
import urllib.parse
import uuid
from pathlib import Path

import httpx
from PIL import Image

from app.database import DATA_DIR

MEDIA_DIR = DATA_DIR / "media"
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"
HF_BASE = "https://router.huggingface.co/hf-inference/models/"


def _hf_key() -> str | None:
    return os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_KEY") or os.getenv("HF_API_KEY")


def _xai_key() -> str | None:
    return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")


def provider() -> str:
    explicit = os.getenv("IMAGE_PROVIDER")
    if explicit:
        return explicit.lower()
    if _hf_key():
        return "huggingface"
    if os.getenv("TOGETHER_API_KEY"):
        return "together"
    if _xai_key():
        return "xai"
    return "pollinations"  # keyless fallback (may be rate-limited)


def is_enabled() -> bool:
    p = provider()
    if p == "huggingface":
        return bool(_hf_key())
    if p == "together":
        return bool(os.getenv("TOGETHER_API_KEY"))
    if p == "xai":
        return bool(_xai_key())
    return p == "pollinations"  # always "available" to try


def _save(raw: bytes) -> dict | None:
    """Validate bytes are a real image, save as JPEG, return media descriptor."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}.jpg"
    dest = MEDIA_DIR / fname
    img.save(dest, format="JPEG", quality=92)
    return {"path": str(dest), "filename": fname, "url": f"/api/media/file/{fname}"}


def _save_all(raws: list[bytes]) -> list[dict]:
    out = []
    for raw in raws:
        item = _save(raw)
        if item:
            out.append(item)
    if not out:
        raise RuntimeError("Provider returned no usable image")
    return out


# --- Hugging Face (recommended free option) ----------------------------------

async def _generate_huggingface(prompt: str, n: int) -> list[dict]:
    token = _hf_key()
    model = os.getenv("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
    url = HF_BASE + model
    headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
    raws: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for _ in range(n):
            for attempt in range(4):
                resp = await client.post(url, headers=headers, json={"inputs": prompt})
                if resp.status_code == 200 and resp.content:
                    raws.append(resp.content)
                    break
                if resp.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(4 * (attempt + 1))  # model loading / rate limit
                    continue
                # Surface the provider error text (often JSON with a helpful message).
                raise RuntimeError(f"Hugging Face error {resp.status_code}: {resp.text[:200]}")
    return _save_all(raws)


# --- Together (OpenAI-compatible images) -------------------------------------

async def _generate_together(prompt: str, n: int) -> list[dict]:
    token = os.getenv("TOGETHER_API_KEY")
    model = os.getenv("TOGETHER_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell-Free")
    raws: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for _ in range(n):  # free tier typically caps n=1 per call
            resp = await client.post(
                "https://api.together.xyz/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": model, "prompt": prompt,
                    "width": 1024, "height": 1024, "steps": 4,
                    "n": 1, "response_format": "b64_json",
                },
            )
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                if item.get("b64_json"):
                    raws.append(base64.b64decode(item["b64_json"]))
                elif item.get("url"):
                    img = await client.get(item["url"])
                    img.raise_for_status()
                    raws.append(img.content)
    return _save_all(raws)


# --- xAI Grok ----------------------------------------------------------------

async def _generate_xai(prompt: str, n: int) -> list[dict]:
    api_key = _xai_key()
    model = os.getenv("GROK_IMAGE_MODEL", "grok-2-image")
    base = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")
    raws: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{base}/images/generations",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "prompt": prompt, "n": n, "response_format": "b64_json"},
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if item.get("b64_json"):
                raws.append(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                img = await client.get(item["url"])
                img.raise_for_status()
                raws.append(img.content)
    return _save_all(raws)


# --- Pollinations (legacy, now gated) ----------------------------------------

_POLLINATIONS_TOKEN_HELP = (
    "Pollinations rate-limits its image host even with a token now. Prefer a free "
    "Hugging Face token (HF_TOKEN, no credit card) at huggingface.co/settings/tokens."
)


async def _generate_pollinations(prompt: str, n: int) -> list[dict]:
    token = os.getenv("POLLINATIONS_TOKEN")
    model = os.getenv("POLLINATIONS_MODEL", "flux")
    raws: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for _ in range(n):
            url = POLLINATIONS_BASE + urllib.parse.quote(prompt)
            params = {"width": 1024, "height": 1024, "model": model,
                      "seed": random.randint(1, 1_000_000)}
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
                params["token"] = token
            last = None
            for attempt in range(3):
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200 and resp.content:
                    raws.append(resp.content)
                    break
                last = resp.status_code
                if resp.status_code in (402, 429, 500, 502, 503):
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
            else:
                raise RuntimeError(_POLLINATIONS_TOKEN_HELP if last == 402
                                   else f"Pollinations failed (HTTP {last})")
    return _save_all(raws)


async def generate_images(prompt: str, n: int = 1) -> list[dict]:
    """Generate n images from a prompt. Returns a list of {path, filename, url}."""
    n = max(1, min(int(n), 4))
    p = provider()
    if p == "huggingface":
        if not _hf_key():
            raise RuntimeError("HF_TOKEN is not set")
        return await _generate_huggingface(prompt, n)
    if p == "together":
        if not os.getenv("TOGETHER_API_KEY"):
            raise RuntimeError("TOGETHER_API_KEY is not set")
        return await _generate_together(prompt, n)
    if p == "xai":
        if not _xai_key():
            raise RuntimeError("XAI_API_KEY is not set")
        return await _generate_xai(prompt, n)
    if p == "pollinations":
        return await _generate_pollinations(prompt, n)
    raise RuntimeError(f"Unknown image provider: {p}")


__all__ = ["generate_images", "is_enabled", "provider"]
