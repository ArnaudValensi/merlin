# Epic: Monitoring Dashboard

## Overview

Build a web-based monitoring dashboard for Merlin, providing real-time visibility into system health, Claude invocation performance, cron job execution, and logs. Served by FastAPI alongside the Discord bot, accessible via browser with basic auth.

## Goals

1. **Observability** — See at a glance whether the system is healthy, what's running, and what failed
2. **Performance Insight** — Visualize Claude execution times, overall and per-caller (discord, cron jobs)
3. **Log Browsing** — Read and filter logs without SSH-ing into the server
4. **Foundation for Control** — Architecture supports future write operations (cron management, etc.)

## Architecture

### Unified Structured Log

Replace the current fragmented logging (separate text files for claude invocations, merlin.log, cron_runner.log) with a **single JSONL file** (`logs/structured.jsonl`). Every event is one JSON line with a `type` field.

Start fresh — no backfill of old logs. Existing text logs stay for human debugging. The structured log is the single source of truth for the dashboard going forward.

**Event types:**

| Type | Source | Key Fields |
|------|--------|------------|
| `invocation` | `claude_wrapper.py` | caller, duration, exit_code, num_turns, tokens_in, tokens_out, session_id, model |
| `bot_event` | `merlin.py` | event (ready, message_received, message_sent, error), details |
| `cron_dispatch` | `cron_runner.py` | job_id, event (started, completed, failed), duration, exit_code |

**Common fields on every event:**

```json
{
  "type": "invocation",
  "timestamp": "2026-02-06T08:04:20.980Z",
  "...type-specific fields..."
}
```

### Web Server

FastAPI running inside `merlin.py` on the same asyncio event loop as the Discord bot.

```
merlin.py starts
  ├── Discord bot (asyncio)
  └── FastAPI / uvicorn (asyncio, same loop)
        │
        ├── Basic Auth middleware (DASHBOARD_USER / DASHBOARD_PASS from .env)
        │
        ├── GET  /                       → redirect to /overview
        ├── GET  /overview               → Overview page
        ├── GET  /performance            → Performance page
        ├── GET  /logs                   → Logs page
        ├── GET  /api/health             → system status summary
        ├── GET  /api/invocations        → invocation logs (filterable)
        ├── GET  /api/events             → all events (filterable)
        ├── GET  /api/jobs               → cron job definitions + recent history
        ├── GET  /api/last-modified      → mtime of structured.jsonl (for smart refresh)
        └── (future) POST/PUT/DELETE /api/jobs → cron management
```

- **Port:** 3123 (from available ports in environment)
- **Auth:** HTTP Basic Auth, credentials in `.env` (`DASHBOARD_USER`, `DASHBOARD_PASS`)
- **Startup:** Launched automatically when `merlin.py` starts

### Frontend

Jinja2 templates served by FastAPI, no build step:
- **Shared base template** (`base.html`) with sidebar nav and common CSS/JS
- Each page extends `base.html` with its own content block
- **Chart.js** + **chartjs-plugin-zoom** for graphs (loaded from CDN)
- Vanilla JS — no framework, no bundler
- CSS: minimal, clean, dark theme (monitoring dashboard aesthetic)
- Responsive (sidebar collapses to hamburger menu on mobile)
- Sidebar nav with labels: Overview / Performance / Logs + live bot status indicator (green/red dot)

### Smart Auto-Refresh

Frontend polls `GET /api/last-modified` (returns mtime of `structured.jsonl`). Only re-fetches page data when the file has actually changed. No unnecessary network traffic, no wasted re-renders.

## Dashboard Pages

### 1. Overview — "Is everything OK?"

**Status cards (top row):**
- Bot status: up since X, green/red indicator
- Cron status: last dispatch time, failures in last 24h
- Today's stats: invocation count, avg response time
- Error count (last 24h) with severity color

**Last error card:**
- If any error in the last 24h, show a prominent card with the stderr snippet and timestamp
- Clickable → navigates to that entry in the Logs page

**Recent activity mini-feed (bottom):**
- Last ~10 events, condensed (timestamp + type badge + one-line summary)
- Each entry clickable → navigates to full detail in Logs page

