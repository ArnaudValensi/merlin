#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""
PreCompact hook — extracts and saves memories before compaction.

Reads the conversation transcript, extracts important facts using Claude,
and appends them to the daily memory log.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MERLIN_BOT_DIR = Path(__file__).parent.parent.parent.resolve()
LOGS_DIR = MERLIN_BOT_DIR / "memory" / "logs"
HOOK_LOG = MERLIN_BOT_DIR / "logs" / "pre-compact-memory.log"

# Configuration
TRANSCRIPT_MESSAGE_LIMIT = int(os.environ.get("MERLIN_MEMORY_MESSAGE_LIMIT", "50"))

# Setup file logging
HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=HOOK_LOG,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

EXTRACT_PROMPT = """TASK: Extract facts from the transcript below.

RULES:
1. Look for USER preferences, decisions, or "remember this" requests
2. Ignore assistant messages except to understand context
3. Output a bullet list of facts, OR just "NOTHING_TO_SAVE"
4. Do NOT continue the conversation
5. Do NOT output code or XML
6. Do NOT role-play as the assistant

EXAMPLE OUTPUT:
- User prefers flat directory structures
- User wants standard markdown links, not wiki-style
- Project uses Python with uv

OR just: NOTHING_TO_SAVE"""


def read_transcript(path: str) -> str:
    """Read and format transcript for analysis."""
    try:
        with open(path, "r") as f:
            lines = f.readlines()

        # Parse JSONL transcript, extract user/assistant messages
        messages = []
        for line in lines:
            try:
                entry = json.loads(line)
                entry_type = entry.get("type", "")

                # User messages (skip tool_result which are technical, not conversation)
                if entry_type == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    # Skip tool results (list of dicts with tool_use_id)
                    if isinstance(content, list):
                        continue
                    if isinstance(content, str) and content:
                        # Skip if it looks like a tool result
                        if content.startswith("[{") and "tool_use_id" in content:
                            continue
                        if len(content) > 2000:
                            content = content[:2000] + "... [truncated]"
                        messages.append(f"**user**: {content}")

                # Assistant messages
                elif entry_type == "assistant":
                    msg = entry.get("message", {})
                    content_parts = msg.get("content", [])
                    # Extract text blocks from content array
                    text_parts = []
                    for part in content_parts:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    if text_parts:
                        content = "\n".join(text_parts)
                        if len(content) > 2000:
                            content = content[:2000] + "... [truncated]"
                        messages.append(f"**assistant**: {content}")

            except json.JSONDecodeError:
                continue

        if not messages:
            return ""

        # Take last N messages (configurable via MERLIN_MEMORY_MESSAGE_LIMIT)
        recent = messages[-TRANSCRIPT_MESSAGE_LIMIT:]
        return "\n\n".join(recent)
    except Exception as e:
        logging.error(f"Failed to read transcript: {e}")
        return ""


def extract_memories(transcript: str) -> str:
    """Use Claude to extract important memories from transcript."""
    if not transcript:
        return ""

    prompt = f"{EXTRACT_PROMPT}\n\n---\n\nTRANSCRIPT:\n{transcript}"

    try:
        # Pipe prompt to Claude CLI via stdin (no wrapper needed)
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", "sonnet",
                "--max-turns", "1",
                "--output-format", "text",
                "--tools", "",  # Disable tools, we just want text
                "--system-prompt", "You are a memory extractor. Output ONLY a markdown bullet list or NOTHING_TO_SAVE. Never output code, XML, or function calls.",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
            cwd="/tmp",  # Neutral dir to avoid loading project CLAUDE.md
        )

        if result.returncode != 0:
            logging.error(f"Claude error (exit {result.returncode}): {result.stderr}")
            return ""

        output = result.stdout.strip()
        # Skip error messages
        if output.startswith("Error:"):
            logging.warning(f"Claude returned error: {output}")
            return ""
        # Only skip if the entire response is just NOTHING_TO_SAVE
        if output == "NOTHING_TO_SAVE" or output.startswith("NOTHING_TO_SAVE\n"):
            return ""
        # Remove any trailing NOTHING_TO_SAVE explanation
        if "\nNOTHING_TO_SAVE" in output:
            output = output.split("\nNOTHING_TO_SAVE")[0].strip()
        return output
    except subprocess.TimeoutExpired:
        logging.error("Claude timed out")
        return ""
    except Exception as e:
        logging.error(f"Failed to extract memories: {e}")
        return ""


def save_to_daily_log(memories: str, session_id: str, trigger: str) -> Path:
    """Append memories to daily log file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"{today}.md"

    # Create file with header if it doesn't exist
    if not log_file.exists():
        log_file.write_text(f"# Daily Log — {today}\n\n")

    now = datetime.now().strftime("%H:%M")

    if memories:
        entry = f"""## {now} — Pre-compaction memories

{memories}

---

"""
    else:
        entry = f"""## {now} — Pre-compaction ({trigger})

(No significant memories to save from session `{session_id}`)

---

"""

    with open(log_file, "a") as f:
        f.write(entry)

    return log_file


def main() -> None:
    # Read hook input from stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        logging.error("Failed to parse hook input")
        sys.exit(2)

    session_id = input_data.get("session_id", "unknown")
    trigger = input_data.get("trigger", "unknown")
    transcript_path = input_data.get("transcript_path", "")

    logging.info(f"PreCompact hook started: session={session_id}, trigger={trigger}")

    # Read and analyze transcript
    transcript = read_transcript(transcript_path) if transcript_path else ""

    # Extract memories using Claude
    memories = extract_memories(transcript) if transcript else ""

    # Save to daily log
    log_file = save_to_daily_log(memories, session_id, trigger)

    # Output success
    saved = "memories saved" if memories else "no memories to save"
    logging.info(f"PreCompact hook finished: {saved} to {log_file.name}")
    print(json.dumps({
        "systemMessage": f"Pre-compaction: {saved} to {log_file.name}"
    }))


if __name__ == "__main__":
    main()
