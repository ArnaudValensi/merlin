#!/bin/bash
# Restart Merlin (single process: dashboard + bot + cron)
#
# main.py — starts everything (project root)
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Kill existing processes
for proc in "uv run cli.py" "uv run main.py" "python main.py" "uv run merlin_bot.py" "python merlin_bot.py"; do
    if pgrep -f "$proc" > /dev/null 2>&1; then
        pkill -f "$proc"
        echo "Stopped: $proc"
    fi
done
sleep 1

# Start via CLI entry point
cd "$PROJECT_ROOT"
nohup uv run cli.py start > nohup.out 2>&1 &
sleep 2

# Verify
if pgrep -f "python.*cli.py" > /dev/null 2>&1; then
    echo "Merlin running (PID $(pgrep -f 'python.*cli.py' | tail -1))"
else
    echo "Merlin failed to start — check nohup.out"
    exit 1
fi
