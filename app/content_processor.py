"""Adapt media and captions to each platform's requirements."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

PLATFORM_IMAGE_SPECS = {
    "instagram": {"max_size": (1080, 1080), "format": "JPEG", "quality": 90},
    "twitter": {"max_size": (1200, 675), "format": "PNG", "quality": 95},
    "bluesky": {"max_size": (1200, 675), "format": "JPEG", "quality": 90},
    "mastodon": {"max_size": (1280, 720), "format": "PNG", "quality": 90},
    "linkedin": {"max_size": (1200, 627), "format": "JPEG", "quality": 90},
}

CHAR_LIMITS = {
    "twitter": 280,
    "instagram": 2200,
    "bluesky": 300,
    "mastodon": 500,
    "linkedin": 3000,
    "tiktok": 2200,
}


def process_image_for_platform(image_path: Path, platform: str) -> bytes:
    """Resize and reformat an image to meet platform specs. Returns image bytes."""
    specs = PLATFORM_IMAGE_SPECS.get(
        platform, {"max_size": (1200, 1200), "format": "JPEG", "quality": 90}
    )
    img = Image.open(image_path)

    # JPEG can't hold alpha; flatten onto white if needed.
    if specs["format"] == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1])
        img = background

    img.thumbnail(specs["max_size"], Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format=specs["format"], quality=specs["quality"])
    return buffer.getvalue()


def adapt_caption_for_platform(
    text: str, platform: str, hashtags: list[str] | None = None
) -> str:
    """Trim to the platform char limit, optionally appending hashtags."""
    limit = CHAR_LIMITS.get(platform, 500)
    hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in (hashtags or []))
    full = f"{text}\n\n{hashtag_str}".strip() if hashtag_str else text
    if len(full) <= limit:
        return full
    if not hashtag_str:
        return text[: limit - 3] + "..."
    # Keep hashtags, truncate the body to fit.
    available = max(0, limit - len(hashtag_str) - 5)
    return f"{text[:available]}...\n\n{hashtag_str}".strip()


__all__ = [
    "process_image_for_platform",
    "adapt_caption_for_platform",
    "PLATFORM_IMAGE_SPECS",
    "CHAR_LIMITS",
]
