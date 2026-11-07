"""``terminal/stream_state.py``: the state a pane's next byte depends on that
tmux does not report, followed from the raw output."""

from __future__ import annotations

import pytest

from terminal.stream_state import StreamState


def _state(*chunks: bytes) -> StreamState:
    s = StreamState(known=True)
    for chunk in chunks:
        s.feed(chunk)
    return s


def test_default_state_restores_a_plain_rendition_and_ascii_charsets():
    assert _state().restore() == b"\x1b[0m\x1b(B\x1b)B\x0f"


@pytest.mark.parametrize(
    ("stream", "params"),
    [
        (b"\x1b[31;1mA", {"1", "31"}),
        (b"\x1b[1;2;3;4;5;7;8;9;53m", {"1", "2", "3", "4", "5", "7", "8", "9", "53"}),
        (b"\x1b[1;31m\x1b[22;39m", set()),
        (b"\x1b[38;5;208;48;2;1;2;3m", {"38;5;208", "48;2;1;2;3"}),
        (b"\x1b[38:2::10:20:30m", {"38:2::10:20:30"}),
        (b"\x1b[4:3m", {"4:3"}),
        (b"\x1b[4:3m\x1b[4:0m", set()),
        (b"\x1b[21m", {"21"}),
        (b"\x1b[91;101m\x1b[49m", {"91"}),
        (b"\x1b[7;31m\x1b[m", set()),
        (b"\x1b[7;31m\x1b[0m", set()),
        (b"\x1b[58;5;3m\x1b[59m", set()),
    ],
)
def test_rendition(stream, params):
    restored = _state(stream).restore()
    head = restored[: restored.index(b"m") + 1]
    assert head.startswith(b"\x1b[0")
    got = {p for p in head[len(b"\x1b[0") : -1].decode().split(";") if p}
    # Parameters made of several fields arrive split by ";" again.
    joined = set()
    parts = list(got)
    for p in params:
        if all(x in parts for x in p.split(";")):
            joined.add(p)
    assert joined == params and (got == set() if not params else True)


def test_an_m_inside_strings_and_private_sequences_is_not_sgr():
    s = _state(
        b"\x1b]0;title with 31m\x07",
        b"\x1b]8;;http://x/?a=1m\x1b\\",
        b"\x1bP1;2m tmux passthrough\x1b\\",
        b"\x1b[>4;2m",  # modifyOtherKeys, not a rendition
    )
    assert s.restore().startswith(b"\x1b[0m")


def test_sequences_split_across_chunks():
    s = _state(b"x\x1b", b"[3", b"1;", b"1mA")
    assert s.restore().startswith(b"\x1b[0;1;31m")
    s = _state(b"\x1b]0;a", b"b 1m", b"\x1b", b"\\\x1b[32m")
    assert s.restore().startswith(b"\x1b[0;32m")


def test_charsets_and_shift():
    s = _state(b"\x1b(0lqk")
    assert s.restore().endswith(b"\x1b(0\x1b)B\x0f")
    s = _state(b"\x1b)0\x0eq")
    assert s.restore().endswith(b"\x1b(B\x1b)0\x0e")
    s = _state(b"\x1b)0\x0eq\x0f")
    assert s.restore().endswith(b"\x0f")


def test_full_reset():
    s = _state(b"\x1b[1;31m\x1b(0\x1bc")
    assert s.restore() == b"\x1b[0m\x1b(B\x1b)B\x0f"


@pytest.mark.parametrize(
    ("chunks", "tail"),
    [
        ([b"a\xc3"], b"\xc3"),
        ([b"a\xc3\xa9"], b""),
        ([b"\xe6\x97"], b"\xe6\x97"),
        ([b"\xe6", b"\x97"], b"\xe6\x97"),
        ([b"\xe6\x97\xa5"], b""),
        ([b"\xf0\x9f\x8e"], b"\xf0\x9f\x8e"),
        ([b"\xf0\x9f\x8e\x89"], b""),
        ([b"\xc3", b"\xa9x"], b""),
        ([b"x\x1b[31"], b""),
    ],
)
def test_unfinished_utf8_character(chunks, tail):
    s = _state(*chunks)
    restored = s.restore()
    assert restored.endswith(b"\x0f" + tail)


def test_known_from_a_reset_only():
    s = StreamState(known=False)
    s.feed(b"\x1b[1m")
    assert not s.known
    s.feed(b"\x1b[0m")
    assert s.known
