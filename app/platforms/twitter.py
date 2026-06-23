"""X (Twitter) driver — posts by driving the real x.com web composer over CDP.

The reverse-engineered HTTP libraries (twikit) keep breaking against X's private
API. Instead we drive the actual web UI in the user's logged-in PostPilot Chrome
(see app/platforms/browser.py), which is robust and looks human.

"Connected" means: you're logged into X in the PostPilot Chrome window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.platforms.base import BasePlatform, PostPayload
from app.platforms.browser import cdp_available, cdp_page

logger = logging.getLogger("postpilot.twitter")

COMPOSE_URL = "https://x.com/compose/post"
DEBUG_SHOT = Path("data/media/twitter_debug.png")

EDITOR_SELECTORS = [
    "div[data-testid='tweetTextarea_0']",
    "div[role='textbox'][contenteditable='true']",
]
FILE_INPUT_SELECTORS = [
    "input[data-testid='fileInput']",
    "input[type='file']",
]
POST_BUTTON_SELECTORS = [
    "button[data-testid='tweetButton']",
    "button[data-testid='tweetButtonInline']",
]
LOGGED_OUT_MARKERS = (
    "/login", "/i/flow/login", "/account/access", "/logout",
    "redirect_after_login", "mode=login", "/onboarding",
)
LOGIN_BUTTON_SELECTORS = [
    "a[data-testid='loginButton']",
    "a[data-testid='login']",
    "a[href='/login']",
]
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


async def _looks_logged_out(page) -> bool:
    if any(m in page.url for m in LOGGED_OUT_MARKERS):
        return True
    # Logged-out X bounces /compose/post to the bare landing page with a login CTA.
    for sel in LOGIN_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _find(page, selectors, timeout=15000):
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


async def _robust_click(el) -> bool:
    """Click that survives overlays/animations: normal click, then force, then JS."""
    for attempt in ("normal", "force", "js"):
        try:
            if attempt == "normal":
                await el.click(timeout=8000)
            elif attempt == "force":
                await el.click(force=True, timeout=8000)
            else:
                await el.evaluate("e => (e.closest('button,[role=button]') || e).click()")
            return True
        except Exception:
            continue
    return False


async def _wait_enabled(page, selectors, timeout=180000):
    """Wait for the Post button to become clickable (X disables it until the
    caption is non-empty and any media finishes uploading)."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    disabled = await el.get_attribute("aria-disabled")
                    if disabled != "true":
                        return el
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.6)


class TwitterPlatform(BasePlatform):
    name = "twitter"
    char_limit = 280
    supports_images = True
    supports_video = True

    async def authenticate(self, auth_data: dict) -> bool:
        # Login lives in the PostPilot Chrome; we can only confirm it's reachable.
        return await cdp_available()

    async def post(self, payload: PostPayload) -> str:
        caption = self.adapt_caption(payload.content)
        media = [Path(p) for p in payload.media_paths if Path(p).exists()][:4]

        async with cdp_page() as page:
            await page.goto(COMPOSE_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            if await _looks_logged_out(page):
                raise RuntimeError("Not logged into X — log in at x.com in the PostPilot Chrome window")

            editor = await _find(page, EDITOR_SELECTORS, timeout=20000)
            if editor is None:
                await _dump(page, "no_editor")
                raise RuntimeError(f"Could not find the X composer (screenshot: {DEBUG_SHOT})")
            # Focus rather than click — the modal backdrop can intercept a real click.
            try:
                await editor.focus()
            except Exception:
                await _robust_click(editor)
            await page.keyboard.type(caption, delay=10)
            await asyncio.sleep(1)

            if media:
                file_input = await _find(page, FILE_INPUT_SELECTORS, timeout=10000)
                if file_input is None:
                    await _dump(page, "no_file_input")
                    raise RuntimeError(f"Could not find X media upload input (screenshot: {DEBUG_SHOT})")
                await file_input.set_input_files([str(m) for m in media])
                # Video transcodes server-side; the Post button stays disabled until done.
                await asyncio.sleep(4)

            post_btn = await _wait_enabled(page, POST_BUTTON_SELECTORS, timeout=240000)
            if post_btn is None:
                await _dump(page, "post_disabled")
                raise RuntimeError(
                    f"X Post button never enabled (upload may have failed; screenshot: {DEBUG_SHOT})"
                )
            if not await _robust_click(post_btn):
                await _dump(page, "post_click_failed")
                raise RuntimeError(f"Could not click X Post button (screenshot: {DEBUG_SHOT})")
            await asyncio.sleep(8)  # let the post commit / modal close
            return "posted"


async def _dump(page, reason: str) -> None:
    try:
        DEBUG_SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_SHOT))
        logger.error("X debug [%s] url=%s", reason, page.url)
    except Exception:
        pass


__all__ = ["TwitterPlatform"]
