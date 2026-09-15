# Notifications: manual acceptance

The cases a person runs on real devices, since a test runner cannot open a phone, a
push service or a second workspace. Run them after any change to the routing, the
popover, the worker or the terminal page. Each case names the setup, the action and
what must happen. "Event" means an agent window flipping to done (an agent finishes a
turn) or ask (it opens a question or a permission dialog): ask the agent to answer in
five seconds and use that time to set the situation up.

Read the outcome twice: on the devices, and in the routing record, which says what the
instance decided and which pages it saw looking:

```bash
grep attention_routed ~/.merlin/logs/engine-log.jsonl | tail -3
curl -s -H "X-Portal-Auth: $MERLIN_SAAS_TOKEN" http://localhost:3123/api/notifications/status
```

Devices to have at hand: a desktop browser with push (Chrome, or Brave with "Use Google
services for push messaging" on), a desktop browser without push (Brave by default), the
phone with Merlin installed on the Home Screen, and the phone's Safari.

## 1. The one rule: everything, except the window being looked at

| # | Setup | Action | Expected |
|---|---|---|---|
| 1.1 | Desktop tab focused, showing the agent's window. Phone on. | Event | Nothing anywhere. The green pill only. Record: skipped `looking`, `looking` names the desktop and the window. |
| 1.2 | Desktop tab focused, showing another tmux window. | Event | Desktop notification, phone push. |
| 1.3 | Desktop tab on another workspace (visible to the OS, unfocused). | Event | Desktop notification in the system tray, phone push. |
| 1.4 | Desktop on another browser tab (Merlin hidden). | Event | Desktop notification, phone push. |
| 1.5 | Merlin closed on the desktop. | Event | Phone push only. |
| 1.6 | Phone app open in front, on the agent's window. Desktop unfocused. | Event | Nothing anywhere: the phone is looking. Record: `looking` names the phone. |
| 1.7 | Phone app open in front, on another window. | Event | Phone notification (from the app) and desktop notification. |
| 1.8 | Phone app in the background, screen locked. | Event | Phone push, desktop as per its own state. |
| 1.9 | Two desktop tabs of the same instance, one focused on the window. | Event | Nothing anywhere. |

## 2. One notification per browser

| # | Setup | Action | Expected |
|---|---|---|---|
| 2.1 | Desktop browser with push on, tab open and hidden. | Event | Exactly one notification on that desktop (the push, not the tab's). |
| 2.2 | Desktop browser without push, tab open and hidden. | Event | Exactly one notification (the tab's). |
| 2.3 | Phone app open on another window, push on. | Event | Exactly one on the phone. |

## 3. Retraction: the notification follows the green pill

| # | Setup | Action | Expected |
|---|---|---|---|
| 3.1 | Notification showing on desktop and phone, app open on the phone. | Visit the window on the desktop | Both disappear within a poll (about two seconds), the pill turns off. |
| 3.2 | Same, app open on the phone. | Visit the window on the phone | Both disappear. |
| 3.3 | Notification showing on the phone, app closed. | Visit the window on the desktop | Phone keeps it (no silent push). Open the app: it disappears within a poll. |
| 3.4 | Agent finished while you were watching the window. | Leave the window | Pill off, nothing was shown (1.1), nothing to retract. |
| 3.5 | An ask notification showing everywhere. | Answer the question on any device | All disappear. |
| 3.6 | An ask notification showing. | Visit the window without answering | Stays everywhere: ask clears only when answered. |
| 3.7 | Two windows waiting, notifications for both. | Visit one | Only that one disappears. |

## 4. Content

| # | Setup | Action | Expected |
|---|---|---|---|
| 4.1 | Any device, event done. | Read the notification | Title `window · session · environment`, most specific first. Body starts with `Finished after <duration>:` then the agent's last line(s). No environment as a container id: the slug on Merlin Cloud. |
| 4.2 | Event ask from a question dialog. | Read | Body `Needs an answer:` then the question text. |
| 4.3 | Event ask from a permission prompt (non-bypass session). | Read | Body `Needs an answer:` and the tool line. If it is a bare `Needs an answer`, note it: the box-row rule in the snippet cleaning. |
| 4.4 | Window first seen after a Merlin restart. | Event | Body `Finished:` with no duration (start unknown). |
| 4.5 | Agent that ran under a minute, over an hour. | Event | `after 40 s`, `after 1 h 20 min`. |

## 5. Tapping

| # | Setup | Action | Expected |
|---|---|---|---|
| 5.1 | Push on the phone, app closed. | Tap | App opens on that window. |
| 5.2 | Push on the phone, app open on another window. | Tap | App comes to front, switched to that window. |
| 5.3 | Desktop tray notification, tab hidden. | Click | Tab comes to front, switched to that window. |
| 5.4 | Desktop tray notification, browser closed (push). | Click | Browser opens Merlin on that window. |
| 5.5 | The window was killed before the tap. | Tap | Lands on the terminal, no error, target ignored. |

## 6. The popover

| # | Setup | Action | Expected |
|---|---|---|---|
| 6.1 | Fresh browser. | Open the bell | One toggle "Notify me on this device", off. Sentence: "Turn on to be told... The browser will ask once." Test disabled. |
| 6.2 | Fresh desktop browser with push possible. | Turn on | Permission asked once. Sentence "On. You will be told here, and on this device even when Merlin is closed." Device listed with "this device". Bell dot hidden. |
| 6.3 | Brave with its push setting off. | Turn on | Toggle stays on. Sentence "On while Merlin is open in this browser. Brave blocks push until..." naming the setting. Bell dot shown. |
| 6.4 | Brave, setting turned on afterwards. | Off, then on | Subscribes. Sentence as 6.2. |
| 6.5 | Phone Safari tab (not installed). | Open the bell | Sentence under the toggle: add Merlin to the Home Screen. Turning on gives in-app notifications only. |
| 6.6 | Phone installed app. | Turn on | Permission asked once, subscribed, listed. |
| 6.7 | Any device, on. | Turn off | Preference off, device removed from the list everywhere, sentence "Off. Turn on...". |
| 6.8 | Phone on. On the desktop, remove the phone from the list. | Open the bell on the phone | Toggle off, sentence "Turned off from another device. Turn on to be told here again." No push arrives on the phone in between. |
| 6.9 | Device removed elsewhere, then turned on again there. | Turn on | Subscribed again, listed on the other device. |
| 6.10 | Permission blocked in the browser's site settings. | Open the bell | Toggle disabled, sentence says blocked and where to allow. |
| 6.11 | Plain HTTP on a LAN address. | Open the bell | Toggle disabled or push impossible with the HTTPS sentence. Title count still works. |
| 6.12 | Merlin running with no tmux server. | Open the bell | The one-sentence notice, re-checked every two seconds, replaced by the toggle once the terminal is opened. |
| 6.13 | Desktop without push, on. | Test it | The tab's own notification, sentence "Test shown." |
| 6.14 | Any device with push, on. | Test it | A push on this device, sentence "Test sent to this device." Not on the others. |
| 6.15 | Popover on the phone. | Look | Sheet above the bottom bar, readable, no code font, the sentence never empty. |

## 7. The phone as an app

| # | Setup | Action | Expected |
|---|---|---|---|
| 7.1 | Installed app, keyboard closed. | Look at the bottom bar | Buttons above the home indicator, clear of the rounded corners. |
| 7.2 | Installed app. | Open the keyboard | Bar and input line land above the keyboard as it animates, bottom padding gone while it is open. |
| 7.3 | Installed app, suspended for over a minute. | Come back | Terminal reconnects without a keystroke. |
| 7.4 | App installed before a rename of the environment. | Reinstall | Name and icon carry the environment. |

## 8. Nothing else fires

| # | Setup | Action | Expected |
|---|---|---|---|
| 8.1 | Page load, any device. | Load | No permission prompt, no notification. |
| 8.2 | A window going busy or idle. | Observe | No notification. Badge and title count update. |
| 8.3 | A window already done when Merlin starts. | Start Merlin | One notification (first sighting counts). |
| 8.4 | Reload a tab with old notifications in its past. | Reload | Nothing replays. |
| 8.5 | A permission-prompt session answered rapidly at the window. | Approve several | Nothing fires (looking). If a flurry ever reaches a device, the record shows the events: that is when a limit would be worth adding. |

## 9. Reading the record

Each `attention_routed` line has `seq`, `target`, `window`, `state`, `pushed` (devices
reached), `skipped` (`looking`, `no subscriptions`, `error` or empty) and `looking`, the
pages that were visible and focused, with the browser and the window each displayed.
A case that fails is explained by comparing its `looking` list with the setup.
