"""APScheduler setup + the job executor that actually posts to every platform.

The job store is the same SQLite file as the app DB, so scheduled jobs survive
restarts. execute_post() is referenced by APScheduler as 'app.scheduler:execute_post'
and must stay importable at module level.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import timedelta
from pathlib import Path

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.content_processor import adapt_caption_for_platform
from app.crypto import decrypt_json
from app.database import (
    Account,
    AgentJob,
    Post,
    PostResult,
    SYNC_DATABASE_URL,
    get_session,
    select,
    utcnow,
)
from app.platforms import CREDENTIAL_PLATFORMS, SESSION_PLATFORMS, get_driver
from app.platforms.base import PostPayload

logger = logging.getLogger("postpilot.scheduler")

# Jobs more than this many minutes overdue are marked failed instead of run.
MAX_OVERDUE_MINUTES = 30

jobstores = {"default": SQLAlchemyJobStore(url=SYNC_DATABASE_URL)}
scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")


# --- scheduling helpers -------------------------------------------------------

def schedule_post(post_id: int, run_at) -> None:
    """(Re)schedule a job to fire execute_post at run_at (UTC datetime).

    Adds 15-45s of random jitter so posts don't fire on an exact round minute.
    """
    jitter = timedelta(seconds=random.uniform(15, 45))
    scheduler.add_job(
        execute_post,
        trigger="date",
        run_date=run_at + jitter,
        args=[post_id],
        id=f"post_{post_id}",
        replace_existing=True,
        misfire_grace_time=MAX_OVERDUE_MINUTES * 60,
    )


def unschedule_post(post_id: int) -> None:
    try:
        scheduler.remove_job(f"post_{post_id}")
    except Exception:
        pass


# --- the executor -------------------------------------------------------------

async def post_with_jitter(driver, payload: PostPayload) -> str:
    """Add a human-like 2-8s delay before each platform post."""
    await asyncio.sleep(random.uniform(2, 8))
    return await driver.post(payload)


async def _mark_account_status(user_id: int, platform: str, status: str) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.user_id == user_id, Account.platform == platform)
        )
        account = result.scalars().first()
        if account:
            account.status = status
            await session.commit()


async def _load_auth_data(user_id: int, platform: str) -> dict | None:
    """Return decrypted auth_data for a user's credential-platform account, {} for
    session platforms, or None if no account exists."""
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.user_id == user_id, Account.platform == platform)
        )
        account = result.scalars().first()
        if account is None:
            return None
        account.last_used = utcnow()
        await session.commit()
        if platform in CREDENTIAL_PLATFORMS:
            return decrypt_json(account.auth_data)
        return {}


async def _save_result(
    user_id: int, post_id: int, platform: str, status: str,
    platform_post_id: str | None, error: str | None,
) -> None:
    async with get_session() as session:
        session.add(
            PostResult(
                user_id=user_id,
                post_id=post_id,
                platform=platform,
                status=status,
                platform_post_id=platform_post_id,
                error_msg=error,
            )
        )
        await session.commit()


async def _enqueue_agent_job(user_id: int, post_id: int, platform: str,
                             caption: str, media_paths: list) -> None:
    """Queue a browser-login post for the user's local agent, and record a
    'queued' PostResult that the agent will later update."""
    async with get_session() as session:
        session.add(AgentJob(
            user_id=user_id, post_id=post_id, platform=platform,
            caption=caption, media_paths=json.dumps([str(p) for p in media_paths]),
            status="pending",
        ))
        session.add(PostResult(
            user_id=user_id, post_id=post_id, platform=platform,
            status="queued", error_msg="Waiting for your local agent",
        ))
        await session.commit()


async def finalize_post_status(post_id: int) -> None:
    """Recompute a post's status from its results + any outstanding agent jobs.
    Called after server-side posting and after each agent result."""
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        pending = await session.execute(
            select(AgentJob).where(
                AgentJob.post_id == post_id, AgentJob.status.in_(["pending", "claimed"])
            )
        )
        if pending.scalars().first():
            post.status = "posting"  # still waiting on the agent
            await session.commit()
            return
        res = await session.execute(select(PostResult).where(PostResult.post_id == post_id))
        results = res.scalars().all()
        post.status = "done" if any(r.status == "success" for r in results) else "failed"
        post.posted_at = utcnow()
        await session.commit()


async def execute_post(post_id: int) -> None:
    """Fire a scheduled post to all its target platforms. Resilient: one platform
    failing never blocks the others. Browser-login platforms are queued for the
    user's local agent."""
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if post is None:
            logger.warning("execute_post: post %s not found", post_id)
            return
        user_id = post.user_id
        platforms = json.loads(post.platforms) if post.platforms else []
        media_paths = [Path(p) for p in (json.loads(post.media_paths) if post.media_paths else [])]
        content = post.content
        scheduled_for = post.scheduled_for

        # Too overdue? Mark failed and don't post (e.g. server was down for hours).
        overdue = utcnow() - scheduled_for
        if overdue > timedelta(minutes=MAX_OVERDUE_MINUTES):
            post.status = "failed"
            await session.commit()
            logger.warning("Post %s is %s overdue; marking failed", post_id, overdue)
            return

        post.status = "posting"
        await session.commit()

    successes = 0
    failures = 0

    for platform in platforms:
        # Browser-login platforms run on the user's local agent — queue a job.
        if platform in SESSION_PLATFORMS:
            await _enqueue_agent_job(
                user_id, post_id, platform,
                adapt_caption_for_platform(content, platform), media_paths,
            )
            continue

        auth_data = await _load_auth_data(user_id, platform)
        if auth_data is None:
            await _save_result(user_id, post_id, platform, "skipped", None, "No connected account")
            failures += 1
            continue

        try:
            driver = get_driver(platform)
        except ValueError as exc:
            await _save_result(user_id, post_id, platform, "skipped", None, str(exc))
            failures += 1
            continue

        try:
            ok = await driver.authenticate(auth_data)
        except Exception:  # noqa: BLE001
            ok = False
            logger.exception("Auth error for %s", platform)

        if not ok:
            await _mark_account_status(user_id, platform, "expired")
            await _save_result(user_id, post_id, platform, "skipped", None, "Authentication failed/expired")
            failures += 1
            continue

        payload = PostPayload(
            content=adapt_caption_for_platform(content, platform),
            media_paths=media_paths,
            platform_options={},
        )

        try:
            platform_post_id = await post_with_jitter(driver, payload)
            await _save_result(user_id, post_id, platform, "success", platform_post_id, None)
            successes += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Posting to %s failed", platform)
            if exc.__class__.__name__ in ("InstagramSessionExpired", "LoginRequired", "ChallengeRequired"):
                await _mark_account_status(user_id, platform, "expired")
            await _save_result(user_id, post_id, platform, "failed", None, str(exc)[:500])
            failures += 1

    # Recompute status (stays "posting" if agent jobs are still pending).
    await finalize_post_status(post_id)
    logger.info("Post %s server-side complete: %s ok, %s failed", post_id, successes, failures)


__all__ = ["scheduler", "execute_post", "schedule_post", "unschedule_post"]
