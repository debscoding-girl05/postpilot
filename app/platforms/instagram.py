"""Instagram driver — posts by driving the real instagram.com web composer over CDP.

instagrapi (private API) gets blocked with login_required even on fresh web
sessions. Instead we drive the actual "Create" flow in the user's logged-in
PostPilot Chrome (see app/platforms/browser.py).

Instagram requires media — text-only posts are rejected.
"Connected" means: you're logged into Instagram in the PostPilot Chrome window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.platforms.base import BasePlatform, PostPayload
from app.platforms.browser import cdp_available, cdp_page

logger = logging.getLogger("postpilot.instagram")

HOME_URL = "https://www.instagram.com/"
DEBUG_SHOT = Path("data/media/instagram_debug.png")


class InstagramSessionExpired(Exception):
    """Kept for compatibility — raised when not logged in at post time."""


# Selectors cover EN + FR. First visible match wins.
NEW_POST_SELECTORS = [
    "svg[aria-label='New post']",
    "svg[aria-label='Nouvelle publication']",
    "[aria-label='New post']",
    "[aria-label='Nouvelle publication']",
    "a[href='#'] svg[aria-label='New post']",
]
# After clicking "+", a small menu may offer Post vs. Reel — pick the plain Post.
POST_MENU_SELECTORS = [
    "svg[aria-label='Post']",
    "[aria-label='Post']",
    "span:has-text('Post')",
    "span:has-text('Publication')",
]
SELECT_FILE_SELECTORS = [
    "button:has-text('Select from computer')",
    "button:has-text('ordinateur')",  # FR: "Sélectionner sur l'ordinateur"
    "button:has-text('Selecionar do computador')",
]
NEXT_SELECTORS = [
    "div[role='button']:has-text('Next')",
    "button:has-text('Next')",
    "div[role='button']:has-text('Suivant')",
    "button:has-text('Suivant')",
]
SHARE_SELECTORS = [
    "div[role='button']:has-text('Share')",
    "button:has-text('Share')",
    "div[role='button']:has-text('Partager')",
    "button:has-text('Partager')",
]
CAPTION_SELECTORS = [
    "div[aria-label='Write a caption...'][contenteditable='true']",
    "div[aria-label='Écrivez une légende...'][contenteditable='true']",
    "textarea[aria-label^='Write a caption']",
    "div[contenteditable='true'][role='textbox']",
]
OK_DIALOG_SELECTORS = [
    "button:has-text('OK')",
    "button:has-text('Ok')",
]
LOGGED_OUT_MARKERS = ("/accounts/login", "/accounts/emailsignup")
LOGIN_FORM_SELECTORS = [
    "input[name='username']",
    "input[name='password']",
    "a:has-text('Log in')",
    "a:has-text('Se connecter')",
]


async def _logged_out(page) -> bool:
    if any(m in page.url for m in LOGGED_OUT_MARKERS):
        return True
    # IG's logged-out home stays at instagram.com/ but shows a login form.
    for sel in LOGIN_FORM_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _find(page, selectors, timeout=12000):
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


async def _click(page, selectors, timeout=12000) -> bool:
    el = await _find(page, selectors, timeout=timeout)
    if el is None:
        return False
    try:
        await el.click()
        return True
    except Exception:
        # Some IG controls are svgs; click the nearest button ancestor.
        try:
            await el.evaluate("e => (e.closest('button,[role=button],a') || e).click()")
            return True
        except Exception:
            return False


async def _dump(page, reason: str) -> None:
    try:
        DEBUG_SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_SHOT))
        logger.error("Instagram debug [%s] url=%s", reason, page.url)
    except Exception:
        pass


class InstagramPlatform(BasePlatform):
    name = "instagram"
    char_limit = 2200
    supports_images = True
    supports_video = True

    async def authenticate(self, auth_data: dict) -> bool:
        return await cdp_available()

    async def post(self, payload: PostPayload) -> str:
        if not payload.media_paths:
            raise ValueError("Instagram requires at least one image or video")
        caption = self.adapt_caption(payload.content)
        media = [str(Path(p)) for p in payload.media_paths if Path(p).exists()]
        if not media:
            raise ValueError("Instagram media file not found")

        async with cdp_page() as page:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            if await _logged_out(page):
                raise InstagramSessionExpired(
                    "Not logged into Instagram — log in at instagram.com in the PostPilot Chrome window"
                )

            # Open the create dialog.
            if not await _click(page, NEW_POST_SELECTORS, timeout=15000):
                await _dump(page, "no_new_post")
                raise RuntimeError(f"Could not open Instagram 'New post' (screenshot: {DEBUG_SHOT})")
            await asyncio.sleep(1)
            await _click(page, POST_MENU_SELECTORS, timeout=3000)  # optional submenu

            # Attach the media via the "Select from computer" file chooser.
            try:
                async with page.expect_file_chooser(timeout=15000) as fc:
                    if not await _click(page, SELECT_FILE_SELECTORS, timeout=12000):
                        raise RuntimeError("select-from-computer button not found")
                chooser = await fc.value
                await chooser.set_files(media)
            except Exception:
                await _dump(page, "file_attach_failed")
                raise RuntimeError(f"Could not attach media to Instagram (screenshot: {DEBUG_SHOT})")
            await asyncio.sleep(3)
            await _click(page, OK_DIALOG_SELECTORS, timeout=4000)  # "shared as reel" dialog

            # Crop → Next, Edit → Next (two steps).
            for _ in range(2):
                if not await _click(page, NEXT_SELECTORS, timeout=20000):
                    break
                await asyncio.sleep(2)

            # Caption.
            cap = await _find(page, CAPTION_SELECTORS, timeout=15000)
            if cap is not None:
                await cap.click()
                await page.keyboard.type(caption, delay=8)
                await asyncio.sleep(1)

            # Share, then wait for upload to finalize.
            if not await _click(page, SHARE_SELECTORS, timeout=15000):
                await _dump(page, "no_share")
                raise RuntimeError(f"Could not find Instagram 'Share' button (screenshot: {DEBUG_SHOT})")
            await asyncio.sleep(12)
            return "posted"


__all__ = ["InstagramPlatform", "InstagramSessionExpired"]
