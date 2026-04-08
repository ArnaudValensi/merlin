"""Git sync watcher for the notes directory.

Watches the notes directory for changes and automatically commits/pushes.
Periodically pulls from remote to sync changes from other environments or Obsidian.

All git operations run as asyncio subprocesses (non-blocking).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("merlin.notes.sync")

# Sync state — visible to the notes UI for conflict banners
conflicted_files: list[str] = []

# Sync state — visible to the extensions page for status display
sync_state: dict = {
    "last_push_at": None,  # ISO timestamp string
    "last_push_ok": None,  # True/False/None (never pushed)
    "last_error": None,  # error string or None
}

# Internal state
_sync_task: asyncio.Task | None = None
_pull_task: asyncio.Task | None = None


def _parse_seconds(value: str, default: float = 5.0) -> float:
    """Parse a value as seconds. Accepts '5', '5s', etc. Falls back to default on invalid input."""
    value = value.strip().lower().rstrip("s")
    try:
        return float(value)
    except ValueError:
        return default


async def _run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode().strip(),
        stderr_bytes.decode().strip(),
    )


async def _ensure_git_repo(notes_dir: Path) -> None:
    """Initialize a git repo in the notes dir if one doesn't exist."""
    if (notes_dir / ".git").exists():
        return
    notes_dir.mkdir(parents=True, exist_ok=True)
    rc, _, stderr = await _run_git("init", cwd=notes_dir)
    if rc != 0:
        logger.error("git init failed: %s", stderr)
        return
    # Set default branch name and local user config (avoids "tell me who you are" on commit)
    await _run_git("checkout", "-b", "main", cwd=notes_dir)
    await _run_git("config", "user.email", "merlin@local", cwd=notes_dir)
    await _run_git("config", "user.name", "Merlin", cwd=notes_dir)
    logger.info("Initialized git repo in %s", notes_dir)


async def _configure_remote(notes_dir: Path, remote_url: str) -> None:
    """Set the git remote origin. Idempotent."""
    rc, current, _ = await _run_git("remote", "get-url", "origin", cwd=notes_dir)
    if rc == 0 and current == remote_url:
        return  # Already configured correctly
    if rc == 0:
        # Remote exists but wrong URL — update it
        await _run_git("remote", "set-url", "origin", remote_url, cwd=notes_dir)
    else:
        # No remote — add it
        await _run_git("remote", "add", "origin", remote_url, cwd=notes_dir)
    logger.info("Configured remote origin: %s", remote_url)


async def _has_remote(notes_dir: Path) -> bool:
    """Check if a remote origin is configured."""
    rc, _, _ = await _run_git("remote", "get-url", "origin", cwd=notes_dir)
    return rc == 0


async def _has_changes(notes_dir: Path) -> bool:
    """Check if there are uncommitted changes."""
    rc, stdout, _ = await _run_git("status", "--porcelain", cwd=notes_dir)
    return rc == 0 and bool(stdout)


async def _detect_conflicts(notes_dir: Path) -> list[str]:
    """Return list of files with merge conflicts."""
    rc, stdout, _ = await _run_git(
        "diff", "--name-only", "--diff-filter=U", cwd=notes_dir
    )
    if rc != 0 or not stdout:
        return []
    return [f.strip() for f in stdout.splitlines() if f.strip()]


async def _commit_all(notes_dir: Path) -> bool:
    """Stage and commit all changes. Returns True if a commit was made."""
    rc, _, stderr = await _run_git("add", "-A", cwd=notes_dir)
    if rc != 0:
        logger.error("git add failed: %s", stderr)
        return False

    rc, stdout, stderr = await _run_git("commit", "-m", "sync notes", cwd=notes_dir)
    if rc != 0:
        combined = stdout + stderr
        if "nothing to commit" in combined or "no changes added" in combined:
            return False
        logger.error("git commit failed: %s", stderr or stdout)
        return False

    return True


async def _pull(notes_dir: Path) -> None:
    """Pull from remote. Handles merge conflicts by preserving markers."""
    global conflicted_files

    if not await _has_remote(notes_dir):
        return

    # Check if remote branch exists (first push may not have happened yet)
    rc, _, _ = await _run_git(
        "ls-remote", "--exit-code", "--heads", "origin", "main", cwd=notes_dir
    )
    if rc != 0:
        return  # No remote branch yet — nothing to pull

    rc, stdout, stderr = await _run_git(
        "pull", "--no-rebase", "origin", "main", cwd=notes_dir
    )

    if rc != 0:
        # Check for merge conflicts
        conflicts = await _detect_conflicts(notes_dir)
        if conflicts:
            conflicted_files = conflicts
            logger.warning("Merge conflicts in: %s", ", ".join(conflicts))
            # Commit the conflicted state so the repo isn't stuck mid-merge
            await _run_git("add", "-A", cwd=notes_dir)
            await _run_git("commit", "-m", "sync: merge conflict", cwd=notes_dir)
        else:
            logger.error("git pull failed: %s", stderr or stdout)
        return

    # Pull succeeded — check if previous conflicts are resolved
    if conflicted_files:
        await _update_conflict_state(notes_dir)


