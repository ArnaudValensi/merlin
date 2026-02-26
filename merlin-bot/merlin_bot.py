# /// script
# dependencies = [
#   "discord.py",
#   "python-dotenv",
#   "httpx",
#   "fastapi",
#   "uvicorn[standard]",
#   "jinja2",
#   "faster-whisper",
#   "python-multipart",
# ]
# ///
"""Merlin Discord bot — listens for messages and feeds them to Claude Code.

Every conversation happens in a Discord thread:
- Channel messages create a new thread → new Claude session
- Thread messages continue the existing session
- Threading on a bot/cron message resumes that session
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

from claude_wrapper import invoke_claude
from discord_send import create_thread_from_message, load_token, send_message
from structured_log import log_event
from transcribe import transcribe
from session_registry import (
    get_message_session,
    get_thread_session,
    set_thread_session,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

import paths

_SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = paths.logs_dir()

load_dotenv(paths.bot_config_path())

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_IDS: set[str] = set()

_raw_channels = os.getenv("DISCORD_CHANNEL_IDS", "")
if _raw_channels.strip():
    DISCORD_CHANNEL_IDS = {ch.strip() for ch in _raw_channels.split(",") if ch.strip()}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

# Bot logger with [bot] prefix
logger = logging.getLogger("merlin")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(LOG_DIR / "merlin.log", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(
    logging.Formatter("[bot]       %(message)s")
)
logger.addHandler(_console_handler)
logger.propagate = False  # Don't propagate to root logger (prevents duplicate lines)

# Tunnel logger with [tunnel] prefix
tunnel_logger = logging.getLogger("tunnel")
tunnel_logger.setLevel(logging.INFO)
_tunnel_console = logging.StreamHandler()
_tunnel_console.setFormatter(logging.Formatter("[tunnel]    %(message)s"))
tunnel_logger.addHandler(_tunnel_console)
tunnel_logger.addHandler(_file_handler)
tunnel_logger.propagate = False

# Dashboard logger with [dashboard] prefix (for uvicorn)
dashboard_logger = logging.getLogger("uvicorn.access")
dashboard_logger.setLevel(logging.INFO)
_dashboard_console = logging.StreamHandler()
_dashboard_console.setLevel(logging.INFO)
_dashboard_console.setFormatter(
    logging.Formatter("[dashboard] %(message)s")
)
dashboard_logger.addHandler(_dashboard_console)
dashboard_logger.propagate = False  # Don't propagate to root logger

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def build_prompt(
    message: discord.Message,
    *,
    thread_id: str,
    parent_id: str,
    transcription: str | None = None,
    is_new_thread: bool = False,
) -> str:
    """Build the rich prompt that Claude receives for a Discord message.

    Thread ID and channel ID are explicit so Claude uses the thread for replies
    but knows the real channel ID for things like cron job creation.

    If *transcription* is provided, the message is formatted as a voice message
    with the transcribed audio text.

    If *is_new_thread* is True, a ``[New thread]`` tag is prepended so Claude
    knows to rename the thread with a descriptive title.
    """
    author = message.author.display_name
    message_id = str(message.id)
    content = message.content
    new_thread_tag = "[New thread]\n" if is_new_thread else ""

    if transcription is not None:
        header = (
            f'[Discord voice message from "{author}" in thread {thread_id},'
            f" channel {parent_id}, message ID {message_id}]"
        )
        parts = [f"{new_thread_tag}{header}", f"[Transcribed audio]: {transcription}"]
        if content:
            parts.append(content)
        return "\n".join(parts)

    return (
        f'{new_thread_tag}[Discord message from "{author}" in thread {thread_id},'
        f" channel {parent_id}, message ID {message_id}]\n"
        f"{content}"
    )


def session_id_for_channel(channel_id: str | int) -> str:
    """Derive a deterministic UUID session ID from a channel ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"discord-channel-{channel_id}"))


