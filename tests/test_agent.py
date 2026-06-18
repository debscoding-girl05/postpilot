"""Phase 2 agent flow: scheduler queues browser-login jobs; the agent API claims
them, reports results, and the post finalizes. Calls route functions directly
(no HTTP layer) with explicit User objects."""
import json

import pytest

from app import agent_api
from app import scheduler as sched
from app.database import AgentJob, Account, Post, PostResult, User, get_session, select, utcnow


async def _user(email="agent@test.com", token="ppa_test_token"):
    async with get_session() as session:
        u = User(email=email, password_hash="x", agent_token=token)
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u


async def _post(user_id, platforms):
    async with get_session() as session:
        p = Post(user_id=user_id, content="hello agent", media_paths=json.dumps([]),
                 platforms=json.dumps(platforms), scheduled_for=utcnow(), status="scheduled")
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p.id


async def _status(post_id):
    async with get_session() as session:
        return (await session.get(Post, post_id)).status


async def test_execute_post_queues_agent_job():
    u = await _user()
    post_id = await _post(u.id, ["tiktok"])
    await sched.execute_post(post_id)

    async with get_session() as session:
        jobs = (await session.execute(
            select(AgentJob).where(AgentJob.post_id == post_id))).scalars().all()
        results = (await session.execute(
            select(PostResult).where(PostResult.post_id == post_id))).scalars().all()
    assert len(jobs) == 1 and jobs[0].platform == "tiktok" and jobs[0].status == "pending"
    assert results[0].status == "queued"
    assert await _status(post_id) == "posting"  # waiting on the agent


async def test_agent_claims_and_completes():
    u = await _user()
    post_id = await _post(u.id, ["tiktok"])
    await sched.execute_post(post_id)

    # Agent pulls jobs (claims them).
    fetched = await agent_api.agent_jobs(user=u)
    assert len(fetched["jobs"]) == 1
    job = fetched["jobs"][0]
    assert job["platform"] == "tiktok" and "caption" in job

    # A second pull returns nothing (already claimed).
    assert (await agent_api.agent_jobs(user=u))["jobs"] == []

    # Agent reports success → post finalizes to done.
    await agent_api.agent_job_result(job["id"], {"status": "done", "platform_post_id": "tt_123"}, user=u)
    async with get_session() as session:
        pr = (await session.execute(
            select(PostResult).where(PostResult.post_id == post_id))).scalars().first()
    assert pr.status == "success" and pr.platform_post_id == "tt_123"
    assert await _status(post_id) == "done"


async def test_agent_failure_marks_post_failed():
    u = await _user()
    post_id = await _post(u.id, ["linkedin"])
    await sched.execute_post(post_id)
    job = (await agent_api.agent_jobs(user=u))["jobs"][0]
    await agent_api.agent_job_result(job["id"], {"status": "failed", "error": "blocked"}, user=u)
    assert await _status(post_id) == "failed"


async def test_agent_token_isolation():
    a = await _user("a@test.com", "ppa_a")
    b = await _user("b@test.com", "ppa_b")
    post_id = await _post(a.id, ["tiktok"])
    await sched.execute_post(post_id)

    # B's agent sees none of A's jobs.
    assert (await agent_api.agent_jobs(user=b))["jobs"] == []
    # B can't report on A's job.
    a_job = (await agent_api.agent_jobs(user=a))["jobs"][0]
    with pytest.raises(Exception):
        await agent_api.agent_job_result(a_job["id"], {"status": "done"}, user=b)


async def test_heartbeat_marks_platform_connected():
    u = await _user()
    await agent_api.agent_heartbeat({"connected": ["tiktok", "linkedin", "bogus"]}, user=u)
    async with get_session() as session:
        accs = {a.platform: a.status for a in (await session.execute(
            select(Account).where(Account.user_id == u.id))).scalars().all()}
    assert accs.get("tiktok") == "active" and accs.get("linkedin") == "active"
    assert "bogus" not in accs  # only real session platforms accepted
