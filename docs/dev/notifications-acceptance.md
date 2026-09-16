# Notifications: what only a person can verify

The routing rule, the retraction, the popover states, the title and body composition,
the deep link and the worker are covered by `tests/unit/test_notifications_*.py` and
`tests/e2e/test_notifications.py` (`uv run scripts.py validate` and `test-e2e`). What is
left needs a real push service, a real phone, a real operating system's notion of focus,
or a real agent's screen. Run these on real devices after a change to the routing, the
popover, the worker or the terminal page, in this order: each stretch adds one thing to
have at hand.

"Event" means an agent window flipping to done or ask: ask the agent to answer in five
seconds and use the time to set the situation up. Read each outcome on the devices and in
the routing record, which says what the instance decided and which pages it saw looking:

```bash
grep attention_routed ~/.merlin/logs/engine-log.jsonl | tail -3
```

## With the phone app and a desktop browser, both on

| # | Setup | Action | Expected |
|---|---|---|---|
| 1 | Desktop tab focused on the agent's window. | Event | Nothing anywhere, the green pill only. Record: skipped `looking`, `looking` names the desktop. |
| 2 | Phone app in front on the agent's window, desktop tab unfocused. | Event | Nothing anywhere. Record: `looking` names the phone. |
| 3 | Desktop tab on another workspace (visible to the OS, unfocused), phone in a pocket. | Event | Desktop notification in the tray, phone push. |
| 4 | Phone app in the background, screen locked. | Event | Push on the phone: title `window · session · environment` with the environment's real name, body with the duration and the agent's last line. |
| 5 | Push showing on the phone, app closed. | Tap it | App opens on that window. |
| 6 | Push showing on the phone, app open on another window. | Tap it | App comes to front, switched to that window. |
| 7 | Push showing on the phone, app closed. Visit the window on the desktop. | Open the app | Notification gone within a poll (about two seconds): a push cannot clear it silently, the app clears it on opening. |
| 8 | Brave with "Use Google services for push messaging" off. | Turn the toggle on | Toggle stays on, sentence names the setting and where it is. Notifications still arrive from the open tab. |
| 9 | Popover on the phone. | Open the bell | Readable sheet above the bottom bar, the sentence never empty. |
| 10 | Installed app, keyboard closed. | Look at the bottom bar | Buttons above the home indicator, clear of the rounded corners. |
| 11 | Installed app. | Open the keyboard | Bar and input line land above the keyboard as it animates, bottom padding gone while it is open. |

## With an agent doing something specific

| # | Setup | Action | Expected |
|---|---|---|---|
| 12 | An agent asks a question (a dialog). | Event ask | Body `Needs an answer:` followed by the question text. |
| 13 | An agent hits a permission prompt (non-bypass session). | Event ask | Body `Needs an answer:` followed by the tool line, not a bare `Needs an answer`. If bare, the box-row rule in the snippet cleaning is the place to look. |
| 14 | Rapid approvals at the window in a non-bypass session. | Approve several | Nothing fires, you are looking. If a flurry ever reaches a device, the record shows it: that is when a limit becomes worth adding. |

## With extra setup

| # | Setup | Action | Expected |
|---|---|---|---|
| 15 | Brave, the push setting turned on. | Toggle off, then on | Subscribed, listed as "this device". |
| 16 | Desktop browser with push, browser closed. | Event | Push in the system tray. Clicking it opens Merlin on that window. |
| 17 | Phone app, fresh install. | Turn on | Permission asked once, subscribed, listed, a test push received. |
| 18 | App installed before an environment rename. | Reinstall | Name and icon carry the environment. |
| 19 | Installed app, suspended for over a minute. | Come back | Terminal reconnects without a keystroke. |
| 20 | Merlin on a LAN address over plain HTTP. | Open the bell | The HTTPS sentence, title count and pills still work. |

## Runs

### 2026-09-13 to 2026-09-15, founder, iPhone (installed app) and Arch Linux with Brave

Run while the routing was being reshaped: the decision moved from the pages to the
backend on the 15th (merlin `06d9f1e`), so the results below are the ones obtained after
that, and everything else is to be redone.

| # | Result | Notes |
|---|---|---|
| 1 | passed 2026-09-16 | Record: skipped `looking`, `looking` named the desktop on the window. |
| 2 | passed 2026-09-16 | Phone in front on the window, desktop unfocused: nothing anywhere. |
| 3 | passed 2026-09-16 | Desktop tray and phone push. |
| 4 | passed 2026-09-16 | Lock screen read: `handoff · merl · merlin`, `Finished after 2 s:` and the last lines. |
| 5, 6 | to run | |
| 7 | to redo | Passed before the move. |
| 8 | to run | Under the two-toggle popover Brave refused the subscription. The sentence not yet read. |
| 9 | to run | One report of an empty sentence with the toggle on, not reproduced. |
| 10, 11 | passed | Unaffected by the move. |
| 12 to 20 | to run | 17 passed before the move (fresh install, test push received). |
