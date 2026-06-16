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


async def generate_caption(
    idea: str, platforms: list[str] | None = None, tone: str | None = None
) -> str:
    """Generate (or refine) a post caption. Returns the caption text.

    Raises RuntimeError if no provider/key is configured.
    """
    name = provider()
    if not name or name not in PROVIDERS:
        raise RuntimeError("No caption provider configured (set GROQ_API_KEY)")
    cfg = PROVIDERS[name]
    api_key = _key_for(name)
    if not api_key:
        raise RuntimeError(f"No API key set for caption provider '{name}'")

    base = os.getenv(cfg.get("base_env", ""), cfg["base"]).rstrip("/")
    model = os.getenv(cfg["model_env"], cfg["default_model"])

    # Keep within the tightest selected platform's limit so the result fits everywhere.
    if platforms:
        limit = min(CHAR_LIMITS.get(p, 500) for p in platforms)
    else:
        limit = 500

    parts = [f"Idea / draft:\n{idea}", f"\nKeep the caption at or under {limit} characters."]
    if platforms:
        parts.append(f"Target platforms: {', '.join(platforms)}.")
    if tone:
        parts.append(f"Tone: {tone}.")
    user_prompt = "\n".join(parts)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return (data["choices"][0]["message"]["content"] or "").strip()


__all__ = ["generate_caption", "is_enabled", "provider", "PROVIDERS"]