### 2. Performance — "How fast is it?"

**Time range selector:** 24h / 7d / 30d / All (toggle buttons, top of page)

**Graphs:**
- **Execution time over time** — scatter chart, all invocations, color-coded by caller type (discord=blue, cron=orange)
- **Execution time by job** — bar chart showing avg/p50/p95 per cron job
- **Success/failure rate** — donut chart or simple indicator showing success % over selected period

### 3. Logs — "What happened?"

**Tabbed view:**
- **All** — unified feed (timestamp, type badge, one-line summary). Activity feed style.
- **Invocations** — table with columns: timestamp, caller, duration, exit_code, tokens_in, tokens_out, num_turns
- **Cron** — table with columns: timestamp, job_id, event, duration, exit_code
- **Bot Events** — table with columns: timestamp, event, details

**Filters (per tab):**
- Date range picker
- Status filter (success / error / all)

**Expandable rows:**
- Click a row to see full details (prompt preview, stderr, full token breakdown)

**Color coding:**
- Green for success, red for errors, yellow for warnings

### Cross-Page Linking

- Clicking a cron job name on Overview → jumps to its Performance breakdown
- Clicking an error on Overview → jumps to that log entry in Logs
- Clicking a job name in Performance → filters Logs to that job

## Current Log Issues to Fix

| Issue | Fix |
|-------|-----|
| Key metrics buried in STDOUT JSON blob | Extract duration, tokens, num_turns to top-level fields in structured log |
| Test noise in merlin.log / cron_runner.log | Structured log only written in production; tests use separate path |
| Duplicate lines in cron_runner.log | Fix handler configuration (remove console handler when running under cron redirect) |
| Unstructured text format | JSONL solves this for the dashboard; text logs remain for humans |

## UX Details

### Timezone
- Display all times in the browser's local timezone (via JS `toLocaleString`)
- Show UTC on hover for precision

### Empty State
- When no data exists yet, show a clean message: "No events yet. Waiting for first invocation..."
- Charts show an empty state, not broken/missing visuals

### Test Data
- Include a script to generate fake structured.jsonl data for development and testing
- Mix of invocations (discord + various cron jobs), bot events, successes and errors, varying durations

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend | FastAPI + uvicorn | Async-native, same loop as discord.py, easy REST APIs |
| Frontend | Vanilla HTML/JS + Chart.js | No build step, PEP 723 spirit (minimal deps), fast |
| Auth | HTTP Basic Auth (starlette) | Simple, sufficient for single-user dashboard |
| Log format | JSONL | One line per event, trivially parseable, appendable, grep-friendly |
| Charting | Chart.js (CDN) | Lightweight, good-looking, no npm needed |

## Components

### 1. Structured Logger (`structured_log.py`)

Module providing `log_event(type, **fields)` that appends one JSON line to `logs/structured.jsonl`.
- Thread-safe (file append with lock)
- Imported by `claude_wrapper.py`, `merlin.py`, `cron_runner.py`
- Consistent timestamp format (ISO 8601 UTC)

### 2. Dashboard Backend (`dashboard.py`)

FastAPI app with:
- HTML page serving (templates or inline)
- API endpoints that parse `structured.jsonl` and `cron-jobs/*.json`
- Basic auth middleware
- Query parameters for filtering (date range, type, caller)
- `/api/last-modified` endpoint for smart refresh

### 3. Dashboard Frontend (`templates/` + `static/`)

Jinja2 templates + static assets:
- `templates/base.html` — shared layout: sidebar nav, CSS, common JS
- `templates/overview.html` — extends base, status cards + mini-feed
- `templates/performance.html` — extends base, charts
- `templates/logs.html` — extends base, tabbed log viewer
- `static/dashboard.css` — dark theme styles
- `static/dashboard.js` — shared JS (auto-refresh, API calls, utils)
- Per-page JS inlined or in separate files for chart/table logic

### 4. Integration in `merlin.py`

- Start uvicorn alongside the Discord bot on the same event loop
- Load `DASHBOARD_USER` / `DASHBOARD_PASS` from `.env`
- Validate dashboard config at startup (fail-fast pattern)

