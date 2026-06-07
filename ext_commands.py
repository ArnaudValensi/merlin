"""
Extension command discovery and dispatch.

Extensions expose CLI commands by convention — no manifest, no registry:

    ~/.merlin/extensions/tasks/
    └── commands/
        ├── add.py        ->  merlin tasks add
        └── recap.py      ->  merlin tasks recap

- Command name = filename without extension. Any executable file works;
  the shebang decides how it runs (Python commands use
  ``#!/usr/bin/env -S uv run --script`` plus a PEP 723 block for deps).
- One-line help = first line of the module docstring, extracted with
  ``ast`` (no code execution). Non-Python executables: a leading
  ``# Description:`` comment line.
- Dispatch: the first unknown CLI token is looked up as an extension
  directory; ``commands/<subcommand>`` is exec'd with the remaining args
  (args, stdio, and exit code pass straight through).

Namespacing is solved by construction: each extension owns exactly one
top-level namespace (its directory name). Core command names and built-in
extension ids are reserved; an installed extension claiming one is
rejected at dispatch and at server load.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import NoReturn

import paths

# Core CLI subcommands (cli.py argparse tree). Canonical list lives here so
# dispatch and the server loader agree without importing cli.py.
CORE_COMMANDS: tuple[str, ...] = (
    "start",
    "version",
    "setup",
    "update",
    "config",
    "agent",
    "cron",
    "chat",
    "dashboard-url",
)

# Built-in extension ids and core module ids — never claimable by an
# installed extension.
BUILTIN_IDS: tuple[str, ...] = (
    "notes",
    "merlin-bot",
    "files",
    "terminal",
    "commits",
    "cron",
)


# Curated top-level aliases — a built-in privilege only. Maps the alias to
# the (extension, command) it expands to; cli.py rewrites argv accordingly.
TOP_LEVEL_ALIASES: dict[str, tuple[str, str]] = {
    "kb": ("notes", "kb"),
    "remember": ("notes", "remember"),
}

# Default enabled state for built-in extensions when extensions.json has no
# entry. Shared with main.py so the CLI and the server agree.
BUILTIN_DEFAULT_ENABLED: dict[str, bool] = {"notes": True, "merlin-bot": False}


def reserved_names() -> set[str]:
    """All names an installed extension directory may not use."""
    return set(CORE_COMMANDS) | set(BUILTIN_IDS) | set(TOP_LEVEL_ALIASES)


# ---------------------------------------------------------------------------
# Help extraction
# ---------------------------------------------------------------------------


def extract_help(path: Path) -> str | None:
    """Return the one-line help for a command file, or None.

    Python files: first line of the module docstring via ``ast`` — no code
    execution, no imports, works offline. Other files: the first
    ``# Description: ...`` comment line. Unparseable or unreadable files
    degrade gracefully to None.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if path.suffix == ".py":
        try:
            docstring = ast.get_docstring(ast.parse(source))
        except (SyntaxError, ValueError):
            return None
        docstring = (docstring or "").strip()
        if not docstring:
            return None
        return docstring.splitlines()[0].strip() or None

    for line in source.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# Description:"):
            description = stripped.removeprefix("# Description:").strip()
            return description or None
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def list_commands(ext_dir: Path) -> dict[str, Path]:
    """Map command name -> executable file under ``<ext_dir>/commands/``.

    Command name is the filename without its extension. Dotfiles,
    underscore-prefixed files (``__init__.py``, templates), directories,
    and non-executable files are skipped. The executable check reads the
    stat mode bits rather than os.access, which over-reports on some bind
    mounts. On a stem collision (``add.py`` and ``add.sh``) the first in
    sorted order wins.
    """
    commands_dir = ext_dir / "commands"
    if not commands_dir.is_dir():
        return {}

    commands: dict[str, Path] = {}
    for entry in sorted(commands_dir.iterdir()):
        if entry.name.startswith((".", "_")) or not entry.is_file():
            continue
        if not (entry.stat().st_mode & 0o111):
            continue
        commands.setdefault(entry.stem, entry)
    return commands


def builtin_extension_dirs() -> dict[str, Path]:
    """Built-in extensions that may ship ``commands/`` dirs (in app code)."""
    app = paths.app_dir()
    return {
        "notes": app / "notes",
        "merlin-bot": app / "merlin-bot",
    }


def installed_extension_dirs() -> dict[str, Path]:
    """Installed extension directories under ``~/.merlin/extensions/``."""
    extensions_dir = paths.extensions_dir()
    if not extensions_dir.is_dir():
        return {}
    return {d.name: d for d in sorted(extensions_dir.iterdir()) if d.is_dir()}


