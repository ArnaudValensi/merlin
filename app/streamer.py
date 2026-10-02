#!/usr/bin/python3
"""WebRTC streamer for one viewer of one app display.

Runs under the **system** Python (PyGObject + GStreamer + python-xlib), not
Merlin's venv. The server starts one per viewer connection and talks to it in
JSON lines:

- stdin  (server -> streamer): ``answer`` ``{"sdp"}``, ``ice``
  ``{"candidate", "sdpMLineIndex"}``, ``stop``.
- stdout (streamer -> server): ``ready`` ``{"encoder", "codec"}``, ``offer``
  ``{"sdp"}``, ``ice``, ``state`` ``{"ice"}``, ``error`` ``{"message"}``.

The pipeline captures the X display (``ximagesrc``), encodes it (NVENC H.264,
OpenH264 or VP8, in that order of preference among the codecs the browser
supports) and sends it through ``webrtcbin`` with no STUN or TURN: host
candidates only, the local network. Input arrives on the ``input`` data
channel and is injected with XTEST. Closing stdin stops everything and
releases any key or button still held.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager, redirect_stdout

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")

from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402
from Xlib import XK, X  # noqa: E402
from Xlib import display as xdisplay  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

# Interfaces whose candidates can never reach a phone on the LAN.
SKIPPED_INTERFACES = ("docker", "br-", "veth", "virbr")

_out_lock = threading.Lock()


def send(message: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def log(text: str) -> None:
    sys.stderr.write(f"streamer: {text}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def _works(description: str) -> bool:
    """Run a one-frame test pipeline: NVENC can exist and still fail to open."""
    try:
        pipe = Gst.parse_launch(description)
    except GLib.Error:
        return False
    pipe.set_state(Gst.State.PLAYING)
    bus = pipe.get_bus()
    msg = bus.timed_pop_filtered(
        5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    pipe.set_state(Gst.State.NULL)
    return msg is not None and msg.type == Gst.MessageType.EOS


def encoder_branch(name: str, kbps: int, fps: int) -> str:
    gop = fps * 2
    if name == "nvh264enc":
        return (
            "cudaupload ! nvh264enc name=enc preset=p1 tune=ultra-low-latency "
            f"zerolatency=true rc-mode=cbr bitrate={kbps} max-bitrate={kbps} "
            f"gop-size={gop} bframes=0 ! "
            "video/x-h264,profile=constrained-baseline,stream-format=byte-stream ! "
            "h264parse config-interval=-1 ! "
            "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        )
    if name == "openh264enc":
        return (
            "videoconvert ! video/x-raw,format=I420 ! "
            f"openh264enc name=enc complexity=low rate-control=bitrate bitrate={kbps * 1000} "
            f"gop-size={gop} ! "
            "video/x-h264,profile=constrained-baseline,stream-format=byte-stream ! "
            "h264parse config-interval=-1 ! "
            "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        )
    return (
        "videoconvert ! video/x-raw,format=I420 ! "
        f"vp8enc name=enc deadline=1 cpu-used=8 end-usage=cbr target-bitrate={kbps * 1000} "
        f"keyframe-max-dist={gop} threads=4 error-resilient=partitions ! "
        "rtpvp8pay pt=96 ! "
        "application/x-rtp,media=video,encoding-name=VP8,payload=96,clock-rate=90000"
    )


def choose_encoder(codecs: list[str]) -> tuple[str, str]:
    """(element, codec) for the best encoder the browser can decode."""
    wanted = {c.upper() for c in codecs}
    if "H264" in wanted:
        if Gst.ElementFactory.find("nvh264enc") and _works(
            "videotestsrc num-buffers=1 ! video/x-raw,width=320,height=240 ! "
            "cudaupload ! nvh264enc ! fakesink"
        ):
            return "nvh264enc", "H264"
        if Gst.ElementFactory.find("openh264enc"):
            return "openh264enc", "H264"
    if "VP8" in wanted and Gst.ElementFactory.find("vp8enc"):
        return "vp8enc", "VP8"
    raise RuntimeError(f"no encoder for the browser's codecs: {sorted(wanted)}")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


TYPE_DELAY_MS = 12  # between typed characters, like `xdotool type --delay 12`


def _printable(keysym: int) -> bool:
    """Latin-1 printable characters: the keysyms whose Shift level matters."""
    return 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF


QUIESCENCE_S = 2.0  # a full slot is reused only after this long unused


class KeyAllocator:
    """Keycodes for the characters the display's keymap lacks (é on a US map).

    X key events carry keycodes, and an app decodes them with the keymap as it
    is when it reads them: a keycode remapped before the app has read an event
    for it changes what that event means. So:

    - each character gets a slot of its own and keeps it: a free keycode
      holds two (plain and Shift level), and filling one level never changes
      the other;
    - when every slot is taken, the least recently used keycode is reassigned
      only once it has gone QUIESCENCE_S unused; until then typing waits
      (X cannot tell when an app has read an event, so this is the guarantee:
      an app more than QUIESCENCE_S behind on its input while more distinct
      characters than slots are typed can still misread one);
    - ownership outlives the streamer: the slots are recorded in a registry
      file per Xvfb identity, and a later streamer adopts those whose keymap
      entry still matches, so reconnecting neither loses nor drops slots.
    """

    def __init__(self, disp, registry: str = "", clock=None) -> None:
        import time as _time

        self.disp = disp
        self.registry = registry
        self.clock = clock or _time.monotonic
        self.slots: dict[int, list[int]] = {}  # keycode -> [plain, shift] keysyms
        self.used: dict[int, float] = {}  # keycode -> last use
        self.where: dict[int, tuple[int, int]] = {}  # keysym -> (keycode, level)
        first = disp.display.info.min_keycode
        last = disp.display.info.max_keycode
        current = disp.get_keyboard_mapping(first, last - first + 1)
        now = self.clock()
        for keycode, syms in self._load().items():
            index = keycode - first
            if 0 <= index < len(current) and self._levels(current[index]) == syms:
                self._own(keycode, syms, now)  # still ours: adopt it
        for index, syms in enumerate(current):
            if not any(syms) and first + index not in self.slots:
                self._own(first + index, [0, 0], now)

    @staticmethod
    def _levels(syms) -> list[int]:
        levels = list(syms[:2]) + [0, 0]
        return [int(levels[0]), int(levels[1])]

    def _own(self, keycode: int, syms: list[int], now: float) -> None:
        self.slots[keycode] = list(syms)
        self.used[keycode] = now
        for level, keysym in enumerate(syms):
            if keysym:
                self.where[keysym] = (keycode, level)

    def find(self, keysym: int) -> tuple[int, int] | None:
        slot = self.where.get(keysym)
        if slot:
            self.used[slot[0]] = self.clock()
        return slot

    @property
    def capacity(self) -> int:
        return 2 * len(self.slots)

    def assign(self, keysym: int) -> tuple[int, int] | None:
        """A slot for ``keysym``; None while every slot is busy (wait)."""
        for keycode, syms in self.slots.items():
            for level in (0, 1):
                if not syms[level]:
                    syms[level] = keysym
                    return self._commit(keycode, level, keysym)
        if not self.slots:
            raise RuntimeError("no keycode is free for characters the keymap lacks")
        keycode = min(self.used, key=lambda kc: self.used[kc])
        if self.clock() - self.used[keycode] < QUIESCENCE_S:
            return None
        for old in self.slots[keycode]:
            self.where.pop(old, None)
        log(f"reusing keycode {keycode} after {QUIESCENCE_S:g} s unused")
        self.slots[keycode] = [keysym, 0]
        return self._commit(keycode, 0, keysym)

    def _commit(self, keycode: int, level: int, keysym: int) -> tuple[int, int]:
        self.disp.change_keyboard_mapping(keycode, [tuple(self.slots[keycode])])
        self.disp.sync()
        self.where[keysym] = (keycode, level)
        self.used[keycode] = self.clock()
        self._save()
        return keycode, level

    def _load(self) -> dict[int, list[int]]:
        if not self.registry:
            return {}
        try:
            with open(self.registry) as handle:
                data = json.load(handle)
            return {int(k): [int(v[0]), int(v[1])] for k, v in data["slots"].items()}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return {}

    def _save(self) -> None:
        if not self.registry:
            return
        tmp = f"{self.registry}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.registry), exist_ok=True)
            with open(tmp, "w") as handle:
                json.dump({"slots": {str(k): v for k, v in self.slots.items()}}, handle)
            os.replace(tmp, self.registry)
        except OSError as exc:
            log(f"cannot save the keymap registry: {exc!r}")


class Injector:
    """XTEST input on the app's display. Main-loop thread only."""

    def __init__(self, display_name: str, keymap_registry: str = "") -> None:
        self.display_name = display_name
        # python-xlib prints its xauthority warning on stdout, which is the
        # JSON protocol to the server: send it to stderr instead.
        with redirect_stdout(sys.stderr):
            self.disp = xdisplay.Display(display_name)
        self.keys_down: set[int] = set()
        self.shift_held: set[int] = set()  # Shift keycodes the user holds
        self.buttons_down: set[int] = set()
        self.typing: list[str] | None = None  # characters left of the text being typed
        self.pending: list[dict] = []
        self.keys = KeyAllocator(self.disp, keymap_registry)

    # Input is applied strictly in arrival order. Text is typed here, on this
    # connection (checked and opened under the state lock, so always the
    # app's own display), one character per main-loop tick: never blocking,
    # cancelled by stop, and everything after it waits for it, so a paste
    # followed by Enter submits the whole paste.
    def submit(self, msg: dict) -> None:
        self.pending.append(msg)
        self._drain()

    def _drain(self) -> None:
        while self.pending and self.typing is None:
            msg = self.pending.pop(0)
            try:
                if msg.get("t") == "text":
                    self._start_typing(str(msg.get("s", "")))
                else:
                    self._apply(msg)
            except Exception as exc:  # a bad message must not stall the rest
                self.typing = None
                log(f"input error: {exc!r}")

    def _start_typing(self, text: str) -> None:
        if not text:
            return
        self.typing = list(text)
        GLib.timeout_add(TYPE_DELAY_MS, self._type_next)

    def _type_next(self) -> bool:
        if self.typing is None:
            return False  # stopped
        if self.typing:
            char = self.typing.pop(0)
            try:
                if not self.type_char(char):
                    self.typing.insert(0, char)  # every slot busy: retry next tick
            except Exception as exc:  # skip the character, keep typing
                log(f"cannot type {char!r}: {exc!r}")
            return True
        self.typing = None
        self._drain()
        return False

    def stop_typing(self) -> None:
        self.pending.clear()
        self.typing = None

    def type_char(self, char: str) -> bool:
        """Press and release the key for one character, Shift as needed.

        A character the keymap lacks (é on a US map, an emoji) gets a slot
        from the KeyAllocator. Returns False when it must wait for one.
        """
        special = {"\n": XK.XK_Return, "\r": XK.XK_Return, "\t": XK.XK_Tab}
        if not char:
            return True
        if char in special:
            keysym = special[char]
        elif 0x20 <= ord(char) <= 0x7E or 0xA0 <= ord(char) <= 0xFF:
            keysym = ord(char)  # Latin-1 keysyms are their code points
        else:
            keysym = 0x01000000 | ord(char)  # Unicode keysym
        slot = self.keys.find(keysym)
        if slot is None:
            entries = sorted(self.disp.keysym_to_keycodes(keysym), key=lambda e: e[1])
            entries = [e for e in entries if e[1] <= 1]
            slot = entries[0] if entries else self.keys.assign(keysym)
            if slot is None:
                return False
        keycode, level = slot
        self._press_with_shift(keycode, level == 1)
        self._fake(keycode, False)
        self.disp.sync()
        return True

    def _apply(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "key":
            self.key(str(msg.get("k", "")), bool(msg.get("d")))
        elif kind == "move":
            xtest.fake_input(
                self.disp, X.MotionNotify, x=int(msg["x"]), y=int(msg["y"])
            )
        elif kind == "rel":
            xtest.fake_input(
                self.disp,
                X.MotionNotify,
                detail=1,
                x=int(msg.get("dx", 0)),
                y=int(msg.get("dy", 0)),
            )
        elif kind == "btn":
            self.button(int(msg.get("b", 1)), bool(msg.get("d")))
        elif kind == "wheel":
            dy = int(msg.get("dy", 0))
            button = 4 if dy < 0 else 5
            for _ in range(min(abs(dy), 20)):
                xtest.fake_input(self.disp, X.ButtonPress, button)
                xtest.fake_input(self.disp, X.ButtonRelease, button)
        self.disp.sync()

    def key(self, name: str, down: bool) -> None:
        """Press or release the key that produces keysym ``name``.

        The browser sends the character the user's layout produced, which may
        sit at another Shift level on the display's keymap (a French ``&`` is
        Shift+7 on the US map, a French Shift+1 is an unshifted ``1``). So the
        key is pressed with Shift added or lifted around the press, and the
        KeyPress carries the right modifiers whatever the user holds.
        """
        keysym = XK.string_to_keysym(name)
        if not keysym:
            return
        if keysym in (XK.XK_Shift_L, XK.XK_Shift_R):
            keycode = self.disp.keysym_to_keycode(keysym)
            if keycode:
                self._fake(keycode, down)
                (self.shift_held.add if down else self.shift_held.discard)(keycode)
            return
        entries = sorted(self.disp.keysym_to_keycodes(keysym), key=lambda e: e[1])
        if not entries:
            return
        keycode, index = entries[0]
        if not down:
            if keycode in self.keys_down:
                self._fake(keycode, False)
            return
        if not _printable(keysym):
            # Arrows, Tab, F-keys...: the user's modifiers are the point
            # (Shift+Right selects, Shift+Tab goes back). Press as is.
            self._fake(keycode, True)
            return
        if index > 1:
            # Only reachable through AltGr levels: type it as text instead, in
            # its place (the queue takes it next, before later input).
            self.pending.insert(
                0, {"t": "text", "s": XK.keysym_to_string(keysym) or ""}
            )
            return
        self._press_with_shift(keycode, index == 1)

    def _press_with_shift(self, keycode: int, needs_shift: bool) -> None:
        """Press ``keycode`` with Shift added or lifted around the press."""
        if needs_shift == bool(self.shift_held):
            self._fake(keycode, True)
        elif needs_shift:
            shift = self.disp.keysym_to_keycode(XK.XK_Shift_L)
            xtest.fake_input(self.disp, X.KeyPress, shift)
            self._fake(keycode, True)
            xtest.fake_input(self.disp, X.KeyRelease, shift)
        else:
            for held in self.shift_held:
                xtest.fake_input(self.disp, X.KeyRelease, held)
            self._fake(keycode, True)
            for held in self.shift_held:
                xtest.fake_input(self.disp, X.KeyPress, held)

    def _fake(self, keycode: int, down: bool) -> None:
        xtest.fake_input(self.disp, X.KeyPress if down else X.KeyRelease, keycode)
        (self.keys_down.add if down else self.keys_down.discard)(keycode)

    def button(self, button: int, down: bool) -> None:
        if button not in (1, 2, 3, 8, 9):  # 4-7 are the wheel, see "wheel"
            return
        xtest.fake_input(self.disp, X.ButtonPress if down else X.ButtonRelease, button)
        (self.buttons_down.add if down else self.buttons_down.discard)(button)

    def release_all(self) -> None:
        self.stop_typing()
        for keycode in list(self.keys_down | self.shift_held):
            xtest.fake_input(self.disp, X.KeyRelease, keycode)
        self.shift_held.clear()
        for button in list(self.buttons_down):
            xtest.fake_input(self.disp, X.ButtonRelease, button)
        self.keys_down.clear()
        self.buttons_down.clear()
        self.disp.sync()


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def skipped_addresses() -> set[str]:
    """Addresses of docker/bridge interfaces, unreachable from a phone."""
    try:
        out = subprocess.run(
            ["ip", "-j", "addr"], capture_output=True, text=True, timeout=5
        ).stdout
        interfaces = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    skipped = set()
    for iface in interfaces:
        if str(iface.get("ifname", "")).startswith(SKIPPED_INTERFACES):
            for addr in iface.get("addr_info", []):
                skipped.add(addr.get("local", ""))
    return skipped


# ---------------------------------------------------------------------------
# Streamer
# ---------------------------------------------------------------------------


def check_display_owner(display: str, pid: int, start: int) -> None:
    """The display must still be served by the app's own Xvfb (PID and start
    time): between the server's check and now, the app may have been stopped
    and its display number taken by another app."""
    try:
        owner = int(open(f"/tmp/.X{display.lstrip(':')}-lock").read().strip())
        stat = open(f"/proc/{owner}/stat").read()
        owner_start = int(stat[stat.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        raise RuntimeError(f"display {display} is gone")
    if owner != pid or owner_start != start:
        raise RuntimeError(f"display {display} now belongs to another X server")


@contextmanager
def state_lock(path: str):
    """Hold Merlin's apps state lock (the one launch and stop take)."""
    if not path:
        yield
        return
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Streamer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.loop = GLib.MainLoop()
        self.skipped = skipped_addresses()
        self.offered = False
        self.channel = None
        encoder, codec = choose_encoder(args.codecs.split(","))
        # Check the display and connect to it (input, then capture) under the
        # lock that stop and launch take: the display cannot be torn down and
        # given to another app between the check and the connections.
        with state_lock(args.state_lock):
            if args.xvfb_pid:
                check_display_owner(args.display, args.xvfb_pid, args.xvfb_start)
            self.injector = Injector(args.display, args.keymap_registry)
            self._build(encoder, codec)
            # READY -> PAUSED starts ximagesrc, which opens its X connection.
            self.pipe.set_state(Gst.State.PAUSED)
        send({"type": "ready", "encoder": encoder, "codec": codec})

    def _build(self, encoder: str, codec: str) -> None:
        args = self.args
        description = (
            f"ximagesrc display-name={args.display} use-damage=false show-pointer=true ! "
            f"video/x-raw,framerate={args.fps}/1 ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            f"{encoder_branch(encoder, args.bitrate, args.fps)} ! "
            "webrtcbin name=webrtc bundle-policy=max-bundle latency=0"
        )
        log(f"pipeline: {description}")
        self.pipe = Gst.parse_launch(description)
        self.webrtc = self.pipe.get_by_name("webrtc")
        transceiver = self.webrtc.emit("get-transceiver", 0)
        transceiver.set_property(
            "direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY
        )
        self.webrtc.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self.on_ice_candidate)
        self.webrtc.connect(
            "notify::ice-connection-state", self.on_ice_connection_state
        )
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_bus_error)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        self.pipe.set_state(Gst.State.PLAYING)
        self.channel = self.webrtc.emit(
            "create-data-channel",
            "input",
            Gst.Structure.new_from_string("application/data-channel,ordered=true"),
        )
        if self.channel is not None:
            self.channel.connect("on-message-string", self.on_channel_message)
        threading.Thread(target=self.read_stdin, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.stop)
        try:
            self.loop.run()
        finally:
            self.injector.release_all()
            self.pipe.set_state(Gst.State.NULL)

    def stop(self) -> bool:
        self.loop.quit()
        return False

    def read_stdin(self) -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            GLib.idle_add(self.on_server_message, msg)
        GLib.idle_add(self.stop)

    # -- signaling ---------------------------------------------------------

    def on_negotiation_needed(self, element) -> None:
        if self.offered:
            return
        self.offered = True
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element)
        element.emit("create-offer", None, promise)

    def on_offer_created(self, promise, element) -> None:
        promise.wait()
        # Keep the reply referenced: the offer lives inside it, and letting
        # the binding drop it first frees the SDP under webrtcbin (segfault).
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        element.emit("set-local-description", offer, Gst.Promise.new())
        send({"type": "offer", "sdp": offer.sdp.as_text()})
        del reply

    def on_ice_candidate(self, _element, mline: int, candidate: str) -> None:
        parts = candidate.split()
        if len(parts) > 4 and parts[4] in self.skipped:
            return
        send({"type": "ice", "candidate": candidate, "sdpMLineIndex": mline})

    def on_ice_connection_state(self, element, _pspec) -> None:
        state = element.get_property("ice-connection-state")
        send({"type": "state", "ice": state.value_nick})

    def on_server_message(self, msg: dict) -> bool:
        kind = msg.get("type")
        if kind == "answer":
            _res, sdp = GstSdp.SDPMessage.new_from_text(msg["sdp"])
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, sdp
            )
            self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        elif kind == "ice" and msg.get("candidate"):
            self.webrtc.emit(
                "add-ice-candidate",
                int(msg.get("sdpMLineIndex") or 0),
                msg["candidate"],
            )
        elif kind == "stop":
            self.stop()
        return False

    # -- input -------------------------------------------------------------

    def on_channel_message(self, _channel, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        GLib.idle_add(self.inject, msg)

    def inject(self, msg: dict) -> bool:
        self.injector.submit(msg)
        return False

    def on_bus_error(self, _bus, message) -> None:
        error, debug = message.parse_error()
        log(f"pipeline error: {error.message} ({debug})")
        send({"type": "error", "message": error.message})
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--display", required=True, help="X display, e.g. :100")
    parser.add_argument("--codecs", default="H264,VP8", help="browser codecs")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", type=int, default=8000, help="kbit/s")
    parser.add_argument("--xvfb-pid", type=int, default=0, help="expected X server")
    parser.add_argument("--xvfb-start", type=int, default=0, help="its start time")
    parser.add_argument("--state-lock", default="", help="Merlin's apps state lock")
    parser.add_argument(
        "--keymap-registry", default="", help="keycode slots owned on this display"
    )
    args = parser.parse_args()
    Gst.init(None)
    try:
        streamer = Streamer(args)
    except Exception as exc:
        log(f"start failed: {exc!r}")
        send({"type": "error", "message": str(exc)})
        return 1
    streamer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
