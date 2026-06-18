"""Agent API — the per-user local agent's sync surface (Phase 2).

The agent authenticates with the user's agent token (X-Agent-Token header), pulls
pending browser-login jobs, downloads their media, posts them locally, and reports
results. A heartbeat reports which platforms the agent currently has sessions for,
so the hosted UI can show them connected.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse

from app.auth import user_from_agent_token
from app.database import (
    Account,
    AgentJob,
    DATA_DIR,
    PostResult,
    User,
    get_session,
    select,
    utcnow,
)
from app.platforms import SESSION_PLATFORMS

router = APIRouter(prefix="/api/agent")
MEDIA_DIR = DATA_DIR / "media"


async def _agent_user(x_agent_token: str = Header(default="")) -> User:
    user = await user_from_agent_token(x_agent_token)
    if user is None:
        raise HTTPException(401, "Invalid agent token")
    return user


@router.get("/jobs")
async def agent_jobs(user: User = Depends(_agent_user)):
    """Return pending jobs for this user and mark them claimed (at-most-once)."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentJob)
            .where(AgentJob.user_id == user.id, AgentJob.status == "pending")
            .order_by(AgentJob.created_at)
        )
        jobs = result.scalars().all()
        out = [j.to_agent_dict() for j in jobs]
        for j in jobs:
            j.status = "claimed"
            j.claimed_at = utcnow()
        await session.commit()
    return {"jobs": out}


@router.post("/jobs/{job_id}/result")
async def agent_job_result(job_id: int, payload: dict, user: User = Depends(_agent_user)):
    status = "done" if payload.get("status") == "done" else "failed"
    async with get_session() as session:
        job = await session.get(AgentJob, job_id)
        if job is None or job.user_id != user.id:
            raise HTTPException(404, "Job not found")
        job.status = status
        job.platform_post_id = payload.get("platform_post_id")
        job.error_msg = (payload.get("error") or None)
        job.finished_at = utcnow()
        # Mirror onto the post's queued PostResult so History reflects the outcome.
        res = await session.execute(
            select(PostResult).where(
                PostResult.post_id == job.post_id, PostResult.platform == job.platform
            )
        )
        pr = res.scalars().first()
        if pr:
            pr.status = "success" if status == "done" else "failed"
            pr.platform_post_id = payload.get("platform_post_id")
            pr.error_msg = payload.get("error")
            pr.posted_at = utcnow()
        post_id = job.post_id
        await session.commit()

    from app.scheduler import finalize_post_status

    await finalize_post_status(post_id)
    return {"ok": True}


@router.get("/media/{filename}")
async def agent_media(filename: str, user: User = Depends(_agent_user)):
    dest = MEDIA_DIR / Path(filename).name
    if not dest.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(dest)


@router.post("/heartbeat")
async def agent_heartbeat(payload: dict, user: User = Depends(_agent_user)):
    """Agent reports which browser-login platforms it currently has sessions for."""
    connected = [p for p in (payload.get("connected") or []) if p in SESSION_PLATFORMS]
    async with get_session() as session:
        for platform in connected:
            r = await session.execute(
                select(Account).where(Account.user_id == user.id, Account.platform == platform)
            )
            acc = r.scalars().first()
            if acc:
                acc.status = "active"
                acc.last_used = utcnow()
            else:
                session.add(Account(
                    user_id=user.id, platform=platform, username=platform,
                    display_name=platform.title(), status="active",
                ))
        await session.commit()
    return {"ok": True, "connected": connected}


__all__ = ["router"]
