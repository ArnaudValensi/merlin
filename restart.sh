#!/bin/bash
# Restart Merlin (single process: dashboard + tunnel + bot + cron)
#
# main.py — starts everything (project root)
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Kill existing processes
for proc in "uv run main.py" "python main.py" "uv run merlin_bot.py" "python merlin_bot.py" "cloudflared tunnel"; do
    if pgrep -f "$proc" > /dev/null 2>&1; then
        pkill -f "$proc"
        echo "Stopped: $proc"
    fi
done
sleep 1

# Start main.py from project root
cd "$PROJECT_ROOT"
nohup uv run main.py > nohup.out 2>&1 &
sleep 2

# Verify
if pgrep -f "python.*main.py" > /dev/null 2>&1; then
    echo "Merlin running (PID $(pgrep -f 'python.*main.py' | tail -1))"
else
    echo "Merlin failed to start — check nohup.out"
    exit 1
fi
