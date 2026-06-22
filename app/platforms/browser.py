"""Shared browser access for the locked platforms (X, Instagram, LinkedIn, TikTok).

Why CDP: the reverse-engineered API libraries (twikit/instagrapi) keep breaking as
the platforms harden their private APIs, and freshly-launched throwaway browsers
look automated and get challenged. Instead we drive a REAL Chrome over the
DevTools protocol (CDP) — a persistent profile you log into once, with a real
browser fingerprint. Because CDP is just an HTTP connection to localhost, even the
always-on background service can post through it; no separate agent needed.

Start the browser with the bundled launcher:

    ./postpilot-chrome            # opens Chrome with --remote-debugging-port=9222

(Recent Chrome blocks remote-debugging on the *default* profile, so the launcher
uses a dedicated PostPilot profile — log into each platform once in that window.)

Env:
  CHROME_CDP_URL   CDP endpoint (default http://127.0.0.1:9222)
"""
from __future__ import annotations

import contextlib
import logging
import os

import httpx

logger = logging.getLogger("postpilot.browser")

LAUNCH_HINT = (
    "PostPilot's Chrome isn't running. Start it with the ./postpilot-chrome "
    "launcher (it opens Chrome with remote-debugging on port 9222), make sure "
    "you're logged into the platform there, then try again."
)


def cdp_url() -> str:
    return os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9222").rstrip("/")


async def cdp_available() -> bool:
    """True if PostPilot's Chrome is reachable over CDP."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cdp_url()}/json/version")
            return resp.status_code == 200
    except Exception:
        return False


@contextlib.asynccontextmanager
async def cdp_page():
    """Open a new tab in the user's running Chrome over CDP and yield the Page.

    Cleans up ONLY the tab we created — never the user's browser or context.
    Raises RuntimeError with a friendly hint if Chrome isn't running with CDP.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(LAUNCH_HINT) from exc

        # The real Chrome already has a default context holding all the logins.
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            yield page
        finally:
            # Close ONLY our tab. Leaving async_playwright's __aexit__ to tear down
            # the driver disconnects the CDP session without closing the user's Chrome.
            with contextlib.suppress(Exception):
                await page.close()


__all__ = ["cdp_url", "cdp_available", "cdp_page", "LAUNCH_HINT"]
