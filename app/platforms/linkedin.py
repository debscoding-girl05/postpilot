"""LinkedIn driver: Playwright browser automation against the captured session.

LinkedIn has no usable public posting API for individuals, and the unofficial
HTTP libraries can't do video. So we drive the real "Start a post" composer with
the browser session captured during connect — attaching video/images, typing the
caption, and clicking Post.

LinkedIn aggressively detects automation, so:
  - we default to a VISIBLE browser (LINKEDIN_HEADLESS=true to force headless),
  - apply playwright-stealth when available,
  - wrap every selector in fallbacks, since LinkedIn's DOM changes often.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.platforms.base import BasePlatform, PostPayload

logger = logging.getLogger("postpilot.linkedin")

SESSION_PATH = Path("data/sessions/linkedin.json")
DEBUG_SHOT = Path("data/media/linkedin_debug.png")
FEED_URL = "https://www.linkedin.com/feed/"

# Selector fallbacks (first match wins). LinkedIn rotates these and is localized,
# so we cover EN + FR text plus language-independent class/aria selectors.
# Precise only — broad aria selectors matched analytics nav and navigated away.
START_POST_SELECTORS = [
    "button.share-box-feed-entry__trigger",
    "button:has-text('Commencer un post')",        # FR (confirmed)
    "[role='button']:has-text('Commencer un post')",
    "button:has-text('Start a post')",             # EN
    "[role='button']:has-text('Start a post')",
    "button:has-text('Créer un post')",
]
# Confirms the composer modal actually opened (vs. a stray navigation).
COMPOSER_OPEN_SELECTORS = [
    "div.share-creation-state",
    "div.share-box",
    "[role='dialog'] div.ql-editor",
    "[role='dialog']",
]
# Direct share-box media buttons (more reliable than opening the empty composer).
VIDEO_TRIGGER_SELECTORS = [
    "button:has-text('Vidéo')",
    "[aria-label*='Vidéo']",
    "[aria-label*='vidéo']",
    "button:has-text('Video')",
    "[aria-label*='video']",
]
PHOTO_TRIGGER_SELECTORS = [
    "button:has-text('Photo')",
    "[aria-label*='Photo']",
    "[aria-label*='photo']",
]
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")
EDITOR_SELECTORS = [
    "div.ql-editor[contenteditable='true']",
    "div[role='textbox'][contenteditable='true']",
    "div[data-placeholder][contenteditable='true']",
]
MEDIA_BUTTON_SELECTORS = [
    "button[aria-label='Add media']",
    "button[aria-label='Add a photo']",
    "button[aria-label*='media']",
    "button[aria-label*='média']",     # FR
    "button[aria-label*='photo']",
    "button[aria-label*='vidéo']",     # FR
    "button:has-text('Media')",
]
NEXT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Suivant')",      # FR
    "button.share-box-footer__primary-btn:has-text('Next')",
]
POST_SELECTORS = [
    "button.share-actions__primary-action",
    "button.share-box-footer__primary-btn",
    "button:has-text('Post'):not(:has-text('Start'))",
    "button:has-text('Publier')",      # FR
]


async def _dump_debug(page, reason: str) -> None:
    """Save a screenshot + log page state so selector failures can be diagnosed."""
    try:
        DEBUG_SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_SHOT), full_page=False)
    except Exception:
        pass
    try:
        buttons = await page.eval_on_selector_all(
            "button",
            "els => els.slice(0,40).map(e => (e.innerText||e.getAttribute('aria-label')||'').trim()).filter(Boolean)",
        )
        logger.error("LinkedIn debug [%s] url=%s title=%s buttons=%s",
                     reason, page.url, await page.title(), buttons)
    except Exception:
        logger.error("LinkedIn debug [%s] url=%s (could not enumerate buttons)", reason, page.url)


async def _find_first(scope, selectors, timeout=10000):
    """Poll all selectors quickly until one is visible or the deadline passes.

    query_selector is instant, so we sweep the whole list every 0.4s instead of
    blocking ~15s per selector (which previously caused overlapping runs)."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            try:
                el = await scope.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


async def _click_first(scope, selectors, timeout=10000):
    """Click the first selector that becomes visible. Returns True if clicked."""
    el = await _find_first(scope, selectors, timeout=timeout)
    if el is None:
        return False
    try:
        await el.click()
        return True
    except Exception:
        return False


