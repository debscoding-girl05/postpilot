"""AI caption tests — no real network. We stub httpx.AsyncClient."""
import pytest

from app import ai


def _install_fake_httpx(monkeypatch, captured, content="Polished caption ✨ #launch"):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(ai.httpx, "AsyncClient", FakeAsyncClient)


def test_disabled_without_any_key(monkeypatch):
    assert ai.provider() is None
    assert ai.is_enabled() is False


def test_groq_autodetected_first(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert ai.provider() == "groq"
    assert ai.is_enabled() is True


def test_xai_detected_when_only_xai_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert ai.provider() == "xai"


def test_explicit_provider_overrides(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("CAPTION_PROVIDER", "xai")
    assert ai.provider() == "xai"


async def test_generate_caption_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    captured = {}
    _install_fake_httpx(monkeypatch, captured)

    out = await ai.generate_caption("launching tomorrow", platforms=["twitter", "bluesky"])

    assert out == "Polished caption ✨ #launch"
    assert "api.groq.com" in captured["url"] and captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer gsk-test"
    assert captured["json"]["model"] == "llama-3.3-70b-versatile"
    # Tightest limit among twitter(280)/bluesky(300) should be referenced.
    assert "280" in captured["json"]["messages"][1]["content"]


async def test_generate_caption_xai_path(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    captured = {}
    _install_fake_httpx(monkeypatch, captured)
    await ai.generate_caption("hi")
    assert "api.x.ai" in captured["url"]
    assert captured["json"]["model"] == "grok-3"


async def test_model_is_overridable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.1-8b-instant")
    captured = {}
    _install_fake_httpx(monkeypatch, captured)
    await ai.generate_caption("hello")
    assert captured["json"]["model"] == "llama-3.1-8b-instant"


async def test_generate_caption_raises_without_provider(monkeypatch):
    with pytest.raises(RuntimeError):
        await ai.generate_caption("hello")
