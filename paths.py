"""
Centralized path resolution for Merlin.

Two modes:
  - Dev mode: Running from git checkout. App code in repo root.
  - Installed mode: Running from ~/.merlin/current/.

User data (notes, jobs, logs, config) always lives under ~/.merlin/
regardless of mode. Only app code location differs.

Dev mode detected by (in order):
  1. Explicit override via set_dev_mode()
  2. MERLIN_DEV env var (1/true/yes enables, 0/false/no disables)
  3. .git/ directory present next to this file

Override the base install directory with MERLIN_HOME env var (default: ~/.merlin).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Directory containing this file — always the app code root
_THIS_DIR = Path(__file__).parent.resolve()

# Explicit override for dev mode (set by CLI --dev flag)
_dev_mode_override: bool | None = None


def set_dev_mode(enabled: bool) -> None:
    """Explicitly set dev/installed mode. Called by CLI for --dev flag."""
    global _dev_mode_override
    _dev_mode_override = enabled


def is_dev_mode() -> bool:
    """True if running from a git checkout (dev mode)."""
    if _dev_mode_override is not None:
        return _dev_mode_override
    env_val = os.environ.get("MERLIN_DEV", "").lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False
    return (_THIS_DIR / ".git").exists()


def merlin_home() -> Path:
    """Base directory for installed Merlin. Default: ~/.merlin."""
    custom = os.environ.get("MERLIN_HOME")
    if custom:
        return Path(custom).resolve()
    return (Path.home() / ".merlin").resolve()


def app_dir() -> Path:
    """Where app code lives.

    Dev:       the repo root (directory containing this file)
    Installed: ~/.merlin/current/
    """
    if is_dev_mode():
        return _THIS_DIR
    return merlin_home() / "current"


def data_dir() -> Path:
    """Where user data lives (notes, jobs, data, logs).

    Always ~/.merlin/ regardless of mode. User data is never in the code repo.
    """
    return merlin_home()


def config_path() -> Path:
    """Main config file path. Always ~/.merlin/config.env."""
    return merlin_home() / "config.env"


def bot_config_path() -> Path:
    """Bot-specific config (Discord token, etc.). Same as config_path()."""
    return merlin_home() / "config.env"


def load_config_env() -> None:
    """Load config.env into os.environ (existing env vars win).

    Stdlib-only equivalent of dotenv's load_dotenv for KEY=VALUE files, so
    dependency-free command scripts can honor config.env (e.g. NOTES_DIR)
    from any cwd. Matches dotenv's handling of hand-edited files: an
    optional ``export `` prefix and matching surrounding quotes are
    stripped, so every Merlin process resolves the same values.
    """
    config = config_path()
    if not config.exists():
        return
    try:
        lines = config.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def launch_cwd() -> Path:
    """Default working directory for jobs and agents: where Merlin was
    launched, falling back to the user's home.

    main.py exports MERLIN_LAUNCH_CWD at startup; jobs layer a
    per-job working_dir on top of this chain.
    """
    return Path(os.environ.get("MERLIN_LAUNCH_CWD") or Path.home())


def notes_dir() -> Path:
    """Notes directory (notes, kb/, user.md, logs/).

    Resolution order:
    1. NOTES_DIR env var or config.env value → use as-is
    2. Managed container (MERLIN_ENVIRONMENT_SLUG set) → ~/shared/notes
    3. Standalone / BYOI → ~/.merlin/notes (data_dir() / "notes")
    """
    custom = os.environ.get("NOTES_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    # Managed containers have MERLIN_ENVIRONMENT_SLUG set and ~/shared/ mounted.
    # BYOI environments have MERLIN_SAAS_TOKEN but no ~/shared/.
    if os.environ.get("MERLIN_ENVIRONMENT_SLUG"):
        return (Path.home() / "shared" / "notes").resolve()
    return data_dir() / "notes"


def jobs_dir() -> Path:
    """Job definitions directory."""
    return data_dir() / "jobs"


def job_logs_dir() -> Path:
    """Job execution log directory."""
    return data_dir() / "job-logs"


def logs_dir() -> Path:
    """Base log directory."""
    return data_dir() / "logs"


def sessions_dir() -> Path:
    """Session transcript directory. Always ~/.merlin/sessions/."""
    return data_dir() / "sessions"


def extensions_dir() -> Path:
    """User extensions directory. Always ~/.merlin/extensions/."""
    return data_dir() / "extensions"


def extensions_state_path() -> Path:
    """Extension state file. Always ~/.merlin/extensions.json."""
    return data_dir() / "extensions.json"


def server_state_path() -> Path:
    """File where the running server publishes its live state (PID + port).

    One atomically-written JSON file — ``{"pid": ..., "port": ...}`` — is the
    single source of truth for a running Merlin: `merlin restart` reads the PID
    to stop the exact process and the port to relaunch on the same one, and CLI
    readers (`merlin job url`) read the port for exact public URLs. Written only
    after the socket binds. Stale after a crash, so readers must confirm the PID
    is a live Merlin before trusting it; stale state with no matching live
    process is ignored, never treated as configuration.
    """
    return data_dir() / "data" / "server-state.json"


def read_server_state() -> dict | None:
    """Parse server-state.json, or None if absent/garbage."""
    try:
        data = json.loads(server_state_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_server_state(pid: int, port: int) -> None:
    """Publish the live PID+port atomically (write temp, then rename)."""
    path = server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"pid": pid, "port": port}))
    os.replace(tmp, path)


def resolve_dashboard_port(flag: int | None) -> int:
    """The port to serve on: ``--port`` flag > MERLIN_DASHBOARD_PORT > 3123.

    MERLIN_DASHBOARD_PORT is the persistent *intent* (config.env or the process
    environment; the environment wins via load_config_env's setdefault). It is
    the local bind port, not a public/proxy port. This is the shared resolver
    for both `merlin start` and direct `main.py` startup.
    """
    if flag is not None:
        return flag
    raw = os.environ.get("MERLIN_DASHBOARD_PORT", "").strip()
    if raw.isdigit():
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    return 3123


def server_port_path() -> Path:
    """Legacy per-release port file (pre-server-state.json). Read-only now, as a
    fallback for CLI readers during the upgrade to the unified state file."""
    return data_dir() / "data" / "server-port"


def server_pid_path() -> Path:
    """Legacy PID file (v0.30.x, pre-server-state.json). Read-only now, so a
    `merlin restart` on the new version can still stop a server started by the
    previous one, which wrote this file instead of server-state.json."""
    return data_dir() / "data" / "server-pid"


def is_supervised() -> bool:
    """True when a service manager (systemd, launchd, ...) owns Merlin's
    lifecycle.

    Declared, never detected: the unit/plist that starts Merlin sets
    MERLIN_SUPERVISED=1. In this mode `merlin restart` stops the process and exits,
    letting the supervisor start the replacement, instead of relaunching Merlin
    itself (which would race the supervisor for the port).

    Keep this flag in the unit's Environment=, never in config.env: config.env
    is read by every launch, so it would leak into a hand-run process and make
    an update stop Merlin with nothing to bring it back.
    """
    return os.environ.get("MERLIN_SUPERVISED", "").lower() in ("1", "true", "yes")
