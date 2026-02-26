---
name: self-awareness
description: Understand your own architecture, source code, configuration, and runtime behavior. Use when you need to inspect how you work, review your logs, debug your own behavior, or answer questions about your capabilities.
user-invocable: false
allowed-tools: Bash, Read, Glob, Grep
---

# Self-Awareness Skill

You can introspect on your own architecture, configuration, and runtime behavior.

## Where You're Documented

- **`../CLAUDE.md`** — Project architecture, script inventory, data flow, logging, conventions
- **`CLAUDE.md`** — Your personality, directives, operational instructions
- Every script: `uv run <script>.py --help`

## What You Can Inspect

- **Your source code** — read any `.py` file in `merlin-bot/`
- **Your skills** — `.claude/skills/*/SKILL.md`
- **Your hooks** — `.claude/settings.json` and `.claude/hooks/*`
- **Your cron jobs** — `uv run cron_manage.py list` and `cron-jobs/*.json`
- **Your memory** — `memory/user.md`, `memory/kb/`, `memory/logs/`

## Your Logs

### Invocation logs (`logs/claude/`)

One file per Claude invocation — contains prompt, stdout/stderr, exit code, duration, usage stats.

```bash
ls -lt logs/claude/ | head -20
grep -rl "exit_code.*[^0]" logs/claude/ | head -10
grep "duration" logs/claude/*.log | sort -t: -k3 -n -r | head -10
```

### Cron logs

```bash
tail -100 logs/cron_runner.log
uv run cron_manage.py history
```

### Bot logs

```bash
tail -100 logs/merlin.log
```
