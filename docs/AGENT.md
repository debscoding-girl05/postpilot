# PostPilot Local Agent (Phase 2)

The hosted PostPilot can't open browsers for the browser-login platforms
(LinkedIn, Instagram, X, TikTok). The **local agent** runs on your own machine,
logs you into those platforms once, then posts your queued jobs locally and
reports results back to the hosted app.

## How it fits together

```
Hosted app  ──(queues browser-login jobs)──▶  agent_jobs table
   ▲                                                  │
   │  POST /api/agent/jobs/{id}/result                │  GET /api/agent/jobs   (X-Agent-Token)
   │  POST /api/agent/heartbeat                        ▼
Local agent (agent.py)  ──posts via Playwright/cookies──▶  LinkedIn / IG / X / TikTok
```

Bluesky and Mastodon post **server-side** (official APIs) — the agent is only for
the browser-login platforms.

## Setup

1. In your PostPilot dashboard, copy your **agent token** (`GET /api/auth/agent-token`).
2. On your machine (with this repo + `uv sync`):

   ```bash
   export PP_SERVER_URL="https://your-postpilot-host"   # default http://localhost:8000
   export PP_AGENT_TOKEN="ppa_..."

   python agent.py connect linkedin     # opens a browser to log in (once per platform)
   python agent.py connect tiktok
   python agent.py run                  # keep running: posts queued jobs as they arrive
   ```

   `python agent.py status` shows the server, whether the token is set, and which
   platforms have a local session.

## Notes

- The agent must be **running** for browser-login posts to go out; if it's off,
  those jobs stay queued until it next polls.
- The agent reports a **heartbeat** of which platforms it has sessions for, so the
  hosted UI shows them connected.
- Sessions live locally (`data/sessions/`), never on the server — same "human
  approach" as the single-user app, now per user.
- The locked platforms remain fragile (TikTok/X bot-detection); failures are
  reported back and shown in History.
