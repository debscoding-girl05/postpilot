# PostPilot — Multi-User SaaS Plan (Hybrid: Hosted Core + Local Agent)

> Status: **planned, not built.** Big, multi-phase undertaking. Build incrementally.
> Decisions locked: hybrid architecture; AI = free tier w/ limits now, paid plans later.

## Why hybrid (the core constraint)

Browser automation (LinkedIn/Instagram/X/TikTok) **cannot run on a shared server** —
those platforms block server automation and ban accounts; it also violates their ToS.
We proved this (TikTok bot wall, X login refusal, IG checks). So:

- **Server-side (hosted):** only official-API platforms → **Bluesky + Mastodon**.
- **Locked platforms (LinkedIn/IG/X/TikTok):** must run on **each user's own machine**
  via a **local agent** (the "human approach", per user), syncing with the cloud.

## Architecture

```
                    ┌────────────────────────── HOSTED (cloud, multi-tenant) ──────────────────────────┐
   Browser/Phone ──▶│  Web app (FastAPI)                                                                │
                    │   • Auth: signup/login, sessions/JWT                                              │
                    │   • Postgres: users, accounts, posts, post_results  (ALL scoped by user_id)       │
                    │   • Compose / schedule / history / AI (captions, images, slideshow)               │
                    │   • Server-side posting: Bluesky, Mastodon (per-user encrypted tokens)            │
                    │   • Object storage (R2/S3) for media                                              │
                    │   • Scheduler: due post → post directly (BS/Masto) OR enqueue agent job           │
                    │   • Agent API: job queue + per-user agent tokens                                  │
                    └───────────────────────────────────▲───────────────────────────────────────────────┘
                                                         │  poll jobs / push results (HTTPS)
                    ┌────────────────────────────────────┴── LOCAL AGENT (each user's machine) ─────────┐
                    │  Repackaged current PostPilot (Python + Playwright)                                │
                    │   • Authenticates with the user's agent token                                     │
                    │   • Browser-login session capture for LinkedIn/IG/X/TikTok (local, per user)       │
                    │   • Pulls due jobs for those platforms → downloads media → posts locally → reports │
                    └───────────────────────────────────────────────────────────────────────────────────┘
```

**Key reuse:** the current single-user PostPilot *becomes the local agent* — it already
does browser login + browser posting. The new build is the **hosted multi-tenant
backend** + the **agent sync protocol**.

## What's new vs. what ports cleanly

| Ports cleanly (reuse) | New build |
|---|---|
| Bluesky / Mastodon drivers | Auth + user accounts |
| AI: captions (Groq), images (HF), slideshow (ffmpeg) | Multi-tenancy: `user_id` on all tables, per-user scoping |
| Scheduler (APScheduler + DB jobstore) | Postgres (replace SQLite for concurrency) |
| Content processor, crypto (per-user keys) | Object storage for media (R2/S3) |
| The UI (becomes the hosted dashboard) | Agent job queue + agent API + agent auth |
| LinkedIn/IG/X/TikTok drivers (move into the agent) | Public hosting + domain + HTTPS |
|  | Usage limits; billing (Stripe) later |

## Sync protocol (hosted ↔ agent), sketch

- Agent auth: user copies an **agent token** from their dashboard into the local agent.
- `GET /api/agent/jobs` → pending jobs for locked platforms (caption + media URLs + platform).
- Agent downloads media, runs local Playwright posting, then `POST /api/agent/jobs/{id}/result`.
- Agent reports **connection status** of locked platforms (only the agent knows if the
  user's LinkedIn/IG/X/TikTok session is still valid).
- Transport: simple polling first; websocket later if needed.

## Phased roadmap

**Phase 1 — Hosted multi-tenant core (the shippable MVP)**
- Auth (email+password, hashed; consider `fastapi-users`), sessions/JWT.
- Postgres; add `users` + `user_id` FK to accounts/posts/post_results; scope every query.
- Per-user connect for Bluesky + Mastodon (their own tokens, encrypted per user).
- Compose / schedule / history / AI, all per-user. Server-side posting for BS/Masto.
- Object storage for media. Public deploy + domain + HTTPS.
- → A real SaaS for the 2 API platforms. Usable on its own.

**Phase 2 — Local agent + the locked platforms**
- Agent job queue + agent token auth in the backend.
- Repackage current PostPilot as the downloadable agent (auth, poll, post locally, report).
- LinkedIn/IG/X/TikTok work per user via their running agent.

**Phase 3 — Plans & polish**
- Usage limits (free tier), Stripe billing, account management, onboarding.

**Phase 4 — Series feature** (see CONTENT_SERIES_PLAN.md) layered on top, per user.

## Tech decisions (proposed)

- **Backend:** keep FastAPI (max reuse) + SQLAlchemy async → **Postgres** (asyncpg).
- **Auth:** `fastapi-users` or hand-rolled argon2 + JWT/session cookies.
- **Storage:** Cloudflare R2 (S3-compatible, free 10GB) for media.
- **Hosting:** always-on PaaS/VPS (Fly.io / Railway / Render / Hetzner) — NOT a personal
  Mac, NOT Tailscale. Managed Postgres (Neon/Supabase free tier to start).
- **Agent:** reuse current PostPilot (Python + Playwright); distribute as a downloadable;
  auth via paste-in agent token.

## Hard constraints / honest notes

- **The agent must be running on the user's machine** for locked-platform posts to fire
  (same as today, per user). Machine off → those posts wait.
- **Locked platforms stay fragile** even via the agent (the blocks we hit don't go away);
  users will see failures on TikTok etc. Set expectations in-product.
- **Costs scale** with users (hosting, Postgres, storage, AI). Free tiers bootstrap Phase 1;
  monetize before real scale.
- **Security:** per-user encrypted tokens; never store platform passwords; isolate tenants;
  scope every query by `user_id`; rotate agent tokens.
- Realistically a **multi-month, multi-phase** build. Phase 1 is the first shippable slice.

## Immediate next step

Start **Phase 1** on a branch: introduce `users` + auth + `user_id` multi-tenancy and the
Postgres swap, keeping Bluesky/Mastodon + AI + scheduling working per user. Everything
else (agent, locked platforms, billing) layers on after.
