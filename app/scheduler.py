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
    Post,
    PostResult,
    SYNC_DATABASE_URL,
    get_session,
    select,
    utcnow,
)
from app.platforms import CREDENTIAL_PLATFORMS, get_driver
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


async def _mark_account_status(platform: str, status: str) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.platform == platform)
        )
        account = result.scalars().first()
        if account:
            account.status = status
            await session.commit()


async def _load_auth_data(platform: str) -> dict | None:
    """Return decrypted auth_data for credential platforms, {} for session platforms,
    or None if no active account exists."""
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.platform == platform)
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
    post_id: int, platform: str, status: str, platform_post_id: str | None, error: str | None
) -> None:
    async with get_session() as session:
        session.add(
            PostResult(
                post_id=post_id,
                platform=platform,
                status=status,
                platform_post_id=platform_post_id,
                error_msg=error,
            )
        )
        await session.commit()


async def execute_post(post_id: int) -> None:
    """Fire a scheduled post to all its target platforms. Resilient: one platform
    failing never blocks the others."""
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if post is None:
            logger.warning("execute_post: post %s not found", post_id)
            return
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
        auth_data = await _load_auth_data(platform)
        if auth_data is None:
            await _save_result(post_id, platform, "skipped", None, "No connected account")
            failures += 1
            continue

        try:
            driver = get_driver(platform)
        except ValueError as exc:
            await _save_result(post_id, platform, "skipped", None, str(exc))
            failures += 1
            continue

        try:
            ok = await driver.authenticate(auth_data)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.exception("Auth error for %s", platform)

        if not ok:
            await _mark_account_status(platform, "expired")
            await _save_result(post_id, platform, "skipped", None, "Authentication failed/expired")
            failures += 1
            continue

        payload = PostPayload(
            content=adapt_caption_for_platform(content, platform),
            media_paths=media_paths,
            platform_options={},
        )

        try:
            platform_post_id = await post_with_jitter(driver, payload)
            await _save_result(post_id, platform, "success", platform_post_id, None)
            successes += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Posting to %s failed", platform)
            # instagrapi session expiry → mark account expired.
            if exc.__class__.__name__ in ("InstagramSessionExpired", "LoginRequired", "ChallengeRequired"):
                await _mark_account_status(platform, "expired")
            await _save_result(post_id, platform, "failed", None, str(exc)[:500])
            failures += 1

    # Finalize post status.
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if post:
            if successes > 0:
                post.status = "done"
            else:
                post.status = "failed"
            post.posted_at = utcnow()
            await session.commit()

    logger.info("Post %s complete: %s ok, %s failed", post_id, successes, failures)


__all__ = ["scheduler", "execute_post", "schedule_post", "unschedule_post"]
