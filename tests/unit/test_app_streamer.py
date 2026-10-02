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

class FakeDisplay:
    def sync(self): pass
    def keysym_to_keycodes(self, keysym):
        return [(keysym % 200 + 8, 0)]
    def keysym_to_keycode(self, keysym):
        return keysym % 200 + 8
    def change_keyboard_mapping(self, *args): pass

inj = streamer.Injector.__new__(streamer.Injector)
inj.disp = FakeDisplay()
inj.display_name = ":0"
inj.keys_down, inj.shift_held, inj.buttons_down = set(), set(), set()
inj.typing, inj.pending, inj.spare_keycode = None, [], 0

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
"""


def test_a_failing_character_or_message_does_not_strand_the_queue():
    """Typing 'a' fails and a move message is malformed: the rest of the
    queue (b, then Return down and up) still goes through, in order."""
    out = _run(
        QUEUE_FAKES
        + """
real_type_char = inj.type_char
def flaky(char):
    if char == "a":
        raise OSError("cannot type")
    real_type_char(char)
inj.type_char = flaky
inj.submit({"t": "text", "s": "ab"})
inj.submit({"t": "move"})  # no coordinates: an input error
inj.submit({"t": "key", "k": "Return", "d": True})
inj.submit({"t": "key", "k": "Return", "d": False})
run_until_idle()
b = ord("b") % 200 + 8
ret = streamer.XK.XK_Return % 200 + 8
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
inj.submit({"t": "text", "s": "hi"})
inj.submit({"t": "key", "k": "Return", "d": True})
first = [e for e in events if e[0] == X.KeyPress]  # before the loop runs
run_until_idle()
order = [e[1] for e in events if e[0] == X.KeyPress]
print(json.dumps({"before_loop": first, "order": order,
                  "h": ord("h") % 200 + 8, "i": ord("i") % 200 + 8,
                  "ret": streamer.XK.XK_Return % 200 + 8}))
"""
    )
    assert out["before_loop"] == []  # Return did not jump the queue
    assert out["order"] == [out["h"], out["i"], out["ret"]]


def test_attachment_happens_inside_the_state_lock():
    """Owner check, input connection, pipeline and capture start (PAUSED)
    all run while the state lock is held; ready is sent after."""
    trace = _run(
        """
from argparse import Namespace
from contextlib import contextmanager

held = {"v": False}
trace = []

@contextmanager
def fake_lock(path):
    held["v"] = True
    try:
        yield
    finally:
        held["v"] = False

class FakePipe:
    def set_state(self, state):
        trace.append(["set_state", state.value_nick, held["v"]])

class FakeInjector:
    def __init__(self, display):
        trace.append(["injector", display, held["v"]])

def fake_build(self, encoder, codec):
    trace.append(["build", encoder, held["v"]])
    self.pipe = FakePipe()

streamer.state_lock = fake_lock
streamer.check_display_owner = lambda *a: trace.append(["check", held["v"]])
streamer.Injector = FakeInjector
streamer.choose_encoder = lambda codecs: ("vp8enc", "VP8")
streamer.skipped_addresses = lambda: set()
streamer.Streamer._build = fake_build
streamer.send = lambda message: trace.append(["send", message["type"], held["v"]])
streamer.Streamer(Namespace(display=":100", codecs="VP8", fps=60, bitrate=8000,
                            xvfb_pid=1, xvfb_start=2, state_lock="/tmp/lock"))
print(json.dumps(trace))
"""
    )
    assert trace == [
        ["check", True],
        ["injector", ":100", True],
        ["build", "vp8enc", True],
        ["set_state", "paused", True],
        ["send", "ready", False],
    ]
