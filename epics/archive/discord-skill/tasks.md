# Discord Skill — Tasks

## T1: Fix wrapper cwd to merlin-bot/
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Update `invoke_claude()` in `claude_wrapper.py` to pass `cwd` pointing to the `merlin-bot/` directory when calling `subprocess.run()`. This ensures Claude Code starts in the right directory and picks up `merlin-bot/CLAUDE.md` and `merlin-bot/.claude/`. Update existing tests to verify `cwd` is set correctly.

## T2: Create discord_send.py with send action
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Create `merlin-bot/discord_send.py` with PEP 723 inline deps. Implement the `send` action:
  - CLI: `uv run discord_send.py send --channel <id> --content "text"`
  - Load `DISCORD_BOT_TOKEN` from `merlin-bot/.env`
  - POST to Discord REST API (`/channels/{id}/messages`)
  - Handle message chunking: split at 2000 chars, preferring splits at newlines or spaces
  - Print JSON result to stdout (`{"message_id": "...", "channel_id": "..."}`)
  - Non-zero exit code + stderr message on failure

## T3: Add reply and react actions
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T2
- **Description**: Add two more actions to `discord_send.py`:
  - `reply`: `uv run discord_send.py reply --channel <id> --message <id> --content "text"`. Same as send but includes `message_reference` in the API call. Chunking applies (only first chunk is the reply, rest are regular sends).
  - `react`: `uv run discord_send.py react --channel <id> --message <id> --emoji "✅"`. PUT to `/channels/{id}/messages/{id}/reactions/{emoji}/@me`. URL-encode the emoji.

## T4: Create .env.example and .gitignore entries
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Create `merlin-bot/.env.example` with `DISCORD_BOT_TOKEN=your-token-here`. Ensure `merlin-bot/.env` is in `.gitignore` (create or update gitignore as needed).

## T5: Create SKILL.md for Claude Code
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T2, T3
- **Description**: Create `merlin-bot/.claude/skills/discord/SKILL.md`. The skill should:
  - Have proper YAML frontmatter (name, description)
  - Document all three actions with exact CLI syntax and examples
  - Include notes on chunking behavior
  - Include Discord writing style tips (short messages, no tables, conversational)
  - Set `user-invocable: false` (Claude uses it autonomously, not triggered by user slash command)

## T6: Unit tests
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2, T3
- **Description**: Write pytest tests for:
  - Wrapper: verify `cwd` is set to `merlin-bot/` in subprocess call
  - `discord_send.py`: mock HTTP calls, verify correct API endpoints, headers, payload
  - Chunking: messages under 2000 chars sent as-is, long messages split correctly at boundaries
  - Reply: first chunk includes `message_reference`, subsequent chunks don't
  - React: correct URL encoding of emoji, correct API endpoint
  - Error handling: missing token, API errors, invalid arguments

## T7: Live validation
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T4, T5, T6
- **Description**: End-to-end test with a real Discord bot token:
  - Send a message to a test channel
  - Reply to that message
  - React to that message
  - Send a long message (>2000 chars) and verify chunking
  - Verify the skill works when invoked by Claude via the wrapper
  - Document results in journal entry
