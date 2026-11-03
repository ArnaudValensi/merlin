---
name: merlin-dashboard
description: Give the user the link to the monitoring dashboard. Use this when the user asks for the dashboard URL or wants to see the dashboard.
user-invocable: false
allowed-tools: Bash
---

# Dashboard Link Skill

When the user asks for the dashboard link, run this command (works from any
directory) and send the output to the user:

```bash
merlin dashboard-url
```

It resolves the URL like the Settings page does (explicit `MERLIN_DASHBOARD_URL`
override, then the Merlin Cloud address, then localhost) and embeds the login
credentials when a dashboard password is set.
