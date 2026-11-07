"""The part of a pane's terminal state that tmux does not report.

A snapshot rebuilds a pane in the page's parser from what tmux exposes: the
cells (``capture-pane -e``), the modes (formats), an escape sequence cut in the
middle (``capture-pane -P``). Three things the next live byte depends on are
not exposed: the current rendition (SGR: a program that set red and prints
``B`` after the snapshot expects a red ``B``), the character sets (G0/G1 and
the shift between them) and a UTF-8 character whose first bytes tmux has read
but not yet placed. ``StreamState`` follows exactly those from the pane's raw
output, and ``restore()`` writes them back.

This is not a terminal parser: the screen is xterm.js's business, in the page.
The scanner only recognises where escape sequences begin and end (so a ``m``
inside an OSC string is not mistaken for an SGR), interprets SGR, charset
designations and SO/SI, and skips everything else.

The rendition is ``known`` once the scanner has seen a full reset (``CSI m`` or
``CSI 0 m``), or from a pane's first byte: before that, a pane that set a
colour before Merlin attached keeps a default rendition until its next reset,
which shells and full-screen programs send all the time.
"""

from __future__ import annotations

import re

_GROUND, _ESC, _CSI, _STRING, _STRING_ESC, _CHARSET = range(6)

_CSI_FINAL = range(0x40, 0x7F)
# ESC followed by one of these opens a string ended by BEL or ST (ESC \):
# DCS, SOS, OSC, PM, APC.
_STRING_OPENER_BYTES = frozenset(b"PX]^_")
_GROUND_SPECIAL = re.compile(rb"[\x1b\x0e\x0f]")
_STRING_END = re.compile(rb"[\x07\x1b\x18\x1a]")

_COLOUR_BASES = {38: "fg", 48: "bg", 58: "ul"}