class LinkedInPlatform(BasePlatform):
    name = "linkedin"
    char_limit = 3000
    supports_images = True
    supports_video = True

    async def authenticate(self, auth_data: dict) -> bool:
        # We can only truly verify at post time; treat a saved session as usable.
        return SESSION_PATH.exists()

    async def post(self, payload: PostPayload) -> str:
        from playwright.async_api import async_playwright

        if not SESSION_PATH.exists():
            raise RuntimeError("No LinkedIn session — connect LinkedIn first")

        caption = self.adapt_caption(payload.content)
        media = [p for p in payload.media_paths if Path(p).exists()]
        headless = os.getenv("LINKEDIN_HEADLESS", "false").lower() == "true"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, channel="chromium")
            context = await browser.new_context(storage_state=str(SESSION_PATH))
            page = await context.new_page()

            # Best-effort stealth (API differs across versions; never fatal).
            try:
                from playwright_stealth import Stealth  # type: ignore

                await Stealth().apply_stealth_async(page)
            except Exception:
                try:
                    from playwright_stealth import stealth_async  # type: ignore

                    await stealth_async(page)
                except Exception:
                    pass

            try:
                await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)

                if "/login" in page.url or "/checkpoint" in page.url or "/authwall" in page.url:
                    raise RuntimeError("LinkedIn session expired — reconnect LinkedIn")

                if media:
                    # Click the share-box "Vidéo"/"Photo" button directly — it opens a
                    # native file chooser, which we intercept (the supported pattern).
                    files = [str(m) for m in media]
                    is_video = any(Path(m).suffix.lower() in VIDEO_EXTS for m in media)
                    trigger = VIDEO_TRIGGER_SELECTORS if is_video else PHOTO_TRIGGER_SELECTORS
                    set_ok = False
                    try:
                        async with page.expect_file_chooser(timeout=15000) as fc_info:
                            if not await _click_first(page, trigger, timeout=12000):
                                raise RuntimeError("media trigger button not found")
                        chooser = await fc_info.value
                        await chooser.set_files(files)
                        set_ok = True
                    except Exception:
                        pass

                    # Fallback: open the composer, then click its media button.
                    if not set_ok:
                        await _click_first(page, START_POST_SELECTORS, timeout=10000)
                        await asyncio.sleep(2)
                        try:
                            async with page.expect_file_chooser(timeout=10000) as fc_info:
                                await _click_first(page, MEDIA_BUTTON_SELECTORS + trigger, timeout=8000)
                            chooser = await fc_info.value
                            await chooser.set_files(files)
                            set_ok = True
                        except Exception:
                            for sel in ("input[type='file'][accept*='video']",
                                        "input[type='file'][accept*='image']",
                                        "input[type='file']"):
                                try:
                                    inp = await page.wait_for_selector(sel, timeout=4000, state="attached")
                                    if inp:
                                        await inp.set_input_files(files)
                                        set_ok = True
                                        break
                                except Exception:
                                    continue

                    if not set_ok:
                        await _dump_debug(page, "media_attach_failed")
                        raise RuntimeError(f"Could not attach media to LinkedIn (screenshot: {DEBUG_SHOT})")

                    await asyncio.sleep(3)
                    # Media "Éditeur" appears; wait for processing, then click through
                    # any "Suivant"/"Next" steps back to the composer (polling waits).
                    for _ in range(3):
                        if not await _click_first(page, NEXT_SELECTORS, timeout=60000):
                            break
                        await asyncio.sleep(2)
                else:
                    # Text-only: open the empty composer and confirm it opened.
                    if not await _click_first(page, START_POST_SELECTORS, timeout=15000):
                        await _dump_debug(page, "start_post_not_found")
                        raise RuntimeError(f"Could not find 'Commencer un post' (screenshot: {DEBUG_SHOT})")
                    await asyncio.sleep(2)
                    if await _find_first(page, COMPOSER_OPEN_SELECTORS, timeout=10000) is None:
                        await _dump_debug(page, "composer_did_not_open")
                        raise RuntimeError(f"LinkedIn composer did not open (at {page.url}; screenshot: {DEBUG_SHOT})")

                # 3. Type the caption.
                editor = await _find_first(page, EDITOR_SELECTORS, timeout=20000)
                if editor is None:
                    await _dump_debug(page, "editor_not_found")
                    raise RuntimeError(f"Could not find LinkedIn text editor (screenshot: {DEBUG_SHOT})")
                await editor.click()
                await page.keyboard.type(caption, delay=15)
                await asyncio.sleep(2)

                # 4. Post.
                if not await _click_first(page, POST_SELECTORS, timeout=10000):
                    await _dump_debug(page, "post_button_not_found")
                    raise RuntimeError(f"Could not find LinkedIn 'Post' button (screenshot: {DEBUG_SHOT})")

                # Wait for the composer to close / upload to finalize.
                await asyncio.sleep(12)
                return "posted"
            finally:
                await context.close()
                await browser.close()


__all__ = ["LinkedInPlatform"]
