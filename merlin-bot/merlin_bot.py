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

from lib.agent_context import compose
from lib.engine import invoke
from discord_send import create_thread_from_message, load_token, send_message
from structured_log import log_event
from transcribe import transcribe

# The engine has no notion of Discord. The bot selects the managed-assistant
# recipe (brain + personality + user memory + Discord overlay, composed by
# lib/agent_context.py) and passes the result through invoke(); the bot
# handler captures engine output and delivers it to Discord.
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

load_dotenv(paths.bot_config_path())

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_IDS: set[str] = set()

_raw_channels = os.getenv("DISCORD_CHANNEL_IDS", "")
if _raw_channels.strip():
    DISCORD_CHANNEL_IDS = {ch.strip() for ch in _raw_channels.split(",") if ch.strip()}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Bot logger — inherits file handler from main.py's merlin.* hierarchy
logger = logging.getLogger("merlin.bot")

# Suppress noisy discord.py loggers
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

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


def _chunk_message(text: str, max_len: int = 1900) -> list[str]:
    """Split a message into chunks that fit Discord's 2000 char limit.

    Tries to split on newlines first, then falls back to hard splits.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to split on a newline near the limit
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            # No good newline found — hard split
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def _generate_thread_title(content: str) -> str:
    """Generate a short thread title from engine response.

    Takes the first sentence or first 60 chars, whichever is shorter.
    """
    # Take first line
    first_line = content.strip().split("\n")[0].strip()
    # Remove markdown formatting
    for prefix in ("**", "##", "# ", "- ", "* "):
        first_line = first_line.removeprefix(prefix)
    first_line = first_line.removesuffix("**")
    # Truncate
    if len(first_line) > 60:
        first_line = first_line[:57] + "..."
    return first_line or "Conversation"


def session_id_for_channel(channel_id: str | int) -> str:
    """Derive a deterministic UUID session ID from a channel ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"discord-channel-{channel_id}"))


