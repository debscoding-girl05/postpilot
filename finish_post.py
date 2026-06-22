#!/usr/bin/env python
"""Finish a post's browser-login platforms from your terminal (GUI session).

The always-on background service can't launch a real browser, so LinkedIn / X /
Instagram / TikTok posts fail there. Run this from a logged-in terminal — where
a browser *can* open — to push the platforms that haven't succeeded yet.

Usage:
    python finish_post.py <post_id>          # finish all not-yet-succeeded platforms
    python finish_post.py <post_id> linkedin # only that platform
    python finish_post.py --pending          # list posts that still need a browser run

It skips platforms already marked "success", records a real PostResult, and
updates the post's overall status — same bookkeeping as the scheduler.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.content_processor import adapt_caption_for_platform  # noqa: E402
from app.database import Post, PostResult, get_session, select  # noqa: E402
from app.platforms.base import PostPayload  # noqa: E402
from app.scheduler import (  # noqa: E402
    _authenticate_with_retry,
    _load_auth_data,
    _save_result,
    get_driver,
)

# Platforms that need a real browser (and so must run from a GUI terminal).
BROWSER_PLATFORMS = {"linkedin", "twitter", "instagram", "tiktok"}


async def _succeeded(post_id: int) -> set[str]:
    async with get_session() as s:
        rows = await s.execute(
            select(PostResult).where(
                PostResult.post_id == post_id, PostResult.status == "success"
            )
        )
        return {r.platform for r in rows.scalars().all()}


async def list_pending() -> None:
    async with get_session() as s:
        rows = await s.execute(select(Post).order_by(Post.id.desc()))
        posts = rows.scalars().all()
    print("Posts that may still need a browser run:")
    for p in posts:
        platforms = set(json.loads(p.platforms) if p.platforms else [])
        done = await _succeeded(p.id)
        pending = (platforms & BROWSER_PLATFORMS) - done
        if pending and p.status != "done":
            print(f"  id={p.id} status={p.status:8} pending={sorted(pending)}  {p.content[:50]!r}")


async def finish(post_id: int, only: str | None) -> None:
    async with get_session() as s:
        post = await s.get(Post, post_id)
        if post is None:
            print(f"Post {post_id} not found.")
            return
        platforms = json.loads(post.platforms) if post.platforms else []
        media_paths = [
            Path(p) for p in (json.loads(post.media_paths) if post.media_paths else [])
        ]
        content = post.content

    done = await _succeeded(post_id)
    targets = [
        p
        for p in platforms
        if p in BROWSER_PLATFORMS and p not in done and (only is None or p == only)
    ]
    if not targets:
        print(f"Nothing to do for post {post_id} (already done or no browser platforms).")
        return

    print(f"Post {post_id}: posting {targets} via browser (a window will open)…")
    any_ok = False
    for platform in targets:
        auth = await _load_auth_data(platform)
        if auth is None:
            print(f"  {platform}: no connected account — skipping")
            continue
        driver = get_driver(platform)
        ok, err = await _authenticate_with_retry(driver, auth, platform)
        if not ok:
            print(f"  {platform}: auth failed — {err}")
            await _save_result(post_id, platform, "skipped", None, f"Authentication failed: {err}")
            continue
        payload = PostPayload(
            content=adapt_caption_for_platform(content, platform),
            media_paths=media_paths,
            platform_options={},
        )
        try:
            pid = await driver.post(payload)
            await _save_result(post_id, platform, "success", pid, None)
            any_ok = True
            print(f"  {platform}: POSTED ✓  ({pid})")
        except Exception as exc:  # noqa: BLE001
            await _save_result(post_id, platform, "failed", None, str(exc)[:500])
            print(f"  {platform}: FAILED — {exc}")

    if any_ok:
        async with get_session() as s:
            post = await s.get(Post, post_id)
            if post and post.status != "done":
                post.status = "done"
                await s.commit()
        print(f"Post {post_id} marked done.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--pending":
        asyncio.run(list_pending())
        return
    post_id = int(args[0])
    only = args[1] if len(args) > 1 else None
    asyncio.run(finish(post_id, only))


if __name__ == "__main__":
    main()
