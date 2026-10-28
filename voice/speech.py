"""Text for the ear: what a voice should read, cut into speakable pieces.

Ported from Nova (the founder's desktop voice tool): ``speakable`` drops what a
voice should not read out loud, ``Chunker`` cuts the answer into sentence-sized
pieces so the first one can be synthesized while the agent is still talking.
Pure functions, no I/O.
"""

from __future__ import annotations

import re

# Said in place of a code block: the code stays on screen, not in the ear.
CODE_PHRASE = {
    "en": "The code is in the terminal.",
    "fr": "Le code est dans le terminal.",
}

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.M)
_LINE_MARKER = re.compile(r"^[ \t]*(?:#+|>+|[-*+•]|\d+[.)])[ \t]+", re.M)
_INNER_UNDERSCORE = re.compile(r"(?<=\w)_(?=\w)")
_MARKS = re.compile(r"[*_#>~|`]")
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\s*\n\s*")


def speakable(text: str, lang: str = "en") -> str:
    """Strip what a voice should not read. Line breaks survive as boundaries.

    Code fences become one short sentence saying the code is in the terminal
    (in the voice language), links keep their label, URLs, markdown marks,
    list markers, rules, table rows and emoji go.
    """
    phrase = CODE_PHRASE.get(lang, CODE_PHRASE["en"])
    text = _FENCE.sub(f"\n{phrase}\n", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _TABLE_ROW.sub("", text)
    text = _RULE.sub("", text)
    text = _LINE_MARKER.sub("", text)
    text = _INNER_UNDERSCORE.sub(" ", text)
    text = _MARKS.sub("", text)
    text = _EMOJI.sub("", text)
    text = _SPACES.sub(" ", text)
    return _BLANK_LINES.sub("\n", text).strip()


_BOUNDARY = re.compile(r"([.!?…:;]+[\"»)]?\s)|(\n+)")


class Chunker:
    """Cut text into sentence-sized pieces for low-latency synthesis.

    The first piece may be short (12 characters) so the voice starts early;
    later pieces gather sentences up to 40 characters, so the voice does not
    stutter on many tiny clips. A line break always ends a piece.
    """

    FIRST_MIN = 12
    NEXT_MIN = 40

    def __init__(self) -> None:
        self.buf = ""
        self.first = True

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self.buf)
            if not m:
                break
            cut = m.end()
            piece = self.buf[:cut]
            minimum = self.FIRST_MIN if self.first else self.NEXT_MIN
            if len(piece.strip()) < minimum and not m.group(2):
                nxt = _BOUNDARY.search(self.buf[cut:])
                if not nxt:
                    break
                cut += nxt.end()
                piece = self.buf[:cut]
            self.buf = self.buf[cut:]
            piece = " ".join(piece.split())
            if piece:
                out.append(piece)
                self.first = False
        return out

    def flush(self) -> list[str]:
        piece, self.buf = " ".join(self.buf.split()), ""
        return [piece] if piece else []
