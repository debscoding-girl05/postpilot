# PostPilot — Content Series / Campaigns (Future Plan)

> Status: **planned, not built.** Captured from a planning discussion. Build only
> after the platform testing pass (Bluesky, Mastodon, Twitter, Instagram) is done.

## Vision

Evolve PostPilot from "post one thing" to "**run an ongoing content program**."
Two driving use cases (same underlying shape — a recurring themed series fed by
small daily inputs):

1. **Build-log / devlog** — reporting on an app in progress (notes / commits /
   screenshots → recurring update posts).
2. **Daily vlog** — a clip + a line each day → a daily post.

## Core new concept: `Series` (a.k.a. Campaigns)

A named content thread defined once:

- **Theme / goal** — e.g. "Building PostPilot in public", "My daily vlog"
- **Platforms** — e.g. LinkedIn + TikTok
- **Cadence** — daily / weekdays / 3×week, at a set time
- **Tone + post template** — a repeatable structure so entries feel consistent

Every post belongs to a series → unlocks planning, AI assistance, and tracking on
top of the existing posting engine.

## Propositions (grouped)

### Plan
- **AI series planner** — given concept + cadence + duration, Groq drafts a full
  content plan (N post ideas with distinct angles/hooks across the schedule);
  user approves/edits → creates draft posts.
- **Content calendar view** — week/month grid of planned + scheduled posts; see
  gaps; drag to reschedule.
- **Idea inbox / backlog** — capture raw thoughts/screenshots/clips anytime;
  convert to posts later.

### Create (the daily magic)
- **Daily check-in → post** — app asks "What did you build/do today?"; user drops
  a line + clip/screenshot; it generates a **per-platform-adapted** post
  (LinkedIn = professional long-form, TikTok = punchy hook + hashtags) using the
  series tone + template.
- **Changelog / commit → update posts** — paste a changelog or git log; turn
  shipped items into build-log posts.
- **Auto-media tied to concept** — cover image (FLUX) from the day's note, or
  auto-slideshow from the day's screenshots/clips.

### Schedule & Track
- **Cadence automation** — define "weekdays 9am"; app reserves slots, user fills.
- **Streak / consistency tracker** — days in a row, entries per series, where each
  went. (Local consistency metrics; real reach analytics need platform APIs we
  don't have.)

## Recommended line (phased, fits current stack: FastAPI + SQLite + Alpine + Groq/HF + APScheduler)

1. **Foundation** — `Series` object (theme, platforms, cadence, tone, template);
   attach posts to a series; simple calendar view.
2. **The daily flow** *(the heart)* — "Today's entry": pick series → drop note +
   media → AI generates adapted post → review/edit → auto-schedule into next
   cadence slot. Add streak tracker.
3. **Scale** — bulk AI planner, idea backlog, changelog-to-posts, per-platform
   repurposing variants.

Phase 2 is where the value is; Phase 1 is the scaffolding it needs.

## Open questions to decide before building

1. **Which use case first** — build-log/devlog (notes/commits → updates) or daily
   vlog (clip + line → daily post)? Shared infra, different input + template.
2. **AI autonomy level** — *assistant* (user writes, AI polishes/adapts per
   platform) vs *generator* (AI drafts whole entry from a one-line prompt, user
   approves)?
3. **Calendar vs queue** — visual week-ahead calendar, or simple "fill the next
   slot" queue?

## Context — what the app already does (reuse for the above)

- Multi-platform scheduling (APScheduler, SQLite job store, survives restart)
- AI captions (Groq, OpenAI-compatible, free) + per-platform char-limit adaptation
- AI image generation (Hugging Face FLUX.1-schnell, free)
- Local slideshow video (ffmpeg, 1080×1920) + media preview lightbox
- Posting: LinkedIn (browser automation, working), API platforms (Bluesky/Mastodon)
- TikTok: blocked by anti-bot on the upload page → treat as semi-manual
