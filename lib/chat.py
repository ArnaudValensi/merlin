"""Chat channel CLI: send, reply, and react to messages from any cwd.

This is the channel-agnostic face of Merlin's message delivery (chat-channel
epic, decision D5). The transport is Discord-only for now; when the
ChatChannel abstraction lands, the transport swaps without renaming the
command. Config (bot token) resolves via paths.py, never the cwd.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import paths
from argparse_help import HelpfulParser


def _transport():
    """Import the active chat transport (Discord for now).

    The transport module lives in the merlin-bot built-in; resolve it via
    paths.app_dir() so this works installed and in dev mode, from any cwd.
    """
    bot_dir = str(paths.app_dir() / "merlin-bot")
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    import discord_send

    return discord_send


def _print_results(results: list[dict]) -> None:
    if len(results) == 1:
        print(json.dumps(results[0]))
    else:
        print(json.dumps(results))


def cmd_send(args: argparse.Namespace) -> None:
    """Handle the ``send`` subcommand."""
    transport = _transport()
    token = transport.load_token()
    files = [Path(f) for f in args.file] if args.file else None
    _print_results(
        transport.send_message(
            args.channel,
            args.content or "",
            token,
            files=files,
            thread_on_chunk=args.thread_on_chunk,
        )
    )


def cmd_reply(args: argparse.Namespace) -> None:
    """Handle the ``reply`` subcommand."""
    transport = _transport()
    token = transport.load_token()
    files = [Path(f) for f in args.file] if args.file else None
    _print_results(
        transport.reply_message(
            args.channel, args.message, args.content or "", token, files=files
        )
    )


def cmd_react(args: argparse.Namespace) -> None:
    """Handle the ``react`` subcommand."""
    transport = _transport()
    token = transport.load_token()
    transport.react_message(args.channel, args.message, args.emoji, token)
    print(json.dumps({"ok": True}))


def cmd_rename_thread(args: argparse.Namespace) -> None:
    """Handle the ``rename-thread`` subcommand."""
    transport = _transport()
    token = transport.load_token()
    data = transport.rename_thread(args.thread, args.name, token)
    print(json.dumps({"ok": True, "thread_id": data["id"], "name": data["name"]}))


def main(argv: list[str] | None = None) -> None:
    parser = HelpfulParser(
        prog="merlin chat",
        description="Send messages, replies, and reactions to the chat channel (Discord).",
        epilog="""
Examples:
  # Send a message to a channel
  merlin chat send --channel YOUR_CHANNEL_ID --content "Hello!"

  # Send a message with an image
  merlin chat send --channel YOUR_CHANNEL_ID --content "Screenshot:" --file screenshot.png

  # Send just a file (no text)
  merlin chat send --channel YOUR_CHANNEL_ID --file report.pdf

  # Reply with an attachment
  merlin chat reply --channel YOUR_CHANNEL_ID --message 123456789 --content "Here you go" --file result.png

  # React to a message
  merlin chat react --channel YOUR_CHANNEL_ID --message 123456789 --emoji "✅"

  # Rename a thread
  merlin chat rename-thread --thread YOUR_CHANNEL_ID --name "New thread title"

Output:
  send/reply:      {"message_id": "...", "channel_id": "..."}  (JSON array if chunked)
  react:           {"ok": true}
  rename-thread:   {"ok": true, "thread_id": "...", "name": "..."}

Notes:
  - Messages over 2000 characters are automatically chunked
  - For replies, only the first chunk is a threaded reply; rest are follow-ups
  - Files are attached to the first chunk; supported types: images, PDFs, etc.
  - Requires DISCORD_BOT_TOKEN in the Merlin config (merlin setup)
  - Common emoji: 🤔 (thinking), ✅ (success), ❌ (error), 👍 (ack)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    send_parser = subparsers.add_parser(
        "send",
        help="Send a message to a channel",
        description="Send a new message to a channel. Long messages are automatically chunked.",
    )
    send_parser.add_argument("--channel", required=True, help="Channel ID")
    send_parser.add_argument("--content", help="Message content to send")
    send_parser.add_argument(
        "--file",
        action="append",
        metavar="PATH",
        help="File to attach (can be repeated)",
    )
    send_parser.add_argument(
        "--thread-on-chunk",
        action="store_true",
        help="If message is chunked, create a thread from the first message "
        "and send remaining chunks there (preserves session continuity)",
    )
    send_parser.set_defaults(func=cmd_send)

    reply_parser = subparsers.add_parser(
        "reply",
        help="Reply to a message",
        description="Reply to a specific message (creates a threaded reply indicator).",
    )
    reply_parser.add_argument(
        "--channel",
        required=True,
        help="Channel ID containing the message (for a message in a thread, the thread ID)",
    )
    reply_parser.add_argument("--message", required=True, help="Message ID to reply to")
    reply_parser.add_argument("--content", help="Reply content")
    reply_parser.add_argument(
        "--file",
        action="append",
        metavar="PATH",
        help="File to attach (can be repeated)",
    )
    reply_parser.set_defaults(func=cmd_reply)

    react_parser = subparsers.add_parser(
        "react",
        help="React to a message",
        description="Add an emoji reaction to a message.",
    )
    react_parser.add_argument(
        "--channel",
        required=True,
        help="Channel ID containing the message (for a message in a thread, the thread ID)",
    )
    react_parser.add_argument("--message", required=True, help="Message ID to react to")
    react_parser.add_argument(
        "--emoji", required=True, help="Emoji to react with (e.g. ✅ or 👍)"
    )
    react_parser.set_defaults(func=cmd_react)

    rename_parser = subparsers.add_parser(
        "rename-thread",
        help="Rename a thread",
        description="Rename a thread. Name is truncated to 100 characters.",
    )
    rename_parser.add_argument("--thread", required=True, help="Thread ID to rename")
    rename_parser.add_argument(
        "--name", required=True, help="New thread name (max 100 chars)"
    )
    rename_parser.set_defaults(func=cmd_rename_thread)

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)
