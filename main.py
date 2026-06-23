"""PostPilot — FastAPI app + scheduler bootstrap (entry point)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import ai, image_gen, video_ai, video_gen
from app.crypto import encrypt_json
from app.database import (
    Account,
    DATA_DIR,
    Post,
    PostResult,
    Series,
    get_session,
    init_db,
    select,
    utcnow,
)
from app.platforms import (
    CREDENTIAL_PLATFORMS,
    SESSION_PLATFORMS,
    SUPPORTED_PLATFORMS,
    get_driver,
)
from app.scheduler import schedule_post, scheduler, unschedule_post
from app.session_capture import capture_session_blocking

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("postpilot")

MEDIA_DIR = DATA_DIR / "media"
SESSIONS_DIR = DATA_DIR / "sessions"

# In-memory connect-flow status per platform (capturing/done/failed).
_connect_status: dict[str, str] = {}

# In-memory AI-video job status: job_id -> {status, result?, error?}.
_video_jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.start()
    logger.info("PostPilot started; scheduler running")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="PostPilot", lifespan=lifespan)

# Optional HTTP Basic auth — set APP_PASSWORD to protect the whole app when it's
# exposed (e.g. via a public tunnel). Unset = no auth (safe for localhost-only use).
APP_PASSWORD = os.getenv("APP_PASSWORD")


# Paths anyone can reach even when APP_PASSWORD is set — the public explainer.
PUBLIC_PATHS = {"/health", "/guide"}


@app.middleware("http")
async def _basic_auth(request, call_next):
    if APP_PASSWORD and request.url.path not in PUBLIC_PATHS:
        import base64
        import secrets

        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            from starlette.responses import Response

            return Response(
                "Authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="PostPilot"'},
            )
    return await call_next(request)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/guide")
async def public_guide():
    """Public, no-password explainer page (what works, what doesn't, how to test)."""
    return FileResponse("static/guide.html")


# --- AI -----------------------------------------------------------------------

@app.get("/api/ai/status")
async def ai_status():
    return {"enabled": ai.is_enabled()}


@app.post("/api/ai/caption")
async def ai_caption(payload: dict):
    idea = (payload.get("idea") or "").strip()
    if not idea:
        raise HTTPException(400, "Provide an idea or draft to work from")
    if not ai.is_enabled():
        raise HTTPException(503, "AI captions are disabled — set GROQ_API_KEY (free, no card)")
    platforms = payload.get("platforms") or []
    tone = payload.get("tone")
    try:
        caption = await ai.generate_caption(idea, platforms=platforms, tone=tone)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Caption generation failed")
        raise HTTPException(502, f"Caption generation failed: {exc}")
    return {"caption": caption}


# --- Media generation: images & video ----------------------------------------

@app.get("/api/media/capabilities")
async def media_capabilities():
    """What media tools are available, so the UI can show/hide controls."""
    return {
        "caption": ai.is_enabled(),
        "caption_provider": ai.provider(),
        "image_gen": image_gen.is_enabled(),
        "image_provider": image_gen.provider(),
        "slideshow": video_gen.is_available(),
        "ai_video": video_ai.is_enabled(),
        "ai_video_provider": video_ai.provider(),
    }


@app.post("/api/media/generate-image")
async def generate_image(payload: dict):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Provide an image prompt")
    if not image_gen.is_enabled():
        raise HTTPException(503, "Image generation is disabled")
    n = payload.get("n", 1)
    try:
        images = await image_gen.generate_images(prompt, n=n)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image generation failed")
        raise HTTPException(502, f"Image generation failed: {exc}")
    return {"images": images}


@app.post("/api/media/slideshow")
async def make_slideshow(payload: dict):
    media_paths = payload.get("media_paths") or []
    if not media_paths:
        raise HTTPException(400, "Provide at least one image")
    if not video_gen.is_available():
        raise HTTPException(503, "ffmpeg is not installed — slideshow unavailable")
    audio = payload.get("audio_path")
    spi = payload.get("seconds_per_image", 3.0)
    ken_burns = payload.get("ken_burns", True)
    try:
        result = await video_gen.create_slideshow(
            media_paths,
            audio_path=Path(audio) if audio else None,
            seconds_per_image=spi,
            ken_burns=ken_burns,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Slideshow creation failed")
        raise HTTPException(502, f"Slideshow creation failed: {exc}")
    return result


async def _run_video_job(job_id: str, prompt: str):
    _video_jobs[job_id] = {"status": "running"}
    try:
        result = await video_ai.generate_video(prompt)
        _video_jobs[job_id] = {"status": "done", "result": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI video generation failed")
        _video_jobs[job_id] = {"status": "failed", "error": str(exc)}


@app.post("/api/media/ai-video")
async def make_ai_video(payload: dict):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Provide a video prompt")
    if not video_ai.is_enabled():
        raise HTTPException(503, "AI video is disabled — set FAL_KEY or REPLICATE_API_TOKEN")
    job_id = uuid.uuid4().hex
    _video_jobs[job_id] = {"status": "running"}
    asyncio.create_task(_run_video_job(job_id, prompt))
    return {"job_id": job_id, "status": "running"}


@app.get("/api/media/ai-video/{job_id}")
async def ai_video_status(job_id: str):
    job = _video_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {"job_id": job_id, **job}


@app.get("/health")
async def health():
    connected = []
    async with get_session() as session:
        result = await session.execute(select(Account).where(Account.status == "active"))
        connected = [a.platform for a in result.scalars().all()]
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "connected_platforms": connected,
    }


# --- Posts -------------------------------------------------------------------

def _to_utc_naive(value: str) -> datetime:
    from datetime import timezone

    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # Assume the client already sent UTC.
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def _post_payload(post: Post) -> dict:
    data = post.to_dict()
    async with get_session() as session:
        res = await session.execute(
            select(PostResult).where(PostResult.post_id == post.id)
        )
        data["results"] = [r.to_dict() for r in res.scalars().all()]
    return data


@app.get("/api/posts")
async def list_posts():
    async with get_session() as session:
        result = await session.execute(select(Post).order_by(Post.scheduled_for.desc()))
        posts = result.scalars().all()
        # Gather all results in one query.
        res = await session.execute(select(PostResult))
        results_by_post: dict[int, list] = {}
        for r in res.scalars().all():
            results_by_post.setdefault(r.post_id, []).append(r.to_dict())
    out = []
    for p in posts:
        d = p.to_dict()
        d["results"] = results_by_post.get(p.id, [])
        out.append(d)
    return out


@app.get("/api/posts/{post_id}")
async def get_post(post_id: int):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        return await _post_payload(post)


@app.post("/api/posts")
async def create_post(
    content: str = Form(...),
    platforms: str = Form(...),  # JSON array string
    scheduled_for: str = Form(...),
    media_paths: str = Form("[]"),  # JSON array of already-uploaded paths
    notes: str = Form(""),
    status: str = Form("scheduled"),
):
    try:
        platform_list = json.loads(platforms)
        media_list = json.loads(media_paths)
    except json.JSONDecodeError:
        raise HTTPException(400, "platforms and media_paths must be JSON arrays")

    if not platform_list:
        raise HTTPException(400, "Select at least one platform")

    when = _to_utc_naive(scheduled_for)

    async with get_session() as session:
        post = Post(
            content=content,
            media_paths=json.dumps(media_list),
            platforms=json.dumps(platform_list),
            scheduled_for=when,
            status=status if status in ("scheduled", "draft") else "scheduled",
            notes=notes or None,
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        post_id = post.id
        post_status = post.status

    if post_status == "scheduled":
        schedule_post(post_id, when)

    return {"id": post_id, "status": post_status}


@app.delete("/api/posts/{post_id}")
async def delete_post(post_id: int):
    unschedule_post(post_id)
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        await session.delete(post)
        # Clean up results too.
        res = await session.execute(select(PostResult).where(PostResult.post_id == post_id))
        for r in res.scalars().all():
            await session.delete(r)
        await session.commit()
    return {"deleted": post_id}


@app.post("/api/posts/{post_id}/post-now")
async def post_now(post_id: int):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        when = utcnow() + timedelta(seconds=10)
        post.scheduled_for = when
        post.status = "scheduled"
        await session.commit()
    # Schedule directly (no extra jitter needed, but schedule_post adds a little).
    scheduler.add_job(
        "app.scheduler:execute_post",
        trigger="date",
        run_date=when,
        args=[post_id],
        id=f"post_{post_id}",
        replace_existing=True,
        misfire_grace_time=300,
    )
    return {"id": post_id, "status": "scheduled", "fires_at": when.isoformat()}


# --- Series (content concepts) -----------------------------------------------

def _next_slot(cadence: str, post_time: str) -> datetime:
    """Next posting slot (UTC-naive) for a series, from 'HH:MM' local + cadence."""
    from datetime import time as _time
    from datetime import timezone as _tz

    try:
        hh, mm = (int(x) for x in post_time.split(":"))
    except Exception:
        hh, mm = 9, 0
    now_local = datetime.now()
    cand = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now_local:
        cand += timedelta(days=1)
    if cadence == "weekdays":
        while cand.weekday() >= 5:  # skip Sat/Sun
            cand += timedelta(days=1)
    elif cadence == "weekly":
        # keep the same weekday as "today" a week out if today's slot has passed
        if (cand - now_local) < timedelta(days=1):
            pass  # tomorrow is fine for a first weekly entry
    # interpret naive as local → convert to UTC-naive (matches stored posts)
    return cand.astimezone().astimezone(_tz.utc).replace(tzinfo=None)


@app.get("/api/series")
async def list_series():
    async with get_session() as session:
        result = await session.execute(select(Series).order_by(Series.created_at.desc()))
        return [s.to_dict() for s in result.scalars().all()]


@app.post("/api/series")
async def create_series(payload: dict):
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Series needs a title")
    async with get_session() as session:
        s = Series(
            title=title,
            concept=(payload.get("concept") or "").strip(),
            platforms=json.dumps(payload.get("platforms") or []),
            cadence=payload.get("cadence") or "daily",
            post_time=payload.get("post_time") or "09:00",
            tone=(payload.get("tone") or None),
            hashtags=json.dumps(payload.get("hashtags") or []),
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.to_dict()


@app.delete("/api/series/{series_id}")
async def delete_series(series_id: int):
    async with get_session() as session:
        s = await session.get(Series, series_id)
        if not s:
            raise HTTPException(404, "Series not found")
        await session.delete(s)
        await session.commit()
    return {"deleted": series_id}


@app.post("/api/series/{series_id}/generate")
async def generate_series_entry(series_id: int, payload: dict):
    note = (payload.get("note") or "").strip()
    if not note:
        raise HTTPException(400, "Add a short note about today's entry")
    if not ai.is_enabled():
        raise HTTPException(503, "AI is disabled — set GROQ_API_KEY")
    async with get_session() as session:
        s = await session.get(Series, series_id)
        if not s:
            raise HTTPException(404, "Series not found")
        data = s.to_dict()
    try:
        caption = await ai.generate_series_post(
            concept=data["concept"], note=note, tone=data["tone"],
            platforms=data["platforms"], hashtags=data["hashtags"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Series generation failed")
        raise HTTPException(502, f"Generation failed: {exc}")
    return {"caption": caption, "platforms": data["platforms"], "next_slot": _next_slot(data["cadence"], data["post_time"]).isoformat() + "Z"}


@app.post("/api/series/{series_id}/schedule")
async def schedule_series_entry(series_id: int, payload: dict):
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Nothing to schedule")
    async with get_session() as session:
        s = await session.get(Series, series_id)
        if not s:
            raise HTTPException(404, "Series not found")
        data = s.to_dict()
    platforms = payload.get("platforms") or data["platforms"]
    if not platforms:
        raise HTTPException(400, "The series has no platforms selected")
    when = _to_utc_naive(payload["when"]) if payload.get("when") else _next_slot(data["cadence"], data["post_time"])
    media = payload.get("media_paths") or []
    async with get_session() as session:
        post = Post(
            content=content, media_paths=json.dumps(media),
            platforms=json.dumps(platforms), scheduled_for=when,
            status="scheduled", series_id=series_id,
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        post_id = post.id
    schedule_post(post_id, when)
    return {"id": post_id, "status": "scheduled", "scheduled_for": when.isoformat() + "Z"}


# --- Accounts ----------------------------------------------------------------

@app.get("/api/accounts")
async def list_accounts():
    async with get_session() as session:
        result = await session.execute(select(Account))
        accounts = {a.platform: a.to_dict() for a in result.scalars().all()}
    # Always return an entry per supported platform so the UI can render all cards.
    out = []
    for platform in SUPPORTED_PLATFORMS:
        if platform in accounts:
            out.append({**accounts[platform], "connected": True})
        else:
            out.append({"platform": platform, "connected": False, "status": "disconnected"})
    return out


async def _upsert_account(platform: str, username: str, auth_data: dict, display_name: str | None = None):
    async with get_session() as session:
        result = await session.execute(select(Account).where(Account.platform == platform))
        account = result.scalars().first()
        encrypted = encrypt_json(auth_data) if auth_data else None
        if account:
            account.username = username
            account.display_name = display_name or username
            account.auth_data = encrypted
            account.status = "active"
            account.last_used = utcnow()
        else:
            account = Account(
                platform=platform,
                username=username,
                display_name=display_name or username,
                auth_data=encrypted,
                status="active",
            )
            session.add(account)
        await session.commit()


@app.post("/api/accounts/connect/bluesky")
async def connect_bluesky(payload: dict):
    handle = payload.get("handle", "").strip()
    app_password = payload.get("app_password", "").strip()
    if not handle or not app_password:
        raise HTTPException(400, "handle and app_password are required")
    auth = {"identifier": handle, "app_password": app_password}
    driver = get_driver("bluesky")
    if not await driver.authenticate(auth):
        raise HTTPException(401, "Bluesky login failed — check handle and app password")
    await _upsert_account("bluesky", handle, auth)
    return {"status": "connected", "platform": "bluesky", "username": handle}


@app.post("/api/accounts/connect/mastodon")
async def connect_mastodon(payload: dict):
    instance_url = payload.get("instance_url", "").strip().rstrip("/")
    access_token = payload.get("access_token", "").strip()
    if not instance_url or not access_token:
        raise HTTPException(400, "instance_url and access_token are required")
    if not instance_url.startswith("http"):
        instance_url = "https://" + instance_url
    auth = {"instance_url": instance_url, "access_token": access_token}
    driver = get_driver("mastodon")
    if not await driver.authenticate(auth):
        raise HTTPException(401, "Mastodon login failed — check instance URL and token")
    username = instance_url.replace("https://", "").replace("http://", "")
    await _upsert_account("mastodon", username, auth)
    return {"status": "connected", "platform": "mastodon", "username": username}


async def _run_capture(platform: str):
    _connect_status[platform] = "capturing"
    try:
        ok = await asyncio.to_thread(capture_session_blocking, platform)
        if ok:
            # Save a placeholder account record; username can be refined later.
            await _upsert_account(platform, platform, {}, display_name=platform.title())
            _connect_status[platform] = "done"
        else:
            _connect_status[platform] = "failed"
    except Exception:
        logger.exception("Session capture failed for %s", platform)
        _connect_status[platform] = "failed"


@app.post("/api/accounts/connect/{platform}")
async def connect_session_platform(platform: str):
    if platform not in SESSION_PLATFORMS:
        raise HTTPException(400, f"{platform} does not use browser session capture")
    if _connect_status.get(platform) == "capturing":
        return {"status": "capturing", "platform": platform}
    # Launch the headed-browser capture in the background.
    asyncio.create_task(_run_capture(platform))
    return {"status": "capturing", "platform": platform}


@app.get("/api/accounts/connect-status/{platform}")
async def connect_status(platform: str):
    return {"status": _connect_status.get(platform, "idle"), "platform": platform}


@app.delete("/api/accounts/{platform}")
async def disconnect_account(platform: str):
    # Remove DB record.
    async with get_session() as session:
        result = await session.execute(select(Account).where(Account.platform == platform))
        account = result.scalars().first()
        if account:
            await session.delete(account)
            await session.commit()
    # Remove session file if present.
    session_file = SESSIONS_DIR / f"{platform}.json"
    if session_file.exists():
        session_file.unlink()
    # TikTok / X / Instagram use a persistent Chrome profile dir for login.
    import shutil

    profile_dir = SESSIONS_DIR / f"{platform}_profile"
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    _connect_status.pop(platform, None)
    return {"disconnected": platform}


# --- Media -------------------------------------------------------------------

@app.post("/api/media/upload")
async def upload_media(file: UploadFile = File(...)):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload").suffix or ".bin"
    fname = f"{uuid.uuid4().hex}{suffix}"
    dest = MEDIA_DIR / fname
    content = await file.read()
    dest.write_bytes(content)
    return {"path": str(dest), "filename": fname, "url": f"/api/media/file/{fname}"}


@app.get("/api/media/file/{filename}")
async def get_media(filename: str):
    safe = Path(filename).name
    dest = MEDIA_DIR / safe
    if not dest.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(dest)


@app.delete("/api/media/{filename}")
async def delete_media(filename: str):
    safe = Path(filename).name
    dest = MEDIA_DIR / safe
    if dest.exists():
        dest.unlink()
    return {"deleted": safe}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
    )
