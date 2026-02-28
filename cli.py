# /// script
# dependencies = ["fastapi", "uvicorn[standard]", "jinja2", "python-dotenv", "python-multipart", "discord.py", "httpx", "faster-whisper"]
# ///
"""
Merlin CLI — entry point for the merlin command.

Subcommands:
    merlin              Start the dashboard (alias for 'merlin start')
    merlin start        Start the dashboard server
    merlin version      Print the current version
    merlin setup        Run the interactive setup wizard
    merlin update       Update to the latest version

Usage:
    uv run cli.py                          # Start dashboard
    uv run cli.py start --port 8080        # Custom port
    uv run cli.py start --dev              # Run from git checkout
    uv run cli.py version                  # Print version
    uv run cli.py setup                    # Run setup wizard
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

import paths

GITHUB_REPO = os.environ.get("MERLIN_REPO", "ArnaudValensi/merlin")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


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
                capture_output=True, text=True, timeout=5,
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
        headers = {"Accept": "application/json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        req = urllib.request.Request(tags_url, headers=headers)
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
    if GITHUB_TOKEN:
        tarball_url = f"https://api.github.com/repos/{GITHUB_REPO}/tarball/v{tag}"
    else:
        tarball_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/v{tag}.tar.gz"

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    # Extract to a temp dir, then rename atomically on success
    staging_dir = target_dir.parent / f".{target_dir.name}.downloading"

    try:
        headers = {}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        req = urllib.request.Request(tarball_url, headers=headers)
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
                        member.name = member.name[len(prefix) + 1:]
                        if not member.name:
                            continue

                        # Reject symlinks, device nodes, FIFOs, etc.
                        if member.type not in _SAFE_TAR_TYPES:
                            continue

                        # Reject path traversal (../ in member name)
                        dest = (resolved_staging / member.name).resolve()
                        if not str(dest).startswith(str(resolved_staging) + os.sep) and dest != resolved_staging:
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
    print(f"Checking for updates...")

    latest = fetch_latest_tag()
    if latest is None:
        print("Could not fetch latest version from GitHub.", file=sys.stderr)
        sys.exit(1)

    # Strip dev suffixes for comparison (e.g., "0.3.0-3-gabcdef" -> "0.3.0")
    current_base = current_version.split("-")[0] if "-" in current_version else current_version
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
    print(f"  To revert: ln -sfn {paths.merlin_home()}/versions/{current_base} {current_link}")


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

    current_base = current_version.split("-")[0] if "-" in current_version else current_version
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
# Setup wizard
# ---------------------------------------------------------------------------


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
        masked = current_token[:8] + "..." + current_token[-4:] if len(current_token) > 12 else "***"
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
        masked_openai = current_openai[:3] + "..." + current_openai[-4:] if len(current_openai) > 7 else "***"
        prompt = f"OpenAI API key [{masked_openai}] (Enter to keep, 'clear' to remove): "
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
    known_keys = {"DASHBOARD_PASS", "TUNNEL_ENABLED", "DISCORD_BOT_TOKEN", "OPENAI_API_KEY"}
    for key, val in existing.items():
        if key not in known_keys:
            lines.append(f"{key}={val}")

    target.write_text("\n".join(lines) + "\n")
    try:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — secrets inside
    except OSError:
        pass  # Windows or unusual filesystem
    print(f"\nConfig saved to {target}")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="merlin",
        description="Merlin — portable mobile dev environment.",
        epilog="""
Run 'merlin' with no arguments to start the dashboard.
Run 'merlin <command> --help' for command-specific help.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command")

    # start (default)
    start_parser = subparsers.add_parser(
        "start", help="Start the dashboard server (default)",
        description="Start the Merlin dashboard server.",
    )
    start_parser.add_argument("--port", type=int, default=3123, help="Port to serve on (default: 3123)")
    start_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    start_parser.add_argument("--no-tunnel", action="store_true", help="Disable Cloudflare tunnel")
    start_parser.add_argument("--dev", action="store_true", help="Run from git checkout (dev mode)")

    # version
    subparsers.add_parser("version", help="Print the current version")

    # setup
    subparsers.add_parser("setup", help="Run the interactive setup wizard")

    # update
    subparsers.add_parser("update", help="Update to the latest version")

    return parser


def cli_main(argv: list[str] | None = None) -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Default to 'start' when no subcommand given
    command = args.command or "start"

    if command == "version":
        print(get_version())

    elif command == "setup":
        run_setup()

    elif command == "update":
        run_update()

    elif command == "start":
        dev = getattr(args, "dev", False)
        if dev:
            paths.set_dev_mode(True)

        port = getattr(args, "port", 3123)
        host = getattr(args, "host", "0.0.0.0")
        no_tunnel = getattr(args, "no_tunnel", False)

        # Check for first-run setup (installed mode only)
        if not paths.is_dev_mode() and not paths.config_path().exists():
            print("No config found — running first-time setup.\n")
            run_setup()
            print()

        _check_for_update()

        import main
        main.start_server(port=port, host=host, no_tunnel=no_tunnel)


if __name__ == "__main__":
    cli_main()
