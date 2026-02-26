---
name: dashboard
description: Give the user the link to the monitoring dashboard. Use this when the user asks for the dashboard URL or wants to see the dashboard.
user-invocable: false
allowed-tools: Bash
---

# Dashboard Link Skill

When the user asks for the dashboard link, run this script and send the output to the user:

```bash
uv run .claude/skills/dashboard/dashboard_url.py
```

The script reads credentials from `.env` and outputs the full URL with basic auth embedded.
