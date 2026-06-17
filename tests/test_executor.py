"""Tests for the scheduler's execute_post — the resilience guarantees matter most:
one platform failing must never block the others, statuses are recorded, and posts
are scoped to their owner. Browser-login platforms are skipped server-side.
"""
import json

import pytest

from app import scheduler as sched
from app.database import Post, PostResult, User, get_session, select, utcnow
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


async def _make_user(email="exec@test.com"):
    async with get_session() as session:
        user = User(email=email, password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _make_post(user_id, platforms, content="hello"):
    async with get_session() as session:
        post = Post(
            user_id=user_id,
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
    async def fast_post(driver, payload):
        return await driver.post(payload)

    monkeypatch.setattr(sched, "post_with_jitter", fast_post)


# _load_auth_data is awaited with (user_id, platform); the fakes must be coroutines.
async def _auth(*_a):
    return {}


async def _none(*_a):
    return None


async def test_single_platform_success(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p))
    monkeypatch.setattr(sched, "_load_auth_data", lambda u, p: _auth())
    uid = await _make_user()
    post_id = await _make_post(uid, ["bluesky"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["bluesky"].status == "success"
    assert results["bluesky"].platform_post_id == "bluesky-post-123"
    assert results["bluesky"].user_id == uid
    assert await _post_status(post_id) == "done"


async def test_one_failure_does_not_block_others(monkeypatch):
    drivers = {
        "bluesky": FakeDriver("bluesky"),
        "mastodon": FakeDriver("mastodon", raise_on_post=True),
    }
    monkeypatch.setattr(sched, "get_driver", lambda p: drivers[p])
    monkeypatch.setattr(sched, "_load_auth_data", lambda u, p: _auth())
    uid = await _make_user()
    post_id = await _make_post(uid, ["bluesky", "mastodon"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["bluesky"].status == "success"
    assert results["mastodon"].status == "failed"
    assert "boom" in results["mastodon"].error_msg
    assert await _post_status(post_id) == "done"


async def test_no_account_is_skipped(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p))
    monkeypatch.setattr(sched, "_load_auth_data", lambda u, p: _none())
    uid = await _make_user()
    post_id = await _make_post(uid, ["mastodon"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["mastodon"].status == "skipped"
    assert await _post_status(post_id) == "failed"


async def test_auth_failure_marks_skipped(monkeypatch):
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p, auth_ok=False))
    monkeypatch.setattr(sched, "_load_auth_data", lambda u, p: _auth())
    uid = await _make_user()
    post_id = await _make_post(uid, ["bluesky"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["bluesky"].status == "skipped"


async def test_session_platform_skipped_server_side(monkeypatch):
    # Browser-login platforms (twitter/tiktok/etc.) are not posted server-side —
    # they're handled by the local agent. execute_post should skip them cleanly.
    monkeypatch.setattr(sched, "get_driver", lambda p: FakeDriver(p))
    monkeypatch.setattr(sched, "_load_auth_data", lambda u, p: _auth())
    uid = await _make_user()
    post_id = await _make_post(uid, ["tiktok"])

    await sched.execute_post(post_id)

    results = await _results(post_id)
    assert results["tiktok"].status == "skipped"
    assert "agent" in (results["tiktok"].error_msg or "").lower()
    assert await _post_status(post_id) == "failed"
