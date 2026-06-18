"""PostPilot — multi-tenant FastAPI app + scheduler bootstrap (SaaS hosted core).

Every data endpoint is scoped to the logged-in user (Depends(current_user)).
Server-side posting covers the API platforms (Bluesky, Mastodon); the browser-login
platforms (LinkedIn/Instagram/X/TikTok) are handled by the per-user local agent
(Phase 2) and are gated here.
"""
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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import ai, image_gen, video_ai, video_gen
from app.auth import (
    COOKIE_NAME,
    TOKEN_TTL_DAYS,
    create_token,
    current_user,
    hash_password,
    new_agent_token,
    verify_password,
)
from app.crypto import encrypt_json
from app.database import (
    Account,
    DATA_DIR,
    Post,
    PostResult,
    User,
    get_session,
    init_db,
    select,
    utcnow,
)
from app.platforms import CREDENTIAL_PLATFORMS, SESSION_PLATFORMS, SUPPORTED_PLATFORMS, get_driver
from app.scheduler import schedule_post, scheduler, unschedule_post

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("postpilot")

MEDIA_DIR = DATA_DIR / "media"
SESSIONS_DIR = DATA_DIR / "sessions"
COOKIE_MAX_AGE = TOKEN_TTL_DAYS * 24 * 3600
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").lower() == "true"

# AI-video jobs: job_id -> {status, result?, error?, user_id}.
_video_jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.start()
    logger.info("PostPilot started; scheduler running")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="PostPilot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

from app.agent_api import router as agent_router  # noqa: E402

app.include_router(agent_router)


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "scheduler_running": scheduler.running}


# --- Auth --------------------------------------------------------------------

def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True,
        samesite="lax", secure=SECURE_COOKIES, path="/",
    )


@app.post("/api/auth/signup")
async def signup(payload: dict, response: Response):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if "@" not in email or "." not in email:
        raise HTTPException(400, "Enter a valid email")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    async with get_session() as session:
        existing = await session.execute(select(User).where(User.email == email))
        if existing.scalars().first():
            raise HTTPException(409, "An account with that email already exists")
        user = User(
            email=email,
            password_hash=hash_password(password),
            agent_token=new_agent_token(),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        uid = user.id
        udict = user.to_dict()
    _set_cookie(response, create_token(uid))
    return udict


@app.post("/api/auth/login")
async def login(payload: dict, response: Response):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    async with get_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().first()
    if user is None or not verify_password(user.password_hash, password):
        raise HTTPException(401, "Invalid email or password")
    _set_cookie(response, create_token(user.id))
    return user.to_dict()


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: User = Depends(current_user)):
    return user.to_dict()


@app.get("/api/auth/agent-token")
async def agent_token(user: User = Depends(current_user)):
    """The token the user pastes into their local agent (Phase 2)."""
    return {"agent_token": user.agent_token}


# --- AI -----------------------------------------------------------------------

@app.get("/api/ai/status")
async def ai_status():
    return {"enabled": ai.is_enabled()}


