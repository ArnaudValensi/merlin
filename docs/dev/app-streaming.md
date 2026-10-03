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
                                       ▲                              DISPLAY=:N, x11-only env,
                                       │ ximagesrc + XTEST            PULSE_SINK=merlin_app_…
                                       │                                      │ plays into
                                       │        app's sink (+ its guard), held by the supervisor
                                       │                                      │ its monitor
                                       │                                      ▼ pipewiresrc
 Browser ⇄ WS /ws/apps/{id}/stream ⇄ Merlin ⇄ stdin/stdout JSON ⇄ app/streamer.py
   ▲      (signaling only, authed,     (relay)                     (/usr/bin/python3,
   │       works through the proxy)                                 webrtcbin, encoders)
   └──── WebRTC over the LAN: H.264/VP8 video + Opus sound + "input" data channel ───┘
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
| `app/sessions.py` | The one library behind the CLI and the API: displays, sound sinks, lifecycle, registry, input, screenshots, saved apps. **Stdlib only** |
| `app/supervise.py` | Per-app supervisor: group leader and child subreaper, exit code, ends leftovers, holds the app's sound nodes. **Stdlib only** |
| `app/sound.py` | The app's sink and its guard: node specs, `pw-dump` reads. **Stdlib only** |
| `app/deps.py` | Prerequisite checks (runs a probe in the system Python) |
| `app/cli_support.py`, `app/commands/*.py` | `merlin app run/list/stop/logs/screenshot/input`, `#!/usr/bin/env python3` |
| `app/routes.py` | `/apps`, `/apps/{id}/play`, `/api/apps/*`, the signaling WebSocket |
| `app/streamer.py` | WebRTC helper, one per viewer, **system Python** (PyGObject, python-xlib) |
| `app/static/client.js` | Browser WebRTC client shared by the player, the panel and the mini-player |
| `app/static/player.*` | Full-screen player and its touch controls |
| `app/static/terminal-panel.*` | Terminal button, toast, docked panel, mobile mini-player |
| `app/static/apps.*`, `app/templates/apps.html` | The Apps page |
| `app/skills/merlin-app/SKILL.md` | The agent skill |
| `tests/fixtures/x_probe.py` | X11 test app: known colors, logs keys and clicks, `--tone HZ` plays a sine |

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

Local, never streamed, and run under the state lock with the session
re-checked (`_on_display`), so the display cannot change hands between the
check and the connection. Long input is split into short locked steps (one
key, or 24 characters of text; holds and delays wait outside the lock) with
the launch generation re-checked each time, so other apps stay usable and a
stop ends the input between two steps: `import -display :N -window root` for screenshots,
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
order: text is typed by the streamer itself, one character per main-loop
tick on its own XTEST connection (so never blocking, and on the display
checked under the state lock), and everything after it waits for it, so a
paste then Enter submits the whole paste. A character the keymap lacks (é on
a US map) gets a slot from `KeyAllocator`: X key events carry keycodes and an
app decodes them with the keymap as it is when it reads them, so a slot keeps
its character. A free keycode holds two (plain and Shift level; filling one
never changes the other). While the Shift level is vacant the row repeats the
plain symbol (`[É, É]`): a lone `[É, NoSymbol]` is read by XKB as the case
pair é/É, so É would arrive as é. Keycodes the allocator owns answer only to
it: the connection's cached keymap, which may still list a reassigned
character there, is never consulted for them. Xvfb's default keymap has only 15 free keycodes,
so 30 slots: when all are taken, the least recently used keycode is
reassigned only after 2 s unused, typing waits until then, and the reuse is
logged. (X cannot tell when an app has read an event: an app more than 2 s
behind on its input while more than 30 distinct such characters are typed
can still misread one.) Slots are recorded per Xvfb identity
(`data/apps/keymaps/<xvfb pid>-<start>.json`, removed with the display); a
later streamer adopts those whose keymap entry still matches. With no free
keycode at all, the character is logged as not typable and skipped. A failing character or a malformed message is logged and skipped; the
queue never stalls. The streamer stops typing and releases any held key or
button when it stops.

Client states (`client.js`): `connecting`, `live`, `unreachable`, `replaced`,
`exited`, `paused` (hidden 30 s, except in picture-in-picture: entering and
leaving it re-arms the timer; pausing cancels a pending reconnect), `error`,
`closed`. "Unreachable" means ICE did not connect within 8 s of the offer;
before the offer the server may be busy (another app starting or stopping),
bounded separately by 60 s. A dead streamer gets one automatic retry per 20 s; a lost server
(a Merlin restart) gets seven, with backoff, before `closed`. Every
connection carries a generation and the client a `destroyed` flag: callbacks
of a superseded or destroyed connection (a late promise, a closing socket)
do nothing. Desktop mouse buttons use pointer capture (a drag ending outside
the video still releases) and are reconciled with the `buttons` bitmask, so
chords (left held, right pressed) reach the app; buttons over the video
(Retry, Logs) are exempt from the capture.