def session_id_for_thread(thread_id: str | int) -> str:
    """Derive a deterministic UUID session ID from a thread ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"discord-thread-{thread_id}"))


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

# Per-channel lock to prevent two messages creating two threads simultaneously
_channel_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

intents = discord.Intents.default()
intents.message_content = True

# Suppress "PyNaCl is not installed" — we don't use Discord voice
logging.getLogger("discord.client").addFilter(lambda r: "PyNaCl" not in r.getMessage())
client = discord.Client(intents=intents)

# Guard: Discord may fire on_ready multiple times (reconnects); only run the
# one-time init on the first firing.
_ready_init_done = False


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
            logger.debug(
                "Found registered session for thread %s: %s", thread_id, session
            )
            return thread_id, parent_id, session, False

        # Check if thread starter message has a session (cron continuation)
        # Thread ID equals the starter message ID for message-created threads
        starter_session = get_message_session(thread_id)
        if starter_session:
            logger.info(
                "Cron continuation: thread %s → session %s", thread_id, starter_session
            )
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

    request_id = str(uuid.uuid4())

    author = message.author.display_name
    content_preview = message.content[:80] + (
        "..." if len(message.content) > 80 else ""
    )
    logger.info(
        "[%s] Message from %s in %s: %s",
        request_id[:8],
        author,
        message.channel.id,
        content_preview,
    )
    log_event(
        "bot_event",
        event="message_received",
        details=f"Message from {author} in {message.channel.id}",
        content=message.content,
        request_id=request_id,
    )

    # Resolve thread and session
    try:
        thread_id, parent_id, session, is_new_thread = await _resolve_session(
            message, allowed_channel
        )
    except Exception:
        logger.exception("Failed to resolve session for message %s", message.id)
        log_event(
            "bot_event",
            event="error",
            details=f"Failed to resolve session for message {message.id}",
        )
        try:
            await message.add_reaction("\N{CROSS MARK}")
        except discord.HTTPException:
            pass
        return

    # Transcribe voice messages
    transcription: str | None = None
    if message.flags.voice and message.attachments:
        attachment = message.attachments[0]
        logger.info(
            "Voice message from %s, transcribing (%s)...", author, attachment.filename
        )
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
            logger.info(
                "Transcription (%.1fs): %s",
                t_duration,
                transcription[:120] if transcription else "(empty)",
            )
            log_event(
                "bot_event",
                event="transcription",
                details=f"Voice from {author} ({t_duration:.1f}s): {transcription}",
                duration=round(t_duration, 2),
                content=transcription,
                author=author,
            )
        except Exception:
            logger.exception("Failed to transcribe voice message %s", message.id)
            log_event(
                "bot_event",
                event="error",
                details=f"Voice transcription failed for {author}",
            )
            transcription = "[transcription failed]"
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if client.user is not None:
            try:
                await message.remove_reaction("\N{MICROPHONE}", client.user)
            except discord.HTTPException:
                pass
        # Post transcription to thread so the user can see what was heard
        if transcription and transcription != "[transcription failed]":
            try:
                token = load_token()
                await asyncio.to_thread(
                    send_message,
                    thread_id,
                    f"> 🎤 *{transcription}* ({t_duration:.1f}s)",
                    token,
                )
            except Exception:
                logger.warning(
                    "Could not send transcription message to thread %s", thread_id
                )

    prompt = build_prompt(
        message,
        thread_id=thread_id,
        parent_id=parent_id,
        transcription=transcription,
        is_new_thread=is_new_thread,
    )

    # Processing indicator: 🤔 while working, ✅ on success, ❌ on error
    try:
        await message.add_reaction("\N{THINKING FACE}")
    except discord.HTTPException:
        logger.warning("Could not add thinking reaction to message %s", message.id)

    try:
        start = time.monotonic()
        # cwd means "where the agent operates": the launch dir (no per-job
        # working_dir concept for the bot). Merlin context arrives by
        # injection and the skill adapters, not by cwd.
        result = await asyncio.to_thread(
            invoke,
            prompt,
            caller="discord",
            session_id=session,
            request_id=request_id,
            cwd=paths.launch_cwd(),
            append_system_prompt=compose("managed-assistant"),
        )

        duration = time.monotonic() - start
        logger.info(
            "Engine returned exit_code=%d duration=%.1fs session=%s",
            result.exit_code,
            duration,
            result.session_id,
        )

        if result.exit_code != 0:
            logger.error("Engine error (exit %d): %s", result.exit_code, result.stderr)
            done_emoji = "\N{CROSS MARK}"
        else:
            done_emoji = "\N{WHITE HEAVY CHECK MARK}"

        # Send engine response to Discord (bot handles delivery, not the engine)
        if result.content and result.content.strip():
            try:
                token = load_token()
                content = result.content.strip()
                # Discord has a 2000 char limit per message — chunk if needed
                chunks = _chunk_message(content, max_len=1900)
                for chunk in chunks:
                    await asyncio.to_thread(send_message, thread_id, chunk, token)
            except Exception:
                logger.warning("Could not send response to thread %s", thread_id)

        # Rename thread for new conversations
        if is_new_thread and result.content:
            try:
                title = _generate_thread_title(result.content)
                token = load_token()
                from discord_send import rename_thread

                await asyncio.to_thread(rename_thread, thread_id, title, token)
            except Exception:
                logger.warning("Could not rename thread %s", thread_id)

    except Exception:
        logger.exception("Exception invoking engine for message %s", message.id)
        log_event(
            "bot_event",
            event="error",
            details=f"Exception invoking engine for message {message.id}",
        )
        done_emoji = "\N{CROSS MARK}"

    try:
        if client.user is not None:
            await message.remove_reaction("\N{THINKING FACE}", client.user)
        await message.add_reaction(done_emoji)
    except discord.HTTPException:
        logger.warning("Could not update reaction on message %s", message.id)


def _validate_config() -> None:
    """Validate required configuration. Fails fast with a helpful message."""
    env_path = paths.bot_config_path()
    errors: list[str] = []

    if not env_path.exists():
        errors.insert(
            0,
            f"Config file not found at {env_path}\n"
            f"  Run the setup wizard to create it:\n"
            f"    merlin setup",
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

    # Validate the configured engine
    from lib.engine import get_engine

    try:
        engine = get_engine()
        engine_error = engine.validate()
        if engine_error:
            errors.append(
                f"Engine '{engine.name}' validation failed:\n  {engine_error}"
            )
    except ValueError as e:
        errors.append(str(e))

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
        msg = "Configuration error(s):\n\n" + "\n\n".join(
            f"  {i + 1}. {e}" for i, e in enumerate(errors)
        )
        logger.error(msg)
        print(msg, file=__import__("sys").stderr)
        raise SystemExit(1)


async def start_bot() -> None:
    """Start Discord client. Called by main.py plugin."""

    @client.event
    async def on_ready() -> None:
        global _ready_init_done
        if not _ready_init_done:
            _ready_init_done = True
            import merlin_app

            merlin_app.BOT_START_TIME = datetime.now(timezone.utc)
            guilds = [g.name for g in client.guilds]
            logger.info("Bot ready as %s | guilds: %s", client.user, guilds)
            logger.info("Listening in channels: %s", DISCORD_CHANNEL_IDS)
            log_event("bot_event", event="ready", details=f"Bot ready as {client.user}")

    await client.start(DISCORD_BOT_TOKEN)


def main() -> None:
    """Standalone entry point (dev mode)."""
    _validate_config()

    @client.event
    async def on_ready() -> None:
        global _ready_init_done
        if not _ready_init_done:
            _ready_init_done = True
            import merlin_app

            merlin_app.BOT_START_TIME = datetime.now(timezone.utc)
            guilds = [g.name for g in client.guilds]
            logger.info("Bot ready as %s | guilds: %s", client.user, guilds)
            logger.info("Listening in channels: %s", DISCORD_CHANNEL_IDS)
            log_event("bot_event", event="ready", details=f"Bot ready as {client.user}")

    client.run(DISCORD_BOT_TOKEN, log_handler=None)


# ---------------------------------------------------------------------------
# Plugin interface — used when main.py does `import merlin_bot as bot_plugin`
# (merlin-bot/ is on sys.path, so `import merlin_bot` finds this file)
# ---------------------------------------------------------------------------

from merlin_app import (  # noqa: F401
    merlin_app_router as router,
    MERLIN_APP_NAV_ITEMS as NAV_ITEMS,
    MERLIN_APP_STATIC_DIR as STATIC_DIR,
)

EXTENSION_META = {
    "name": "Merlin Bot",
    "description": "Discord AI assistant powered by Claude Code",
    "icon": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/></svg>',
    "config_fields": [
        {
            "key": "DISCORD_BOT_TOKEN",
            "label": "Discord Bot Token",
            "secret": True,
            "required": True,
        },
        {
            "key": "DISCORD_CHANNEL_IDS",
            "label": "Discord Channel IDs (comma-separated)",
            "secret": False,
            "required": True,
        },
    ],
}


def validate():
    """Validate bot configuration. Raises SystemExit on errors."""
    _validate_config()


async def start():
    """Start Discord client + cron scheduler."""
    await start_bot()


def notify(channel_id: str, message: str, *, session_id: str | None = None) -> None:
    """Send a notification to a Discord channel. Called by cron and other core modules.

    If *session_id* is provided, the message is registered with that session
    so replies can resume the conversation. Long messages automatically create
    a thread from the first message.
    """
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.warning("Cannot notify: DISCORD_BOT_TOKEN not configured")
        return
    send_message(
        channel_id, message, token, thread_on_chunk=True, session_id=session_id
    )


if __name__ == "__main__":
    main()
