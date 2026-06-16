"""The 'human approach': open a real visible browser, let the user log in, then
save the resulting session (cookies + storage) to data/sessions/<platform>.json.

capture_session() is blocking (it runs Playwright's sync-ish polling loop in its
own event loop), so callers should run it via asyncio.to_thread / a thread.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

PLATFORM_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "twitter": "https://x.com/login",
    "linkedin": "https://www.linkedin.com/login",
    "tiktok": "https://www.tiktok.com/login",
}

LOGGED_IN_INDICATORS = {
    "instagram": lambda page: "instagram.com" in page.url and "/login" not in page.url and "/accounts/login" not in page.url,
    "twitter": lambda page: "x.com/home" in page.url or "twitter.com/home" in page.url,
    "linkedin": lambda page: "linkedin.com/feed" in page.url,
    "tiktok": lambda page: "tiktok.com/foryou" in page.url or "tiktok.com/@" in page.url,
}

# The auth cookie whose presence reliably means "actually logged in" — far more
# robust than URL matching (the URL can match a logged-out landing page).
AUTH_COOKIES = {
    "instagram": "sessionid",
    "twitter": "auth_token",
    "linkedin": "li_at",
}


TIKTOK_PROFILE_DIR = Path("data/sessions/tiktok_profile")

# Platforms that refuse the bundled automated browser at login (TikTok, X, Instagram)
# use a persistent REAL-Chrome profile instead. For X/Instagram we then export the
# cookies to a storage_state JSON so the API-based drivers (twikit/instagrapi, which
# post over HTTP) can reuse them. TikTok posts from the profile directly (no JSON).
PERSISTENT_LOGIN_URLS = {
    "tiktok": "https://www.tiktok.com/login",
    "twitter": "https://x.com/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}
PERSISTENT_PLATFORMS = set(PERSISTENT_LOGIN_URLS)


async def _capture_persistent(platform: str) -> bool:
    """Login into a persistent real-Chrome profile (less detectable than bundled
    Chromium). For X/Instagram, export cookies to data/sessions/<platform>.json."""
    from playwright.async_api import async_playwright

    profile_dir = Path(f"data/sessions/{platform}_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    auth_cookie = AUTH_COOKIES.get(platform)            # twitter/instagram → real cookie
    export_json = platform in ("twitter", "instagram")  # tiktok posts from the profile
    session_json = Path(f"data/sessions/{platform}.json")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            from playwright_stealth import Stealth  # type: ignore

            await Stealth().apply_stealth_async(page)
        except Exception:
            pass

        await page.goto(PERSISTENT_LOGIN_URLS[platform])
        for _ in range(150):  # poll 2s x 150 = 5 min
            await asyncio.sleep(2)
            if not context.pages:
                await context.close()
                return False
            try:
                if auth_cookie:
                    names = {c["name"] for c in await context.cookies()}
                    ok = auth_cookie in names
                else:  # tiktok: on the site and off any login/signup page
                    url = page.url
                    ok = "tiktok.com" in url and not any(x in url for x in ("/login", "/signup"))
                if ok:
                    await asyncio.sleep(2)  # let cookies settle
                    if export_json:
                        await context.storage_state(path=str(session_json))
                    await context.close()
                    return True
            except Exception:
                pass
        await context.close()
        return False


async def capture_session(platform: str) -> bool:
    """Open a headed browser, let the user log in, detect success, save the session.

    Returns True on success, False if the user closes the browser or times out.
    """
    from playwright.async_api import async_playwright

    # TikTok / X / Instagram refuse the bundled browser at login → real-Chrome profile.
    if platform in PERSISTENT_PLATFORMS:
        return await _capture_persistent(platform)

    if platform not in PLATFORM_URLS:
        raise ValueError(f"No login flow for platform: {platform}")

    session_path = Path(f"data/sessions/{platform}.json")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    login_url = PLATFORM_URLS[platform]
    is_logged_in = LOGGED_IN_INDICATORS[platform]
    auth_cookie = AUTH_COOKIES.get(platform)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(viewport=None)
        page = await context.new_page()

        # Best-effort stealth (hides navigator.webdriver, etc.). Never fatal.
        try:
            from playwright_stealth import Stealth  # type: ignore

            await Stealth().apply_stealth_async(page)
        except Exception:
            try:
                from playwright_stealth import stealth_async  # type: ignore

                await stealth_async(page)
            except Exception:
                pass

        await page.goto(login_url)

        # Poll every 2 seconds for up to 5 minutes.
        for _ in range(150):
            await asyncio.sleep(2)

            # User closed the window?
            if not context.pages:
                try:
                    await browser.close()
                except Exception:
                    pass
                return False

            try:
                # Prefer the auth cookie (set only after a real login); fall back to URL.
                if auth_cookie:
                    names = {c["name"] for c in await context.cookies()}
                    logged_in = auth_cookie in names
                else:
                    logged_in = is_logged_in(page)
                if logged_in:
                    await context.storage_state(path=str(session_path))
                    await browser.close()
                    return True
            except Exception:
                # Page may be mid-navigation; ignore and retry.
                pass

        try:
            await browser.close()
        except Exception:
            pass
        return False


def capture_session_blocking(platform: str) -> bool:
    """Synchronous wrapper that runs capture_session in a fresh event loop.

    Meant to be dispatched via asyncio.to_thread() from the FastAPI handler so the
    headed browser never blocks the main server event loop.
    """
    return asyncio.run(capture_session(platform))


__all__ = ["capture_session", "capture_session_blocking", "PLATFORM_URLS"]
