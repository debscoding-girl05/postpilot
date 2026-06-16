"""TikTok driver: Playwright browser automation against the captured session.

TikTok has no public posting API for individuals, so we drive the TikTok Studio
upload page with the browser session captured during connect.

TikTok has very aggressive bot detection, so:
  - we default to a VISIBLE browser (TIKTOK_HEADLESS=true to force headless),
  - apply playwright-stealth when available,
  - try TikTok Studio first (uploads moved there), fall back to /upload,
  - search the main page AND iframes for the upload input,
  - save a debug screenshot on failure.

Even so, TikTok may show a captcha / verification that blocks automation — that's
a TikTok-side limitation, not a wiring bug.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.platforms.base import BasePlatform, PostPayload

logger = logging.getLogger("postpilot.tiktok")

SESSION_PATH = Path("data/sessions/tiktok.json")
# TikTok aggressively fingerprints automated browsers, so we use a persistent,
# real-Chrome profile (login + posting share it) instead of an ephemeral context.
TIKTOK_PROFILE_DIR = Path("data/sessions/tiktok_profile")
DEBUG_SHOT = Path("data/media/tiktok_debug.png")
UPLOAD_URLS = [
    "https://www.tiktok.com/tiktokstudio/upload?from=upload&lang=en",
    "https://www.tiktok.com/upload?lang=en",
]
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")

SELECT_VIDEO_SELECTORS = [
    "button:has-text('Sélectionner une vidéo')",   # FR
    "button:has-text('Select video')",
    "button:has-text('Sélectionner')",
    "button:has-text('Select')",
    "[data-e2e='select_video_button']",
]
CAPTION_SELECTORS = [
    "div.public-DraftEditor-content",
    "div[contenteditable='true']",
    "div[data-contents='true']",
    "div[data-text='true']",
]
POST_SELECTORS = [
    "button[data-e2e='post_video_button']",
    "div[data-e2e='post_video_button'] button",
    "button:has-text('Post')",
    "button:has-text('Publier')",   # FR
]


def _scopes(page):
    """The page plus all its frames — TikTok sometimes renders inside an iframe."""
    return [page, *page.frames]


async def _find_in_scopes(page, selectors, timeout=20000, state="visible"):
    deadline = time.monotonic() + timeout / 1000
    while True:
        for scope in _scopes(page):
            for sel in selectors:
                try:
                    el = await scope.query_selector(sel)
                    if el and (state != "visible" or await el.is_visible()):
                        return el
                except Exception:
                    continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


async def _click_in_scopes(page, selectors, timeout=15000):
    el = await _find_in_scopes(page, selectors, timeout=timeout)
    if el is None:
        return False
    try:
        await el.click()
        return True
    except Exception:
        return False


async def _is_blocked(page) -> bool:
    """Detect TikTok's error / anti-bot page ('Une erreur est survenue' + Retry)."""
    for sel in (
        "text=Une erreur est survenue",
        "text=Something went wrong",
        "button:has-text('Réessayer')",
        "button:has-text('Try again')",
    ):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _dismiss_tour(page, press_escape: bool = False) -> None:
    """TikTok Studio shows product-tour / feature-announcement overlays that intercept
    clicks. Remove the joyride overlay and click any close/skip button.

    press_escape is only safe BEFORE upload — pressing Escape in the editor cancels
    the in-progress upload and reverts to the select-video screen."""
    try:
        await page.evaluate(
            "() => document.querySelectorAll("
            "'#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight'"
            ").forEach(e => e.remove())"
        )
    except Exception:
        pass
    for sel in (
        "button[data-test-id='close-button']",
        "button[aria-label='Close']",
        "button[aria-label='Fermer']",       # FR
        "div[role='dialog'] button[aria-label*='lose']",
        "button:has-text('Skip')",
        "button:has-text('Ignorer')",        # FR
        "button:has-text('Passer')",         # FR
        "button:has-text('Got it')",
        "button:has-text('Compris')",        # FR
        "button:has-text(\"J'ai compris\")", # FR
        "button:has-text('OK')",
    ):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.3)
        except Exception:
            continue
    # Escape closes many modals/announcements — but cancels an in-progress upload,
    # so only use it before the upload starts.
    if press_escape:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def _dump_debug(page, reason: str) -> None:
    try:
        DEBUG_SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_SHOT), full_page=False)
    except Exception:
        pass
    try:
        logger.error("TikTok debug [%s] url=%s title=%s", reason, page.url, await page.title())
    except Exception:
        logger.error("TikTok debug [%s] url=%s", reason, page.url)


