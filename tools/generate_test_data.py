# /// script
# dependencies = []
# ///
"""
Generate fake structured log data for dashboard development.

Populates logs/structured.jsonl with realistic events spanning several days:
invocations (discord + cron), bot events, cron dispatches, mix of successes
and errors, with varying durations.

Usage:
    uv run tools/generate_test_data.py
    uv run tools/generate_test_data.py --days 14
    uv run tools/generate_test_data.py --clear  # wipe and regenerate

Output:
    merlin-bot/logs/structured.jsonl (appended, or replaced with --clear)
"""

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
STRUCTURED_LOG_PATH = REPO_ROOT / "merlin-bot" / "logs" / "structured.jsonl"

CRON_JOBS = [
    "daily-digest",
    "daily-python-check",
    "kb-gardening",
    "echo-merlinsessionid-every-minute",
]

MODELS = ["claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929"]


def random_ts(base: datetime, offset_hours: float) -> str:
    """Generate a timestamp offset from base."""
    dt = base + timedelta(hours=offset_hours, seconds=random.uniform(0, 59))
    return dt.isoformat()


def gen_invocation(ts: str, caller: str, *, is_error: bool = False) -> dict:
    duration = random.uniform(2, 15) if caller == "discord" else random.uniform(10, 120)
    if is_error:
        duration = random.uniform(0.5, 5)
    return {
        "type": "invocation",
        "timestamp": ts,
        "caller": caller,
        "duration": round(duration, 3),
        "exit_code": 1 if is_error else 0,
        "num_turns": random.randint(1, 3) if is_error else random.randint(1, 12),
        "tokens_in": random.randint(5000, 150000),
        "tokens_out": random.randint(100, 5000),
        "session_id": f"fake-session-{random.randint(1000, 9999)}",
        "model": random.choice(MODELS),
    }


def gen_cron_dispatch(ts: str, job_id: str, *, is_error: bool = False) -> dict:
    duration = random.uniform(10, 90) if not is_error else random.uniform(1, 5)
    return {
        "type": "cron_dispatch",
        "timestamp": ts,
        "job_id": job_id,
        "event": "failed" if is_error else "completed",
        "duration": round(duration, 3),
        "exit_code": 1 if is_error else 0,
    }


def gen_bot_event(ts: str, event: str, details: str = "") -> dict:
    return {
        "type": "bot_event",
        "timestamp": ts,
        "event": event,
        "details": details,
    }


def generate(days: int = 7) -> list[dict]:
    """Generate realistic events spanning the given number of days."""
    events = []
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=days)

    # Bot ready event at the start
    events.append(gen_bot_event(random_ts(start, 0), "ready", "Bot started as Merlin#0000"))

    for day in range(days):
        day_base = start + timedelta(days=day)

        # Bot restart occasionally (every 2-3 days)
        if day > 0 and random.random() < 0.35:
            events.append(gen_bot_event(random_ts(day_base, random.uniform(0, 2)), "ready", "Bot restarted"))

        # Discord messages (3-10 per day)
        n_discord = random.randint(3, 10)
        for _ in range(n_discord):
            hour = random.uniform(8, 23)
            ts = random_ts(day_base, hour)
            is_error = random.random() < 0.08
            events.append(gen_invocation(ts, "discord", is_error=is_error))
            events.append(gen_bot_event(
                random_ts(day_base, hour - 0.001),
                "message_received",
                f"Message from user in 1234567890123456789",
            ))

        # Cron jobs (each runs once per day at roughly the right time)
        for job_id in CRON_JOBS:
            hour = {"daily-digest": 7.5, "daily-python-check": 8.0, "kb-gardening": 21.0, "echo-merlinsessionid-every-minute": 9.5}.get(job_id, 12)
            ts = random_ts(day_base, hour)
            is_error = random.random() < 0.1
            caller = f"cron-{job_id}"

            # cron_dispatch started
            events.append({
                "type": "cron_dispatch",
                "timestamp": ts,
                "job_id": job_id,
                "event": "started",
                "duration": 0,
                "exit_code": 0,
            })

            # invocation
            events.append(gen_invocation(ts, caller, is_error=is_error))

            # cron_dispatch completed/failed
            events.append(gen_cron_dispatch(ts, job_id, is_error=is_error))

        # Occasional errors
        if random.random() < 0.15:
            hour = random.uniform(0, 23)
            ts = random_ts(day_base, hour)
            events.append(gen_bot_event(ts, "error", "Exception invoking Claude: timeout"))

    # Sort by timestamp
    events.sort(key=lambda e: e["timestamp"])
    return events


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate fake structured log data for dashboard development.",
        epilog="""
Examples:
  uv run generate_test_data.py            # 7 days of data (appended)
  uv run generate_test_data.py --days 14  # 14 days of data
  uv run generate_test_data.py --clear    # wipe existing and regenerate
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=7, help="Number of days to generate (default: 7)")
    parser.add_argument("--clear", action="store_true", help="Clear existing log before generating")

    args = parser.parse_args()

    STRUCTURED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if args.clear and STRUCTURED_LOG_PATH.exists():
        STRUCTURED_LOG_PATH.unlink()
        print("Cleared existing structured.jsonl")

    events = generate(args.days)

    with open(STRUCTURED_LOG_PATH, "a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, default=str) + "\n")

    print(f"Generated {len(events)} events spanning {args.days} days → {STRUCTURED_LOG_PATH}")


if __name__ == "__main__":
    main()
