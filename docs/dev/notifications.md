# Notifications: Internals

Implementation reference for the notifications feature. The user guide is
[`docs/notifications.md`](../notifications.md). The module is `notifications/`, three files with
fixed responsibilities that the future hub relay reuses unchanged.

```
notifications/
├── watcher.py     # tmux sweep -> attention events (knows nothing about delivery)
├── content.py     # pure: the title, the body, the duration, the snippet cleaning
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
`state`, `project` (basename of `@agent_cwd`), `ts` (UTC ISO), `target` (`session:window_id`),
plus the fields added for the content: `machine` (the environment name), `busy_seconds`
(an integer, or `null` when unknown), `snippet`, `title` and `body`. Fields are only ever
added, never renamed or removed: the hub forwards these events.

**Busy tracking.** `_busy_since` maps a sid to the clock time of the sweep that last saw
the window enter `busy` from a state the watcher had seen. A transition into `done` or `ask`
takes that span (`busy_seconds`, rounded) and spends it, so an `ask` answered (`busy`) and
then `done` counts from the answer. A window first seen `busy` went busy before Merlin
looked: its span is unknown and the body omits the duration rather than guessing. An
`idle` in between does not reset the span, a later `busy` does (the last entry wins). A
`None` sweep keeps the spans with the states, a window that disappears loses its span.

**The pane capture.** For each window that transitioned, and only those, the watcher reads
the window's active pane with `board.sweep.capture_pane` (`tmux capture-pane -p`, 3 second
timeout, read-only) and cleans it with `content.clean_snippet`. The capture never runs on
the sweep's clock: `tick()` reserves each transition in order with its timestamp and
starts its capture in an owned task (`_enrich`, `asyncio.to_thread`), then returns, so the
next sweep runs on schedule and a stalled pane cannot hide a later transition of another
window. When a capture completes, the reservation queue is published from its head while
the head's capture is done (`_publish_ready`), so events reach the ring and the listeners
in transition order, each carrying its transition's `ts`. A capture that fails, times out,
raises or yields only chrome publishes its event with an empty snippet, never a missing
event. At shutdown `run()` waits up to `CAPTURE_SETTLE` (4 seconds, above one capture
timeout) for the captures in flight to publish, then cancels the rest and waits for their
cleanup: a cancelled capture withdraws its reservation and publishes the completed ones
behind it, and `run()` returns with no reservation and no task left, so a later start is
never wedged by stale state. `observe()` is the synchronous path (tests, scripted sweeps)
and captures inline. A watcher built with a scripted sweep captures nothing unless given a
`capture` too, so unit tests never touch a real tmux.

**Ring and cursor.** Events sit in a `deque(maxlen=200)`. A cursor is `<epoch>:<seq>`, the
epoch minted per process. `events_since(cursor)` returns `(events, cursor, dropped)`:

| cursor | events | dropped |
|---|---|---|
| none | nothing, current position | 0 |
| same epoch | events after `seq` | events between `seq` and the oldest kept |
| other epoch (a restart) | everything still in the ring | events of this process already evicted |

Publication (seq plus ring append) and reads share one `threading.Lock`, because the board
handler is synchronous and runs in a worker thread while the watcher publishes on the loop.

## What a notification says

`content.py` is pure and composes once, server-side. The watcher hands it the facts and
carries the result in the event's `title` and `body`. The page, the push payload and the
service worker show those two strings verbatim: there is no second composition in
JavaScript, and the hub inherits the same text by forwarding the event.

**Title**: `<window> · <session> · <environment>`, most specific first, `·` U+00B7. A
missing window name reads `window`, a missing environment is omitted with its separator.
The environment is `machine_name` (`lib/merlin_ext.resolve_machine_name`: the environment
slug on Merlin Cloud, the hostname elsewhere), resolved lazily on the first event so
`config.env` is loaded by then. The cwd basename (`project`) stays in the event but leaves
the title.

**Body**: the state, then what happened. `Needs an answer: <snippet>` for `ask`, `Finished
after <duration>: <snippet>` for `done` with a known duration, `Finished: <snippet>` without
one, and each of the three without the colon and the snippet when there is no snippet.

**Duration** (`format_duration`): `40 s` under a minute, `14 min` under an hour, `1 h 20 min`
beyond, `3 h` when the minutes round to nothing, `12 h` once the hours pass the single
digit. Rounded at every step (`59.6` seconds is `1 min`).

**Snippet** (`clean_snippet`): from the pane's visible text, ANSI sequences and control
characters removed, non-breaking spaces and tabs read as spaces. The last prompt line
(a line starting with `❯`, Claude Code's input line and the cursor of its option dialogs,
or `›`, Codex's) and everything below it go. Then the chrome above it: rule and box lines
(a first character in the box-drawing block, `────`, `╭`, `│`, `╰`, Codex's `─ Worked for
1m 16s ───`), the timing line (`✻ Cogitated for 1m 14s · done`, any glyph then a word then
`for` and a number) and blank lines. What remains ends with the agent's last message: walk
back over non-empty lines up to three, stopping after the line that starts the message
(`●` or `•`, stripped from the result) or at chrome. Whitespace is collapsed, the lines
are joined by single spaces, and the result is clipped to 240 characters with `…`. A
capture of only chrome, or none, gives an empty snippet. The rules are pinned by the
fixtures under `tests/unit/fixtures/panes/`: a Claude Code pane after a turn, a Claude
Code pane with an open question (the question sits above the `❯` option cursor), a Codex
pane after a turn, and a pane holding only the input chrome.

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
  (`MerlinTerminal.currentWindow()`, from the socket's session frame). The event's `title`
  and `body` are shown as they come (see "What a notification says"), tag the sid,
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
`title` and `body` are the event's own. `encode_payload` clips `title` to 120 and `body` to
300 characters (the snippet inside it is already clipped at 240, the state and the duration
fit in front), `tag` and `sid` to 200, then halves every string until the UTF-8 JSON is
strictly under 3 KB, and falls back to a minimal payload past that. The snippet is the one
piece of pane content that leaves the machine: Web Push encrypts the payload end to end
with the subscription's keys (RFC 8291), the push service relays ciphertext.

**The VAPID subject** (`vapid_subject`) is the contact claim the push service can hold the
sender to (RFC 8292). Apple's service validates its domain and answers `403 BadJwtToken` to
a placeholder, Chrome and Firefox do not check, which is why a fake push service cannot
catch it. The rule: the instance's public base URL (`job.webhook.resolve_public_base`, the
`MERLIN_DASHBOARD_URL` override, then the portal's whoami answer, then the environment slug)
when its scheme is `https`, otherwise the project contact `https://merlincloud.dev`. The LAN
`http://` tier, an empty answer and a resolver that raises all fall back. It is computed
once per batch of sends, at send time, and never stored: a renamed environment or a new
override applies to the next push, and nothing about the key pair or the subscriptions
changes. `PushSender` takes the rule as a `subject` callable so the hub can pass its own.

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
  cursor semantics (restart, overrun, concurrent publication), the task, the busy tracking,
  the capture (only for transitioned windows, off the loop in `tick`, failing or raising
  still emits) and the composed fields on the event.
- `tests/unit/test_notifications_content.py`: the title, the eight body shapes, the duration
  at its boundaries, the snippet cleaning against the pane fixtures and the edge rules.
- `tests/unit/test_notifications_routes.py`: the poll transport, the routes and their auth,
  the lifespan, the glue, the displayed-window registry.
- `tests/unit/test_notifications_push.py`: stores, 0600 modes, corrupt files, VAPID once,
  the subject rule per tier and on failure (claims asserted on the replaced `pywebpush`),
  payload, the sender with `pywebpush` replaced, the suppression rules with a fake clock.
- `tests/unit/test_notifications_app.py`: manifest, worker, icons, page shell.
- `tests/e2e/test_notifications.py`: the browser side over `http://localhost` on a throwaway
  instance with its own home and tmux server, including the push flow against a push
  service run by the test (real VAPID and `aes128gcm` encryption, fake endpoint), and the
  content: a window whose pane holds known text flipped to `done` and to `ask` shows the
  `window · session · machine` title and a body ending with that text, with the duration
  once the watcher has seen the window enter busy.
