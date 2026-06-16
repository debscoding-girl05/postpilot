# PostPilot

A lightweight, single-process social media post automator with a web UI.

Write a post once, pick platforms, schedule it — PostPilot posts automatically.

## The human approach

- **Free open APIs** (Bluesky, Mastodon) use official SDKs with app passwords / tokens.
- **Locked platforms** (Instagram, Twitter/X, LinkedIn, TikTok) capture a real browser
  session once: a visible browser opens, you log in normally, and the cookies are saved
  for silent headless reuse later.
- No Redis, no Celery, no message queues, no frontend build step. One Python process.

## Run

```bash
uv sync
uv run playwright install chromium
cp .env.example .env   # then edit APP_SECRET
uv run uvicorn main:app --reload
```

Open http://localhost:8000

## Architecture

| File | Role |
|------|------|
| `main.py` | FastAPI app, REST API, scheduler bootstrap |
| `app/database.py` | SQLAlchemy 2.x async models (accounts, posts, post_results) |
| `app/scheduler.py` | APScheduler job store + `execute_post` (posts to every platform) |
| `app/session_capture.py` | Headed-browser login capture → saved session JSON |
| `app/content_processor.py` | Per-platform image resize + caption adaptation |
| `app/platforms/*` | One driver per platform behind a `BasePlatform` interface |
| `app/ai.py` | Optional AI captions via xAI Grok (set `XAI_API_KEY`) |
| `app/image_gen.py` | Optional AI image generation via xAI Grok (`grok-2-image`) |
| `app/video_gen.py` | Local short-video slideshow from images via ffmpeg (no key) |
| `app/video_ai.py` | Optional AI text-to-video via fal.ai or Replicate |
| `static/index.html` | Entire frontend (Tailwind CDN + Alpine.js) |

## Media creation

The composer can build the media for a post, not just attach it:

- **AI images** — describe an image; Grok (`grok-2-image`) generates it. Reuses `XAI_API_KEY`.
- **Slideshow video** — turns the post's images into a 1080×1920 MP4 with fades and a gentle Ken Burns zoom. Pure ffmpeg, no API key, ideal for TikTok/Reels.
- **AI text-to-video** — true generative video via fal.ai or Replicate (`FAL_KEY` / `REPLICATE_API_TOKEN` + a model id). Runs as a background job the UI polls.

All require `ffmpeg` on PATH for the slideshow path (the Dockerfile installs it).

Sessions and credentials never leave the box; `auth_data` is Fernet-encrypted with `APP_SECRET`.
