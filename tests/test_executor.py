"""Tests for the scheduler's execute_post — the resilience guarantees matter most:
one platform failing must never block the others, and statuses must be recorded.
"""
import json

import pytest

from app import scheduler as sched
from app.database import Post, PostResult, get_session, select, utcnow
from app.platforms.base import PostPayload


class FakeDriver:
    def __init__(self, name, *, auth_ok=True, raise_on_post=False):
        self.name = name
        self.char_limit = 300
        self._auth_ok = auth_ok
        self._raise = raise_on_post

    def adapt_caption(self, text):
        return text

    async def authenticate(self, auth_data):
        return self._auth_ok

    async def post(self, payload: PostPayload):
        if self._raise:
            raise RuntimeError("boom")
        return f"{self.name}-post-123"


async def _make_post(platforms, content="hello"):
    async with get_session() as session:
        post = Post(
            content=content,
            media_paths=json.dumps([]),
            platforms=json.dumps(platforms),
            scheduled_for=utcnow(),
            status="scheduled",
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        return post.id


async def _results(post_id):
    async with get_session() as session:
        res = await session.execute(select(PostResult).where(PostResult.post_id == post_id))
        return {r.platform: r for r in res.scalars().all()}


async def _post_status(post_id):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        return post.status


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    # Skip the 2-8s human-like delay so tests run fast.
    async def fast_post(driver, payload):
        return await driver.post(payload)

    monkeypatch.setattr(sched, "post_with_jitter", fast_post)


async def test_single_platform_success(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p))
    monkeypatch.setattr(sched, "_load_auth_data", lambda p: _auth())
    post_id = await _make_post(["bluesky"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["bluesky"].status == "success"
    assert results["bluesky"].platform_post_id == "bluesky-post-123"
    assert await _post_status(post_id) == "done"


async def test_one_failure_does_not_block_others(monkeypatch):
    drivers = {
        "bluesky": FakeDriver("bluesky"),
        "mastodon": FakeDriver("mastodon", raise_on_post=True),
    }
    monkeypatch.setattr(sched, "get_driver", lambda p: drivers[p])
    monkeypatch.setattr(sched, "_load_auth_data", lambda p: _auth())
    post_id = await _make_post(["bluesky", "mastodon"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["bluesky"].status == "success"
    assert results["mastodon"].status == "failed"
    assert "boom" in results["mastodon"].error_msg
    # At least one success -> overall "done".
    assert await _post_status(post_id) == "done"


async def test_no_account_is_skipped(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p))
    monkeypatch.setattr(sched, "_load_auth_data", lambda p: _none())
    post_id = await _make_post(["twitter"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["twitter"].status == "skipped"
    assert await _post_status(post_id) == "failed"  # nothing succeeded


async def test_auth_failure_marks_skipped(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p, auth_ok=False))
    monkeypatch.setattr(sched, "_load_auth_data", lambda p: _auth())
    post_id = await _make_post(["instagram"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["instagram"].status == "skipped"


# _load_auth_data is awaited inside execute_post, so the monkeypatched replacement
# must be a coroutine function. These tiny helpers provide that.
async def _auth():
    return {}


async def _none():
    return None
