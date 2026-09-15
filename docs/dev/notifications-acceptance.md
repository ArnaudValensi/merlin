# Notifications: what only a person can verify

The routing rule, the retraction, the popover states, the title and body composition,
the deep link and the worker are covered by `tests/unit/test_notifications_*.py` and
`tests/e2e/test_notifications.py` (`uv run scripts.py validate` and `test-e2e`). What is
left needs a real push service, a real phone, a real operating system's notion of focus,
or a real agent's screen. Run these on real devices after a change to the routing, the
popover, the worker or the terminal page.

"Event" means an agent window flipping to done or ask: ask the agent to answer in five
seconds and use the time to set the situation up. Read each outcome on the devices and in
the routing record, which says what the instance decided and which pages it saw looking:

```bash
grep attention_routed ~/.merlin/logs/engine-log.jsonl | tail -3
```

## Real push delivery

| # | Setup | Action | Expected |
|---|---|---|---|
| P1 | Phone installed app, on. Merlin closed on every other device. | Event | Push on the phone, title `window · session · environment`, body with the duration and the agent's last line. |
| P2 | Desktop browser with push, browser closed. | Event | Push in the system tray. Clicking it opens Merlin on that window. |
| P3 | Phone push showing, app closed. | Tap it | App opens on that window. |
| P4 | Phone push showing, app open on another window. | Tap it | App comes to front, switched to that window. |
| P5 | Phone push showing, app closed. Visit the window on the desktop. | Open the app | Notification gone within a poll (about two seconds): a push cannot clear it silently, the app clears it on opening. |

## Real focus

| # | Setup | Action | Expected |
|---|---|---|---|
| F1 | Desktop tab focused on the agent's window, phone on. | Event | Nothing anywhere, the green pill only. Record: skipped `looking`. |
| F2 | Same tab moved to another workspace (visible to the OS, unfocused). | Event | Desktop notification in the tray, phone push. |
| F3 | Phone app in front on the agent's window, desktop tab unfocused. | Event | Nothing anywhere. Record: `looking` names the phone. |

## Real agent screens

| # | Setup | Action | Expected |
|---|---|---|---|
| S1 | An agent asks a question (a dialog). | Event ask | Body `Needs an answer:` followed by the question text. |
| S2 | An agent hits a permission prompt (non-bypass session). | Event ask | Body `Needs an answer:` followed by the tool line, not a bare `Needs an answer`. If bare, the box-row rule in the snippet cleaning is the place to look. |
| S3 | Rapid approvals at the window in a non-bypass session. | Approve several | Nothing fires, you are looking. If a flurry ever reaches a device, the record shows it: that is when a limit becomes worth adding. |

## Real browsers

| # | Setup | Action | Expected |
|---|---|---|---|
| B1 | Brave with "Use Google services for push messaging" off. | Turn on | Toggle stays on, sentence names the setting and where it is. Notifications still arrive from the open tab. |
| B2 | Brave, setting turned on. | Off, then on | Subscribed, listed, pushes arrive with the browser closed. |
| B3 | Phone installed app, fresh install. | Turn on | Permission asked once, subscribed, listed as "this device". |
| B4 | Merlin on a LAN address over plain HTTP. | Open the bell | The HTTPS sentence, title count and pills still work. |

## The phone as an app

| # | Setup | Action | Expected |
|---|---|---|---|
| A1 | Installed app, keyboard closed. | Look at the bottom bar | Buttons above the home indicator, clear of the rounded corners. |
| A2 | Installed app. | Open the keyboard | Bar and input line land above the keyboard as it animates, bottom padding gone while it is open. |
| A3 | Installed app, suspended for over a minute. | Come back | Terminal reconnects without a keystroke. |
| A4 | Popover on the phone. | Look | Readable sheet above the bottom bar, the sentence never empty. |
| A5 | App installed before an environment rename. | Reinstall | Name and icon carry the environment. |
