# Notifications: Internals

Implementation reference for the notifications feature. The user guide is
[`docs/notifications.md`](../notifications.md). The module is `notifications/`, three files with
fixed responsibilities that the future hub relay reuses unchanged.

```
notifications/
├── watcher.py     # tmux sweep -> attention events (knows nothing about delivery)
├── push.py        # VAPID keys, subscription store, Web Push sender (knows nothing about tmux)
├── routes.py      # /api/notifications, and the glue: sender subscribed to the watcher
└── static/
    ├── notifications.js   # bell, popover, in-tab rule, badge, title count
    ├── notifications.css
    └── sw.js              # the service worker, served at /sw.js
```

## The watcher

`watcher.Watcher` runs the sessions panel's `tmux list-windows -a -F` sweep
(`board/sweep.py`) every 2 seconds in a thread (`asyncio.to_thread`) and diffs it against
the previous sweep, keyed by the stable `@agent_sid`. One event per window entering `done`
or `ask` from any other state, or seen for the first time in one of them (the agent finished
while Merlin was down). `busy` and idle never emit, the same state twice never emits, and a
row without a sid is ignored (the identity hook and the state hook install together, so a
state without a sid is only the SessionStart race, which sets idle).

A sweep that returns `None` (no tmux server, or tmux failed) marks `tmux_available` false,
keeps the last known states so a transient failure never re-fires, and the loop retries on
the next tick. `tick()` never raises. The task starts in `main._run()` next to the scheduler
and the app lifespan's shutdown stops it (`watcher.stop()`).

**Event shape** (`Event.to_dict()`): `seq`, `sid`, `session`, `window_id`, `window_name`,
`state`, `project` (basename of `@agent_cwd`), `ts` (UTC ISO), `target` (`session:window_id`).

**Ring and cursor.** Events sit in a `deque(maxlen=200)`. A cursor is `<epoch>:<seq>`, the
epoch minted per process. `events_since(cursor)` returns `(events, cursor, dropped)`:

| cursor | events | dropped |
|---|---|---|
| none | nothing, current position | 0 |
| same epoch | events after `seq` | events between `seq` and the oldest kept |
| other epoch (a restart) | everything still in the ring | events of this process already evicted |

Publication (seq plus ring append) and reads share one `threading.Lock`, because the board
handler is synchronous and runs in a worker thread while the watcher publishes on the loop.

## The poll transport

Events ride the sessions panel's poll, not a new channel: `GET /api/board?since=<cursor>`
adds `events`, `cursor` and `dropped` to the tree. `board.js` starts with no cursor (so a
reload never replays), sends its cursor on every poll, keeps one poll in flight (later
triggers coalesce into one follow-up), consumes events on every successful response whether
or not the tree changed, and hands them to `MerlinNotifications.handleEvents`. A response to
an older cursor neither replays nor regresses the cursor.

## The page

`notifications.js` (`window.MerlinNotifications`) owns:

- **The in-tab rule.** Preference `notify-in-browser` in `localStorage`, plus a granted
  permission, plus a secure context. An event shows a `Notification` unless the page is
  visible and the event's window is this client's current window
  (`MerlinTerminal.currentWindow()`, from the socket's session frame). Title
  `<project or session> · <window name>`, body `Finished` or `Needs an answer`, tag the sid,
  icon the favicon. Click focuses the tab and switches through
  `MerlinTerminal.switchSession(target)`. Where `new Notification` throws (Android Chrome),
  the worker's `showNotification` is used with the deep link.
- **Badge and title.** `setAttention(n)` from the panel's `onAttention` drives
  `navigator.setAppBadge` / `clearAppBadge` when present, and `MerlinPageTitle.setCount(n)`,
  which prefixes `(n) ` and survives later `set()` calls. No page writes `document.title`.
- **The popover.** The only `Notification.requestPermission()` call is the browser toggle's
  change handler. The push toggle subscribes with `PushManager.subscribe` using the key from
  `/api/notifications/public-key` and posts `toJSON()` to `/subscribe`. The devices list,
  the remove action and **Send a test** call the routes below. `/api/notifications/status`
  is read when the popover opens: `swept` and `tmux === false` replace the whole body with
  the tmux sentence. An iPhone browser that is not the installed app gets the install
  sentence in place of the push toggle.
- **The deep link.** `/terminal?target=<session>:<window_id>` is applied by `terminal.html`
  on the first confirmed session frame and only then stripped from the URL.

**Reconnect after a suspension** (`terminal.html`): `visibilitychange` records when the
page left the foreground. Back after 2 to 60 seconds, the page sends a `{type: "ping"}`
control message and the socket answers `pong`, and no pong within 3 seconds replaces the
socket. After 60 seconds or more, or a back-forward-cache restore, the socket is replaced
without waiting. A probe deadline is bound to the socket it probed and cancelled in that
socket's `onclose`, so a reconnect is never torn down by a stale deadline.

## The installable app

`GET /manifest.webmanifest` (`application/manifest+json`) and `GET /sw.js` are the only
unauthenticated routes this feature adds, registered in `main.py` beside the app-shell
routes. The manifest is `Merlin · <machine>` with `short_name` the machine (`machine_name`
from `lib/merlin_ext.py`), `display` standalone, `start_url` `/terminal`, `scope` `/`, both
colours the page background `#0f1117`, icons at 192 and 512 plus a maskable 512 under
`static/icons/` (rendered from `static/favicon.svg` with `rsvg-convert`). `base.html` links
the manifest and registers `/sw.js?v=<merlin version>` at scope `/`, in secure contexts only.

**The worker does two things.** `push` shows the payload's notification (title, body, tag,
icon, deep link in `data.url`). `notificationclick` focuses a same-origin window and
navigates it to the deep link, or opens one. No `fetch` handler, no cache, no precache: a
proxied page is never served stale. `tests/unit/test_notifications_app.py` asserts the
source never mentions `'fetch'`, `caches` or `respondWith`.

