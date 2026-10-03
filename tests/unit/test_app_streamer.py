"""The streamer's input queue and display attachment, with fakes.

app/streamer.py runs under the system Python (PyGObject, python-xlib), so
each case runs there as a script; skipped when that Python lacks them.
"""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREAMBLE = f"""
import json, sys
sys.path.insert(0, {str(ROOT)!r})
from app import streamer
"""


def _system_python_ok() -> bool:
    probe = (
        "import gi; gi.require_version('GstWebRTC', '1.0'); "
        "from gi.repository import GstWebRTC; import Xlib"
    )
    try:
        return (
            subprocess.run(
                ["/usr/bin/python3", "-c", probe], capture_output=True
            ).returncode
            == 0
        )
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _system_python_ok(),
    reason="needs PyGObject and python-xlib in /usr/bin/python3",
)


def _run(body: str):
    script = PREAMBLE + textwrap.dedent(body)
    result = subprocess.run(
        ["/usr/bin/python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


QUEUE_FAKES = """
from gi.repository import GLib
from Xlib import X

events = []
streamer.xtest.fake_input = lambda d, kind, detail=0, **kw: events.append([kind, detail])
streamer.log = lambda text: events.append(["log", text])

class Clock:
    now = 1000.0
    def __call__(self):
        return self.now
clock = Clock()

class FakeDisplay:
    \"\"\"A tiny X server keymap: natives a b c d e x Return Shift_L, then
    ``free`` keycodes with no symbol.\"\"\"
    def __init__(self, free=2):
        natives = [ord(c) for c in "abcdex"] + [0xFF0D, 0xFFE1]
        count = len(natives) + free
        info = type("info", (), {"min_keycode": 8, "max_keycode": 8 + count - 1})
        self.display = type("display", (), {"info": info})
        self.server = {8 + i: [ks, 0] for i, ks in enumerate(natives)}
        for kc in range(8 + len(natives), 8 + count):
            self.server[kc] = [0, 0]
        self.snapshot = {kc: list(v) for kc, v in self.server.items()}  # stale cache
        self.history = []
    def sync(self): pass
    def get_keyboard_mapping(self, first, count):
        return [list(self.server[kc]) for kc in range(first, first + count)]
    def change_keyboard_mapping(self, keycode, rows):
        self.server[keycode] = [int(rows[0][0]), int(rows[0][1])]
        self.history.append([keycode, list(self.server[keycode])])
    def keysym_to_keycodes(self, keysym):
        return [(kc, lvl) for kc, v in self.snapshot.items() for lvl, ks in enumerate(v) if ks == keysym]
    def keysym_to_keycode(self, keysym):
        hits = self.keysym_to_keycodes(keysym)
        return hits[0][0] if hits else 0

def make_injector(free=2, registry=""):
    inj = streamer.Injector.__new__(streamer.Injector)
    inj.disp = FakeDisplay(free)
    inj.display_name = ":0"
    inj.keys_down, inj.shift_held, inj.buttons_down = set(), set(), set()
    inj.typing, inj.pending = None, []
    inj.keys = streamer.KeyAllocator(inj.disp, registry, clock=clock)
    return inj

inj = make_injector()

def run_until_idle(seconds=3):
    loop = GLib.MainLoop()
    def check():
        if not inj.pending and inj.typing is None:
            loop.quit()
            return False
        return True
    GLib.timeout_add(10, check)
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()

def keycode_of(char):
    return inj.disp.keysym_to_keycode(ord(char))

def decode_late(disp, presses):
    \"\"\"What a reader that only now processes the queued presses decodes:
    the current keymap, at the level the press's Shift state selects.\"\"\"
    out, shift = [], False
    for kind, detail in presses:
        if detail == disp.keysym_to_keycode(0xFFE1) and detail:
            shift = kind == X.KeyPress
        elif kind == X.KeyPress:
            out.append(disp.server[detail][1 if shift else 0])
    return out
"""


def test_a_failing_character_or_message_does_not_strand_the_queue():
    """Typing 'a' fails and a move message is malformed: the rest of the
    queue (b, then x down and up) still goes through, in order."""
    out = _run(
        QUEUE_FAKES
        + """
real_type_char = inj.type_char
def flaky(char):
    if char == "a":
        raise OSError("cannot type")
    return real_type_char(char)
inj.type_char = flaky
inj.submit({"t": "text", "s": "ab"})
inj.submit({"t": "move"})  # no coordinates: an input error
inj.submit({"t": "key", "k": "x", "d": True})
inj.submit({"t": "key", "k": "x", "d": False})
run_until_idle()
b = keycode_of("b")
ret = keycode_of("x")
presses = [e for e in events if e[0] in (X.KeyPress, X.KeyRelease)]
print(json.dumps({"presses": presses, "pending": len(inj.pending),
                  "typing": inj.typing is not None,
                  "b": b, "ret": ret,
                  "logs": [e[1] for e in events if e[0] == "log"]}))
"""
    )
    assert out["pending"] == 0 and not out["typing"]
    assert out["presses"] == [
        [2, out["b"]],
        [3, out["b"]],
        [2, out["ret"]],
        [3, out["ret"]],
    ]
    assert any("cannot type" in line for line in out["logs"])
    assert any("input error" in line for line in out["logs"])


def test_keys_wait_for_the_text_before_them():
    out = _run(
        QUEUE_FAKES
        + """
inj.submit({"t": "text", "s": "ab"})
inj.submit({"t": "key", "k": "x", "d": True})
first = [e for e in events if e[0] == X.KeyPress]  # before the loop runs
run_until_idle()
order = [e[1] for e in events if e[0] == X.KeyPress]
print(json.dumps({"before_loop": first, "order": order,
                  "h": keycode_of("a"), "i": keycode_of("b"),
                  "ret": keycode_of("x")}))
"""
    )
    assert out["before_loop"] == []  # Return did not jump the queue
    assert out["order"] == [out["h"], out["i"], out["ret"]]


ATTACH = """
from argparse import Namespace
from contextlib import contextmanager

held = {"v": False}
trace = []
FAIL_FIRST_PAUSE = %s

@contextmanager
def fake_lock(path):
    held["v"] = True
    try:
        yield
    finally:
        held["v"] = False

class FakePipe:
    def __init__(self, audio):
        self.audio = audio
    def set_state(self, state):
        trace.append(["set_state", state.value_nick, held["v"]])
        if FAIL_FIRST_PAUSE and self.audio and state == streamer.Gst.State.PAUSED:
            return streamer.Gst.StateChangeReturn.FAILURE
        return streamer.Gst.StateChangeReturn.SUCCESS

class FakeInjector:
    def __init__(self, display, registry=""):
        trace.append(["injector", display, held["v"]])

def fake_build(self, encoder, audio):
    trace.append(["build", encoder, audio, held["v"]])
    self.pipe = FakePipe(audio)

def fake_discard(self):
    trace.append(["discard", held["v"]])

def sink_exists(name):
    trace.append(["sink", name, held["v"]])
    return True

streamer.state_lock = fake_lock
streamer.check_display_owner = lambda *a: trace.append(["check", held["v"]])
streamer.Injector = FakeInjector
streamer.choose_encoder = lambda codecs: ("vp8enc", "VP8")
streamer.missing_audio_elements = lambda: []
streamer.sink_exists = sink_exists
streamer.skipped_addresses = lambda: set()
streamer.Streamer._build = fake_build
streamer.Streamer._discard = fake_discard
streamer.send = lambda message: trace.append(
    ["send", message["type"], message.get("audio"), held["v"]])
streamer.Streamer(Namespace(display=":100", codecs="VP8", fps=60, bitrate=8000,
                            xvfb_pid=1, xvfb_start=2, state_lock="/tmp/lock",
                            keymap_registry="", audio_sink="merlin_app_x"))
print(json.dumps(trace))
"""


def test_attachment_happens_inside_the_state_lock():
    """Owner check, input connection, the sink check, pipeline and capture
    start (PAUSED) all run while the state lock is held; ready is sent after."""
    trace = _run(ATTACH % "False")
    assert trace == [
        ["check", True],
        ["injector", ":100", True],
        ["sink", "merlin_app_x", True],
        ["build", "vp8enc", True, True],
        ["set_state", "paused", True],
        ["send", "ready", True, False],
    ]


def test_sound_that_cannot_start_leaves_a_silent_stream():
    trace = _run(ATTACH % "True")
    assert trace[-5:] == [
        ["set_state", "paused", True],
        ["discard", True],
        ["build", "vp8enc", False, True],
        ["set_state", "paused", True],
        ["send", "ready", False, False],
    ]


def test_distinct_characters_keep_their_meaning_for_a_late_reader():
    """Four characters the keymap lacks, two free keycodes (two levels each):
    a reader that reads everything only afterwards decodes all four."""
    out = _run(
        QUEUE_FAKES
        + """
inj.submit({"t": "text", "s": "éñüø"})
run_until_idle()
presses = [e for e in events if e[0] in (X.KeyPress, X.KeyRelease)]
print(json.dumps({"decoded": decode_late(inj.disp, presses),
                  "remaps": len(inj.disp.history)}))
"""
    )
    assert out["decoded"] == [ord(c) for c in "éñüø"]
    assert out["remaps"] == 4  # each slot filled once, nothing reassigned


def test_when_every_slot_is_busy_typing_waits_then_reuses_the_oldest():
    out = _run(
        QUEUE_FAKES
        + """
for char in "éñüø":
    assert inj.type_char(char)
before = len(inj.disp.history)
waited = inj.type_char("ß")          # all four slots used just now
still = len(inj.disp.history)
clock.now += streamer.QUIESCENCE_S + 0.1
reused = inj.type_char("ß")
print(json.dumps({"waited": waited, "remapped_while_busy": still - before,
                  "reused": reused, "logs": [e[1] for e in events if e[0] == "log"],
                  "last": inj.disp.history[-1]}))
"""
    )
    assert out["waited"] is False
    assert out["remapped_while_busy"] == 0
    assert out["reused"] is True
    assert any("reusing keycode" in line for line in out["logs"])
    assert out["last"][1][0] == ord("ß")


def test_a_new_streamer_adopts_the_slots_and_keeps_typing(tmp_path):
    registry = tmp_path / "keymap.json"
    out = _run(
        QUEUE_FAKES
        + f"""
first = make_injector(registry={str(registry)!r})
for char in "éñüø":
    assert first.type_char(char)
server = first.disp.server
second = streamer.Injector.__new__(streamer.Injector)
second.disp = FakeDisplay()
second.disp.server = server               # same Xvfb, mappings kept
second.display_name = ":0"
second.keys_down, second.shift_held, second.buttons_down = set(), set(), set()
second.typing, second.pending = None, []
second.keys = streamer.KeyAllocator(second.disp, {str(registry)!r}, clock=clock)
adopted = {{k: v for k, v in second.keys.where.items()}}
known = second.type_char("é")              # an adopted slot: no remap
remaps_after_known = len(second.disp.history)
clock.now += streamer.QUIESCENCE_S + 0.1
new = second.type_char("ß")                # pool full: reused, not dropped
print(json.dumps({{"adopted": len(adopted), "known": known,
                  "remaps_after_known": remaps_after_known, "new": new,
                  "remaps": len(second.disp.history)}}))
"""
    )
    assert out["adopted"] == 4
    assert out["known"] is True and out["remaps_after_known"] == 0
    assert out["new"] is True and out["remaps"] == 1


def test_a_registry_that_no_longer_matches_is_not_adopted(tmp_path):
    registry = tmp_path / "keymap.json"
    out = _run(
        QUEUE_FAKES
        + f"""
first = make_injector(registry={str(registry)!r})
assert first.type_char("é")
kc = first.keys.where[ord("é")][0]
first.disp.server[kc] = [ord("q"), 0]      # someone else remapped it since
second = streamer.KeyAllocator(first.disp, {str(registry)!r}, clock=clock)
print(json.dumps({{"adopted_e": ord("é") in second.where, "owns_kc": kc in second.slots}}))
"""
    )
    assert out == {"adopted_e": False, "owns_kc": False}


def test_no_free_keycode_is_reported_and_the_queue_goes_on():
    out = _run(
        QUEUE_FAKES
        + """
inj = make_injector(free=0)
inj.submit({"t": "text", "s": "é"})
inj.submit({"t": "key", "k": "x", "d": True})
inj.submit({"t": "key", "k": "x", "d": False})
run_until_idle()
x = inj.disp.keysym_to_keycode(ord("x"))
presses = [e for e in events if e[0] in (X.KeyPress, X.KeyRelease)]
print(json.dumps({"presses": presses, "x": x,
                  "logs": [e[1] for e in events if e[0] == "log"]}))
"""
    )
    assert out["presses"] == [[2, out["x"]], [3, out["x"]]]
    assert any("no keycode is free" in line for line in out["logs"])


def test_a_vacant_shift_level_repeats_the_symbol():
    """[É, NoSymbol] would make XKB type é unshifted: the row is [É, É] until
    the Shift level is used, and filling it keeps the plain symbol."""
    out = _run(
        QUEUE_FAKES
        + """
assert inj.type_char("É")
kc = inj.keys.where[0xC9][0]
first_row = list(inj.disp.server[kc])
assert inj.type_char("ñ")
same_kc = inj.keys.where[0xF1][0] == kc
second_row = list(inj.disp.server[kc])
print(json.dumps({"first": first_row, "second": second_row, "same": same_kc,
                  "logical": inj.keys.slots[kc]}))
"""
    )
    assert out["first"] == [0xC9, 0xC9]
    assert out["same"] is True
    assert out["second"] == [0xC9, 0xF1]
    assert out["logical"] == [0xC9, 0xF1]


def test_after_reconnect_and_reuse_a_stale_cache_never_types_the_old_character(
    tmp_path,
):
    """A new connection's cached keymap lists é on a keycode that is later
    reused for ß: typing é again must not press that keycode."""
    registry = tmp_path / "keymap.json"
    out = _run(
        QUEUE_FAKES
        + f"""
first = make_injector(registry={str(registry)!r})
for char in "éñüø":
    assert first.type_char(char)
second = streamer.Injector.__new__(streamer.Injector)
second.disp = FakeDisplay()
second.disp.server = first.disp.server
second.disp.snapshot = {{kc: list(v) for kc, v in first.disp.server.items()}}  # real reconnect
second.display_name = ":0"
second.keys_down, second.shift_held, second.buttons_down = set(), set(), set()
second.typing, second.pending = None, []
second.keys = streamer.KeyAllocator(second.disp, {str(registry)!r}, clock=clock)
clock.now += streamer.QUIESCENCE_S + 0.1
assert second.type_char("ß")              # reuses the oldest keycode (é and ñ)
events.clear()
typed = []
for _ in range(100):
    if second.type_char("é"):
        break
    clock.now += 0.5
presses = [e for e in events if e[0] in (X.KeyPress, X.KeyRelease)]
print(json.dumps({{"decoded": decode_late(second.disp, presses)}}))
"""
    )
    assert out["decoded"] == [0xE9]


DROP = """
from argparse import Namespace
from contextlib import contextmanager

streamer.Gst.init(None)  # _start builds a Gst.Structure

held = {"v": False}
trace = []
FAIL_PLAYING_WITH_AUDIO = %s

@contextmanager
def fake_lock(path):
    held["v"] = True
    try:
        yield
    finally:
        held["v"] = False

class FakeWebrtc:
    def emit(self, *args):
        return None

class FakePipe:
    def __init__(self, audio):
        self.audio = audio
    def set_state(self, state):
        trace.append(["set_state", state.value_nick, self.audio, held["v"]])
        if FAIL_PLAYING_WITH_AUDIO and self.audio and state == streamer.Gst.State.PLAYING:
            return streamer.Gst.StateChangeReturn.FAILURE
        return streamer.Gst.StateChangeReturn.SUCCESS

class FakeInjector:
    def __init__(self, display, registry=""):
        pass
    def release_all(self):
        pass

def fake_build(self, encoder, audio):
    trace.append(["build", audio, held["v"]])
    self.pipe = FakePipe(audio)
    self.webrtc = FakeWebrtc()
    self.audio_elements = streamer.AUDIO_NAMES if audio else frozenset()

class Src:
    def __init__(self, name):
        self.name = name
    def get_name(self):
        return self.name

class Message:
    def __init__(self, name):
        self.src = Src(name)
    def parse_error(self):
        return streamer.GLib.Error("boom"), "debug"

streamer.state_lock = fake_lock
streamer.check_display_owner = lambda *a: trace.append(["check", held["v"]])
streamer.Injector = FakeInjector
streamer.choose_encoder = lambda codecs: ("vp8enc", "VP8")
streamer.missing_audio_elements = lambda: []
streamer.sink_exists = lambda name: True
streamer.skipped_addresses = lambda: set()
streamer.Streamer._build = fake_build
streamer.Streamer._discard = lambda self: trace.append(["discard"])
streamer.Streamer.stop = lambda self: trace.append(["stop"])
streamer.GLib.idle_add = lambda fn, *args: fn(*args)
streamer.GLib.timeout_add_seconds = lambda *a: trace.append(["watchdog"])
streamer.send = lambda message: trace.append(
    ["send", message["type"], message.get("audio")])
s = streamer.Streamer(Namespace(display=":100", codecs="VP8", fps=60, bitrate=8000,
                                xvfb_pid=1, xvfb_start=2, state_lock="/tmp/lock",
                                keymap_registry="", audio_sink="merlin_app_x"))
%s
print(json.dumps(trace))
"""


def test_sound_that_does_not_play_restarts_the_picture_alone():
    script = """
s.loop = type("L", (), {"run": lambda self: trace.append(["loop"])})()
s.read_stdin = lambda: None
s.run()
"""
    trace = _run(DROP % ("True", script))
    after_ready = trace[trace.index(["send", "ready", True]) + 1 :]
    assert after_ready == [
        ["set_state", "playing", True, False],
        ["discard"],
        ["check", True],  # the display's owner, checked again
        ["build", False, True],  # rebuilt under the state lock
        ["set_state", "paused", False, True],
        ["send", "ready", False],
        ["set_state", "playing", False, False],
        ["loop"],
        ["set_state", "null", False, False],
    ]


def test_a_sound_error_before_the_offer_drops_the_sound():
    script = """
s.on_bus_error(None, Message("audiosrc"))
"""
    trace = _run(DROP % ("False", script))
    tail = trace[trace.index(["send", "ready", True]) + 1 :]
    assert ["discard"] in tail
    assert tail[-2:] == [
        ["send", "ready", False],
        ["set_state", "playing", False, False],
    ]


def test_after_the_offer_a_sound_error_only_silences():
    script = """
s.negotiated = True
s.on_bus_error(None, Message("audiosrc"))
trace.append(["video error"])
s.on_bus_error(None, Message("enc"))
"""
    trace = _run(DROP % ("False", script))
    tail = trace[trace.index(["send", "ready", True]) + 1 :]
    assert tail == [["video error"], ["send", "error", None], ["stop"]]


def test_a_dropped_pipeline_never_speaks_to_the_browser():
    script = """
old = s.webrtc
s.webrtc = FakeWebrtc()
s.on_ice_candidate(old, 0, "candidate:1 1 UDP 1 192.168.1.2 5000 typ host")
s.on_negotiation_needed(old)
s.on_offer_created(None, old)
trace.append(["negotiated", s.negotiated, s.offered])
"""
    trace = _run(DROP % ("False", script))
    assert trace[-1] == ["negotiated", False, False]
    assert not [t for t in trace if t[:2] in (["send", "ice"], ["send", "offer"])]


OFFERS = """
import threading, time

class Reply:
    def get_value(self, key):
        sdp = type("S", (), {"as_text": lambda self: "v=0"})()
        return type("O", (), {"sdp": sdp})()

class Promise:
    def __init__(self, gate=None):
        self.gate = gate
    def wait(self):
        if self.gate is not None:
            self.gate.wait()
    def get_reply(self):
        return Reply()
"""


def test_an_offer_finishing_during_the_drop_is_never_sent():
    """The old webrtcbin's offer passed its first check, then completes after
    the sound was dropped: it must not reach the browser."""
    script = (
        OFFERS
        + """
gate = threading.Event()
old = s.webrtc
t = threading.Thread(target=s.on_offer_created, args=(Promise(gate), old))
t.start()
time.sleep(0.1)  # waiting for its reply, past the early check
s.on_bus_error(None, Message("audiosrc"))  # the sound fails: drop
gate.set()
t.join()
"""
    )
    trace = _run(DROP % ("False", script))
    assert ["discard"] in trace
    assert ["send", "ready", False] in trace
    assert not [t for t in trace if t[:2] == ["send", "offer"]]


def test_an_offer_already_out_keeps_the_sound():
    script = (
        OFFERS
        + """
s.on_offer_created(Promise(), s.webrtc)
s.on_bus_error(None, Message("audiosrc"))
"""
    )
    trace = _run(DROP % ("False", script))
    assert ["send", "offer", None] in trace
    assert ["discard"] not in trace
