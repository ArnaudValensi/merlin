"""Tests for notifications/content.py: the title, the eight body shapes, the
duration at its boundaries, and the snippet cleaning against pane fixtures."""

from pathlib import Path

import pytest

from notifications.content import (
    SNIPPET_MAX,
    clean_snippet,
    compose,
    compose_body,
    compose_title,
    format_duration,
)

PANES = Path(__file__).parent / "fixtures" / "panes"


def pane(name: str) -> str:
    return (PANES / f"{name}.txt").read_text()


class TestTitle:
    def test_window_session_environment(self):
        assert compose_title("claude", "merlin-saas", "sandbox") == (
            "claude · merlin-saas · sandbox"
        )

    def test_missing_window_name_reads_window(self):
        assert compose_title("", "merlin-saas", "sandbox") == (
            "window · merlin-saas · sandbox"
        )

    def test_missing_environment_is_omitted_with_its_separator(self):
        assert compose_title("claude", "merlin-saas", "") == "claude · merlin-saas"

    def test_both_missing(self):
        assert compose_title("", "s", "") == "window · s"


class TestBody:
    @pytest.mark.parametrize(
        "state,busy,snippet,expected",
        [
            ("ask", None, "Which one?", "Needs an answer: Which one?"),
            ("ask", 40, "Which one?", "Needs an answer: Which one?"),
            ("ask", None, "", "Needs an answer"),
            ("ask", 40, "", "Needs an answer"),
            ("done", 840, "All tests pass.", "Finished after 14 min: All tests pass."),
            ("done", None, "All tests pass.", "Finished: All tests pass."),
            ("done", 840, "", "Finished after 14 min"),
            ("done", None, "", "Finished"),
        ],
    )
    def test_the_eight_shapes(self, state, busy, snippet, expected):
        assert compose_body(state, busy, snippet) == expected

    def test_zero_seconds_is_a_known_duration(self):
        assert compose_body("done", 0, "") == "Finished after 0 s"

    def test_compose_returns_both(self):
        assert compose(
            state="done",
            window_name="claude",
            session="s",
            machine="m",
            busy_seconds=40,
            snippet="Done.",
        ) == ("claude · s · m", "Finished after 40 s: Done.")


class TestDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0 s"),
            (40, "40 s"),
            (59.4, "59 s"),
            (59.6, "1 min"),
            (60, "1 min"),
            (89, "1 min"),
            (90, "2 min"),
            (840, "14 min"),
            (3569, "59 min"),
            (3570, "1 h"),
            (3600, "1 h"),
            (4800, "1 h 20 min"),
            (3 * 3600, "3 h"),
            (3 * 3600 + 29, "3 h"),
            (3 * 3600 + 31, "3 h 1 min"),
            (9 * 3600 + 59 * 60, "9 h 59 min"),
            (10 * 3600 + 20 * 60, "10 h"),
            (12 * 3600 + 59 * 60 + 40, "13 h"),
            (-5, "0 s"),
        ],
    )
    def test_boundaries(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestSnippet:
    def test_claude_pane_after_a_turn(self):
        # The last three non-empty lines of the last message (a paragraph gap
        # is skipped, not a boundary), the timing line and the input box gone.
        assert clean_snippet(pane("claude-done")) == (
            "with the state before the snippet. Next I will wire the watcher's "
            "busy tracking and run the full validate before committing."
        )

    def test_claude_pane_with_an_open_question(self):
        # The option cursor is the prompt glyph: the question stays above it.
        assert clean_snippet(pane("claude-ask")) == (
            "Before I remove the old constant, one question. ☐ Constant Should "
            "VAPID_SUBJECT stay as an alias of FALLBACK_SUBJECT for one release?"
        )

    def test_codex_pane(self):
        assert clean_snippet(pane("codex-done")) == (
            "unit tests, Ruff, Ty, and git diff --check. Response recorded for "
            "m1-subject-001. Reviewer is ready for the next request."
        )

    def test_empty_capture(self):
        assert clean_snippet("") == ""
        assert clean_snippet(None) == ""
        assert clean_snippet("\n\n   \n") == ""

    def test_only_chrome(self):
        assert clean_snippet(pane("chrome-only")) == ""

    def test_prompt_line_and_everything_below_it_go(self):
        text = "● Done.\n\n────\n❯ half-typed reply\n────\n  ⏵⏵ status\n"
        assert clean_snippet(text) == "Done."

    def test_codex_prompt_with_typed_text_goes(self):
        text = "• Done.\n\n─ Worked for 2s ───\n\n› what next\n\n  gpt-5 · ~/x\n"
        assert clean_snippet(text) == "Done."

    def test_stops_at_the_start_of_the_last_message(self):
        text = "● Earlier message line.\n\n● Bash(ls)\n  ⎿  a b c\n\n● Last.\n"
        assert clean_snippet(text) == "Last."

    def test_three_lines_at_most_without_a_bullet(self):
        text = "one\ntwo\nthree\nfour\nfive\n"
        assert clean_snippet(text) == "three four five"

    def test_bullet_within_three_lines_stops_the_walk(self):
        text = "• first message\n• second\n  continues here\n"
        assert clean_snippet(text) == "second continues here"

    def test_whitespace_collapsed_and_controls_stripped(self):
        text = "●   Two   spaces\x07 and\ta tab\r\n  \x1b[32mgreen\x1b[0m text   \n"
        assert clean_snippet(text) == "Two spaces and a tab green text"

    def test_clipped_with_an_ellipsis(self):
        text = "● " + "word " * 100
        out = clean_snippet(text)
        assert len(out) == SNIPPET_MAX
        assert out.endswith("…")
        assert out.startswith("word word")

    def test_box_rows_and_rules_are_chrome(self):
        text = "● Real.\n\n╭────╮\n│ box │\n╰────╯\n\n────\n"
        assert clean_snippet(text) == "Real."

    def test_no_prompt_and_no_chrome_takes_the_tail(self):
        assert clean_snippet("The report is written\n\n\n") == "The report is written"
