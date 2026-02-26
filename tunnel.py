# /// script
# dependencies = []
# ///
"""Cloudflare Tunnel manager — exposes the dashboard over HTTPS.

Two modes:
  - Quick Tunnel (default): No config needed. Generates a random
    https://<random>.trycloudflare.com URL. Changes on each restart.
  - Named Tunnel: Set TUNNEL_TOKEN in .env for a stable subdomain.

Usage:
    # Quick Tunnel (automatic)
    TUNNEL_ENABLED=true  (default)

    # Named Tunnel
    TUNNEL_TOKEN=eyJ...
    TUNNEL_HOSTNAME=merlin.example.com

    # Disable
    TUNNEL_ENABLED=false
"""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger("tunnel")

# Module-level state
_public_url: str | None = None
_process: asyncio.subprocess.Process | None = None
_status: str = "stopped"  # stopped | starting | running | error


def get_public_url() -> str | None:
    """Return the current public tunnel URL, or None if not running."""
    return _public_url


def get_status() -> str:
    """Return tunnel status: stopped, starting, running, or error."""
    return _status


async def start_tunnel(
    *,
    port: int = 3123,
    tunnel_token: str = "",
    tunnel_hostname: str = "",
    max_restarts: int = 5,
    restart_delay: float = 5.0,
    on_url: object = None,
) -> str | None:
    """Start the cloudflared tunnel, monitor it, and restart on crash.

    Returns the public URL after the first successful start, or None if
    the tunnel fails to start. Continues monitoring and restarting in the
    background — this function does not return until the tunnel gives up
    after *max_restarts* consecutive failures.

    For Quick Tunnel: parses the URL from cloudflared's stderr output.
    For Named Tunnel: returns the configured hostname.
    """
    if tunnel_token:
        return await _run_tunnel_loop(
            _launch_named_tunnel,
            tunnel_token=tunnel_token,
            tunnel_hostname=tunnel_hostname,
            max_restarts=max_restarts,
            restart_delay=restart_delay,
            on_url=on_url,
        )
    else:
        return await _run_tunnel_loop(
            _launch_quick_tunnel,
            port=port,
            max_restarts=max_restarts,
            restart_delay=restart_delay,
            on_url=on_url,
        )


async def stop_tunnel() -> None:
    """Stop the tunnel process cleanly."""
    global _process, _status, _public_url

    if _process and _process.returncode is None:
        logger.info("Stopping tunnel process (pid=%d)", _process.pid)
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Tunnel process didn't exit, killing")
            _process.kill()
            await _process.wait()

    _process = None
    _status = "stopped"
    _public_url = None


# ---------------------------------------------------------------------------
# Internal: tunnel launch + monitoring loop
# ---------------------------------------------------------------------------


async def _run_tunnel_loop(launch_fn, *, max_restarts: int, restart_delay: float, on_url=None, **kwargs) -> str | None:
    """Generic tunnel lifecycle: launch, get URL, monitor, restart on crash.

    *launch_fn* should start the cloudflared process and return (url, process).
    *on_url* is an optional async callback called with the URL on first success.
    """
    global _public_url, _process, _status

    _status = "starting"
    restarts = 0
    first_url: str | None = None

    while restarts <= max_restarts:
        if restarts > 0:
            delay = restart_delay * (2 ** (restarts - 1))  # exponential backoff
            logger.warning(
                "Tunnel crashed, restarting in %.0fs (attempt %d/%d)",
                delay, restarts, max_restarts,
            )
            await asyncio.sleep(delay)

        url, proc = await launch_fn(**kwargs)
        _process = proc

        if url:
            _public_url = url
            _status = "running"
            if first_url is None:
                first_url = url
                if on_url:
                    try:
                        await on_url(url)
                    except Exception:
                        logger.debug("on_url callback failed", exc_info=True)

            # Block until the process exits
            await proc.wait()

            logger.warning("Tunnel process exited (code=%s)", proc.returncode)
            _status = "error"
            _public_url = None
            restarts += 1
        else:
            logger.error("Failed to start tunnel")
            _status = "error"
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            restarts += 1

    logger.error("Tunnel gave up after %d restarts", max_restarts)
    _status = "error"
    _public_url = None
    return first_url


async def _launch_quick_tunnel(*, port: int) -> tuple[str | None, asyncio.subprocess.Process]:
    """Launch a Quick Tunnel subprocess. Returns (url, process)."""
    logger.info("Starting Quick Tunnel on port %d", port)

    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    url = await _parse_url_from_stderr(proc)
    if url:
        logger.info("Quick Tunnel active: %s", url)
    return url, proc


async def _launch_named_tunnel(
    *, tunnel_token: str, tunnel_hostname: str,
) -> tuple[str | None, asyncio.subprocess.Process]:
    """Launch a Named Tunnel subprocess. Returns (url, process)."""
    logger.info("Starting Named Tunnel%s", f" ({tunnel_hostname})" if tunnel_hostname else "")

    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "run", "--token", tunnel_token,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    url = f"https://{tunnel_hostname}" if tunnel_hostname else None
    if url:
        logger.info("Named Tunnel active: %s", url)
    return url, proc


async def _parse_url_from_stderr(proc: asyncio.subprocess.Process) -> str | None:
    """Read cloudflared stderr line-by-line until the tunnel URL appears.

    The URL appears in a line like:
        +-----------------------------------------------------------+
        |  Your quick Tunnel has been created! Visit it at ...      |
        |  https://random-thing.trycloudflare.com                   |
        +-----------------------------------------------------------+

    Returns the URL or None after timeout.
    """
    url_pattern = re.compile(r"https://[a-zA-Z0-9._-]+\.trycloudflare\.com")

    try:
        async with asyncio.timeout(30):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("cloudflared: %s", text)
                match = url_pattern.search(text)
                if match:
                    # Keep draining stderr in background so the pipe doesn't block
                    asyncio.create_task(_drain_stderr(proc))
                    return match.group(0)
    except TimeoutError:
        logger.error("Timed out waiting for tunnel URL (30s)")
    return None


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Drain stderr to prevent pipe buffer from blocking cloudflared."""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.debug("cloudflared: %s", text)
    except Exception:
        pass