async def _push(notes_dir: Path) -> None:
    """Push to remote (best effort). Updates sync_state."""
    if not await _has_remote(notes_dir):
        return
    rc, _, stderr = await _run_git("push", "-u", "origin", "main", cwd=notes_dir)
    if rc != 0:
        logger.warning("git push failed: %s", stderr)
        sync_state["last_push_ok"] = False
        sync_state["last_error"] = stderr
    else:
        sync_state["last_push_at"] = datetime.now(timezone.utc).isoformat()
        sync_state["last_push_ok"] = True
        sync_state["last_error"] = None


async def _sync_cycle(notes_dir: Path) -> bool:
    """Run one sync cycle: commit → pull → push. Returns True if changes were committed."""
    committed = False
    if await _has_changes(notes_dir):
        committed = await _commit_all(notes_dir)

    if committed:
        await _pull(notes_dir)
        if not conflicted_files:
            await _push(notes_dir)

    return committed


async def _debounced_watcher(notes_dir: Path, debounce_seconds: float) -> None:
    """Watch notes dir for changes and sync after debounce period."""
    while True:
        try:
            if await _has_changes(notes_dir):
                # Debounce: wait for quiet period before committing
                await asyncio.sleep(debounce_seconds)
                # Check again — more changes may have arrived during debounce
                while await _has_changes(notes_dir):
                    await _sync_cycle(notes_dir)
                    # Brief pause to batch rapid consecutive changes
                    await asyncio.sleep(1)
                    if not await _has_changes(notes_dir):
                        break
            else:
                # No changes — poll again after a short interval
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in sync watcher")
            await asyncio.sleep(10)


async def _periodic_puller(notes_dir: Path, pull_interval_seconds: float) -> None:
    """Periodically pull from remote to catch external changes."""
    while True:
        try:
            await asyncio.sleep(pull_interval_seconds)
            await _pull(notes_dir)
            # Check if pull brought conflict resolutions
            await _update_conflict_state(notes_dir)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in periodic pull")
            await asyncio.sleep(pull_interval_seconds)


async def _update_conflict_state(notes_dir: Path) -> None:
    """Check if previously conflicted files have been resolved (async-safe)."""
    global conflicted_files
    if not conflicted_files:
        return

    def _scan() -> list[str]:
        still = []
        for fname in conflicted_files:
            fpath = notes_dir / fname
            if fpath.exists():
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    if "<<<<<<<" in content and ">>>>>>>" in content:
                        still.append(fname)
                except Exception:
                    pass
        return still

    conflicted_files = await asyncio.to_thread(_scan)


async def test_remote(remote_url: str, notes_dir: Path) -> tuple[bool, str]:
    """Test if a remote URL is accessible via git ls-remote. Returns (ok, message)."""
    await _ensure_git_repo(notes_dir)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            remote_url,
            cwd=str(notes_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            return True, "Connection successful"
        return False, stderr_bytes.decode().strip() or "Unknown error"
    except asyncio.TimeoutError:
        return False, "Connection timed out (15s)"
    except Exception as e:
        return False, str(e)


async def start_sync(notes_dir: Path) -> None:
    """Start the git sync watcher and periodic puller.

    Reads config from environment:
    - NOTES_GIT_SYNC: "true" to enable (default: off)
    - NOTES_GIT_REMOTE: remote URL (default: none)
    - NOTES_SYNC_DEBOUNCE: debounce duration (default: "20s")
    - NOTES_SYNC_PULL_INTERVAL: pull interval (default: "60s")
    """
    global _sync_task, _pull_task

    enabled = os.environ.get("NOTES_GIT_SYNC", "").lower() in ("true", "1", "yes")
    if not enabled:
        return

    remote = os.environ.get("NOTES_GIT_REMOTE", "")
    debounce = _parse_seconds(os.environ.get("NOTES_SYNC_DEBOUNCE", "20"), default=20.0)
    pull_interval = _parse_seconds(
        os.environ.get("NOTES_SYNC_PULL_INTERVAL", "60"), default=60.0
    )

    # Initialize repo
    await _ensure_git_repo(notes_dir)

    # Configure remote if provided
    if remote:
        await _configure_remote(notes_dir, remote)

    logger.info(
        "Git sync enabled: debounce=%.0fs, pull_interval=%.0fs, remote=%s",
        debounce,
        pull_interval,
        remote or "(none)",
    )

    # Pull immediately on startup to catch changes from other devices
    await _pull(notes_dir)

    _sync_task = asyncio.create_task(_debounced_watcher(notes_dir, debounce))
    _pull_task = asyncio.create_task(_periodic_puller(notes_dir, pull_interval))


async def stop_sync() -> None:
    """Stop the sync watcher and puller."""
    global _sync_task, _pull_task
    for task in (_sync_task, _pull_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _sync_task = None
    _pull_task = None