def session_id_for_thread(thread_id: str | int) -> str:
    """Derive a deterministic UUID session ID from a thread ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"discord-thread-{thread_id}"))


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

# Sessions we know don't exist yet (need --session-id instead of --resume)
_new_sessions: set[str] = set()

# Per-channel lock to prevent two messages creating two threads simultaneously
_channel_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

intents = discord.Intents.default()
intents.message_content = True

# Suppress "PyNaCl is not installed" — we don't use Discord voice
logging.getLogger("discord.client").addFilter(
    lambda r: "PyNaCl" not in r.getMessage()
)
client = discord.Client(intents=intents)


def _resolve_allowed_channel(message: discord.Message) -> str | None:
    """Return the allowed channel ID, or None if this message should be ignored.

    For threads, checks the parent channel against the allowlist.
    For regular channels, checks the channel itself.
    """
    if isinstance(message.channel, discord.Thread):
        parent_id = str(message.channel.parent_id)
        return parent_id if parent_id in DISCORD_CHANNEL_IDS else None
    channel_id = str(message.channel.id)
    return channel_id if channel_id in DISCORD_CHANNEL_IDS else None


async def _resolve_session(
    message: discord.Message, allowed_channel: str
) -> tuple[str, str, str, bool]:
    """Determine thread_id, parent_id, session_id, and is_new_thread for this message.

    For thread messages: look up existing session or create deterministic one.
    For channel messages: create a new thread from the message.

    Returns (thread_id, parent_id, session_id, is_new_thread).
    """
    if isinstance(message.channel, discord.Thread):
        thread_id = str(message.channel.id)
        parent_id = str(message.channel.parent_id)

        # Check registry for existing session (handles cron continuation)
        session = get_thread_session(thread_id)
        if session:
            logger.debug("Found registered session for thread %s: %s", thread_id, session)
            return thread_id, parent_id, session, False

        # Check if thread starter message has a session (cron continuation)
        # Thread ID equals the starter message ID for message-created threads
        starter_session = get_message_session(thread_id)
        if starter_session:
            logger.info("Cron continuation: thread %s → session %s", thread_id, starter_session)
            set_thread_session(thread_id, starter_session)
            return thread_id, parent_id, starter_session, False

        # New thread with no registered session — generate deterministic one
        session = session_id_for_thread(thread_id)
        set_thread_session(thread_id, session)
        logger.info("New session for thread %s: %s", thread_id, session)
        return thread_id, parent_id, session, False

    # Channel message — create a thread
    channel_id = str(message.channel.id)
    async with _channel_locks[channel_id]:
        thread_name = message.content[:80] or "Conversation"
        token = load_token()
        thread_data = await asyncio.to_thread(
            create_thread_from_message,
            channel_id,
            str(message.id),
            thread_name,
            token,
        )
        thread_id = str(thread_data["id"])
        session = session_id_for_thread(thread_id)
        set_thread_session(thread_id, session)
        logger.info(
            "Created thread %s from message %s, session %s",
            thread_id,
            message.id,
            session,
        )
        return thread_id, channel_id, session, True


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore bots (including ourselves)
    if message.author.bot:
        return

    # Ignore system messages (thread starters, pins, joins, etc.)
    # Thread starter messages have author=thread creator but can't be
    # used with create_thread_from_message, causing spurious ❌ reactions.
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return

    # Check allowlist (parent channel for threads, channel itself otherwise)
    allowed_channel = _resolve_allowed_channel(message)
    if allowed_channel is None:
        return

    author = message.author.display_name
    content_preview = message.content[:80] + ("..." if len(message.content) > 80 else "")
    logger.info("Message from %s in %s: %s", author, message.channel.id, content_preview)
    log_event("bot_event", event="message_received",
              details=f"Message from {author} in {message.channel.id}",
              content=message.content)

    # Resolve thread and session
    try:
        thread_id, parent_id, session, is_new_thread = await _resolve_session(message, allowed_channel)
    except Exception:
        logger.exception("Failed to resolve session for message %s", message.id)
        log_event("bot_event", event="error",
                  details=f"Failed to resolve session for message {message.id}")
        try:
            await message.add_reaction("\N{CROSS MARK}")
        except discord.HTTPException:
            pass
        return

    # Transcribe voice messages
    transcription: str | None = None
    if message.flags.voice and message.attachments:
        attachment = message.attachments[0]
        logger.info("Voice message from %s, transcribing (%s)...", author, attachment.filename)
        try:
            await message.add_reaction("\N{MICROPHONE}")
        except discord.HTTPException:
            pass
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
                audio_bytes = await attachment.read()
                tmp.write(audio_bytes)
            t_start = time.monotonic()
            transcription = await asyncio.to_thread(transcribe, tmp_path)
            t_duration = time.monotonic() - t_start
            logger.info("Transcription (%.1fs): %s", t_duration, transcription[:120] if transcription else "(empty)")
            log_event("bot_event", event="transcription",
                      details=f"Voice from {author} ({t_duration:.1f}s): {transcription}",
                      duration=round(t_duration, 2),
                      content=transcription,
                      author=author)
        except Exception:
            logger.exception("Failed to transcribe voice message %s", message.id)
            log_event("bot_event", event="error",
                      details=f"Voice transcription failed for {author}")
            transcription = "[transcription failed]"
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        try:
            await message.remove_reaction("\N{MICROPHONE}", client.user)
        except discord.HTTPException:
            pass
        # Post transcription to thread so the user can see what was heard
        if transcription and transcription != "[transcription failed]":
            try:
                token = load_token()
                await asyncio.to_thread(
                    send_message, thread_id, f"> 🎤 *{transcription}* ({t_duration:.1f}s)", token
                )
            except Exception:
                logger.warning("Could not send transcription message to thread %s", thread_id)

    prompt = build_prompt(message, thread_id=thread_id, parent_id=parent_id, transcription=transcription, is_new_thread=is_new_thread)

    # Processing indicator: 🤔 while working, ✅ on success, ❌ on error
    try:
        await message.add_reaction("\N{THINKING FACE}")
    except discord.HTTPException:
        logger.warning("Could not add thinking reaction to message %s", message.id)

    try:
        # Try --resume first (works after restart if session exists on disk).
        # If session doesn't exist, fall back to --session-id to create it.
        should_create = session in _new_sessions
        start = time.monotonic()
        result = await asyncio.to_thread(
            invoke_claude,
            prompt,
            caller="discord",
            session_id=session,
            resume=not should_create,
        )

        # If resume failed (session not found), retry with --session-id
        if not should_create and result.exit_code != 0 and "No conversation found" in result.stderr:
            logger.info("Session %s not found, creating new session", session)
            result = await asyncio.to_thread(
                invoke_claude,
                prompt,
                caller="discord",
                session_id=session,
                resume=False,
            )

        duration = time.monotonic() - start
        if result.exit_code == 0:
            _new_sessions.discard(session)
        elif "already in use" in result.stderr:
            pass
        elif "No conversation found" in result.stderr:
            _new_sessions.add(session)
        logger.info(
            "Claude returned exit_code=%d duration=%.1fs session=%s",
            result.exit_code,
            duration,
            result.session_id,
        )
        if result.exit_code != 0:
            logger.error("Claude error (exit %d): %s", result.exit_code, result.stderr)
            done_emoji = "\N{CROSS MARK}"
        else:
            done_emoji = "\N{WHITE HEAVY CHECK MARK}"
    except Exception:
        logger.exception("Exception invoking Claude for message %s", message.id)
        log_event("bot_event", event="error",
                  details=f"Exception invoking Claude for message {message.id}")
        done_emoji = "\N{CROSS MARK}"

    try:
        await message.remove_reaction("\N{THINKING FACE}", client.user)
        await message.add_reaction(done_emoji)
    except discord.HTTPException:
        logger.warning("Could not update reaction on message %s", message.id)


def _validate_config() -> None:
    """Validate required configuration. Fails fast with a helpful message."""
    env_path = paths.bot_config_path()
    errors: list[str] = []

    if not env_path.exists():
        errors.insert(0,
            f"Config file not found at {env_path}\n"
            f"  Run the setup wizard to create it:\n"
            f"    merlin setup"
        )

    if not DISCORD_BOT_TOKEN:
        errors.append(
            "DISCORD_BOT_TOKEN is not set.\n"
            "  Get your bot token from https://discord.com/developers/applications\n"
            "  Then add it to your .env file:\n"
            f"    echo 'DISCORD_BOT_TOKEN=your-token-here' >> {env_path}"
        )

    if not DISCORD_CHANNEL_IDS:
        errors.append(
            "DISCORD_CHANNEL_IDS is not set.\n"
            "  Find your channel ID: right-click a channel in Discord → Copy Channel ID\n"
            "  (requires Developer Mode: Settings → Advanced → Developer Mode)\n"
            "  Then add it to your .env file:\n"
            f"    echo 'DISCORD_CHANNEL_IDS=123456789' >> {env_path}"
        )

    if not shutil.which("claude"):
        errors.append(
            "claude CLI not found on PATH.\n"
            "  Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"
        )

    if not shutil.which("uv"):
        errors.append(
            "uv not found on PATH.\n"
            "  Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )

    if not shutil.which("ffmpeg"):
        errors.append(
            "ffmpeg not found on PATH.\n"
            "  Required for voice message transcription.\n"
            "  Install: sudo pacman -S --noconfirm ffmpeg"
        )

    if errors:
        msg = "Configuration error(s):\n\n" + "\n\n".join(f"  {i+1}. {e}" for i, e in enumerate(errors))
        logger.error(msg)
        print(msg, file=__import__("sys").stderr)
        raise SystemExit(1)


async def _run_cron_runner() -> None:
    """Run cron_runner.py as a subprocess and alert on crashes."""
    start = datetime.now()

    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "cron_runner.py",
        cwd=str(_SCRIPT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()
    duration = (datetime.now() - start).total_seconds()

    if proc.returncode != 0:
        error_msg = stderr.decode()[:500]

        # Log to dashboard
        log_event(
            "cron_runner_crash",
            exit_code=proc.returncode,
            duration=duration,
            stderr=error_msg,
        )

        # Alert via Discord
        if DISCORD_CHANNEL_IDS:
            default_channel_id = int(list(DISCORD_CHANNEL_IDS)[0])
            channel = client.get_channel(default_channel_id)
            if channel:
                await channel.send(
                    f"🚨 **Cron runner crashed** (exit {proc.returncode})\n"
                    f"```\n{error_msg}\n```"
                )


async def _cron_scheduler() -> None:
    """Run cron_runner.py at the start of every minute (replaces cron)."""
    cron_logger = logging.getLogger("cron")
    cron_logger.setLevel(logging.INFO)
    _cron_console = logging.StreamHandler()
    _cron_console.setFormatter(logging.Formatter("[cron]      %(message)s"))
    cron_logger.addHandler(_cron_console)
    cron_logger.propagate = False

    cron_logger.info("Cron scheduler started")

    while True:
        # Sleep until the next minute starts
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)

        # Fire and forget - run in background
        asyncio.create_task(_run_cron_runner())


async def start_bot() -> None:
    """Start Discord client + cron scheduler. Called by main.py plugin."""
    # Suppress discord.py's default logging handler (normally done by client.run)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    @client.event
    async def on_ready() -> None:
        if not hasattr(client, "_ready_done"):
            client._ready_done = True
            import merlin_app
            merlin_app.BOT_START_TIME = datetime.now(timezone.utc)
            guilds = [g.name for g in client.guilds]
            logger.info("Bot ready as %s | guilds: %s", client.user, guilds)
            logger.info("Listening in channels: %s", DISCORD_CHANNEL_IDS)
            log_event("bot_event", event="ready", details=f"Bot ready as {client.user}")
            asyncio.create_task(_cron_scheduler())

    await client.start(DISCORD_BOT_TOKEN)


def main() -> None:
    """Standalone entry point (dev mode)."""
    _validate_config()

    @client.event
    async def on_ready() -> None:
        if not hasattr(client, "_ready_done"):
            client._ready_done = True
            import merlin_app
            merlin_app.BOT_START_TIME = datetime.now(timezone.utc)
            guilds = [g.name for g in client.guilds]
            logger.info("Bot ready as %s | guilds: %s", client.user, guilds)
            logger.info("Listening in channels: %s", DISCORD_CHANNEL_IDS)
            log_event("bot_event", event="ready", details=f"Bot ready as {client.user}")
            asyncio.create_task(_cron_scheduler())

    client.run(DISCORD_BOT_TOKEN, log_handler=None)


# ---------------------------------------------------------------------------
# Plugin interface — used when main.py does `import merlin_bot as bot_plugin`
# (merlin-bot/ is on sys.path, so `import merlin_bot` finds this file)
# ---------------------------------------------------------------------------

from merlin_app import (
    merlin_app_router as router,
    MERLIN_APP_NAV_ITEMS as NAV_ITEMS,
    MERLIN_APP_STATIC_DIR as STATIC_DIR,
)


async def on_tunnel_url(url: str) -> None:
    """Send the tunnel URL to Discord via REST API (no subprocess)."""
    channel = os.getenv("DISCORD_CHANNEL_IDS", "").split(",")[0].strip()
    if not channel:
        return
    dashboard_pass = os.getenv("DASHBOARD_PASS", "")
    msg = f"Dashboard is live at {url}" if dashboard_pass else f"Dashboard is live at {url} (no password)"
    token = load_token()
    await asyncio.to_thread(send_message, channel, msg, token)


def validate():
    """Validate bot configuration. Raises SystemExit on errors."""
    _validate_config()


async def start():
    """Start Discord client + cron scheduler."""
    await start_bot()


if __name__ == "__main__":
    main()