class TikTokPlatform(BasePlatform):
    name = "tiktok"
    char_limit = 2200
    supports_images = False
    supports_video = True

    async def authenticate(self, auth_data: dict) -> bool:
        return TIKTOK_PROFILE_DIR.exists() and any(TIKTOK_PROFILE_DIR.iterdir())

    async def post(self, payload: PostPayload) -> str:
        from playwright.async_api import async_playwright

        if not (TIKTOK_PROFILE_DIR.exists() and any(TIKTOK_PROFILE_DIR.iterdir())):
            raise RuntimeError("No TikTok session — connect TikTok first")

        videos = [p for p in payload.media_paths if Path(p).suffix.lower() in VIDEO_EXTS]
        if not videos:
            raise ValueError("TikTok requires a video file")
        video_path = str(videos[0])
        caption = self.adapt_caption(payload.content)
        headless = os.getenv("TIKTOK_HEADLESS", "false").lower() == "true"

        async with async_playwright() as p:
            # Persistent real-Chrome profile — the same one used to log in.
            context = await p.chromium.launch_persistent_context(
                str(TIKTOK_PROFILE_DIR),
                channel="chrome",
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()

            # Best-effort stealth.
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
                # 1. Open the upload page and wait for the upload widget to load.
                await page.goto(UPLOAD_URLS[0], wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                if "/login" in page.url:
                    raise RuntimeError("TikTok session expired — reconnect TikTok")
                await _dismiss_tour(page, press_escape=True)
                if await _is_blocked(page):
                    await _dump_debug(page, "tiktok_blocked")
                    raise RuntimeError(
                        "TikTok showed an error/anti-bot page ('Une erreur est survenue'). "
                        "This happens after repeated automated uploads — wait ~15-30 min and "
                        f"retry, or reconnect TikTok (screenshot: {DEBUG_SHOT})"
                    )
                if await _find_in_scopes(page, SELECT_VIDEO_SELECTORS, timeout=30000) is None:
                    # Fall back to the legacy upload URL.
                    await page.goto(UPLOAD_URLS[1], wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(4)
                    await _dismiss_tour(page, press_escape=True)

                video_inputs = ["input[type=file][accept*=video]",
                                "input[type=file][accept*=mp4]",
                                "input[type=file]"]

                async def _attach_once() -> bool:
                    inp = await _find_in_scopes(page, video_inputs, timeout=15000, state="attached")
                    if inp is None:
                        return False
                    await inp.set_input_files(video_path)
                    # Confirm the upload actually started: the "Sélectionner une vidéo"
                    # widget is replaced by the editor, so the Select button disappears.
                    progress_deadline = time.monotonic() + 60
                    while time.monotonic() < progress_deadline:
                        if await _find_in_scopes(page, SELECT_VIDEO_SELECTORS, timeout=1500) is None:
                            return True
                        await asyncio.sleep(2)
                    return False

                uploaded = await _attach_once()
                if not uploaded:
                    await _dismiss_tour(page, press_escape=True)
                    uploaded = await _attach_once()  # one retry

                if await _is_blocked(page):
                    await _dump_debug(page, "tiktok_blocked")
                    raise RuntimeError(
                        "TikTok showed an error/anti-bot page mid-upload. Wait ~15-30 min and "
                        f"retry, or reconnect TikTok (screenshot: {DEBUG_SHOT})"
                    )
                if not uploaded:
                    await _dump_debug(page, "upload_failed")
                    raise RuntimeError(
                        "Could not start TikTok upload — TikTok likely blocked the automated "
                        f"browser or showed a verification (screenshot: {DEBUG_SHOT})"
                    )

                # 2. Wait for upload + processing: the caption editor appears and is
                # auto-filled with the filename. Poll for it to become non-empty so we
                # don't mistake an unrelated empty contenteditable for the caption box.
                editor = None
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    if await _is_blocked(page):
                        await _dump_debug(page, "tiktok_blocked")
                        raise RuntimeError(
                            "TikTok served its anti-bot error page on the upload screen. "
                            "TikTok blocks automated uploads from this browser/IP — see notes. "
                            f"(screenshot: {DEBUG_SHOT})"
                        )
                    cand = await _find_in_scopes(page, CAPTION_SELECTORS, timeout=5000)
                    if cand is not None:
                        try:
                            if (await cand.inner_text()).strip():
                                editor = cand
                                break
                        except Exception:
                            pass
                    await asyncio.sleep(2)
                if editor is None:
                    # Last resort: any caption editor, even if empty.
                    editor = await _find_in_scopes(page, CAPTION_SELECTORS, timeout=5000)
                if editor is None:
                    await _dump_debug(page, "caption_editor_not_found")
                    raise RuntimeError(f"TikTok upload didn't finish processing (screenshot: {DEBUG_SHOT})")

                # 3. Set the caption. TikTok pre-fills it with the video FILENAME
                # (our UUID), so we must select that text and replace it. The editor
                # re-renders (React), so re-find it fresh each attempt and verify the
                # text actually changed — never silently post the filename.
                probe = caption.strip()[:25]
                typed = False
                last_got = ""
                for attempt in range(3):
                    await _dismiss_tour(page)  # clear the product-tour overlay
                    await asyncio.sleep(0.5)
                    editor = await _find_in_scopes(page, CAPTION_SELECTORS, timeout=15000)
                    if editor is None:
                        continue
                    try:
                        await editor.click(click_count=3)  # select the pre-filled line
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Backspace")
                        for combo in ("Meta+A", "Control+A"):  # mac / others select-all
                            await page.keyboard.press(combo)
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(0.3)
                        await page.keyboard.type(caption, delay=20)
                        await asyncio.sleep(1.5)
                        last_got = (await editor.inner_text()).strip()
                        if not probe or probe in last_got:
                            typed = True
                            break
                    except Exception as exc:  # stale handle / re-render — retry fresh
                        logger.warning("TikTok caption attempt %s: %s", attempt + 1, exc)
                        await asyncio.sleep(1)

                if not typed:
                    await _dump_debug(page, "caption_not_set")
                    raise RuntimeError(
                        f"TikTok caption did not update (field shows '{last_got[:40]}'); "
                        f"screenshot: {DEBUG_SHOT}"
                    )
                await asyncio.sleep(2)

                # 4. Post (dismiss any tour step that reappeared first).
                await _dismiss_tour(page)
                if not await _click_in_scopes(page, POST_SELECTORS, timeout=15000):
                    await _dump_debug(page, "post_button_not_found")
                    raise RuntimeError(f"Could not find TikTok 'Post' button (screenshot: {DEBUG_SHOT})")

                # Give TikTok time to submit.
                await asyncio.sleep(12)
                return "posted"
            finally:
                await context.close()


__all__ = ["TikTokPlatform"]
