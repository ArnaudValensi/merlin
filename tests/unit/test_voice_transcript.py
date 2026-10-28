"""voice/transcript.py on real Claude Code lines.

``fixtures/voice/claude-turn.jsonl`` is a real transcript of one turn (Claude
Code 2.1.312, `claude -p` with a UserPromptSubmit hook printing context), with
the request records and private attachments removed and a `turn_duration`
line added the way an interactive session writes it.
"""

import json
from pathlib import Path

import pytest

from voice import transcript
from voice.transcript import ClaudeTranscriptReader, Event

FIXTURE = Path(__file__).parent / "fixtures" / "voice" / "claude-turn.jsonl"
LINES = FIXTURE.read_text().splitlines(keepends=True)


def _line(**fields) -> str:
    return json.dumps(fields) + "\n"


def _user(text, uid, **extra):
    return _line(
        type="user", uuid=uid, message={"role": "user", "content": text}, **extra
    )


def _assistant(blocks, uid, **extra):
    return _line(type="assistant", uuid=uid, message={"content": blocks}, **extra)


TURN_END = _line(type="system", subtype="turn_duration", durationMs=10)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "conv.jsonl"


def _append(path, *chunks):
    with open(path, "a") as f:
        for c in chunks:
            f.write(c)


class TestRealTurn:
    def test_events_of_a_real_turn(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, *LINES)
        assert reader.poll() == [
            Event(
                "user_prompt",
                "Say hello in one short sentence, then run the shell command "
                "'echo hi' with Bash, then say done.",
            ),
            Event("assistant_text", "Hello!"),
            Event("tool_use", "Bash"),
            Event("assistant_text", "Done."),
            Event("turn_end"),
        ]

    def test_the_hook_context_is_never_an_event(self, path):
        assert "VOICE-CONTEXT-MARKER" in FIXTURE.read_text()
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, *LINES)
        assert not any("VOICE-CONTEXT-MARKER" in e.text for e in reader.poll())

    def test_tool_results_are_not_prompts(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, *LINES)
        prompts = [e for e in reader.poll() if e.kind == "user_prompt"]
        assert len(prompts) == 1

    def test_line_by_line_with_partial_writes(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        events = []
        for line in LINES:
            half = len(line) // 2
            _append(path, line[:half])
            events += reader.poll()
            _append(path, line[half:])
            events += reader.poll()
        assert [e.kind for e in events] == [
            "user_prompt",
            "assistant_text",
            "tool_use",
            "assistant_text",
            "turn_end",
        ]


class TestReader:
    def test_starts_at_the_end_by_default(self, path):
        _append(
            path,
            _user("old question", "u1"),
            _assistant([{"type": "text", "text": "old"}], "a1"),
        )
        reader = ClaudeTranscriptReader(path)
        assert reader.poll() == []
        _append(path, _user("new question", "u2"))
        assert reader.poll() == [Event("user_prompt", "new question")]

    def test_a_missing_file_is_read_from_its_start_once_it_appears(self, path):
        reader = ClaudeTranscriptReader(path)
        assert reader.poll() == []
        _append(path, _user("first", "u1"))
        assert reader.poll() == [Event("user_prompt", "first")]

    def test_sidechain_lines_are_ignored(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(
            path,
            _user("sub task", "s1", isSidechain=True),
            _assistant(
                [{"type": "text", "text": "from the subagent"}], "s2", isSidechain=True
            ),
            _assistant(
                [{"type": "tool_use", "id": "t9", "name": "Grep"}],
                "s3",
                isSidechain=True,
            ),
            _assistant([{"type": "text", "text": "main"}], "m1"),
        )
        assert reader.poll() == [Event("assistant_text", "main")]

    @pytest.mark.parametrize(
        "line",
        [
            _user("<command-name>/clear</command-name>", "c1"),
            _user("<local-command-stdout>ok</local-command-stdout>", "c2"),
            _user("<task-notification>done</task-notification>", "c3"),
            _user([{"type": "text", "text": "[Request interrupted by user]"}], "c4"),
            _user("caveat", "c5", isMeta=True),
            _user("summary of before", "c6", isCompactSummary=True),
            _user([{"type": "tool_result", "tool_use_id": "t", "content": "x"}], "c7"),
            _user("   ", "c8"),
        ],
    )
    def test_records_that_are_not_prompts(self, path, line):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, line)
        assert reader.poll() == []

    def test_prompt_given_as_text_blocks(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(
            path,
            _user([{"type": "text", "text": "look at this"}, {"type": "image"}], "u1"),
        )
        assert reader.poll() == [Event("user_prompt", "look at this")]

    def test_unknown_types_and_garbage_are_ignored(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(
            path,
            _line(type="mode", mode="default"),
            _line(
                type="attachment", attachment={"type": "hook_success", "content": "x"}
            ),
            "not json at all\n",
            "[1, 2]\n",
            _line(type="assistant", uuid="a", message={"content": "a string"}),
            _assistant(
                [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": " "},
                ],
                "a2",
            ),
            _assistant([{"type": "text", "text": "kept"}], "a3"),
        )
        assert reader.poll() == [Event("assistant_text", "kept")]

    def test_nothing_twice(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        line = _assistant(
            [
                {"type": "text", "text": "once"},
                {"type": "tool_use", "id": "t1", "name": "Bash"},
            ],
            "a1",
        )
        _append(path, line, line, _user("q", "u1"), _user("q", "u1"))
        assert reader.poll() == [
            Event("assistant_text", "once"),
            Event("tool_use", "Bash"),
            Event("user_prompt", "q"),
        ]

    def test_a_replaced_file_is_read_from_its_start(self, path, tmp_path):
        _append(path, _user("old " * 50, "u1"))
        reader = ClaudeTranscriptReader(path)
        fresh = tmp_path / "fresh.jsonl"
        _append(fresh, _user("after replace", "u2"))
        fresh.replace(path)
        assert reader.poll() == [Event("user_prompt", "after replace")]

    def test_a_truncated_file_is_read_from_its_start(self, path):
        _append(path, _user("long " * 50, "u1"))
        reader = ClaudeTranscriptReader(path)
        path.write_text(_user("short", "u2"))
        assert reader.poll() == [Event("user_prompt", "short")]

    def test_a_vanished_file_yields_nothing(self, path):
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, _user("q", "u1"))
        path.unlink()
        assert reader.poll() == []

    def test_reads_are_bounded(self, path, monkeypatch):
        monkeypatch.setattr(transcript, "MAX_READ", 64)
        reader = ClaudeTranscriptReader(path, offset=0)
        _append(path, *(_user(f"prompt {i}", f"u{i}") for i in range(5)))
        events = []
        for _ in range(50):
            events += reader.poll()
        assert [e.text for e in events] == [f"prompt {i}" for i in range(5)]


class TestSeam:
    def test_claude_is_the_only_provider(self, path):
        assert isinstance(
            transcript.open_reader("claude", path), ClaudeTranscriptReader
        )
        with pytest.raises(transcript.UnsupportedProvider):
            transcript.open_reader("codex", path)

    def test_stamped_path_wins(self, tmp_path):
        assert transcript.find_transcript("claude", "/t/x.jsonl", "conv") == Path(
            "/t/x.jsonl"
        )

    def test_found_by_conversation_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        target = tmp_path / "projects" / "-home-u-p" / "abc-1.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("")
        assert transcript.find_transcript("claude", "", "abc-1") == target
        assert transcript.find_transcript("claude", "", "missing") is None
        assert transcript.find_transcript("claude", "", "../x") is None
        assert transcript.find_transcript("claude", "", "*") is None
        assert transcript.find_transcript("codex", "", "abc-1") is None
