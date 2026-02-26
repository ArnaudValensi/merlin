"""Git operations for the notes editor."""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()  # merlin/


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a command asynchronously, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd or REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def commit_and_push(filepath: Path, message: str) -> dict:
    """Git add, commit, and push a file.

    Returns {"committed": bool, "pushed": bool, "error": str | None}.
    """
    rel = filepath.relative_to(REPO_ROOT)

    # git add
    rc, _, err = await _run(["git", "add", str(rel)])
    if rc != 0:
        logger.error("git add failed: %s", err)
        return {"committed": False, "pushed": False, "error": f"git add failed: {err}"}

    # git commit
    rc, out, err = await _run(["git", "commit", "-m", message])
    if rc != 0:
        # Nothing to commit is fine (no changes)
        combined = out + err
        if "nothing to commit" in combined or "no changes added" in combined:
            return {"committed": False, "pushed": False, "error": None}
        logger.error("git commit failed: %s", err or out)
        return {"committed": False, "pushed": False, "error": f"git commit failed: {err or out}"}

    # git push (best effort)
    rc, _, err = await _run(["git", "push"])
    if rc != 0:
        logger.warning("git push failed: %s", err)
        return {"committed": True, "pushed": False, "error": f"push failed: {err}"}

    return {"committed": True, "pushed": True, "error": None}


async def delete_and_push(filepath: Path, message: str) -> dict:
    """Git rm, commit, and push a file.

    Returns {"committed": bool, "pushed": bool, "error": str | None}.
    """
    rel = filepath.relative_to(REPO_ROOT)

    # git rm
    rc, _, err = await _run(["git", "rm", str(rel)])
    if rc != 0:
        logger.error("git rm failed: %s", err)
        return {"committed": False, "pushed": False, "error": f"git rm failed: {err}"}

    # git commit
    rc, out, err = await _run(["git", "commit", "-m", message])
    if rc != 0:
        logger.error("git commit failed: %s", err or out)
        return {"committed": False, "pushed": False, "error": f"git commit failed: {err or out}"}

    # git push (best effort)
    rc, _, err = await _run(["git", "push"])
    if rc != 0:
        logger.warning("git push failed: %s", err)
        return {"committed": True, "pushed": False, "error": f"push failed: {err}"}

    return {"committed": True, "pushed": True, "error": None}


async def commit_and_push_files(filepaths: list[Path], message: str) -> dict:
    """Git add multiple files, commit, and push."""
    rels = [str(f.relative_to(REPO_ROOT)) for f in filepaths]

    for rel in rels:
        rc, _, err = await _run(["git", "add", rel])
        if rc != 0:
            return {"committed": False, "pushed": False, "error": f"git add {rel} failed: {err}"}

    rc, out, err = await _run(["git", "commit", "-m", message])
    if rc != 0:
        combined = out + err
        if "nothing to commit" in combined or "no changes added" in combined:
            return {"committed": False, "pushed": False, "error": None}
        return {"committed": False, "pushed": False, "error": f"git commit failed: {err or out}"}

    rc, _, err = await _run(["git", "push"])
    if rc != 0:
        return {"committed": True, "pushed": False, "error": f"push failed: {err}"}

    return {"committed": True, "pushed": True, "error": None}
