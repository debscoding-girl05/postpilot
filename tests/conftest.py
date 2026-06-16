"""Test setup: point DATA_DIR at a temp dir BEFORE any app module is imported.

app.database builds its async engine at import time from DATA_DIR, so this must
run first. Putting it at module top-level in conftest guarantees it runs before
test modules (which import app.*) are collected.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="postpilot-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["APP_SECRET"] = "test-secret-fixed-value-for-determinism"
# Ensure AI providers are unset by default in tests unless a test opts in.
for _k in (
    "XAI_API_KEY", "GROK_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "HF_TOKEN", "HUGGINGFACE_API_KEY", "HF_API_KEY", "TOGETHER_API_KEY",
    "POLLINATIONS_TOKEN", "POLLINATIONS_MODEL", "GROK_IMAGE_MODEL",
    "HF_IMAGE_MODEL", "TOGETHER_IMAGE_MODEL",
    "CAPTION_PROVIDER", "IMAGE_PROVIDER", "VIDEO_AI_PROVIDER", "FAL_KEY",
    "REPLICATE_API_TOKEN",
):
    os.environ.pop(_k, None)

import pytest_asyncio  # noqa: E402

from app.database import init_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    yield
