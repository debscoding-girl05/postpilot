"""PostPilot local agent — runs on the user's own machine.

The hosted PostPilot can't open browsers for browser-login platforms (LinkedIn,
Instagram, X, TikTok), so this agent does it locally and syncs with the server:
it logs you in once per platform, then polls for queued posts, posts them with the
same drivers the single-user app uses, and reports results back.

Setup (from your PostPilot dashboard, copy your agent token):

    export PP_SERVER_URL="https://your-postpilot-host"     # default: http://localhost:8000
    export PP_AGENT_TOKEN="ppa_..."

Connect a platform once (opens a browser to log in):

    python agent.py connect linkedin

Then keep it running to post your scheduled jobs:

    python agent.py run
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx

from app import session_capture
from app.platforms import SESSION_PLATFORMS, get_driver
from app.platforms.base import PostPayload

SERVER = os.getenv("PP_SERVER_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("PP_AGENT_TOKEN", "")
POLL_SECONDS = float(os.getenv("PP_POLL_SECONDS", "20"))
SESSIONS = Path("data/sessions")


def _headers() -> dict:
    return {"X-Agent-Token": TOKEN}


def _has_session(platform: str) -> bool:
    if (SESSIONS / f"{platform}.json").exists():
        return True
    profile = SESSIONS / f"{platform}_profile"
    return profile.is_dir() and any(profile.iterdir())


def connected_platforms() -> list[str]:
    return [p for p in SESSION_PLATFORMS if _has_session(p)]


async def cmd_connect(platform: str) -> None:
    if platform not in SESSION_PLATFORMS:
        print(f"'{platform}' isn't a browser-login platform. Choose: {', '.join(SESSION_PLATFORMS)}")
        return
    print(f"Opening a browser to log into {platform} — log in normally, then come back…")
    ok = await session_capture.capture_session(platform)
    print("Connected ✅" if ok else "Not connected (window closed or timed out).")


async def _download_media(client: httpx.AsyncClient, url: str) -> Path:
    resp = await client.get(SERVER + url, headers=_headers())
    resp.raise_for_status()
    name = url.rsplit("/", 1)[-1]
    tmp = tempfile.NamedTemporaryFile(suffix=Path(name).suffix or ".bin", delete=False)
    tmp.write(resp.content)
    tmp.close()
    return Path(tmp.name)


async def _process_job(client: httpx.AsyncClient, job: dict) -> None:
    platform, job_id = job["platform"], job["id"]
    media: list[Path] = []
    try:
        for url in job.get("media", []):
            media.append(await _download_media(client, url))
        driver = get_driver(platform)
        if not await driver.authenticate({}):
            raise RuntimeError(f"No valid {platform} session — run: python agent.py connect {platform}")
        payload = PostPayload(content=job["caption"], media_paths=media, platform_options={})
        ppid = await driver.post(payload)
        await client.post(f"{SERVER}/api/agent/jobs/{job_id}/result", headers=_headers(),
                          json={"status": "done", "platform_post_id": str(ppid)})
        print(f"  ✅ posted job {job_id} → {platform}")
    except Exception as exc:  # noqa: BLE001
        await client.post(f"{SERVER}/api/agent/jobs/{job_id}/result", headers=_headers(),
                          json={"status": "failed", "error": str(exc)[:500]})
        print(f"  ❌ job {job_id} → {platform} failed: {exc}")
    finally:
        for m in media:
            try:
                m.unlink()
            except Exception:
                pass


async def cmd_run() -> None:
    if not TOKEN:
        print("Set PP_AGENT_TOKEN (copy it from your PostPilot dashboard).")
        return
    print(f"PostPilot agent → {SERVER}")
    print(f"Local sessions: {', '.join(connected_platforms()) or '(none — run: python agent.py connect <platform>)'}")
    async with httpx.AsyncClient(timeout=180) as client:
        while True:
            try:
                await client.post(f"{SERVER}/api/agent/heartbeat", headers=_headers(),
                                  json={"connected": connected_platforms()})
                resp = await client.get(f"{SERVER}/api/agent/jobs", headers=_headers())
                resp.raise_for_status()
                jobs = resp.json().get("jobs", [])
                if jobs:
                    print(f"{len(jobs)} job(s) to process…")
                for job in jobs:
                    await _process_job(client, job)
            except Exception as exc:  # noqa: BLE001
                print("poll error:", exc)
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "connect" and len(sys.argv) > 2:
        asyncio.run(cmd_connect(sys.argv[2]))
    elif cmd == "run":
        asyncio.run(cmd_run())
    elif cmd == "status":
        print(f"Server:   {SERVER}")
        print(f"Token set: {'yes' if TOKEN else 'no'}")
        print(f"Sessions:  {', '.join(connected_platforms()) or '(none)'}")
    else:
        print("Usage: python agent.py [connect <platform> | run | status]")


if __name__ == "__main__":
    main()
