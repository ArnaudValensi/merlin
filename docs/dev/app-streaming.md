# App Streaming

**Status: implemented, experimental, behind the `app` feature flag.** Runs
Linux GUI apps on private Xvfb displays, lets an agent drive them through the
`merlin app` CLI, and streams them to the dashboard over WebRTC on the local
network. The user guide is [`docs/apps.md`](../apps.md).

## Feature flag

The `app` built-in exists only when `MERLIN_FEATURES` lists `app` (environment
first, then `config.env`; see `features.py` and
[`extension-system.md`](extension-system.md#feature-flags)). Flag off: no
registry entry, nav item, page, `/api/apps` routes, `merlin app` namespace,
help line, skill, or terminal button. Changing it needs `merlin restart`.

## Architecture

```
 Agent (tmux window) ─ merlin app run/screenshot/input/logs/stop ─┐
 Apps page / player ─ /api/apps/* ─────────────────────────────────┤
                                                                   ▼
                                       app/sessions.py (stdlib only)
                                       registry ~/.merlin/data/apps/
                                                                   │ spawns (detached)
                                       ┌───────────────────────────┴──────────┐
                                       ▼                                      ▼
                               Xvfb :N (WxHx24)                     app: [vglrun -d egl] CMD
                                       ▲                              DISPLAY=:N, x11-only env
                                       │ ximagesrc + XTEST
 Browser ⇄ WS /ws/apps/{id}/stream ⇄ Merlin ⇄ stdin/stdout JSON ⇄ app/streamer.py
   ▲      (signaling only, authed,     (relay)                     (/usr/bin/python3,
   │       works through the proxy)                                 webrtcbin, encoder)
   └──────────── WebRTC over the LAN: H.264/VP8 video + "input" data channel ───────┘
```

The proxy (merlincloud.dev) only ever carries the signaling. The video and the
input travel peer to peer with host candidates only: no STUN, no TURN. Off the
local network (or Tailscale, which looks like a LAN to WebRTC) the client
times out after 8 s and says so.

## Code

| Path | Role |
|---|---|
| `features.py` | `enabled(name)`: the generic flag check |
| `ext_commands.py` | `FLAGGED_BUILTINS`; `builtin_extension_dirs()` drops a flagged built-in whose flag is off |
| `main.py` | `_load_flagged_builtins()` loads `app` only with its flag |
| `app/__init__.py` | Extension exports, routes loaded lazily so CLI imports stay light |
| `app/sessions.py` | The one library behind the CLI and the API: displays, lifecycle, registry, input, screenshots, saved apps. **Stdlib only** |
| `app/supervise.py` | Per-app supervisor: group leader and child subreaper, exit code, ends leftovers. **Stdlib only** |
| `app/deps.py` | Prerequisite checks (runs a probe in the system Python) |
| `app/cli_support.py`, `app/commands/*.py` | `merlin app run/list/stop/logs/screenshot/input`, `#!/usr/bin/env python3` |
| `app/routes.py` | `/apps`, `/apps/{id}/play`, `/api/apps/*`, the signaling WebSocket |
| `app/streamer.py` | WebRTC helper, one per viewer, **system Python** (PyGObject, python-xlib) |
| `app/static/client.js` | Browser WebRTC client shared by the player, the panel and the mini-player |
| `app/static/player.*` | Full-screen player and its touch controls |
| `app/static/terminal-panel.*` | Terminal button, toast, docked panel, mobile mini-player |
| `app/static/apps.*`, `app/templates/apps.html` | The Apps page |
| `app/skills/merlin-app/SKILL.md` | The agent skill |
| `tests/fixtures/x_probe.py` | X11 test app: known colors, logs keys and clicks |

## Sessions

A session is one app on one private display, recorded as
`~/.merlin/data/apps/sessions/<id>.json`; its output goes to
`logs/<id>.log`, its exit code to `exit/<id>`, its last frame to
`thumbs/<id>.png`. Saved apps live in `saved.json`.

- **Launch**: the first free display from `:100` (a stale lock whose PID is
  dead is reclaimed; readiness requires the lock file to name our Xvfb, so a
  display another X server took meanwhile is skipped), `Xvfb -nolisten tcp`,
  then the app under `app/supervise.py`, both with `start_new_session=True`,
  so they survive the CLI call and any Merlin restart. No window manager: the
  first new top-level window is moved to `0,0`, resized to the display and
  focused (`--no-fill` keeps its size).
- **Supervisor**: leads the app's process group and is a child subreaper, so
  every orphan (and a `setsid` escapee) is re-parented to it. It writes the
  main process's exit code (128+N for a signal), then ends whatever is left
  and exits last; a SIGTERM to it does the same. Ending is a loop of passes
  (a killed process may leave children in another group, adopted next): new
  processes get SIGTERM once, and after 3 s every pass sends SIGKILL. It
  signals individual processes through pidfds, re-checked against /proc
  after opening, never by bare PID. While anything of the app lives, its
  leader does, so the group number cannot be reused. Merlin's stop waits up
  to 10 s for it before signalling the group itself.
- **Environment**: the caller's, minus `WAYLAND_DISPLAY`/`WAYLAND_SOCKET`,
  plus `DISPLAY=:N` and the X11 overrides for SDL, GTK and Qt. Without the
  scrub, an app launched from a Wayland desktop session opens on the real
  desktop.
- **GPU**: `auto` uses `vglrun -d egl` when VirtualGL and a
  `/dev/dri/renderD*` node exist. `off` forces Mesa
  (`__GLX_VENDOR_LIBRARY_NAME=mesa`, `LIBGL_ALWAYS_SOFTWARE=1`): on a desktop
  that pins the NVIDIA GLX vendor, an app on Xvfb would otherwise still
  render on the GPU.
- **Liveness** is PID plus `/proc/<pid>/stat` start time: a zombie or a
  recycled PID counts as dead. **Ownership** (whether a group may be
  signalled) needs the group's leader present, alive or zombie, with the
  recorded start time; a group seen without its leader is never signalled.
  `_kill_group` sends SIGTERM to the group, waits for the leader and members,
  re-checks ownership, then SIGKILL.
- **Exit**: any read (`list`, `get`, the server's start-up sweep) notices a
  dead app, records `exited` with its code, tears the display down and keeps
  the record and log until a stop or a relaunch with the same id.
- **State lock and generations**: launch, stop and the exit transition run
  under one `flock` (re-entrant per thread). Each launch gets a `generation`
  id; every later update goes through `_update_record`, which re-reads under
  the lock and applies only to the same generation, so a stop is never
  resurrected (by the window wait, the exit check or an agent input) and a
  `run --replace` is never overwritten. Stop reads and marks the record
  `stopping` under the lock before killing.
- **Origin**: a CLI call inside tmux records `$TMUX_PANE`'s session, window
  id and window name. That is what puts the ▶ button on the right terminal
  window.

## Agent input and screenshots

Local, never streamed: `import -display :N -window root` for screenshots,
`xdotool` for keys (`--hold` sends keydown, sleep, keyup for games that read
key state once per frame), text, clicks and moves. Each input stamps
`last_agent_input_at`, which the server pushes to the viewer as
`agent_input`.

## Streaming

`/ws/apps/{id}/stream`, registered with `register_routes` and authed like the
terminal (`verify_ws_cookie`, close `4401`).

1. Server → browser `welcome` (`host`, the app's public record). Browser →
   server `hello` with the codecs from `RTCRtpReceiver.getCapabilities`.
2. Under a per-app lock (so simultaneous viewers still end with one), the
   server stops any previous viewer's streamer (it gets `replaced`), re-reads
   the record (the app may have been relaunched during the handshake), binds
   the viewer to its `generation`, and starts `/usr/bin/python3
   app/streamer.py --display :N --codecs ... --fps 60 --bitrate K --xvfb-pid P
   --xvfb-start T` in its own process group (8 Mbit/s at 1080p, proportional,
   1.5 to 12). The streamer takes the apps state lock (`--state-lock`, the
   one launch and stop take) while it checks that the display's lock still
   names that Xvfb and opens its input and capture connections, so the
   display cannot be torn down and handed to another app in between.
3. The streamer picks NVENC H.264 (after a one-frame test pipeline, since
   `nvh264enc` can exist and fail to open), else OpenH264, else VP8, builds
   `ximagesrc ! encoder ! payloader ! webrtcbin`, creates the `input` data
   channel, then the offer. It drops candidates on `docker*`, `br-*`,
   `veth*`, `virbr*` interfaces.
4. The server relays `offer`, `answer` and `ice` both ways and polls the
   record every 500 ms (`exited`, `agent_input`, and `restarted` when the
   generation changed: the client reconnects to the new launch). When the
   socket closes, the streamer is stopped gracefully (stdin closed: it stops
   typing, releases held input, quits), its group signalled only if it does
   not, and a thumbnail is captured.

Input on the data channel: `key {k, d}` (X keysym names), `move {x, y}`,
`rel {dx, dy}`, `btn {b, d}` (1-3, 8, 9), `wheel {dy}`, `text {s}`. For
printable characters the streamer presses the keycode with the Shift level
the display's keymap needs (adding or lifting Shift around the press), so a
French `&` or Shift+`1` arrives as typed on a US keymap; other keys (arrows,
Tab, F-keys) keep the user's modifiers. Input is applied strictly in arrival
order: text goes through `xdotool type` asynchronously (a long paste never
blocks the main loop or shutdown) and everything after it waits for it, so a
paste then Enter submits the whole paste. The typist is started with
`PR_SET_PDEATHSIG` (after checking its parent is still the streamer), so it
dies even if the streamer is killed outright. The streamer releases any held
key or button, and stops typing, when it stops.

Client states (`client.js`): `connecting`, `live`, `unreachable`, `replaced`,
`exited`, `paused` (hidden 30 s, except in picture-in-picture: entering and
leaving it re-arms the timer; pausing cancels a pending reconnect), `error`,
`closed`. A dead streamer gets one automatic retry per 20 s; a lost server
(a Merlin restart) gets seven, with backoff, before `closed`. Every
connection carries a generation and the client a `destroyed` flag: callbacks
of a superseded or destroyed connection (a late promise, a closing socket)
do nothing. Desktop mouse buttons use pointer capture (a drag ending outside
the video still releases) and are reconciled with the `buttons` bitmask, so
chords (left held, right pressed) reach the app; buttons over the video
(Retry, Logs) are exempt from the capture.

## UI

- **Terminal**: the page dispatches `merlin:terminal-window` when its tmux
  window changes; `terminal-panel.js` polls `/api/apps/sessions` every 3 s,
  shows `▶ <name>` for running apps whose origin is the current window (a
  chooser for several), a toast for a new one, a docked panel on desktop
  (takes the Sessions slot, gives it back on close) and a draggable
  mini-player on mobile (above the key toolbar, corner persisted, native
  picture-in-picture, tap for the player).
- **Player** (`/apps/{id}/play`, standalone page): ⋯ sheet, status chip,
  Gamepad / Trackpad / Touch profiles on touch devices (remembered per app),
  client-side pinch zoom, key row and phone keyboard, Wake Lock, fullscreen
  plus landscape lock on Android, a home-screen hint on iPhone. Desktop gets
  keyboard (mapped from `KeyboardEvent.key`, so the user's layout types the
  right letters) and mouse.
- **Apps page**: cards for saved and running apps, thumbnails retried until
  they exist, two-tap Stop and Delete, the form (folder picker, fit-to-device
  size), logs, the prerequisites checklist.

## Gotchas

- **The offer lives in the promise reply.** In `on_offer_created`, keep the
  reply referenced until `set-local-description` returns: letting PyGObject
  free it first segfaults inside webrtcbin.
- **System Python only for the streamer.** PyGObject and python-xlib come
  from the distribution; `app/streamer.py` is excluded from ty for that
  reason. Everything the CLI imports stays stdlib-only.
- **Playwright's bundled Chromium decodes H.264**, so the default path in
  tests is NVENC; the VP8 fallback is tested by hiding H.264 from
  `getCapabilities`.
- **Apps run as the user**, with the user's files: a game loads and writes the
  user's real saves. The skill tells agents so.
- **Audio** is not streamed: the app plays on the machine's own output.

## Tests

- Unit: `test_app_flag.py` (the flag everywhere), `test_app_sessions.py`
  (lifecycle on real Xvfb), `test_app_agent.py` (screenshots, input, skill),
  `test_app_routes.py` (API, auth, saved apps, missing prerequisites).
- E2E (`uv run scripts.py test-e2e`): `test_app_stream.py` (pixels, input,
  codecs, single viewer, cleanup), `test_app_terminal.py`,
  `test_app_player.py` (touch profiles through CDP touch events),
  `test_app_page.py`, `test_app_hardening.py` (crash, streamer death, restart,
  no Xvfb).
- Tests needing Xvfb or GStreamer skip with a reason on machines without
  them.
