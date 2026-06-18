"""Optional AI caption generation via an OpenAI-compatible chat provider.

Provider-agnostic: Groq, xAI (Grok), and Google Gemini all expose an
OpenAI-compatible /chat/completions endpoint, so one code path serves them. The
active provider is chosen by CAPTION_PROVIDER, or auto-detected from whichever
API key is present (Groq first — it's free with no card).

Env vars:
  CAPTION_PROVIDER          "groq" | "xai" | "gemini" (default: auto-detect)

  GROQ_API_KEY              Groq key (free: console.groq.com)
  GROQ_MODEL                default: llama-3.3-70b-versatile

  XAI_API_KEY / GROK_API_KEY    xAI key
  GROK_MODEL                default: grok-3
  XAI_BASE_URL              override xAI base (default: https://api.x.ai/v1)

  GEMINI_API_KEY / GOOGLE_API_KEY   Gemini key (free: aistudio.google.com)
  GEMINI_MODEL              default: gemini-2.0-flash
"""
from __future__ import annotations

import os

import httpx

from app.content_processor import CHAR_LIMITS

# provider -> config. `base` may be overridden per provider where noted.
PROVIDERS = {
    "groq": {
        "keys": ["GROQ_API_KEY"],
        "base": "https://api.groq.com/openai/v1",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
    },
    "xai": {
        "keys": ["XAI_API_KEY", "GROK_API_KEY"],
        "base": "https://api.x.ai/v1",
        "base_env": "XAI_BASE_URL",
        "model_env": "GROK_MODEL",
        "default_model": "grok-3",
    },
    "gemini": {
        "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.0-flash",
    },
}

SYSTEM_PROMPT = (
    "You are a social media copywriter. Write a single engaging post caption from "
    "the user's idea or draft. Return only the caption text — no preamble, no quotes, "
    "no surrounding commentary, no markdown. Keep it natural and platform-appropriate. "
    "Include a few relevant hashtags only when they fit naturally."
)


def _key_for(name: str) -> str | None:
    for env in PROVIDERS[name]["keys"]:
        val = os.getenv(env)
        if val:
            return val
    return None


def provider() -> str | None:
    explicit = os.getenv("CAPTION_PROVIDER")
    if explicit:
        return explicit.lower()
    # Auto-detect: Groq first (free, no card), then xAI, then Gemini.
    for name in ("groq", "xai", "gemini"):
        if _key_for(name):
            return name
    return None


def is_enabled() -> bool:
    p = provider()
    return bool(p and p in PROVIDERS and _key_for(p))


SERIES_SYSTEM = (
    "You are a social media copywriter running an ongoing content series for a creator. "
    "Given the series concept, its tone, and the creator's short note about today's "
    "entry, write a single engaging post. Return only the post text — no preamble, no "
    "quotes, no markdown, no 'Here is'. Stay on the series concept and match the tone. "
    "Add a few relevant hashtags only if they fit naturally."
)


def _platform_limit(platforms: list[str] | None) -> int:
    if platforms:
        return min(CHAR_LIMITS.get(p, 500) for p in platforms)
    return 500


async def _chat(system: str, user_prompt: str) -> str:
    """Single OpenAI-compatible chat completion via the active provider."""
    name = provider()
    if not name or name not in PROVIDERS:
        raise RuntimeError("No AI provider configured (set GROQ_API_KEY)")
    cfg = PROVIDERS[name]
    api_key = _key_for(name)
    if not api_key:
        raise RuntimeError(f"No API key set for provider '{name}'")
    base = os.getenv(cfg.get("base_env", ""), cfg["base"]).rstrip("/")
    model = os.getenv(cfg["model_env"], cfg["default_model"])

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


async def generate_caption(
    idea: str, platforms: list[str] | None = None, tone: str | None = None
) -> str:
    """Generate (or refine) a post caption. Returns the caption text."""
    limit = _platform_limit(platforms)
    parts = [f"Idea / draft:\n{idea}", f"\nKeep the caption at or under {limit} characters."]
    if platforms:
        parts.append(f"Target platforms: {', '.join(platforms)}.")
    if tone:
        parts.append(f"Tone: {tone}.")
    return await _chat(SYSTEM_PROMPT, "\n".join(parts))


async def generate_series_post(
    concept: str,
    note: str,
    tone: str | None = None,
    platforms: list[str] | None = None,
    hashtags: list[str] | None = None,
) -> str:
    """Generate a full post for a content series from a one-line daily note."""
    limit = _platform_limit(platforms)
    parts = [
        f"Series concept: {concept}" if concept else "Series concept: (general updates)",
        f"\nToday's note from the creator:\n{note}",
        f"\nWrite the post at or under {limit} characters.",
    ]
    if tone:
        parts.append(f"Tone: {tone}.")
    if platforms:
        parts.append(f"Target platforms: {', '.join(platforms)}.")
    if hashtags:
        parts.append("Prefer these hashtags if relevant: " + " ".join("#" + h.lstrip("#") for h in hashtags))
    return await _chat(SERIES_SYSTEM, "\n".join(parts))


__all__ = ["generate_caption", "generate_series_post", "is_enabled", "provider", "PROVIDERS"]
