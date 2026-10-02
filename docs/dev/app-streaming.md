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
  dead is reclaimed), `Xvfb -nolisten tcp`, then the app through
  `sh -c '"$@"; echo $? > exitfile'`, both with `start_new_session=True`, so
  they survive the CLI call and any Merlin restart. No window manager: the
  first new top-level window is moved to `0,0`, resized to the display and
  focused (`--no-fill` keeps its size).
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
  recycled PID counts as dead and is never signalled. Merlin only kills the
  process groups it recorded.
- **Exit**: any read (`list`, `get`, the server's start-up sweep) notices a
  dead app, records `exited` with its code, tears the display down and keeps
  the record and log until a stop or a relaunch with the same id.
- **State lock**: launch, stop and the exit transition run under one
  `flock` (re-entrant per thread). Stop marks the record `stopping` before
  killing, and the exit transition re-reads the record under the lock, so a
  stop racing a poll is never resurrected as a crash.
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
2. The server kills any previous viewer's streamer (it gets `replaced`) and
   starts `/usr/bin/python3 app/streamer.py --display :N --codecs ... --fps 60
   --bitrate K` (8 Mbit/s at 1080p, proportional, 1.5 to 12).
3. The streamer picks NVENC H.264 (after a one-frame test pipeline, since
   `nvh264enc` can exist and fail to open), else OpenH264, else VP8, builds
   `ximagesrc ! encoder ! payloader ! webrtcbin`, creates the `input` data
   channel, then the offer. It drops candidates on `docker*`, `br-*`,
   `veth*`, `virbr*` interfaces.
4. The server relays `offer`, `answer` and `ice` both ways and polls the
   record every 500 ms (`exited`, `agent_input`). When the socket closes, the
   streamer is killed and a thumbnail is captured.

Input on the data channel: `key {k, d}` (X keysym names), `move {x, y}`,
`rel {dx, dy}`, `btn {b, d}`, `wheel {dy}`, `text {s}` (xdotool type). The
streamer releases any held key or button when it stops.

Client states (`client.js`): `connecting`, `live`, `unreachable`, `replaced`,
`exited`, `paused` (hidden 30 s, except in picture-in-picture), `error`,
`closed`. A dead streamer gets one automatic retry per 20 s; a lost server
(a Merlin restart) gets seven, with backoff, before `closed`.

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