## Web Push

`push.py` is standard RFC 8030 with VAPID through `pywebpush`, called with
`asyncio.to_thread` because it is synchronous. Files under `~/.merlin/notifications/`, both
written atomically with mode 0600:

- `vapid.json`: `private_pem`, `public_key` (base64url of the uncompressed P-256 point, the
  application server key), generated on first use by `VapidKeys`.
- `subscriptions.json`: `{"subscriptions": {endpoint: {endpoint, keys, label, created,
  last_success}}}`, managed by `SubscriptionStore`.

A store file that fails to parse is moved to `<name>.corrupt-<timestamp>` and the store
starts empty. A send answered with 404 or 410 removes the subscription. Any other failure is
logged with `log_event("push_failed", ...)` and never raised. TTL 300, urgency `high`,
payload under 3 KB: `title`, `body`, `tag`, `url` (`/terminal?target=...`), `sid`, `state`.

**Suppression, push only** (`PushSender.suppression_reason`): no push when a connected
terminal socket displays the event's window (`terminal.routes.is_displayed`, fed by a
per-connection registry of the last reported session frame), and no second push for the
same sid within 20 seconds. A suppressed event does not arm the rate limit. In-tab
notifications keep their own rule.

**Routes** (`mount_module`, under `require_auth`): `GET /api/notifications/status`,
`GET /public-key`, `POST /subscribe` (`{subscription, label?}`, the label falls back to a
short one derived from the user agent), `DELETE /subscribe` (`{endpoint}`),
`GET /devices`, `POST /test` (`{endpoint?}`, bypasses suppression, 409 with no device).

**Glue** (`routes.wire_push`, called from `main._run()`): the sender is built for the
current Merlin home with the terminal's `is_displayed`, and subscribed to the watcher with a
listener that schedules `sender.deliver(event)` on the running loop.

## Seams kept for the hub

The hub epic (`epics/cli/hub/`, milestone 7) forwards the watcher's events from a node and
runs the same `PushSender` on the hub with its own store. The watcher never imports the
sender, the sender never imports tmux or the terminal, and `routes.py` is the only file that
knows both. Nothing else needs to move.

## Tests

- `tests/unit/test_notifications_watcher.py`: transitions, tmux gone and back, event shape,
  cursor semantics (restart, overrun, concurrent publication), the task.
- `tests/unit/test_notifications_routes.py`: the poll transport, the routes and their auth,
  the lifespan, the glue, the displayed-window registry.
- `tests/unit/test_notifications_push.py`: stores, 0600 modes, corrupt files, VAPID once,
  payload, the sender with `pywebpush` replaced, the suppression rules with a fake clock.
- `tests/unit/test_notifications_app.py`: manifest, worker, icons, page shell.
- `tests/e2e/test_notifications.py`: the browser side over `http://localhost` on a throwaway
  instance with its own home and tmux server, including the push flow against a push
  service run by the test (real VAPID and `aes128gcm` encryption, fake endpoint).
