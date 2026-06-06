"""Discord REST API transport: send, reply, react, and thread operations.

Library module used by the bot handler and by the 'merlin chat' CLI
(lib/chat.py). The CLI entry point lives there; this file has none.
"""

from __future__ import annotations

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


def _check_status(resp: httpx.Response) -> None:
    """Raise RuntimeError if the response is not 2xx. For bodiless endpoints."""
    if 200 <= resp.status_code < 300:
        return
    try:
        error_body = resp.json()
    except Exception:
        error_body = resp.text
    msg = (
        f"Discord API returned {resp.status_code}: "
        f"{json.dumps(error_body) if isinstance(error_body, dict) else error_body}"
    )
    raise RuntimeError(msg)


def _check_response(resp: httpx.Response) -> dict:
    """Raise if non-2xx or empty body. Return the parsed JSON body.

    Use :func:`_check_status` for endpoints that return 204 No Content.
    """
    _check_status(resp)
    if resp.status_code == 204 or not resp.content:
        raise RuntimeError(
            f"Expected JSON body from Discord API, got {resp.status_code}"
        )
    return resp.json()


def _auth_headers(token: str, *, json_content: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bot {token}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _register_message(message_id: str, session_id: str | None = None) -> None:
    """Register a bot message → session mapping for session continuity.

    Uses the explicit *session_id* if provided, otherwise falls back to the
    ``MERLIN_SESSION_ID`` environment variable (set by the engine wrapper).
    """
    session_id = session_id or os.environ.get("MERLIN_SESSION_ID")
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
    channel_id: str,
    content: str,
    token: str,
    *,
    files: list[Path] | None = None,
    thread_on_chunk: bool = False,
    session_id: str | None = None,
) -> list[dict]:
    """Send *content* to the given Discord channel, chunking if necessary.

    If *files* is provided, the first chunk is sent with the attachments.
    If *thread_on_chunk* is True and there are multiple chunks, a thread is
    created from the first message and subsequent chunks are sent there.
    This preserves session continuity for replies (the thread inherits the
    first message's session ID).
    If *session_id* is provided, each sent message is registered with that
    session so replies can resume the conversation.
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
                resp = client.post(
                    target_url, headers=_auth_headers(token), json=payload
                )
                data = _check_response(resp)

            results.append({"message_id": data["id"], "channel_id": data["channel_id"]})
            _register_message(data["id"], session_id)

            # After the first chunk, create a thread and redirect subsequent chunks there
            if i == 0 and thread_on_chunk and len(chunks) > 1:
                thread_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{data['id']}/threads"
                thread_payload = {
                    "name": (chunk[:97] + "...")
                    if len(chunk) > 100
                    else chunk[:100] or "Continued",
                    "auto_archive_duration": 4320,
                }
                resp = client.post(
                    thread_url, headers=_auth_headers(token), json=thread_payload
                )
                thread_data = _check_response(resp)
                target_url = f"{DISCORD_API_BASE}/channels/{thread_data['id']}/messages"

    return results


def reply_message(
    channel_id: str,
    message_id: str,
    content: str,
    token: str,
    *,
    files: list[Path] | None = None,
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


def react_message(channel_id: str, message_id: str, emoji: str, token: str) -> None:
    """Add a reaction to a message."""
    encoded_emoji = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded_emoji}/@me"
    )
    with httpx.Client() as client:
        resp = client.put(url, headers=_auth_headers(token))
        _check_status(resp)


def create_thread_from_message(
    channel_id: str, message_id: str, name: str, token: str
) -> dict:
    """Create a public thread from a message.

    Returns the thread data dict (includes 'id' for the new thread).
    """
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}/threads"
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