class StreamState:
    def __init__(self, known: bool = False) -> None:
        self.known = known
        self._state = _GROUND
        self._params = bytearray()
        self._charset_slot = 0
        self._tail = b""  # an unfinished UTF-8 character, in ground state
        self.flags: dict[str, str] = {}
        self.colours: dict[str, str] = {}
        self.charsets = [b"B", b"B"]
        self.shifted = False

    # -- scanning -------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        i, n = 0, len(data)
        if self._tail and self._state == _GROUND:
            data = self._tail + data
            n = len(data)
            self._tail = b""
        while i < n:
            state = self._state
            if state == _GROUND:
                m = _GROUND_SPECIAL.search(data, i)
                if m is None:
                    self._tail = _utf8_tail(data, i, n)
                    return
                b = data[m.start()]
                i = m.start() + 1
                if b == 0x1B:
                    self._state = _ESC
                elif b == 0x0E:
                    self.shifted = True
                else:
                    self.shifted = False
            elif state == _ESC:
                b = data[i]
                i += 1
                if b == 0x5B:  # [
                    self._state = _CSI
                    self._params.clear()
                elif b in _STRING_OPENER_BYTES:
                    self._state = _STRING
                elif b in (0x28, 0x29):  # ( or ): designate G0 / G1
                    self._charset_slot = b - 0x28
                    self._state = _CHARSET
                elif b == 0x63:  # ESC c: full reset
                    self._reset_all()
                    self._state = _GROUND
                elif 0x20 <= b <= 0x2F:
                    # Other intermediates (ESC # 8, ESC % G, ...): one more byte.
                    self._state = _CHARSET
                    self._charset_slot = -1
                elif b == 0x1B:
                    self._state = _ESC
                else:
                    self._state = _GROUND
            elif state == _CHARSET:
                b = data[i]
                i += 1
                if self._charset_slot in (0, 1) and 0x30 <= b <= 0x7E:
                    self.charsets[self._charset_slot] = bytes([b])
                self._state = _GROUND
            elif state == _CSI:
                b = data[i]
                i += 1
                if b in _CSI_FINAL:
                    if b == 0x6D:  # m
                        self._sgr(bytes(self._params))
                    self._state = _GROUND
                elif b == 0x1B:
                    self._state = _ESC
                elif b in (0x18, 0x1A):
                    self._state = _GROUND
                elif b >= 0x20:
                    self._params.append(b)
                # C0 controls inside a CSI are executed, not part of it.
            elif state == _STRING:
                m = _STRING_END.search(data, i)
                if m is None:
                    return
                b = data[m.start()]
                i = m.start() + 1
                self._state = _STRING_ESC if b == 0x1B else _GROUND
            else:  # _STRING_ESC
                b = data[i]
                if b == 0x5C:  # ST
                    i += 1
                    self._state = _GROUND
                else:
                    # ESC that is not ST ends the string and starts a sequence.
                    self._state = _ESC
        if self._state != _GROUND:
            self._tail = b""

    # -- SGR -------------------------------------------------------------------

    def _reset_all(self) -> None:
        self.flags.clear()
        self.colours.clear()
        self.charsets = [b"B", b"B"]
        self.shifted = False
        self.known = True

    def _sgr(self, raw: bytes) -> None:
        if raw[:1] in (b"?", b">", b"<", b"="):
            return  # a private sequence ending in m (xterm modifyOtherKeys...)
        text = raw.decode("ascii", "replace")
        params = text.split(";") if text else [""]
        i = 0
        while i < len(params):
            p = params[i]
            head, _, sub = p.partition(":")
            code = int(head) if head.isdigit() else 0 if head == "" else -1
            if code in _COLOUR_BASES:
                slot = _COLOUR_BASES[code]
                if sub:
                    self.colours[slot] = p
                elif (
                    i + 1 < len(params) and params[i + 1] == "5" and i + 2 < len(params)
                ):
                    self.colours[slot] = ";".join(params[i : i + 3])
                    i += 2
                elif (
                    i + 1 < len(params) and params[i + 1] == "2" and i + 4 < len(params)
                ):
                    self.colours[slot] = ";".join(params[i : i + 5])
                    i += 4
            elif code == 0:
                self.flags.clear()
                self.colours.clear()
                self.known = True
            elif code in (1, 2):
                self.flags[str(code)] = str(code)
            elif code == 3:
                self.flags["3"] = "3"
            elif code == 4:
                if sub == "0":
                    self.flags.pop("4", None)
                else:
                    self.flags["4"] = p
            elif code == 21:
                self.flags["4"] = "21"
            elif code in (5, 6):
                self.flags["5"] = str(code)
            elif code in (7, 8, 9, 53):
                self.flags[str(code)] = str(code)
            elif code == 22:
                self.flags.pop("1", None)
                self.flags.pop("2", None)
            elif code == 23:
                self.flags.pop("3", None)
            elif code == 24:
                self.flags.pop("4", None)
            elif code == 25:
                self.flags.pop("5", None)
            elif code == 27:
                self.flags.pop("7", None)
            elif code == 28:
                self.flags.pop("8", None)
            elif code == 29:
                self.flags.pop("9", None)
            elif code == 55:
                self.flags.pop("53", None)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.colours["fg"] = head
            elif code == 39:
                self.colours.pop("fg", None)
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.colours["bg"] = head
            elif code == 49:
                self.colours.pop("bg", None)
            elif code == 59:
                self.colours.pop("ul", None)
            i += 1

    # -- restoring -------------------------------------------------------------

    def restore(self) -> bytes:
        """Sequences that put a fresh parser in this state, then the bytes of
        an unfinished UTF-8 character, which the live stream completes."""
        out = bytearray()
        params = list(self.flags.values()) + [
            self.colours[k] for k in ("fg", "bg", "ul") if k in self.colours
        ]
        out += b"\x1b[0" + b"".join(b";" + p.encode() for p in params) + b"m"
        out += b"\x1b(" + self.charsets[0] + b"\x1b)" + self.charsets[1]
        out += b"\x0e" if self.shifted else b"\x0f"
        out += self._tail
        return bytes(out)


def _utf8_tail(data: bytes, start: int, end: int) -> bytes:
    """The bytes of an unfinished UTF-8 character at the end of
    ``data[start:end]``, or nothing."""
    for back in range(1, 4):
        pos = end - back
        if pos < start:
            return b""
        b = data[pos]
        if b & 0xC0 == 0x80:
            continue  # a continuation byte: look further back
        need = (
            2
            if b & 0xE0 == 0xC0
            else 3
            if b & 0xF0 == 0xE0
            else 4
            if b & 0xF8 == 0xF0
            else 1
        )
        return bytes(data[pos:end]) if need > back else b""
    return b""
