# /// script
# dependencies = [
#   "httpx",
#   "python-dotenv",
# ]
# ///
"""Standalone script for sending messages to Discord via the REST API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module
import paths

DISCORD_API_BASE = "https://discord.com/api/v10"


def chunk_message(text: str, max_len: int = 2000) -> list[str]:
    """Split *text* into chunks that each fit within *max_len* characters.

    Splitting strategy (in order of preference):
    1. Split at the last newline that keeps the chunk within the limit.
    2. Split at the last space that keeps the chunk within the limit.
    3. Hard cut at *max_len*.
    """
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to find a newline to split on
        candidate = remaining[:max_len]
        split_pos = candidate.rfind("\n")

        if split_pos == -1:
            # No newline — try a space
            split_pos = candidate.rfind(" ")

        if split_pos == -1:
            # No space either — hard cut
            split_pos = max_len
        else:
            # Include the delimiter in the current chunk, then advance past it
            split_pos += 1

        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:]

    return chunks


def load_token() -> str:
    """Load the Discord bot token from the config file."""
    load_dotenv(paths.bot_config_path())
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print(
            f"Error: DISCORD_BOT_TOKEN not found. "
            f"Make sure it is set in {paths.bot_config_path()}",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def _check_response(resp: httpx.Response) -> dict | None:
    """Raise SystemExit with a clear message if the response is not 2xx.

    Returns parsed JSON body, or None for 204 No Content.
    """
    if 200 <= resp.status_code < 300:
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
    try:
        error_body = resp.json()
    except Exception:
        error_body = resp.text
    msg = (
        f"Discord API returned {resp.status_code}: "
        f"{json.dumps(error_body) if isinstance(error_body, dict) else error_body}"
    )
    raise RuntimeError(msg)


def _auth_headers(token: str, *, json_content: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bot {token}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _register_message(message_id: str) -> None:
    """Register a bot message → session mapping if MERLIN_SESSION_ID is set."""
    session_id = os.environ.get("MERLIN_SESSION_ID")
    if session_id:
        try:
            from session_registry import set_message_session
            set_message_session(message_id, session_id)
        except Exception:
            pass  # Best-effort; don't break sends if registry fails


import mimetypes


def _send_multipart(
    client: httpx.Client,
    url: str,
    token: str,
    payload: dict,
    file_paths: list[Path],
) -> dict:
    """Send a message with file attachments via multipart/form-data."""
    files_list = []
    for i, fp in enumerate(file_paths):
        mime = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        files_list.append((f"files[{i}]", (fp.name, fp.read_bytes(), mime)))

    resp = client.post(
        url,
        headers=_auth_headers(token, json_content=False),
        data={"payload_json": json.dumps(payload)},
        files=files_list,
    )
    return _check_response(resp)


def send_message(
    channel_id: str, content: str, token: str, *,
    files: list[Path] | None = None,
    thread_on_chunk: bool = False,
) -> list[dict]:
    """Send *content* to the given Discord channel, chunking if necessary.

    If *files* is provided, the first chunk is sent with the attachments.
    If *thread_on_chunk* is True and there are multiple chunks, a thread is
    created from the first message and subsequent chunks are sent there.
    This preserves session continuity for replies (the thread inherits the
    first message's session ID).
    Returns a list of Discord message response dicts (one per chunk).
    """
    chunks = chunk_message(content) if content else [""]
    results: list[dict] = []

    with httpx.Client() as client:
        target_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"

        for i, chunk in enumerate(chunks):
            payload: dict = {}
            if chunk:
                payload["content"] = chunk

            # Attach files to the first chunk only
            if i == 0 and files:
                data = _send_multipart(client, target_url, token, payload, files)
            else:
                resp = client.post(target_url, headers=_auth_headers(token), json=payload)
                data = _check_response(resp)

            results.append({"message_id": data["id"], "channel_id": data["channel_id"]})
            _register_message(data["id"])

            # After the first chunk, create a thread and redirect subsequent chunks there
            if i == 0 and thread_on_chunk and len(chunks) > 1:
                thread_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{data['id']}/threads"
                thread_payload = {
                    "name": (chunk[:97] + "...") if len(chunk) > 100 else chunk[:100] or "Continued",
                    "auto_archive_duration": 4320,
                }
                resp = client.post(thread_url, headers=_auth_headers(token), json=thread_payload)
                thread_data = _check_response(resp)
                target_url = f"{DISCORD_API_BASE}/channels/{thread_data['id']}/messages"

    return results


def reply_message(
    channel_id: str, message_id: str, content: str, token: str,
    *, files: list[Path] | None = None,
) -> list[dict]:
    """Reply to a specific message, chunking if necessary.

    The first chunk is sent as a reply (with message_reference).
    If *files* is provided, attachments are sent with the first chunk.
    Subsequent chunks are sent as regular messages in the same channel.
    """
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    chunks = chunk_message(content) if content else [""]
    results: list[dict] = []

    with httpx.Client() as client:
        for i, chunk in enumerate(chunks):
            payload: dict = {}
            if chunk:
                payload["content"] = chunk
            if i == 0:
                payload["message_reference"] = {"message_id": message_id}

            # Attach files to the first chunk only
            if i == 0 and files:
                data = _send_multipart(client, url, token, payload, files)
            else:
                resp = client.post(url, headers=_auth_headers(token), json=payload)
                data = _check_response(resp)

            results.append({"message_id": data["id"], "channel_id": data["channel_id"]})
            _register_message(data["id"])

    return results


def react_message(
    channel_id: str, message_id: str, emoji: str, token: str
) -> None:
    """Add a reaction to a message."""
    encoded_emoji = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded_emoji}/@me"
    )
    with httpx.Client() as client:
        resp = client.put(url, headers=_auth_headers(token))
        _check_response(resp)


def create_thread_from_message(
    channel_id: str, message_id: str, name: str, token: str
) -> dict:
    """Create a public thread from a message.

    Returns the thread data dict (includes 'id' for the new thread).
    """
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}/threads"
    )
    payload = {
        "name": name[:100],
        "auto_archive_duration": 4320,  # 3 days
    }
    with httpx.Client() as client:
        resp = client.post(url, headers=_auth_headers(token), json=payload)
        data = _check_response(resp)
    return data


def rename_thread(thread_id: str, name: str, token: str) -> dict:
    """Rename a Discord thread (channel) via PATCH.

    Returns the updated channel data dict.
    """
    url = f"{DISCORD_API_BASE}/channels/{thread_id}"
    payload = {"name": name[:100]}
    with httpx.Client() as client:
        resp = client.patch(url, headers=_auth_headers(token), json=payload)
        data = _check_response(resp)
    return data


def _print_results(results: list[dict]) -> None:
    if len(results) == 1:
        print(json.dumps(results[0]))
    else:
        print(json.dumps(results))


def cmd_send(args: argparse.Namespace) -> None:
    """Handle the ``send`` subcommand."""
    token = load_token()
    files = [Path(f) for f in args.file] if args.file else None
    _print_results(send_message(
        args.channel, args.content or "", token,
        files=files, thread_on_chunk=args.thread_on_chunk,
    ))


def cmd_reply(args: argparse.Namespace) -> None:
    """Handle the ``reply`` subcommand."""
    token = load_token()
    files = [Path(f) for f in args.file] if args.file else None
    _print_results(reply_message(args.channel, args.message, args.content or "", token, files=files))


def cmd_react(args: argparse.Namespace) -> None:
    """Handle the ``react`` subcommand."""
    token = load_token()
    react_message(args.channel, args.message, args.emoji, token)
    print(json.dumps({"ok": True}))


def cmd_rename_thread(args: argparse.Namespace) -> None:
    """Handle the ``rename-thread`` subcommand."""
    token = load_token()
    data = rename_thread(args.thread, args.name, token)
    print(json.dumps({"ok": True, "thread_id": data["id"], "name": data["name"]}))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send messages to Discord via the REST API.",
        epilog="""
Examples:
  # Send a message to a channel
  uv run discord_send.py send --channel YOUR_CHANNEL_ID --content "Hello!"

  # Send a message with an image
  uv run discord_send.py send --channel YOUR_CHANNEL_ID --content "Screenshot:" --file screenshot.png

  # Send just a file (no text)
  uv run discord_send.py send --channel YOUR_CHANNEL_ID --file report.pdf

  # Send multiple files
  uv run discord_send.py send --channel YOUR_CHANNEL_ID --file a.png --file b.png

  # Reply with an attachment
  uv run discord_send.py reply --channel YOUR_CHANNEL_ID --message 123456789 --content "Here you go" --file result.png

  # React to a message
  uv run discord_send.py react --channel YOUR_CHANNEL_ID --message 123456789 --emoji "✅"

  # Rename a thread
  uv run discord_send.py rename-thread --thread YOUR_CHANNEL_ID --name "New thread title"

Output:
  send/reply:      {"message_id": "...", "channel_id": "..."}  (JSON array if chunked)
  react:           {"ok": true}
  rename-thread:   {"ok": true, "thread_id": "...", "name": "..."}

Notes:
  - Messages over 2000 characters are automatically chunked
  - For replies, only the first chunk is a threaded reply; rest are follow-ups
  - Files are attached to the first chunk; supported types: images, PDFs, etc.
  - Requires DISCORD_BOT_TOKEN in .env file
  - Common emoji: 🤔 (thinking), ✅ (success), ❌ (error), 👍 (ack)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    send_parser = subparsers.add_parser(
        "send",
        help="Send a message to a channel",
        description="Send a new message to a Discord channel. Long messages are automatically chunked.",
    )
    send_parser.add_argument("--channel", required=True, help="Discord channel ID")
    send_parser.add_argument("--content", help="Message content to send")
    send_parser.add_argument("--file", action="append", metavar="PATH", help="File to attach (can be repeated)")
    send_parser.add_argument("--thread-on-chunk", action="store_true",
                             help="If message is chunked, create a thread from the first message "
                                  "and send remaining chunks there (preserves session continuity)")
    send_parser.set_defaults(func=cmd_send)

    reply_parser = subparsers.add_parser(
        "reply",
        help="Reply to a message",
        description="Reply to a specific message (creates a threaded reply indicator).",
    )
    reply_parser.add_argument("--channel", required=True, help="Discord channel ID")
    reply_parser.add_argument("--message", required=True, help="Message ID to reply to")
    reply_parser.add_argument("--content", help="Reply content")
    reply_parser.add_argument("--file", action="append", metavar="PATH", help="File to attach (can be repeated)")
    reply_parser.set_defaults(func=cmd_reply)

    react_parser = subparsers.add_parser(
        "react",
        help="React to a message",
        description="Add an emoji reaction to a message.",
    )
    react_parser.add_argument("--channel", required=True, help="Discord channel ID")
    react_parser.add_argument("--message", required=True, help="Message ID to react to")
    react_parser.add_argument("--emoji", required=True, help="Emoji to react with (e.g. ✅ or 👍)")
    react_parser.set_defaults(func=cmd_react)

    rename_parser = subparsers.add_parser(
        "rename-thread",
        help="Rename a thread",
        description="Rename a Discord thread. Name is truncated to 100 characters.",
    )
    rename_parser.add_argument("--thread", required=True, help="Thread ID to rename")
    rename_parser.add_argument("--name", required=True, help="New thread name (max 100 chars)")
    rename_parser.set_defaults(func=cmd_rename_thread)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
