"""AI text-to-video generation via a configurable provider (fal.ai or Replicate).

Both follow submit -> poll -> download. Which one is used is chosen by
VIDEO_AI_PROVIDER, or auto-detected from whichever key is present. The model id
is configurable per provider because video model ids change often.

Env vars:
  VIDEO_AI_PROVIDER         "fal" | "replicate" (default: auto-detect)

  # fal.ai
  FAL_KEY                   fal.ai API key
  FAL_VIDEO_MODEL           model path (default: fal-ai/ltx-video)

  # Replicate
  REPLICATE_API_TOKEN       Replicate API token
  REPLICATE_VIDEO_MODEL     "owner/name" (required for Replicate)

This path costs money and takes minutes per clip; the API surface is implemented
to each provider's documented shape but should be validated against a live key.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx

from app.database import DATA_DIR

MEDIA_DIR = DATA_DIR / "media"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0  # 10 minutes


def provider() -> str | None:
    explicit = os.getenv("VIDEO_AI_PROVIDER")
    if explicit:
        return explicit.lower()
    if os.getenv("FAL_KEY"):
        return "fal"
    if os.getenv("REPLICATE_API_TOKEN"):
        return "replicate"
    return None


def is_enabled() -> bool:
    p = provider()
    if p == "fal":
        return bool(os.getenv("FAL_KEY"))
    if p == "replicate":
        return bool(os.getenv("REPLICATE_API_TOKEN"))
    return False


async def _download(url: str) -> dict:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.content
    fname = f"{uuid.uuid4().hex}.mp4"
    dest = MEDIA_DIR / fname
    dest.write_bytes(raw)
    return {"path": str(dest), "filename": fname, "url": f"/api/media/file/{fname}"}


def _extract_video_url(result: dict | list | str) -> str | None:
    """Pull a video URL out of a provider result of varying shape."""
    if isinstance(result, str):
        return result if result.startswith("http") else None
    if isinstance(result, list):
        for item in result:
            url = _extract_video_url(item)
            if url:
                return url
        return None
    if isinstance(result, dict):
        # Common shapes: {"video": {"url": ...}}, {"video": "..."}, {"url": ...}, {"output": ...}
        for key in ("video", "output", "url", "video_url"):
            if key in result:
                url = _extract_video_url(result[key])
                if url:
                    return url
    return None


async def _generate_fal(prompt: str) -> dict:
    key = os.getenv("FAL_KEY")
    model = os.getenv("FAL_VIDEO_MODEL", "fal-ai/ltx-video")
    headers = {"Authorization": f"Key {key}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        submit = await client.post(
            f"https://queue.fal.run/{model}", headers=headers, json={"prompt": prompt}
        )
        submit.raise_for_status()
        job = submit.json()
        status_url = job["status_url"]
        response_url = job["response_url"]

        waited = 0.0
        while waited < POLL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            st = await client.get(status_url, headers=headers)
            st.raise_for_status()
            status = st.json().get("status")
            if status == "COMPLETED":
                break
            if status in ("FAILED", "ERROR"):
                raise RuntimeError(f"fal job failed: {st.json()}")
        else:
            raise RuntimeError("fal job timed out")

        result = await client.get(response_url, headers=headers)
        result.raise_for_status()
        url = _extract_video_url(result.json())

    if not url:
        raise RuntimeError("fal result contained no video URL")
    return await _download(url)


async def _generate_replicate(prompt: str) -> dict:
    token = os.getenv("REPLICATE_API_TOKEN")
    model = os.getenv("REPLICATE_VIDEO_MODEL")
    if not model:
        raise RuntimeError("REPLICATE_VIDEO_MODEL must be set (e.g. 'owner/name')")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        create = await client.post(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            headers=headers,
            json={"input": {"prompt": prompt}},
        )
        create.raise_for_status()
        pred = create.json()
        get_url = pred["urls"]["get"]

        waited = 0.0
        while waited < POLL_TIMEOUT:
            if pred.get("status") == "succeeded":
                break
            if pred.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"replicate prediction failed: {pred.get('error')}")
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            poll = await client.get(get_url, headers=headers)
            poll.raise_for_status()
            pred = poll.json()
        else:
            raise RuntimeError("replicate prediction timed out")

        url = _extract_video_url(pred.get("output"))

    if not url:
        raise RuntimeError("replicate result contained no video URL")
    return await _download(url)


async def generate_video(prompt: str) -> dict:
    """Generate an AI video from a prompt. Returns {path, filename, url}."""
    p = provider()
    if p == "fal":
        return await _generate_fal(prompt)
    if p == "replicate":
        return await _generate_replicate(prompt)
    raise RuntimeError("No AI video provider configured (set FAL_KEY or REPLICATE_API_TOKEN)")


__all__ = ["generate_video", "is_enabled", "provider"]
