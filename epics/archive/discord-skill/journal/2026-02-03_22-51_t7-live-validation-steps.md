# 2026-02-03 22:51 — T7: Live Validation Steps

## Prerequisites

1. Create a Discord bot and get its token (if you haven't already):
   - Go to https://discord.com/developers/applications
   - Create a new application (or use an existing one)
   - Go to **Bot** tab, click **Reset Token**, copy it
   - Under **Privileged Gateway Intents**, enable **Message Content Intent**
   - Go to **OAuth2 > URL Generator**, select `bot` scope, select `Send Messages` + `Add Reactions` permissions
   - Open the generated URL to invite the bot to your server

2. Set the token:
   ```bash
   cp merlin-bot/.env.example merlin-bot/.env
   # Edit merlin-bot/.env and paste your token
   ```

3. Get a test channel ID:
   - In Discord, enable Developer Mode (Settings > Advanced > Developer Mode)
   - Right-click a channel > Copy Channel ID

## Validation Steps

Run all commands from the `merlin-bot/` directory.

### Step 1: Send a simple message

```bash
cd merlin-bot
uv run discord_send.py send --channel <CHANNEL_ID> --content "Hello from Merlin 🧙"
```

**Expected**: Message appears in Discord. JSON output with `message_id` and `channel_id`.

### Step 2: Reply to that message

Copy the `message_id` from Step 1 output.

```bash
uv run discord_send.py reply --channel <CHANNEL_ID> --message <MESSAGE_ID> --content "This is a reply"
```

**Expected**: Reply appears in Discord with the reply indicator pointing to the original message.

### Step 3: React to that message

```bash
uv run discord_send.py react --channel <CHANNEL_ID> --message <MESSAGE_ID> --emoji "✅"
```

**Expected**: Checkmark reaction appears on the message. Output: `{"ok": true}`.

### Step 4: Test chunking (long message)

```bash
uv run discord_send.py send --channel <CHANNEL_ID> --content "$(python -c "print('Line %d: This is a test of message chunking. ' * 3 % (i, i, i) for i in range(100))" | tr "'" " ")"
```

Or simpler:

```bash
uv run discord_send.py send --channel <CHANNEL_ID> --content "$(python -c "print('abcdefghij ' * 250)")"
```

**Expected**: Message split into multiple Discord messages (2750 chars > 2000 limit). Output is a JSON array.

### Step 5: Test via the wrapper (Claude Code integration)

```bash
uv run claude_wrapper.py --caller test-discord "Send a test message to Discord channel <CHANNEL_ID> saying 'Wrapper integration test'"
```

**Expected**: Claude Code picks up the Discord skill from `merlin-bot/.claude/skills/discord/SKILL.md`, invokes `discord_send.py` via Bash, message appears in Discord. A log file is created in `merlin-bot/logs/claude/`.

### Step 6: Test error cases

```bash
# Bad channel ID
uv run discord_send.py send --channel 000000000000000000 --content "Should fail"
# Expected: Non-zero exit, stderr shows Discord API error (Unknown Channel)

# Bad token (temporarily edit .env)
# Expected: Non-zero exit, stderr shows 401 Unauthorized
```

## Done criteria

All 6 steps pass. Document the actual output in a follow-up journal entry and mark T7 as done in tasks.md.
