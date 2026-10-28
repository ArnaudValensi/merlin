"""Voice mode hook scripts (terminal/hooks/), against a private tmux server.

  - agent-voice-context.sh: prints the voice instructions only when its own
    window has @voice = on, and always exits 0.
  - agent-session-init.sh: stamps @agent_transcript from `transcript_path`.

The private server and its `run` helper come from test_board_hooks.py.
"""

import time

import pytest
from test_board_hooks import HOOKS, INIT_SH, Server, pytestmark  # noqa: F401

VOICE_SH = HOOKS / "agent-voice-context.sh"
PROMPT = '{"hook_event_name": "UserPromptSubmit", "prompt": "hello"}'


@pytest.fixture
def srv(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    s = Server(tmp_path / "s.sock", home)
    s.tmux("new-session", "-d", "-s", "t", "-x", "100", "-y", "30")
    wins = {"a": s.tmux("display-message", "-p", "#{window_id}")}
    wins["b"] = s.tmux("new-window", "-P", "-F", "#{window_id}", "-t", "t:")
    yield s, wins
    s.kill()


class TestVoiceContext:
    def test_silent_when_voice_is_unset(self, srv):
        s, w = srv
        r = s.run(VOICE_SH, pane=s.pane_of(w["a"]), stdin=PROMPT)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_prints_the_instructions_when_voice_is_on(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@voice", "on")
        r = s.run(VOICE_SH, pane=s.pane_of(w["a"]), stdin=PROMPT)
        assert r.returncode == 0
        assert r.stdout.startswith("Voice mode is on")
        assert "read aloud" in r.stdout
        assert "language of the question" in r.stdout
        assert "—" not in r.stdout  # no em-dash in prose

    def test_only_its_own_window_counts(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@voice", "on")
        r = s.run(VOICE_SH, pane=s.pane_of(w["b"]), stdin=PROMPT)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_any_other_value_is_off(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@voice", "off")
        r = s.run(VOICE_SH, pane=s.pane_of(w["a"]), stdin=PROMPT)
        assert r.stdout == ""

    def test_harmless_outside_tmux(self, tmp_path):
        s = Server(tmp_path / "none.sock", tmp_path)  # no server running
        r = s.run(VOICE_SH, stdin=PROMPT)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_fast(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@voice", "on")
        started = time.monotonic()
        r = s.run(VOICE_SH, pane=s.pane_of(w["a"]), stdin=PROMPT)
        assert r.returncode == 0
        assert time.monotonic() - started < 1.0


class TestTranscriptStamp:
    def test_stamps_the_transcript_path(self, srv):
        s, w = srv
        payload = (
            '{"session_id": "abc", "transcript_path": '
            '"/home/u/.claude/projects/-home-u-my dir/abc.jsonl", "cwd": "/x"}'
        )
        s.run(INIT_SH, "claude", pane=s.pane_of(w["a"]), cwd=s.home, stdin=payload)
        assert s.wopt(w["a"], "@agent_transcript") == (
            "/home/u/.claude/projects/-home-u-my dir/abc.jsonl"
        )

    def test_overwritten_on_the_next_start(self, srv):
        s, w = srv
        pane = s.pane_of(w["a"])
        for conv in ("first", "second"):
            payload = f'{{"session_id":"{conv}","transcript_path":"/t/{conv}.jsonl"}}'
            s.run(INIT_SH, "claude", pane=pane, cwd=s.home, stdin=payload)
        assert s.wopt(w["a"], "@agent_transcript") == "/t/second.jsonl"

    def test_escaped_or_relative_paths_are_skipped(self, srv):
        s, w = srv
        pane = s.pane_of(w["a"])
        for path in (r"/t/we\\\"ird.jsonl", "relative/x.jsonl"):
            payload = f'{{"session_id": "c", "transcript_path": "{path}"}}'
            s.run(INIT_SH, "claude", pane=pane, cwd=s.home, stdin=payload)
            assert s.wopt(w["a"], "@agent_transcript") == ""

    def test_not_stamped_without_a_provider(self, srv):
        s, w = srv
        payload = '{"session_id": "c", "transcript_path": "/t/c.jsonl"}'
        s.run(INIT_SH, pane=s.pane_of(w["a"]), cwd=s.home, stdin=payload)
        assert s.wopt(w["a"], "@agent_transcript") == ""