def enabled_extension_source_dirs() -> dict[str, Path]:
    """Extension roots whose surfaces (skills) are active per extensions.json.

    Mirrors the server's enabled resolution (explicit state, then built-in
    defaults, then enabled-by-default for installed) without importing
    main.py, so 'merlin setup' aggregates the same skill set the server
    will. Reserved-named installed dirs are excluded, matching the server
    loader's rejection.
    """
    import json

    state: dict = {}
    state_path = paths.extensions_state_path()
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            if isinstance(data, dict):
                state = data
        except (OSError, json.JSONDecodeError):
            state = {}

    sources: dict[str, Path] = {}
    for ext_id, ext_dir in builtin_extension_dirs().items():
        if state.get(ext_id, BUILTIN_DEFAULT_ENABLED.get(ext_id, True)):
            sources[ext_id] = ext_dir

    reserved = reserved_names()
    for ext_id, ext_dir in installed_extension_dirs().items():
        if ext_id in reserved:
            continue
        if state.get(ext_id, True):
            sources[ext_id] = ext_dir

    return sources


# ---------------------------------------------------------------------------
# Help enumeration (merlin --help)
# ---------------------------------------------------------------------------


def _format_group(title: str, ext_dirs: dict[str, Path]) -> list[str]:
    """Format one help group; empty list if no extension has commands."""
    lines: list[str] = []
    for ext_id, ext_dir in ext_dirs.items():
        commands = list_commands(ext_dir)
        if not commands:
            continue
        if not lines:
            lines.append(f"{title}:")
        for name, file in commands.items():
            help_line = extract_help(file)
            invocation = f"merlin {ext_id} {name}"
            if help_line:
                lines.append(f"  {invocation:<28} {help_line}")
            else:
                lines.append(f"  {invocation}")
    return lines


def format_extension_help() -> str:
    """Epilog block for ``merlin --help``: built-in and installed commands."""
    sections: list[str] = []

    builtin_lines = _format_group("Built-in extensions", builtin_extension_dirs())
    if builtin_lines:
        sections.append("\n".join(builtin_lines))

    installed = installed_extension_dirs()
    rejected = sorted(set(installed) & reserved_names())
    for name in rejected:
        installed.pop(name)
        print(
            f"Warning: ignoring installed extension '{name}': "
            "the name is reserved by a core command or built-in extension. "
            "Rename the extension directory.",
            file=sys.stderr,
        )

    installed_lines = _format_group("Installed extensions", installed)
    if installed_lines:
        sections.append("\n".join(installed_lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _fail(message: str, exit_code: int = 2) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


def dispatch(argv: list[str]) -> NoReturn:
    """Route ``merlin <ext> <command> [args...]`` to an extension command.

    Replaces the current process via ``os.execv`` so args, stdio, and the
    exit code pass through untouched. Errors are descriptive and exit 2
    (or 126 for a non-executable command file).
    """
    token, *rest = argv

    ext_dir = builtin_extension_dirs().get(token)
    if ext_dir is None or not ext_dir.is_dir():
        installed = installed_extension_dirs()
        if token in installed and token in reserved_names():
            _fail(
                f"Extension name '{token}' is reserved by a core command or "
                "built-in extension and cannot be used by an installed "
                f"extension. Rename {installed[token]}."
            )
        ext_dir = installed.get(token)

    if ext_dir is None:
        available = ", ".join(
            sorted(set(installed_extension_dirs()) - reserved_names())
        )
        hint = f" Installed extensions: {available}." if available else ""
        _fail(
            f"Unknown command: '{token}'. Run 'merlin --help' for the full "
            f"command list.{hint}"
        )

    commands = list_commands(ext_dir)
    if not rest:
        listing = "\n".join(
            f"  merlin {token} {name:<16} {extract_help(file) or ''}".rstrip()
            for name, file in commands.items()
        )
        _fail(
            f"usage: merlin {token} <command> [args...]\n\n"
            f"Commands for '{token}':\n{listing or '  (none found)'}"
        )

    command, *args = rest
    file = commands.get(command)
    if file is None:
        # A matching file that exists but lacks +x deserves a precise error.
        commands_dir = ext_dir / "commands"
        if commands_dir.is_dir():
            for entry in sorted(commands_dir.iterdir()):
                if entry.is_file() and entry.stem == command:
                    _fail(
                        f"Command file {entry} is not executable. "
                        f"Fix with: chmod +x {entry}",
                        exit_code=126,
                    )
        available = ", ".join(sorted(commands)) or "(none)"
        _fail(
            f"Unknown command '{command}' for extension '{token}'. "
            f"Available commands: {available}."
        )

    os.execv(str(file), [str(file), *args])
    raise AssertionError("unreachable")  # pragma: no cover
