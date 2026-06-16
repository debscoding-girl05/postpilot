"""Image generation tests — stub httpx, no real network."""
import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from app import image_gen


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, json_payload=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_payload
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _install_fake_httpx(monkeypatch, captured, *, post_json=None, post_responses=None, get_responses=None):
    """get_responses / post_responses: FakeResponses returned in order per call."""
    get_q = list(get_responses or [])
    post_q = list(post_responses or [])

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            captured.setdefault("posts", []).append(
                {"url": url, "json": json, "headers": headers}
            )
            if post_q:
                return post_q.pop(0)
            return FakeResponse(json_payload=post_json)

        async def get(self, url, headers=None, params=None):
            captured.setdefault("gets", []).append(
                {"url": url, "params": params, "headers": headers}
            )
            return get_q.pop(0) if get_q else FakeResponse(content=_jpeg_bytes())

    monkeypatch.setattr(image_gen.httpx, "AsyncClient", FakeAsyncClient)
    # Make backoff instant.
    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(image_gen.asyncio, "sleep", _no_sleep)


def test_default_provider_is_pollinations():
    assert image_gen.provider() == "pollinations"
    assert image_gen.is_enabled() is True


def test_huggingface_autodetected_first(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    assert image_gen.provider() == "huggingface"
    assert image_gen.is_enabled() is True


def test_together_detected(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "tg_test")
    assert image_gen.provider() == "together"


def test_xai_provider_when_key_present(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert image_gen.provider() == "xai"


async def test_huggingface_generates(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    captured = {}
    # First call 503 (model loading), then 200 with image bytes -> exercises retry.
    _install_fake_httpx(
        monkeypatch, captured,
        post_responses=[FakeResponse(status_code=503), FakeResponse(content=_jpeg_bytes())],
    )
    out = await image_gen.generate_images("a misty forest", n=1)
    assert len(out) == 1 and Path(out[0]["path"]).exists()
    assert "router.huggingface.co" in captured["posts"][0]["url"]
    assert captured["posts"][0]["headers"]["Authorization"] == "Bearer hf_test"


async def test_together_generates(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "tg_test")
    captured = {}
    b64 = base64.b64encode(_jpeg_bytes()).decode()
    _install_fake_httpx(
        monkeypatch, captured,
        post_responses=[FakeResponse(json_payload={"data": [{"b64_json": b64}]})],
    )
    out = await image_gen.generate_images("a city skyline", n=1)
    assert len(out) == 1
    assert "api.together.xyz" in captured["posts"][0]["url"]


def test_explicit_provider_overrides(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("IMAGE_PROVIDER", "pollinations")
    assert image_gen.provider() == "pollinations"


async def test_pollinations_generates(monkeypatch):
    captured = {}
    _install_fake_httpx(
        monkeypatch, captured,
        get_responses=[FakeResponse(content=_jpeg_bytes()), FakeResponse(content=_jpeg_bytes())],
    )
    out = await image_gen.generate_images("a neon city", n=2)
    assert len(out) == 2
    assert "pollinations.ai" in captured["gets"][0]["url"]
    for item in out:
        assert Path(item["path"]).exists() and item["filename"].endswith(".jpg")


async def test_pollinations_sends_token(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_TOKEN", "tok-123")
    captured = {}
    _install_fake_httpx(monkeypatch, captured, get_responses=[FakeResponse(content=_jpeg_bytes())])
    await image_gen.generate_images("x", n=1)
    g = captured["gets"][0]
    assert g["headers"]["Authorization"] == "Bearer tok-123"
    assert g["params"]["token"] == "tok-123"


async def test_pollinations_retries_then_succeeds(monkeypatch):
    captured = {}
    _install_fake_httpx(
        monkeypatch, captured,
        get_responses=[FakeResponse(status_code=402), FakeResponse(content=_jpeg_bytes())],
    )
    out = await image_gen.generate_images("x", n=1)
    assert len(out) == 1
    assert len(captured["gets"]) == 2  # retried once after 402


async def test_pollinations_402_gives_token_hint(monkeypatch):
    captured = {}
    _install_fake_httpx(
        monkeypatch, captured,
        get_responses=[FakeResponse(status_code=402)] * 3,
    )
    with pytest.raises(RuntimeError, match="huggingface.co"):
        await image_gen.generate_images("x", n=1)


async def test_xai_generates(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    captured = {}
    b64 = base64.b64encode(_jpeg_bytes()).decode()
    _install_fake_httpx(monkeypatch, captured, post_json={"data": [{"b64_json": b64}]})
    out = await image_gen.generate_images("sunset", n=2)
    assert len(out) == 1
    assert captured["json"]["model"] == "grok-2-image"
    assert captured["url"].endswith("/images/generations")


async def test_xai_raises_without_key(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER", "xai")
    with pytest.raises(RuntimeError):
        await image_gen.generate_images("x")