## Sound

Each app's sound streams with its picture, in the same WebRTC connection (one
connection keeps them in sync: both tracks share the stream id and CNAME, and
the browser's jitter buffer aligns them).

- **A sink per app, held by its supervisor.** With `audio: stream` (the
  default; CLI `--audio`, saved apps' `audio` field) launch names a sink
  `merlin_app_<id>_<generation[:12]>` and passes it to the supervisor
  (`--audio-sink`, `--audio-status`). Before starting the app the supervisor
  opens a `pw-cli` connection and creates two PipeWire null nodes on it
  (`app/sound.py` has the specs), both `object.linger=false` (they die with
  the connection) and `state.restore-*=false` (WirePlumber keeps no state for
  them):
  - the **guard** `<sink>_guard`, created first, `priority.session=0`, never
    captured (at 0 it ties with ordinary virtual sinks, and WirePlumber then
    prefers the older one: the user's own);
  - the **sink** `<sink>`, `priority.session=-1`, class `Audio/Sink` (so
    PipeWire's pulse server lists it and pulse clients can play into it): the
    only node the streamer captures.
  WirePlumber picks a default output by `priority.session`, then by age: the
  guard always outranks the sink, so if the machine's real outputs go away
  the silent guard becomes the default, never the sink, and no other
  program's sound reaches the capture. The supervisor waits until WirePlumber
  has set each node up (it has input ports) before the next (4 s for both,
  every `pw-dump` bounded by what is left), writes `stream`
  or `local` to the status file (on `local` it drops the routing variables:
  the app plays on the machine) and only then starts the app; launch waits
  for that file and records the effective mode (`audio`, `audio_requested`,
  `audio_sink`).
- **Routing by name only.** The app's environment points at the sink:
  `PULSE_SINK` (libpulse), `PIPEWIRE_NODE` (native PipeWire, and its ALSA
  plugin), `SDL_AUDIO_DRIVER` / `SDL_AUDIODRIVER=pipewire,alsa,pulseaudio`:
  SDL's pulse backend opens the default output by name, past `PULSE_SINK`,
  while its pipewire backend (and ALSA through PipeWire's plugin) honors
  `PIPEWIRE_NODE`. Nothing moves streams: an app that names another output
  plays there, on the machine, and is not streamed. With `stream` the app is
  silent on the machine even when nobody watches.
- **Ownership is lifetime.** The holder is the supervisor's child in a
  process group of its own (Merlin's group signals do not reach it), with a
  parent-death signal (SIGKILL), signalled only through a pidfd opened before
  the supervisor reaps anything. When the app ends (exit or stop) the
  supervisor spares it until no other process of the app is left, so an app
  still playing during its grace never falls back to the speakers, then
  kills it: the nodes go. A killed supervisor takes it along. Nothing is
  unloaded by number and there is nothing to sweep.
- **Capture**: the streamer gets `--audio-sink`, checks the sink is there
  (`pw-dump`), and adds `pipewiresrc target-object=<sink>` (the sink's
  monitor, never falling back, reconnecting or moving: see Gotchas) `!
  opusenc` (96 kbit/s, 20 ms frames, `restricted-lowdelay`) `! rtpopuspay
  pt=97 ! webrtcbin`, a second send-only transceiver. `ready` carries
  `audio: true|false`; the UI shows the sound toggles only when it is true.
  Until the offer exists the sound can hold up the whole stream (webrtcbin
  offers once every track has data), so a sound failure before it (PAUSED or
  PLAYING refused, an error from an audio element, or no offer within 5 s)
  restarts the pipeline without sound, under the state lock with the
  display's owner checked again, and sends `ready` again with `audio: false`;
  callbacks of the dropped webrtcbin are ignored and the client closes a
  replaced peer. After the offer, an audio error (the sinks went away) is
  logged and the picture goes on. Publishing an offer and dropping the sound
  share a lock: an offer is either out (the sound stays) or its webrtcbin
  was invalidated first and it is never sent. In the browser every callback
  of a peer checks it is still the current one, so a replaced peer finishing
  late (well or badly) never touches its successor.
- **Browser**: the `<video>` element carries both tracks (one `MediaStream`
  per connection collects them). Its answer adds `stereo=1` to the Opus fmtp
  (otherwise Chromium and WebKit decode mono). Sound needs a user gesture:
  `MerlinApps.sound(video, surface, target)` keeps a per-surface choice in
  `localStorage` (`app-sound-player|panel|mini`, defaults on, on, off) and
  applies it on the next `pointerup`, `touchend` or `keydown` (elements marked
  `data-sound`, the toggles, are left to their own click). The player starts
  muted and the first touch brings the sound; the docked panel unmutes on the
  click that opened it; the mini-player stays muted until its speaker. If the
  browser still refuses unmuted playback, the client falls back to muted
  playback (the picture never waits for a gesture) and the next gesture
  retries.
- **PipeWire only.** `audio_available()` requires `pactl info` to name
  PipeWire and `pw-cli`, `pw-dump`; anything else, or nodes that cannot be
  made, falls back to `local` (see Gotchas).

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
- **The capture must never fall back.** A capture whose target is missing,
  or whose sink goes away, is linked by PipeWire (WirePlumber) to the default
  device: the microphone or everything the machine plays, streamed to the
  phone. Through `pulsesrc` nothing prevents it (verified: the pulse layer
  ignores the stream properties). `pipewiresrc` with
  `node.dont-fallback`, `node.dont-reconnect`, `node.dont-move` and
  `stream.capture.sink` stays unlinked instead, and needs `media.type=Audio`
  / `media.category=Capture` or WirePlumber finds no target at all. Plain
  PulseAudio moves a capture to the default source when its sink goes away,
  so sound streaming is PipeWire only.
- **SDL's pulse backend opens the default output by name** (SDL3, and SDL2
  through sdl2-compat), past `PULSE_SINK`: oob played on the speakers. Its
  pipewire backend honors `PIPEWIRE_NODE`, hence the driver list. The probe's
  `--tone-device` reproduces a named output in the tests.
- **Moving streams is not safe.** An earlier version moved the app's streams
  to its sink (`pw-metadata`, `pactl move-sink-input`): a node's global id is
  reused right after it goes, so a move can land on another program's new
  stream; a pulse client's `application.process.id` is what it says (its
  `pipewire.sec.pid` is the pulse server's), so a sandboxed client's could
  collide with an app PID; and WirePlumber stores a moved stream's target per
  application name, restoring it onto the next stream of that name or
  erasing the user's saved choice. Routing by name at creation has none of
  this.
- **A pulse stream aimed at a node pulse cannot list never starts** (it stays
  suspended, its links paused), so the captured sink is an ordinary
  `Audio/Sink`, kept from being a default by its guard rather than by a
  class WirePlumber ignores.
- **The pipeline runs on the system clock** (`use_clock`): `pipewiresrc`
  provides a clock, and one that stops with a lost sink would stop the
  picture.
- **Opus in-band FEC needs SILK**; `restricted-lowdelay` is CELT only, so
  there is no FEC (negligible loss on a LAN).

## Tests

- Unit: `test_app_flag.py` (the flag everywhere), `test_app_sessions.py`
  (lifecycle on real Xvfb), `test_app_agent.py` (screenshots, input, skill),
  `test_app_routes.py` (API, auth, saved apps, missing prerequisites),
  `test_app_audio.py` (on the real PipeWire: routing, the guard outranks the
  sink, the tone reaches the sink and nothing else, stop, exit and a killed
  supervisor remove both nodes, a stopping app that ignores SIGTERM never
  reaches a real output, a stream naming another output is left alone and
  unheard, no WirePlumber state, local mode, degradation),
  `test_app_streamer.py` (attachment under the lock, the restart without
  sound, an offer finishing during it, stale webrtcbin callbacks),
  `tests/js/app-client.test.js` (client states, stereo answer, sound choices).
- E2E (`uv run scripts.py test-e2e`): `test_app_stream.py` (pixels, input,
  codecs, single viewer, cleanup), `test_app_terminal.py`,
  `test_app_player.py` (touch profiles through CDP touch events),
  `test_app_page.py`, `test_app_hardening.py` (crash, streamer death, restart,
  no Xvfb), `test_app_audio.py` (a WebAudio analyser hears the probe's
  440 Hz; local mode is picture only; each surface's sound default and
  toggle under Chromium's real autoplay rule; sinks lost mid-stream keep the
  picture coming (decoded frames) and never move the capture; sinks gone
  before the viewer give a silent stream).
- Tests needing Xvfb or GStreamer skip with a reason on machines without
  them.
