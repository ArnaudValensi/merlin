"""
Merlin CLI — entry point for the merlin command.

Subcommands:
    merlin              Start the dashboard (alias for 'merlin start')
    merlin start        Start the dashboard server
    merlin version      Print the current version
    merlin setup        Run the interactive setup wizard
    merlin update       Update to the latest version
    merlin config       Print resolved config values

Usage:
    uv run cli.py                          # Start dashboard
    uv run cli.py start --port 8080        # Custom port
    uv run cli.py start --dev              # Run from git checkout
    uv run cli.py version                  # Print version
    uv run cli.py setup                    # Run setup wizard
    uv run cli.py config notes-dir         # Print notes directory
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
# lib/ holds shared modules (structured_log, engine) imported by cron and
# other delegated core commands.
sys.path.insert(1, str(Path(__file__).parent.resolve() / "lib"))

import ext_commands
import paths

GITHUB_REPO = os.environ.get("MERLIN_REPO", "ArnaudValensi/merlin")


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def get_version() -> str:
    """Detect the current Merlin version.

    Installed mode: read from the 'current' symlink target folder name.
        ~/.merlin/current -> versions/0.3.0 -> "0.3.0"
    Dev mode: use 'git describe --tags', fall back to "dev".
    """
    if paths.is_dev_mode():
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--always"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=paths.app_dir(),
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip()
                # Strip leading 'v' if present (v0.3.0 -> 0.3.0)
                return version.removeprefix("v")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "dev"

    # Installed mode: read immediate symlink target (don't follow chains)
    current = paths.merlin_home() / "current"
    if current.is_symlink():
        return Path(os.readlink(current)).name
    return "unknown"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def fetch_latest_tag() -> str | None:
    """Fetch the latest tag from GitHub. Returns version without 'v' prefix."""
    tags_url = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
    try:
        req = urllib.request.Request(tags_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data and isinstance(data, list) and data[0].get("name"):
                return data[0]["name"].removeprefix("v")
    except Exception as e:
        print(f"Warning: could not fetch tags: {e}", file=sys.stderr)

    return None


_SAFE_TAR_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}


def download_and_extract(tag: str, target_dir: Path) -> None:
    """Download a release tarball and extract to target_dir.

    Security: validates that extracted paths stay within target_dir,
    rejects symlinks/device nodes, and cleans up on failure.
    """
    tarball_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/v{tag}.tar.gz"

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    # Extract to a temp dir, then rename atomically on success
    staging_dir = target_dir.parent / f".{target_dir.name}.downloading"

    try:
        req = urllib.request.Request(tarball_url)
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        staging_dir.mkdir(parents=True, exist_ok=True)
        resolved_staging = staging_dir.resolve()

        with tarfile.open(tmp_path, "r:gz") as tar:
            members = tar.getmembers()
            if members:
                prefix = members[0].name.split("/")[0]
                for member in members:
                    if member.name.startswith(prefix + "/"):
                        member.name = member.name[len(prefix) + 1 :]
                        if not member.name:
                            continue

                        # Reject symlinks, device nodes, FIFOs, etc.
                        if member.type not in _SAFE_TAR_TYPES:
                            continue

                        # Reject path traversal (../ in member name)
                        dest = (resolved_staging / member.name).resolve()
                        if (
                            not str(dest).startswith(str(resolved_staging) + os.sep)
                            and dest != resolved_staging
                        ):
                            raise ValueError(f"Path traversal detected: {member.name}")

                        tar.extract(member, staging_dir)

        # Atomic rename on success
        os.rename(str(staging_dir), str(target_dir))
    except BaseException:
        # Clean up partial extraction on any failure
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def atomic_symlink(target: Path, link: Path) -> None:
    """Atomically swap a symlink by creating temp + rename.

    Uses a unique temp name to avoid TOCTOU races with concurrent updates.
    """
    tmp_link = link.parent / f".{link.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp_link.symlink_to(target)
        os.replace(str(tmp_link), str(link))
    except BaseException:
        tmp_link.unlink(missing_ok=True)
        raise


def run_update() -> None:
    """Update Merlin to the latest version."""
    current_version = get_version()
    print(f"Current version: {current_version}")
    print("Checking for updates...")

    latest = fetch_latest_tag()
    if latest is None:
        print("Could not fetch latest version from GitHub.", file=sys.stderr)
        sys.exit(1)

    # Strip dev suffixes for comparison (e.g., "0.3.0-3-gabcdef" -> "0.3.0")
    current_base = (
        current_version.split("-")[0] if "-" in current_version else current_version
    )
    if current_base == latest:
        print(f"Already up to date ({latest})")
        return

    print(f"New version available: {latest}")

    versions_dir = paths.merlin_home() / "versions"
    version_dir = versions_dir / latest

    if not version_dir.exists():
        print(f"Downloading {latest}...")
        download_and_extract(latest, version_dir)

    print(f"Switching to {latest}...")
    current_link = paths.merlin_home() / "current"
    atomic_symlink(version_dir, current_link)

    print(f"Updated: {current_version} -> {latest}")
    print(
        f"  To revert: ln -sfn {paths.merlin_home()}/versions/{current_base} {current_link}"
    )


# ---------------------------------------------------------------------------
# Startup update check
# ---------------------------------------------------------------------------

_UPDATE_CHECK_FILE = ".last-update-check"
_UPDATE_CHECK_INTERVAL = 86400  # 24 hours


def _check_for_update() -> None:
    """Check for updates on startup (once per day, installed mode only)."""
    if paths.is_dev_mode():
        return

    # Rate limit: skip if checked recently
    check_file = paths.merlin_home() / _UPDATE_CHECK_FILE
    if check_file.exists():
        age = time.time() - check_file.stat().st_mtime
        if age < _UPDATE_CHECK_INTERVAL:
            return

    current_version = get_version()
    latest = fetch_latest_tag()

    # Touch the check file regardless of result
    check_file.parent.mkdir(parents=True, exist_ok=True)
    check_file.touch()

    if latest is None:
        return

    current_base = (
        current_version.split("-")[0] if "-" in current_version else current_version
    )
    if current_base == latest:
        return

    print(f"\n  New version available: {latest} (current: {current_version})")
    try:
        answer = input("  Update now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer in ("y", "yes"):
        run_update()
        print()


# ---------------------------------------------------------------------------
# SaaS token persistence
# ---------------------------------------------------------------------------


def _save_saas_token(token: str) -> None:
    """Save MERLIN_SAAS_TOKEN to config.env, preserving existing keys."""
    config = paths.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    replaced = False

    if config.exists():
        for line in config.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("MERLIN_SAAS_TOKEN="):
                lines.append(f"MERLIN_SAAS_TOKEN={token}")
                replaced = True
            else:
                lines.append(line)

    if not replaced:
        lines.append(f"MERLIN_SAAS_TOKEN={token}")

    config.write_text("\n".join(lines) + "\n")
    try:
        config.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — contains secrets
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _refresh_skills() -> None:
    """Build the skill registry and refresh the interactive shims.

    Best-effort: setup must not fail because a shim could not be created.
    The server startup repeats this on every start anyway.
    """
    try:
        from lib import skills

        # Same enabled-state resolution as the server: a disabled
        # extension's skills must not be re-exposed by setup.
        sources = ext_commands.enabled_extension_source_dirs()

        registry = skills.rebuild(sources)
        skills.sync_interactive_shims()
        print(
            f"Skills: {len(registry)} aggregated into {skills.canonical_dir()}\n"
            f"  Exposed to your own agents via {skills.claude_skills_dir()} "
            f"and {skills.agents_skills_dir()}"
        )
    except Exception as e:
        print(f"Warning: could not refresh skill shims: {e}", file=sys.stderr)


def run_setup(config_path: Path | None = None) -> None:
    """Interactive first-run setup wizard.

    Prompts for dashboard password, tunnel config, and Discord bot token.
    Writes results to config.env.
    """
    target = config_path or paths.config_path()

    existing = {}
    if target.exists():
        print(f"Config file exists: {target}")
        for line in target.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                existing[key.strip()] = val.strip()

        answer = input("Overwrite existing config? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Setup cancelled.")
            return

    print("\n--- Merlin Setup ---\n")

    # Dashboard password
    current_pass = existing.get("DASHBOARD_PASS", "")
    if current_pass:
        prompt = f"Dashboard password [{current_pass}]: "
    else:
        prompt = "Dashboard password (empty for no auth): "
    password = input(prompt).strip()
    if not password and current_pass:
        password = current_pass

    # Tunnel
    current_tunnel = existing.get("TUNNEL_ENABLED", "false")
    default_yn = "Y/n" if current_tunnel.lower() in ("true", "1", "yes") else "y/N"
    tunnel_input = input(f"Enable Cloudflare tunnel? [{default_yn}] ").strip().lower()
    if not tunnel_input:
        tunnel_enabled = current_tunnel.lower() in ("true", "1", "yes")
    else:
        tunnel_enabled = tunnel_input in ("y", "yes")

    # Discord bot token
    current_token = existing.get("DISCORD_BOT_TOKEN", "")
    if current_token:
        masked = (
            current_token[:8] + "..." + current_token[-4:]
            if len(current_token) > 12
            else "***"
        )
        prompt = f"Discord bot token [{masked}] (Enter to keep): "
    else:
        prompt = "Discord bot token (Enter to skip): "
    token = input(prompt).strip()
    if not token and current_token:
        token = current_token

    # OpenAI API key (voice transcription)
    print("\n─── Voice Transcription ───\n")
    print("Merlin can transcribe voice input using OpenAI's Whisper API")
    print("for faster, lighter transcription. This is optional — without")
    print("an API key, Merlin uses local transcription (faster-whisper),")
    print("which works offline but requires a ~1.5GB model download.\n")
    print("The Whisper API costs ~$0.006 per minute of audio.")
    print("Get a key: https://platform.openai.com/api-keys\n")

    current_openai = existing.get("OPENAI_API_KEY", "")
    if current_openai:
        masked_openai = (
            current_openai[:3] + "..." + current_openai[-4:]
            if len(current_openai) > 7
            else "***"
        )
        prompt = (
            f"OpenAI API key [{masked_openai}] (Enter to keep, 'clear' to remove): "
        )
    else:
        prompt = "OpenAI API key (Enter to skip): "
    openai_key = input(prompt).strip()
    if openai_key.lower() == "clear":
        openai_key = ""
    elif not openai_key and current_openai:
        openai_key = current_openai

    # Write config
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Merlin configuration",
        f"DASHBOARD_PASS={password}",
        f"TUNNEL_ENABLED={'true' if tunnel_enabled else 'false'}",
    ]
    if token:
        lines.append(f"DISCORD_BOT_TOKEN={token}")
    if openai_key:
        lines.append(f"OPENAI_API_KEY={openai_key}")

    # Preserve any extra keys from existing config
    known_keys = {
        "DASHBOARD_PASS",
        "TUNNEL_ENABLED",
        "DISCORD_BOT_TOKEN",
        "OPENAI_API_KEY",
    }
    for key, val in existing.items():
        if key not in known_keys:
            lines.append(f"{key}={val}")

    target.write_text("\n".join(lines) + "\n")
    try:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — secrets inside
    except OSError:
        pass  # Windows or unusual filesystem
    print(f"\nConfig saved to {target}")

    _refresh_skills()


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _delegate_cron(argv: list[str]) -> None:
    from cron.manage import main as cron_main

    cron_main(argv, prog="merlin cron")


def _delegate_chat(argv: list[str]) -> None:
    from lib.chat import main as chat_main

    chat_main(argv)


# Delegated core commands: cli_main routes them before argparse so every arg
# (including --help) reaches the command's own parser, and build_parser
# registers a help stub from the same entry. Adding a command here is the
# whole job (plus listing it in ext_commands.CORE_COMMANDS, which the drift
# test enforces).
DELEGATED_COMMANDS: dict[str, tuple] = {
    "cron": (
        _delegate_cron,
        "Manage scheduled cron jobs (list/get/add/enable/disable/remove/trigger/history)",
    ),
    "chat": (
        _delegate_chat,
        "Send messages, replies, and reactions to the chat channel",
    ),
}


def build_parser(include_extension_help: bool = True) -> argparse.ArgumentParser:
    """Build the CLI argument parser with subcommands.

    Extension commands (built-in and installed) are enumerated into the
    epilog so 'merlin --help' is the full discovery catalog. The
    enumeration reads and parses every command file, so cli_main only
    requests it when help will actually render; other invocations
    (merlin config, version, start) skip the scan.
    """
    epilog = (
        "Run 'merlin' with no arguments to start the dashboard.\n"
        "Run 'merlin <command> --help' for command-specific help.\n"
    )
    alias_lines = ["Top-level aliases:"]
    for alias, (ext_id, command) in ext_commands.TOP_LEVEL_ALIASES.items():
        target = f"merlin {ext_id} {command}"
        alias_lines.append(f"  merlin {alias:<21} alias for: {target}")
    epilog = "\n".join(alias_lines) + "\n\n" + epilog
    if include_extension_help:
        extension_help = ext_commands.format_extension_help()
        if extension_help:
            epilog = f"{extension_help}\n\n{epilog}"

    parser = argparse.ArgumentParser(
        prog="merlin",
        description="Merlin — portable mobile dev environment.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", title="Core commands")

    # start (default)
    start_parser = subparsers.add_parser(
        "start",
        help="Start the dashboard server (default)",
        description="Start the Merlin dashboard server.",
    )
    start_parser.add_argument(
        "--port", type=int, default=3123, help="Port to serve on (default: 3123)"
    )
    start_parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    start_parser.add_argument(
        "--no-tunnel", action="store_true", help="Disable Cloudflare tunnel"
    )
    start_parser.add_argument(
        "--dev", action="store_true", help="Run from git checkout (dev mode)"
    )
    start_parser.add_argument(
        "--saas-token",
        metavar="TOKEN",
        help="Connect to Merlin Cloud with this environment token (saves to config for future runs)",
    )

    # agent
    subparsers.add_parser(
        "agent",
        help="Print the agent-facing brain doc",
        description=(
            "Print the Merlin brain doc: what Merlin is and how to operate "
            "it. Intended for AI agents; pipe it into a prompt or read it "
            "on demand."
        ),
    )

    # Delegated commands — routed before argparse in cli_main so all args
    # (including --help) pass through to the command's own parser. The same
    # table entry yields the routing and this help stub; they cannot drift.
    for name, (_handler, help_text) in DELEGATED_COMMANDS.items():
        subparsers.add_parser(name, help=help_text, add_help=False)

    # dashboard-url
    subparsers.add_parser(
        "dashboard-url",
        help="Print the dashboard URL (credentials embedded if set)",
        description=(
            "Print the dashboard URL with login credentials embedded when "
            "DASHBOARD_PASS is set. Resolution: MERLIN_DASHBOARD_URL > "
            "https://TUNNEL_HOSTNAME > http://localhost:3123."
        ),
    )

    # version
    subparsers.add_parser("version", help="Print the current version")

    # setup
    subparsers.add_parser("setup", help="Run the interactive setup wizard")

    # update
    subparsers.add_parser("update", help="Update to the latest version")

    # config
    config_parser = subparsers.add_parser(
        "config",
        help="Print resolved config values",
        description="Print resolved configuration values. Reads from config.env, environment, and defaults.",
        epilog="""
