"""Capture the tmux workspace as plain JSON data.

One snapshot holds every tmux session -> window -> pane, with the pane's
directory and, when a Claude Code or Codex process runs in it, what is needed to
resume that conversation: the provider, the conversation id, the directory to
relaunch from and the launch options to keep. No scrollback, no screen content.
Every other program is recorded only as its pane's directory: the restore never
reruns a command (see ``docs/dev/workspace-restore.md``).

Conversation ids, in order:

- Claude: ``~/.claude/sessions/<pid>.json``, which every interactive Claude
  writes with its live session id (exact, per process). Else the window's
  ``@agent_conv``, stamped by the SessionStart hook.
- Codex: the window's ``@agent_conv`` (stamped by the SessionStart hook from the
  hook payload). Else a launch as ``codex resume <id>``. Else not resumable: the
  pane comes back as a shell.

The argv handling (``split_cli`` & co.) is ported from the ``reboot-restore``
skill, where it was proven on real launch lines.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

SNAPSHOT_VERSION = 1

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_SEP = "\x1f"

# Window options carried over so the Sessions board keeps each agent's identity
# and family links. @agent_state is transient (the agent's hooks set it again)
# and @agent_provider / @agent_conv are restamped by the resumed agent's hook.
WINDOW_OPTIONS = ("@agent_sid", "@agent_cwd", "@agent_parent", "@agent_relation")

SHELLS = frozenset({"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"})

# --- CLI argument filtering -------------------------------------------------
# Each table lists the options of one CLI so a launch command line can be split
# into options (kept) and positionals (the initial prompt, dropped).

CLAUDE_VALUE = {
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--autocompact", "--debug-file", "--effort", "--environment", "--fallback-model",
    "--input-format", "--json-schema", "--max-budget-usd", "--model", "-n", "--name",
    "--output-format", "--permission-mode", "--permission-prompts",
    "--permission-prompt-tool", "--plugin-dir", "--plugin-url",
    "--remote-control-session-name-prefix", "--session-id", "--setting-sources",
    "--settings", "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot",
}  # fmt: skip
CLAUDE_VARIADIC = {
    "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
    "--disallowed-tools", "--file", "--mcp-config", "--tools",
}  # fmt: skip
# Options that pick or create a conversation, or make a one-shot run: the
# restore supplies its own --resume, so these never carry over.
CLAUDE_DROP = {
    "-c", "--continue", "-r", "--resume", "--session-id", "--fork-session",
    "--from-pr", "--teleport", "-w", "--worktree", "--tmux", "-n", "--name",
    "--cloud", "--desktop", "--bg", "--background",
}  # fmt: skip
CLAUDE_NON_INTERACTIVE = {"-p", "--print"}

CODEX_VALUE = {
    "-c", "--config", "-m", "--model", "-p", "--profile", "-s", "--sandbox",
    "-a", "--ask-for-approval", "-C", "--cd", "--enable", "--disable",
    "--local-provider", "--remote", "--remote-auth-token-env", "--add-dir",
}  # fmt: skip
CODEX_VARIADIC = {"-i", "--image"}
CODEX_DROP = {
    "-i", "--image", "--worktree", "--last", "--all", "--include-non-interactive",
}  # fmt: skip
CODEX_INTERACTIVE_SUBCOMMANDS = {"resume", "fork"}
CODEX_SUBCOMMANDS = {
    "agents", "exec", "e", "review", "login", "logout", "mcp", "plugin",
    "app-server", "remote-control", "completion", "update", "doctor", "sandbox",
    "debug", "apply", "a", "resume", "queue", "archive", "delete",
    "migrate-rollouts", "unarchive", "fork", "cloud", "exec-server", "features",
    "help", "mcp-server",
}  # fmt: skip


def split_cli(tokens, value_opts, variadic_opts, stop_at_subcommand=None):
    """Split argv tokens into option groups and positionals.

    Returns (options, positionals, rest): ``options`` is a list of token lists,
    one per option with its values. When ``stop_at_subcommand`` is a set and a
    positional matches it, parsing stops there and ``rest`` holds the
    subcommand and everything after it.
    """
    options: list[list[str]] = []
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            positionals.extend(tokens[i + 1 :])
            break
        if tok.startswith("-") and tok != "-":
            name = tok.split("=", 1)[0]
            if "=" in tok:
                options.append([tok])
                i += 1
            elif name in value_opts:
                options.append(tokens[i : i + 2])
                i += 2
            elif name in variadic_opts:
                j = i + 1
                # A token with whitespace is the prompt, not one more value.
                while (
                    j < len(tokens)
                    and not tokens[j].startswith("-")
                    and not re.search(r"\s", tokens[j])
                ):
                    j += 1
                options.append(tokens[i:j])
                i = j
            elif (
                name in ("-r", "--resume")
                and i + 1 < len(tokens)
                and UUID_RE.match(tokens[i + 1])
            ):
                options.append(tokens[i : i + 2])
                i += 2
            else:
                options.append([tok])
                i += 1
            continue
        if (
            stop_at_subcommand is not None
            and not positionals
            and tok in stop_at_subcommand
        ):
            return options, positionals, tokens[i:]
        positionals.append(tok)
        i += 1
    return options, positionals, []


def keep_options(options, drop):
    kept: list[str] = []
    for group in options:
        if group[0].split("=", 1)[0] not in drop:
            kept.extend(group)
    return kept


def claude_resume_args(argv):
    """Options to relaunch a Claude process with, or None if not interactive."""
    options, _, _ = split_cli(argv[1:], CLAUDE_VALUE, CLAUDE_VARIADIC)
    names = {g[0].split("=", 1)[0] for g in options}
    if names & CLAUDE_NON_INTERACTIVE:
        return None
    return keep_options(options, CLAUDE_DROP)


def codex_args(argv_after_codex):
    """(kept options, subcommand, positionals) for a Codex TUI, or None if the
    command line is not the interactive TUI (``codex exec`` & co.)."""
    options, _, rest = split_cli(
        argv_after_codex, CODEX_VALUE, CODEX_VARIADIC, CODEX_SUBCOMMANDS
    )
    sub = None
    positionals: list[str] = []
    if rest:
        sub = rest[0]
        if sub not in CODEX_INTERACTIVE_SUBCOMMANDS:
            return None
        more, positionals, _ = split_cli(rest[1:], CODEX_VALUE, CODEX_VARIADIC)
        options += more
    return keep_options(options, CODEX_DROP), sub, positionals


def codex_argv_tail(argv):
    """Arguments after the ``codex`` token, or None if not a codex launcher."""
    if not argv:
        return None
    if os.path.basename(argv[0]) == "codex":
        return argv[1:]
    if (
        os.path.basename(argv[0]) in ("node", "bun")
        and len(argv) > 1
        and os.path.basename(argv[1]) == "codex"
    ):
        return argv[2:]
    return None


def resume_command(agent: dict) -> list[str]:
    """The argv that resumes a snapshot agent on its conversation."""
    if agent["provider"] == "claude":
        return ["claude", *agent.get("options", []), "--resume", agent["conv"]]
    return ["codex", *agent.get("options", []), "resume", agent["conv"]]


# --- /proc ------------------------------------------------------------------


class Proc:
    """Read-only view of /proc. A seam so tests can fake the process tree."""

    root = Path("/proc")

    def argv(self, pid: int) -> list[str]:
        try:
            raw = (self.root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return []
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [p.decode(errors="replace") for p in parts]

    def stat(self, pid: int) -> list[str] | None:
        try:
            text = (self.root / str(pid) / "stat").read_text()
            return text.rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None

    def cwd(self, pid: int) -> str | None:
        try:
            return os.readlink(self.root / str(pid) / "cwd")
        except OSError:
            return None

    def children(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        try:
            entries = os.listdir(self.root)
        except OSError:
            return out
        for entry in entries:
            if entry.isdigit():
                stat = self.stat(int(entry))
                if stat:
                    out.setdefault(int(stat[1]), []).append(int(entry))
        return out


def subtree(root: int, children: dict[int, list[int]]) -> list[int]:
    """Breadth-first pids under ``root``, root first."""
    order, queue = [], [root]
    while queue:
        pid = queue.pop(0)
        order.append(pid)
        queue.extend(sorted(children.get(pid, [])))
    return order


def claude_sessions_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(custom).expanduser() if custom else Path.home() / ".claude"
    return base / "sessions"


def live_claude_sessions(proc: Proc, sessions_dir: Path | None = None) -> dict:
    """pid -> session record, for Claude processes still alive (PID reuse is
    checked against the recorded process start time)."""
    out: dict[int, dict] = {}
    directory = sessions_dir or claude_sessions_dir()
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        stat = proc.stat(pid)
        if not stat:
            continue
        start = data.get("procStart")
        if start and len(stat) > 19 and stat[19] != str(start):
            continue
        out[pid] = data
    return out


# --- tmux -------------------------------------------------------------------


def _tmux_rows(args: list[str], fields: tuple[str, ...]) -> list[dict] | None:
    """Rows of a tmux list command, or None when there is no tmux server."""
    fmt = _SEP.join("#{" + f + "}" for f in fields)
    try:
        r = subprocess.run(
            ["tmux", *args, "-F", fmt],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(_SEP)
        if len(parts) == len(fields):
            rows.append(dict(zip(fields, parts)))
    return rows


_WINDOW_FIELDS = (
    "session_name",
    "window_id",
    "window_index",
    "window_name",
    "window_layout",
    "window_active",
    "automatic-rename",
    "@agent_provider",
    "@agent_conv",
    *WINDOW_OPTIONS,
)
_PANE_FIELDS = (
    "window_id",
    "pane_index",
    "pane_pid",
    "pane_active",
    "pane_current_path",
    "pane_current_command",
)


def live_session_names() -> list[str] | None:
    """Names of the live tmux sessions; None when no server is running."""
    rows = _tmux_rows(["list-sessions"], ("session_name",))
    return None if rows is None else [r["session_name"] for r in rows]


def read_tmux() -> tuple[list[dict], list[dict], list[dict]] | None:
    """(sessions, windows, panes) rows of the whole server, or None when there
    is no server or any of the three reads failed."""
    sessions = _tmux_rows(["list-sessions"], ("session_name",))
    if sessions is None:
        return None
    # A failed read is not an empty one: the sweep must skip the write rather
    # than persist a workspace with its windows or panes missing.
    windows = _tmux_rows(["list-windows", "-a"], _WINDOW_FIELDS)
    if windows is None:
        return None
    panes = _tmux_rows(["list-panes", "-a"], _PANE_FIELDS)
    if panes is None:
        return None
    return sessions, windows, panes


# --- capture ----------------------------------------------------------------


def _find_agent(
    pane: dict,
    window: dict,
    children: dict[int, list[int]],
    claude_by_pid: dict,
    proc: Proc,
) -> dict | None:
    """The resumable agent running in a pane, or None."""
    try:
        root = int(pane["pane_pid"])
    except (KeyError, ValueError):
        return None
    win_provider = window.get("@agent_provider", "")
    win_conv = window.get("@agent_conv", "")
    for pid in subtree(root, children):
        argv = proc.argv(pid)
        if not argv:
            continue
        if pid in claude_by_pid or os.path.basename(argv[0]) == "claude":
            record = claude_by_pid.get(pid) or {}
            if record.get("kind") not in (None, "interactive"):
                continue
            options = claude_resume_args(argv)
            if options is None:
                continue
            conv = record.get("sessionId") or (
                win_conv if win_provider == "claude" else ""
            )
            if not conv:
                return None
            return {
                "provider": "claude",
                "conv": conv,
                "cwd": record.get("cwd") or proc.cwd(pid) or pane["pane_current_path"],
                "options": options,
            }
        tail = codex_argv_tail(argv)
        if tail is not None:
            parsed = codex_args(tail)
            if parsed is None:
                continue
            options, sub, positionals = parsed
            conv = win_conv if win_provider == "codex" else ""
            if not conv and sub == "resume" and positionals:
                if UUID_RE.match(positionals[0]):
                    conv = positionals[0]
            if not conv:
                return None
            return {
                "provider": "codex",
                "conv": conv,
                "cwd": proc.cwd(pid) or pane["pane_current_path"],
                "options": options,
            }
    return None


def build_snapshot(
    sessions: list[dict],
    windows: list[dict],
    panes: list[dict],
    proc: Proc | None = None,
    claude_by_pid: dict | None = None,
    now: float | None = None,
) -> dict:
    """Assemble a snapshot from tmux rows and the process tree. Pure apart
    from the /proc reads, which go through ``proc``."""
    proc = proc or Proc()
    children = proc.children()
    if claude_by_pid is None:
        claude_by_pid = live_claude_sessions(proc)
    panes_by_window: dict[str, list[dict]] = {}
    for p in panes:
        panes_by_window.setdefault(p["window_id"], []).append(p)
    windows_by_session: dict[str, list[dict]] = {}
    for w in windows:
        windows_by_session.setdefault(w["session_name"], []).append(w)

    out_sessions = []
    for s in sessions:
        name = s["session_name"]
        out_windows = []
        active_window = None
        for w in sorted(
            windows_by_session.get(name, []), key=lambda w: int(w["window_index"])
        ):
            index = int(w["window_index"])
            if w["window_active"] == "1":
                active_window = index
            out_panes = []
            for p in sorted(
                panes_by_window.get(w["window_id"], []),
                key=lambda p: int(p["pane_index"]),
            ):
                pane = {
                    "index": int(p["pane_index"]),
                    "cwd": p["pane_current_path"],
                    "active": p["pane_active"] == "1",
                }
                agent = _find_agent(p, w, children, claude_by_pid, proc)
                if agent:
                    pane["agent"] = agent
                out_panes.append(pane)
            out_windows.append(
                {
                    "index": index,
                    "name": w["window_name"],
                    "automatic_rename": w["automatic-rename"] == "1",
                    "layout": w["window_layout"],
                    "options": {k: w[k] for k in WINDOW_OPTIONS if w.get(k)},
                    "panes": out_panes,
                }
            )
        if not out_windows:
            continue
        out_sessions.append(
            {"name": name, "active_window": active_window, "windows": out_windows}
        )
    return {
        "version": SNAPSHOT_VERSION,
        "saved_at": time.time() if now is None else now,
        "sessions": out_sessions,
    }


def take_snapshot() -> dict | None:
    """Snapshot the live tmux server, or None when there is no server."""
    rows = read_tmux()
    if rows is None:
        return None
    return build_snapshot(*rows)


def same_content(a: dict | None, b: dict | None) -> bool:
    """True when two snapshots hold the same workspace (``saved_at`` aside)."""
    if a is None or b is None:
        return a is b
    strip = lambda s: {k: v for k, v in s.items() if k != "saved_at"}  # noqa: E731
    return strip(a) == strip(b)


def counts(sessions: list[dict]) -> tuple[int, int]:
    """(windows, resumable agents) across snapshot sessions."""
    windows = sum(len(s["windows"]) for s in sessions)
    agents = sum(
        1 for s in sessions for w in s["windows"] for p in w["panes"] if p.get("agent")
    )
    return windows, agents


def shell_quote(argv: list[str]) -> str:
    return shlex.join(argv)
