"""AI text-to-video tests — stub httpx, no real network or provider charges."""
import pytest

from app import video_ai


def test_provider_autodetect(monkeypatch):
    monkeypatch.delenv("VIDEO_AI_PROVIDER", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert video_ai.provider() is None
    assert video_ai.is_enabled() is False

    monkeypatch.setenv("FAL_KEY", "fal-test")
    assert video_ai.provider() == "fal"
    assert video_ai.is_enabled() is True

    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8-test")
    assert video_ai.provider() == "replicate"


def test_explicit_provider_wins(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("VIDEO_AI_PROVIDER", "replicate")
    assert video_ai.provider() == "replicate"


def test_extract_video_url_shapes():
    assert video_ai._extract_video_url("https://x/v.mp4") == "https://x/v.mp4"
    assert video_ai._extract_video_url({"video": {"url": "https://x/a.mp4"}}) == "https://x/a.mp4"
    assert video_ai._extract_video_url({"output": ["https://x/b.mp4"]}) == "https://x/b.mp4"
    assert video_ai._extract_video_url({"video": "https://x/c.mp4"}) == "https://x/c.mp4"
    assert video_ai._extract_video_url({"nope": 1}) is None


class _FakeResp:
    def __init__(self, payload, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


async def test_fal_flow(monkeypatch):
    monkeypatch.setenv("VIDEO_AI_PROVIDER", "fal")
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setattr(video_ai.asyncio, "sleep", _no_sleep)

    posts, gets = [], []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            posts.append(url)
            return _FakeResp({
                "status_url": "https://fal/status",
                "response_url": "https://fal/response",
            })

        async def get(self, url, headers=None):
            gets.append(url)
            if url.endswith("/status"):
                return _FakeResp({"status": "COMPLETED"})
            if url.endswith("/response"):
                return _FakeResp({"video": {"url": "https://fal/out.mp4"}})
            return _FakeResp({}, content=b"FAKEVIDEO")  # the download

    monkeypatch.setattr(video_ai.httpx, "AsyncClient", FakeClient)

    result = await video_ai.generate_video("a cat surfing")
    from pathlib import Path

    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_bytes() == b"FAKEVIDEO"
    assert any("queue.fal.run" in u for u in posts)


async def _no_sleep(*a, **k):
    return None
