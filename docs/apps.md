# Apps

> **Experimental.** Apps are hidden unless you turn them on: add
> `MERLIN_FEATURES=app` to `~/.merlin/config.env`, then run `merlin restart`.
> Linux only.

Run a graphical Linux app (a game you are building, a GUI tool, a browser) on
its own invisible display, and watch or play it from the dashboard, on your
desktop or your phone. Your agent can run it too, take screenshots and press
keys, while you watch the same app live next to its terminal.

The video goes **directly over your local network** with WebRTC: low latency,
up to 60 fps, GPU-encoded on NVIDIA machines. Your phone must be on the same
Wi-Fi as the machine running Merlin (beyond it, see below). Opening the
dashboard through merlincloud.dev works: only the connection setup goes
through it.

## Prerequisites

The Apps page lists what is missing. On Arch Linux:

```bash
sudo pacman -S xorg-server-xvfb virtualgl xdotool python-xlib
```

GStreamer with WebRTC (`gst-plugins-bad`), PyGObject and ImageMagick are also
needed; most desktops already have them. VirtualGL is optional (GPU rendering
for OpenGL apps); without it apps render in software. Sound streaming needs
PipeWire (the default on current desktops) with `pactl` and its GStreamer
plugin (`gst-plugin-pipewire`); without them apps play on the machine and the
stream is silent.

## Let your agent run it

Ask your agent to run the app; it knows the `merlin app` commands:

```bash
merlin app run --name oob -- ./build/OutOfBody     # starts it, returns at once
merlin app screenshot oob                          # a PNG of the screen
merlin app input oob key Right                     # keys, text, clicks
merlin app logs oob                                # its output, also after a crash
merlin app stop oob
```

When an agent starts an app from a terminal window, a **▶ oob** button appears
in that window's status bar, with a toast. Tap it:

- on desktop the app docks beside the terminal; click it to send it your
  keyboard and mouse, click the terminal to get them back;
- on a phone it floats as a small player you can drag to any corner; tap it
  for the full-screen player, or use picture-in-picture to keep watching while
  you do something else.

## Launch it yourself

**Apps** in the sidebar lists your saved apps and everything running. **Add**
one with a name, the command, its folder, the touch controls it needs and a
size. "Fit to the device you launch from" gives a phone a display of its own
shape. **Launch** opens the player; **Leave** keeps the app running, like the
terminal; **Stop** ends it.

## Play on your phone

The player fills the screen (on Android it also goes full screen and turns
landscape; on iPhone, add Merlin to your Home Screen for that). The **⋯**
button opens the menu. The chip at the top right shows the connection: bars
for its quality (four when the stream runs at full quality, fewer as it
lowers its bitrate for a weak network; yellow, then red), how you are
connected (`LAN`, `Internet · IPv6`…) and the round trip in
milliseconds (a packet's trip to the machine and back). Tap it for the
numbers. It says "agent is pressing keys" when your agent is.

Three control profiles, switchable from the menu:

| Profile | For | How |
|---|---|---|
| Gamepad | Games | D-pad = arrow keys; the buttons send the keys set for the app (e.g. `A=x,B=z,Start=Escape`) |
| Trackpad | Desktop apps | Drag moves the cursor, tap clicks, two-finger tap right-clicks, two-finger drag scrolls, double-tap-and-drag drags |
| Touch | Touch-friendly apps | Tap where you touch, long-press to right-click, drag to drag |

Pinch to zoom in on the picture in any profile. **Keyboard** in the menu opens
your phone's keyboard plus a row of Esc, Tab, Ctrl, Alt, arrows, Enter and
Backspace.

When the app ends (you quit the game), the player offers **Logs** (they open
in the player; **Back** returns) and **Leave**.

## Sound

The app's sound comes with the picture. Browsers only play sound after you
touch the page, so the player starts silent and your first tap, click or key
turns the sound on. **Sound** in the ⋯ menu turns it off and on. In the
terminal, the docked panel plays the sound and the phone's mini-player stays
silent until you tap its speaker. Each place remembers your choice.

While an app streams its sound, it is silent on the machine itself. To hear it
there instead, launch it with `--audio local` (or pick "Play on this machine"
in the app's form); its stream is then silent. An app that picks a particular
output device by name (rather than "the default one") plays there instead, on
the machine, and its sound is not streamed.

## Good to know

- Apps keep running when you close the page or restart Merlin; only Stop ends
  them.
- One screen at a time watches an app: opening it elsewhere takes over.
- Apps run as you, with your files: a game uses your real saves.
- From outside your home: it works over IPv6 when your machine and your
  phone both have it and your router lets the stream in (try it on 4G with
  the Wi-Fi off). "Still connecting…" can take a few seconds over the
  internet; "Can't reach … from this network" means no path was found: join
  the same Wi-Fi as the machine.
- If the connection drops (switching from Wi-Fi to 4G, say), the player
  reconnects by itself, three times, before asking you to retry.