### 5. Screenshot Utility (`screenshot.py`)

PEP 723 script using Playwright (headless Firefox) to capture the dashboard at multiple viewports for visual validation during development.

```bash
cd merlin-bot && uv run screenshot.py http://localhost:3123/overview
```

**Viewports:**

| Name | Width | Height |
|------|-------|--------|
| desktop | 1200 | 800 |
| tablet | 768 | 1024 |
| mobile | 375 | 667 |
| mobile-large | 414 | 896 |
| tablet-landscape | 1024 | 768 |
| 4k | 1920 | 1080 |

Outputs PNGs to `screenshots/<page>-<viewport>.png` (from repo root). Directory is gitignored.
Supports basic auth via `--user`/`--pass` flags.

### 6. Test Data Generator (`generate_test_data.py`)

Script to populate `logs/structured.jsonl` with realistic fake data for dashboard development:
- Invocations from discord and cron jobs with varying durations (5s-120s)
- Bot events (ready, message_received)
- Cron dispatches (successes and occasional failures)
- Spread over several days

## Acceptance Criteria

### Must Have
- [ ] `structured_log.py` module writing JSONL events
- [ ] `claude_wrapper.py` emits `invocation` events with duration, tokens, num_turns
- [ ] `merlin.py` emits `bot_event` events (ready, message received)
- [ ] `cron_runner.py` emits `cron_dispatch` events
- [ ] FastAPI dashboard served on port 3123 with basic auth
- [ ] Sidebar navigation (Overview / Performance / Logs) with bot status dot
- [ ] Overview page: status cards, last error card, recent activity mini-feed
- [ ] Performance page: execution time scatter, per-job bar chart, time range selector
- [ ] Logs page: tabbed view (All / Invocations / Cron / Bot), date filter, status filter
- [ ] Cross-page linking (Overview errors → Logs, job names → Performance)
- [ ] Smart auto-refresh (mtime-based)
- [ ] Dashboard starts automatically with `merlin.py`
- [ ] `.env` credentials for dashboard auth
- [ ] Responsive layout (mobile-friendly)
- [ ] Clean empty state when no data
- [ ] Test data generator script
- [ ] Dark theme

### Should Have
- [ ] Success/failure rate indicator on Performance page
- [ ] Expandable log rows with full detail view
- [ ] UTC on hover for timestamps

### Nice to Have
- [ ] Token usage graph (stacked input/output)
- [ ] Log rotation / archival for structured.jsonl
- [ ] Cron job management from the UI (future epic)
- [ ] Export logs as CSV

## Decisions Made

1. **Log retention** — Keep all logs for now, no rotation
2. **Historical backfill** — Start fresh, no migration of old logs
3. **Auto-refresh** — Smart polling based on file mtime, not fixed interval
4. **No cost tracking** — Focus on execution time, not USD cost
5. **Log viewer** — Tabbed view (All / Invocations / Cron / Bot), each with type-appropriate columns
6. **Time range** — User-selectable: 24h / 7d / 30d / All
7. **Layout** — Separate pages with sidebar nav (not single-page scroll)
8. **Timezone** — Browser local timezone, UTC on hover
9. **Bot status in nav** — Green/red dot in sidebar, always visible
10. **Error surfacing** — Prominent "last error" card on Overview with link to Logs
11. **Cross-page linking** — Errors and job names are clickable links between pages
12. **Responsive** — Sidebar collapses to hamburger menu on mobile
13. **Test data** — Generator script for development
14. **Templating** — Jinja2 with shared base template (server-side rendered)
15. **Sidebar** — Always show labels, not icon-only
16. **Chart zoom** — Include chartjs-plugin-zoom for drill-down on scatter plot
17. **Scope** — Focused on health, execution time, logs. No extra metrics for now

## References

- [FastAPI docs](https://fastapi.tiangolo.com/)
- [Chart.js docs](https://www.chartjs.org/docs/)
- [Starlette BasicAuth](https://www.starlette.io/authentication/)
- Existing logging: `claude_wrapper.py` lines 55-101