@app.post("/api/ai/caption")
async def ai_caption(payload: dict, user: User = Depends(current_user)):
    idea = (payload.get("idea") or "").strip()
    if not idea:
        raise HTTPException(400, "Provide an idea or draft to work from")
    if not ai.is_enabled():
        raise HTTPException(503, "AI captions are disabled — set GROQ_API_KEY (free, no card)")
    try:
        caption = await ai.generate_caption(idea, platforms=payload.get("platforms") or [], tone=payload.get("tone"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Caption generation failed")
        raise HTTPException(502, f"Caption generation failed: {exc}")
    return {"caption": caption}


# --- Media generation: images & video ----------------------------------------

@app.get("/api/media/capabilities")
async def media_capabilities():
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
async def generate_image(payload: dict, user: User = Depends(current_user)):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Provide an image prompt")
    if not image_gen.is_enabled():
        raise HTTPException(503, "Image generation is disabled")
    try:
        images = await image_gen.generate_images(prompt, n=payload.get("n", 1))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image generation failed")
        raise HTTPException(502, f"Image generation failed: {exc}")
    return {"images": images}


@app.post("/api/media/slideshow")
async def make_slideshow(payload: dict, user: User = Depends(current_user)):
    media_paths = payload.get("media_paths") or []
    if not media_paths:
        raise HTTPException(400, "Provide at least one image")
    if not video_gen.is_available():
        raise HTTPException(503, "ffmpeg is not installed — slideshow unavailable")
    audio = payload.get("audio_path")
    try:
        result = await video_gen.create_slideshow(
            media_paths,
            audio_path=Path(audio) if audio else None,
            seconds_per_image=payload.get("seconds_per_image", 3.0),
            ken_burns=payload.get("ken_burns", True),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Slideshow creation failed")
        raise HTTPException(502, f"Slideshow creation failed: {exc}")
    return result


async def _run_video_job(job_id: str, prompt: str):
    job = _video_jobs[job_id]
    try:
        result = await video_ai.generate_video(prompt)
        job.update(status="done", result=result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI video generation failed")
        job.update(status="failed", error=str(exc))


@app.post("/api/media/ai-video")
async def make_ai_video(payload: dict, user: User = Depends(current_user)):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Provide a video prompt")
    if not video_ai.is_enabled():
        raise HTTPException(503, "AI video is disabled — set FAL_KEY or REPLICATE_API_TOKEN")
    job_id = uuid.uuid4().hex
    _video_jobs[job_id] = {"status": "running", "user_id": user.id}
    asyncio.create_task(_run_video_job(job_id, prompt))
    return {"job_id": job_id, "status": "running"}


@app.get("/api/media/ai-video/{job_id}")
async def ai_video_status(job_id: str, user: User = Depends(current_user)):
    job = _video_jobs.get(job_id)
    if not job or job.get("user_id") != user.id:
        raise HTTPException(404, "Unknown job")
    return {"job_id": job_id, **{k: v for k, v in job.items() if k != "user_id"}}


# --- Posts -------------------------------------------------------------------

def _to_utc_naive(value: str) -> datetime:
    from datetime import timezone

    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@app.get("/api/posts")
async def list_posts(user: User = Depends(current_user)):
    async with get_session() as session:
        result = await session.execute(
            select(Post).where(Post.user_id == user.id).order_by(Post.scheduled_for.desc())
        )
        posts = result.scalars().all()
        res = await session.execute(select(PostResult).where(PostResult.user_id == user.id))
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
async def get_post(post_id: int, user: User = Depends(current_user)):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post or post.user_id != user.id:
            raise HTTPException(404, "Post not found")
        data = post.to_dict()
        res = await session.execute(select(PostResult).where(PostResult.post_id == post.id))
        data["results"] = [r.to_dict() for r in res.scalars().all()]
    return data


@app.post("/api/posts")
async def create_post(
    content: str = Form(...),
    platforms: str = Form(...),
    scheduled_for: str = Form(...),
    media_paths: str = Form("[]"),
    notes: str = Form(""),
    status: str = Form("scheduled"),
    user: User = Depends(current_user),
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
            user_id=user.id,
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
        post_id, post_status = post.id, post.status

    if post_status == "scheduled":
        schedule_post(post_id, when)
    return {"id": post_id, "status": post_status}


@app.delete("/api/posts/{post_id}")
async def delete_post(post_id: int, user: User = Depends(current_user)):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post or post.user_id != user.id:
            raise HTTPException(404, "Post not found")
        unschedule_post(post_id)
        await session.delete(post)
        res = await session.execute(select(PostResult).where(PostResult.post_id == post_id))
        for r in res.scalars().all():
            await session.delete(r)
        await session.commit()
    return {"deleted": post_id}


@app.post("/api/posts/{post_id}/post-now")
async def post_now(post_id: int, user: User = Depends(current_user)):
    async with get_session() as session:
        post = await session.get(Post, post_id)
        if not post or post.user_id != user.id:
            raise HTTPException(404, "Post not found")
        when = utcnow() + timedelta(seconds=10)
        post.scheduled_for = when
        post.status = "scheduled"
        await session.commit()
    scheduler.add_job(
        "app.scheduler:execute_post", trigger="date", run_date=when, args=[post_id],
        id=f"post_{post_id}", replace_existing=True, misfire_grace_time=300,
    )
    return {"id": post_id, "status": "scheduled", "fires_at": when.isoformat()}


# --- Accounts ----------------------------------------------------------------

@app.get("/api/accounts")
async def list_accounts(user: User = Depends(current_user)):
    async with get_session() as session:
        result = await session.execute(select(Account).where(Account.user_id == user.id))
        accounts = {a.platform: a.to_dict() for a in result.scalars().all()}
    out = []
    for platform in SUPPORTED_PLATFORMS:
        # Browser-login platforms post via the per-user local agent (Phase 2).
        via_agent = platform in SESSION_PLATFORMS
        if platform in accounts:
            out.append({**accounts[platform], "connected": True, "via_agent": via_agent})
        else:
            out.append({"platform": platform, "connected": False,
                        "status": "disconnected", "via_agent": via_agent})
    return out


async def _upsert_account(user_id: int, platform: str, username: str, auth_data: dict,
                          display_name: str | None = None):
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.user_id == user_id, Account.platform == platform)
        )
        account = result.scalars().first()
        encrypted = encrypt_json(auth_data) if auth_data else None
        if account:
            account.username = username
            account.display_name = display_name or username
            account.auth_data = encrypted
            account.status = "active"
            account.last_used = utcnow()
        else:
            session.add(Account(
                user_id=user_id, platform=platform, username=username,
                display_name=display_name or username, auth_data=encrypted, status="active",
            ))
        await session.commit()


@app.post("/api/accounts/connect/bluesky")
async def connect_bluesky(payload: dict, user: User = Depends(current_user)):
    handle = (payload.get("handle") or "").strip()
    app_password = (payload.get("app_password") or "").strip()
    if not handle or not app_password:
        raise HTTPException(400, "handle and app_password are required")
    auth = {"identifier": handle, "app_password": app_password}
    if not await get_driver("bluesky").authenticate(auth):
        raise HTTPException(401, "Bluesky login failed — check handle and app password")
    await _upsert_account(user.id, "bluesky", handle, auth)
    return {"status": "connected", "platform": "bluesky", "username": handle}


@app.post("/api/accounts/connect/mastodon")
async def connect_mastodon(payload: dict, user: User = Depends(current_user)):
    instance_url = (payload.get("instance_url") or "").strip().rstrip("/")
    access_token = (payload.get("access_token") or "").strip()
    if not instance_url or not access_token:
        raise HTTPException(400, "instance_url and access_token are required")
    if not instance_url.startswith("http"):
        instance_url = "https://" + instance_url
    auth = {"instance_url": instance_url, "access_token": access_token}
    if not await get_driver("mastodon").authenticate(auth):
        raise HTTPException(401, "Mastodon login failed — check instance URL and token")
    username = instance_url.replace("https://", "").replace("http://", "")
    await _upsert_account(user.id, "mastodon", username, auth)
    return {"status": "connected", "platform": "mastodon", "username": username}


@app.post("/api/accounts/connect/{platform}")
async def connect_session_platform(platform: str, user: User = Depends(current_user)):
    if platform in CREDENTIAL_PLATFORMS:
        raise HTTPException(400, f"Use /api/accounts/connect/{platform}")
    # Browser-login platforms are handled by the local agent (Phase 2), not the server.
    raise HTTPException(
        501, f"{platform.title()} connects through the PostPilot local agent (coming soon)."
    )


@app.get("/api/accounts/connect-status/{platform}")
async def connect_status(platform: str, user: User = Depends(current_user)):
    return {"status": "idle", "platform": platform}


@app.delete("/api/accounts/{platform}")
async def disconnect_account(platform: str, user: User = Depends(current_user)):
    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.user_id == user.id, Account.platform == platform)
        )
        account = result.scalars().first()
        if account:
            await session.delete(account)
            await session.commit()
    return {"disconnected": platform}


# --- Media -------------------------------------------------------------------

@app.post("/api/media/upload")
async def upload_media(file: UploadFile = File(...), user: User = Depends(current_user)):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload").suffix or ".bin"
    fname = f"{uuid.uuid4().hex}{suffix}"
    dest = MEDIA_DIR / fname
    dest.write_bytes(await file.read())
    return {"path": str(dest), "filename": fname, "url": f"/api/media/file/{fname}"}


@app.get("/api/media/file/{filename}")
async def get_media(filename: str, user: User = Depends(current_user)):
    dest = MEDIA_DIR / Path(filename).name
    if not dest.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(dest)


@app.delete("/api/media/{filename}")
async def delete_media(filename: str, user: User = Depends(current_user)):
    dest = MEDIA_DIR / Path(filename).name
    if dest.exists():
        dest.unlink()
    return {"deleted": dest.name}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=os.getenv("APP_HOST", "0.0.0.0"),
                port=int(os.getenv("APP_PORT", "8000")), reload=True)