Available keys:
  notes-dir       Notes directory (notes, KB, user.md, logs)
  skills-user-dir Personal skill home (always-active, per-environment)
  home            Merlin home directory (~/.merlin)
  app-dir         Application code directory
  data-dir        User data directory
  config-path     Config file path
  logs-dir        Logs directory
  sessions-dir    Session transcripts directory
  cron-jobs-dir   Cron job definitions directory
  extensions-dir  User extensions directory
  version         Current version

Examples:
  merlin config notes-dir                        # Print notes directory
  cat "$(merlin config notes-dir)/kb/topic.md"   # Use in shell commands
  merlin config                                  # List all config values
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config_parser.add_argument(
        "key", nargs="?", help="Config key to print (omit to list all)"
    )

    return parser


def _get_config_values() -> dict[str, str]:
    """Resolve all config values."""
    # Load config.env so NOTES_DIR etc. are available
    from dotenv import load_dotenv

    load_dotenv(paths.config_path())

    from lib import skills

    return {
        "notes-dir": str(paths.notes_dir()),
        "skills-user-dir": str(skills.user_skills_dir()),
        "home": str(paths.merlin_home()),
        "app-dir": str(paths.app_dir()),
        "data-dir": str(paths.data_dir()),
        "config-path": str(paths.config_path()),
        "logs-dir": str(paths.logs_dir()),
        "sessions-dir": str(paths.sessions_dir()),
        "cron-jobs-dir": str(paths.cron_jobs_dir()),
        "extensions-dir": str(paths.extensions_dir()),
        "version": get_version(),
    }


