# Monitoring Dashboard — Tasks

## Phase 1: Foundation

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Create `structured_log.py` module | done | JSONL logger, thread-safe, UTC timestamps |
| 2 | Create `generate_test_data.py` | done | 7-day realistic fake data |
| 3 | Create `screenshot.py` utility | done | Playwright, 6 viewports, screenshots/ gitignored |
| 4 | Wire into `claude_wrapper.py` | done | Emits `invocation` events |
| 5 | Wire into `merlin.py` | done | Emits `bot_event` events |
| 6 | Wire into `cron_runner.py` | done | Emits `cron_dispatch` events |
| 7 | Tests for structured_log + wiring | done | 13 new tests, 275 total passing |

## Phase 2: Backend

| # | Task | Status | Notes |
|---|------|--------|-------|
| 8 | Create `dashboard.py` FastAPI app | done | Basic auth, Jinja2, static files, all API endpoints |
| 9 | Integrate into `merlin.py` | done | Uvicorn on port 3123, same asyncio loop, fail-fast config |

## Phase 3-5: Frontend

| # | Task | Status | Notes |
|---|------|--------|-------|
| 10 | `base.html` + dark theme CSS | done | Sidebar nav, bot status dot, responsive hamburger |
| 11 | Overview page | done | Status cards, last error card, activity mini-feed |
| 12 | Performance page | done | Scatter chart, per-job bar chart, success donut, time range selector |
| 13 | Logs page | done | Tabbed view (All/Invocations/Cron/Bot), filters, expandable rows |

## Phase 6: Polish

| # | Task | Status | Notes |
|---|------|--------|-------|
| 14 | Smart auto-refresh | done | Polls /api/last-modified, refreshes on mtime change |
| 15 | Cross-page linking | done | Error card → Logs, job names → Performance |
| 16 | Empty states | done | Clean wizard icon + message when no data |
| 17 | Timezone display | done | Browser local time, UTC on hover |
| 18 | Screenshot validation | done | 18 screenshots (3 pages × 6 viewports) all verified |
| 19 | Full test suite | done | 275 passed, 0 failed |