def run_agent() -> None:
    """Print the agent-facing brain doc.

    Stub content for now; the agent-documentation epic writes the real doc
    and adds the --personality / --user layer flags (names reserved there).
    Read from the app dir so 'merlin update' refreshes it via the 'current'
    symlink.
    """
    brain = paths.app_dir() / "agent" / "MERLIN.md"
    try:
        content = brain.read_text()
    except OSError:
        print(f"Brain doc not found at {brain}", file=sys.stderr)
        sys.exit(1)
    print(content.rstrip())


def run_dashboard_url() -> None:
    """Print the dashboard URL, with login credentials embedded if set.

    Resolution: MERLIN_DASHBOARD_URL (explicit override, e.g. a DNS name
    pointing at the box) > https://TUNNEL_HOSTNAME (named tunnel) >
    http://localhost:3123. Quick-tunnel URLs are ephemeral and unknown
    here; set MERLIN_DASHBOARD_URL for a stable address. A scheme-less
    override (bare host or host:port) is normalized to http://.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    paths.load_config_env()

    base = os.getenv("MERLIN_DASHBOARD_URL", "").strip()
    if base and "://" not in base:
        # A bare DNS name (which the resolution above invites) would land
        # in urlsplit's .path and produce a mangled URL with credentials
        # attached to an empty host.
        base = f"http://{base}"
    if not base:
        hostname = os.getenv("TUNNEL_HOSTNAME", "").strip()
        base = f"https://{hostname}" if hostname else "http://localhost:3123"

    user = os.getenv("DASHBOARD_USER", "admin")
    password = os.getenv("DASHBOARD_PASS", "")

    parts = urlsplit(base)
    if password and "@" not in parts.netloc:
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.netloc}"
        parts = parts._replace(netloc=netloc)

    print(urlunsplit(parts))


def run_config(key: str | None) -> None:
    """Print resolved config values."""
    values = _get_config_values()
    if key is None:
        # List all
        max_key_len = max(len(k) for k in values)
        for k, v in values.items():
            print(f"{k:<{max_key_len}}  {v}")
    elif key in values:
        print(values[key])
    else:
        print(f"Unknown config key: {key}", file=sys.stderr)
        print(f"Available keys: {', '.join(sorted(values))}", file=sys.stderr)
        sys.exit(1)


def cli_main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]

    # Allow `merlin --saas-token X` (and other start flags) without typing `start`.
    # If the first token is a flag (not -h/--help), route to the `start` subparser.
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["start", *argv]

    # Curated top-level aliases (built-in privilege): expand to their
    # extension command, then fall through to dispatch below.
    if argv and argv[0] in ext_commands.TOP_LEVEL_ALIASES:
        ext_id, command = ext_commands.TOP_LEVEL_ALIASES[argv[0]]
        argv = [ext_id, command, *argv[1:]]

    # Delegated core commands: routed before argparse so every arg
    # (including --help) passes through to the command's own parser.
    if argv and argv[0] in DELEGATED_COMMANDS:
        handler, _help = DELEGATED_COMMANDS[argv[0]]
        handler(argv[1:])
        return

    # First token is not a core command: try extension command dispatch.
    # dispatch() either execs the command (never returns) or exits with a
    # descriptive error.
    if (
        argv
        and not argv[0].startswith("-")
        and argv[0] not in ext_commands.CORE_COMMANDS
    ):
        ext_commands.dispatch(argv)

    wants_help = any(arg in ("-h", "--help") for arg in argv)
    parser = build_parser(include_extension_help=wants_help)
    args = parser.parse_args(argv)

    # Default to 'start' when no subcommand given
    command = args.command or "start"

    if command == "version":
        print(get_version())

    elif command == "agent":
        run_agent()

    elif command == "dashboard-url":
        run_dashboard_url()

    elif command == "setup":
        run_setup()

    elif command == "update":
        run_update()

    elif command == "config":
        run_config(getattr(args, "key", None))

    elif command == "start":
        dev = getattr(args, "dev", False)
        if dev:
            paths.set_dev_mode(True)

        port = getattr(args, "port", 3123)
        host = getattr(args, "host", "0.0.0.0")
        no_tunnel = getattr(args, "no_tunnel", False)
        saas_token = getattr(args, "saas_token", None)

        # Save SaaS token to config and set in environment
        if saas_token:
            _save_saas_token(saas_token)
            os.environ["MERLIN_SAAS_TOKEN"] = saas_token

        # Check for first-run setup (installed mode only, skip in SaaS mode)
        if (
            not paths.is_dev_mode()
            and not paths.config_path().exists()
            and not os.getenv("MERLIN_SAAS_TOKEN")
        ):
            print("No config found — running first-time setup.\n")
            run_setup()
            print()

        _check_for_update()

        import main

        main.start_server(port=port, host=host, no_tunnel=no_tunnel)


if __name__ == "__main__":
    cli_main()
