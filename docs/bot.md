# Discord Bot

The Discord bot is the same agent, in your pocket. It is an optional built-in
extension (off by default) that runs inside the same `merlin` process as the
dashboard and puts your agent in a Discord channel: you message it from
anywhere, each conversation lives in its own Discord thread mapped to a
persistent agent session, and it answers in that thread.

[SCREENSHOT PLACEHOLDER (mobile): a Discord thread showing a conversation with the bot, thinking-then-check reactions visible on the user message]

The bot is Merlin's chat channel: it runs the same agent as the
[terminal](terminal.md) and [jobs](jobs.md), and it reads and writes the
same [notes and knowledge base](notes.md). Scheduled
work flows back into conversation, and conversations flow into the shared
memory.

## Set it up

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent** under Privileged Gateway Intents.
3. Invite it with the `bot` scope plus **Send Messages**, **Send Messages
   in Threads**, **Create Public Threads**, and **Add Reactions**
   permissions.
4. Run `merlin setup` and paste the bot token. Setup is rerunnable any time;
   pressing Enter keeps the existing (masked) token.
5. Allow your channel:
   `echo 'DISCORD_CHANNEL_IDS=<channel-id>' >> ~/.merlin/config.env`

To get a channel ID, right-click the channel in Discord and pick
**Copy Channel ID** (requires Developer Mode: Settings > Advanced >
Developer Mode). Multiple channels are comma-separated.

Steps 4 and 5 can also be done from the dashboard instead of the shell:
the [Extensions page](extensions.md) edits both fields (next section).

## Enable it

The bot is disabled by default. Toggle it on the
[Extensions page](extensions.md), which also edits its two config fields
(Discord Bot Token and Discord Channel IDs, saved to `~/.merlin/config.env`).
Changes require a restart; the page shows a banner with a Restart button.

## Start a conversation

Send any message in an allowed channel. The bot creates a thread from your
message (initially named with its first 80 characters) and replies inside it.
After the first reply it renames the thread to a short generated title.

## Continue a conversation

Send more messages in the thread: the bot resumes the same agent session,
with full history. The thread-to-session mapping is persisted, so it survives
restarts.

## Pick up a job report

[Job](jobs.md) reports are posted to one of the bot's allowed
channels (or the job's own channel override)
with their run's session attached. Start a thread on the report message and
your messages there continue that run's session. Note: a plain channel reply
does not do this; it starts a fresh conversation. Use a thread on the report.

## Read the reactions

The bot reacts to your message to show progress: a thinking face while your
agent works, a white check mark on success, a cross mark on error.

## Send a voice message

Record a voice message in Discord and send it. The bot reacts with a
microphone emoji while transcribing, posts the transcription back to the
thread as a quoted line so you can verify what it heard, then answers the
transcribed prompt. Transcription uses the same backends as terminal voice
input, in priority order: the [Merlin Cloud](getting-started.md#merlin-cloud)
proxy if you have a SaaS token,
the OpenAI Whisper API if `OPENAI_API_KEY` is set (`merlin setup` prompts
for it, roughly $0.006/min), otherwise local faster-whisper (offline, ~1.5GB
model download, needs ffmpeg).

## Read long answers

Replies over Discord's 2000-character limit are split into ~1900-character
chunks sent as multiple messages.

## Monitor it

When the bot is enabled, a **Bot** page appears in the sidebar at `/bot`:
an Overview tab (status, invocations today, average response time, cost
today, errors, recent activity), a Performance tab (execution time, success
rate, and cost charts), and a Logs tab with status and date filters and a
"View session" link per invocation that opens the full transcript.

## Send messages from the CLI

`merlin chat` sends messages, replies, and reactions to the chat channel
from the command line. See `merlin chat --help`.

## Mobile notes

- Discord is the phone channel by design: message your agent from the
  Discord app and it works for you back home.
- Voice messages are handled entirely server-side: your phone only records
  and sends; download, transcription, and the verification quote all happen
  on your machine.

## Troubleshooting

- **Bot not responding**: check `~/.merlin/logs/merlin.log`, verify
  `DISCORD_CHANNEL_IDS` matches your channel, and make sure Message Content
  Intent is enabled in the Developer Portal. Messages in channels not on the
  allowlist are silently ignored, including threads whose parent channel is
  not allowed; messages from other bots and system messages (pins, joins,
  thread-starter notices) are ignored too.
- **Enabled but not configured**: Merlin still starts. It disables the bot
  at runtime, removes the Bot nav item, and logs "Bot disabled". The startup
  validator lists exactly what is missing (config file, token, channel IDs,
  engine, uv, ffmpeg) with the fix for each.
- **Cross-mark reaction on your message**: the agent run failed or the
  thread could not be created. Check the `/bot` Logs tab or
  `~/.merlin/logs/merlin.log`.
- **Voice transcription failed**: the bot still invokes your agent with
  `[transcription failed]` as the audio content and posts no transcription
  quote. The local backend requires ffmpeg.
- **Transcription comes out wrong**: Discord voice messages are currently
  transcribed as French no matter the backend (the terminal's EN/FR
  selector does not apply here).
- **Discord send failing**: verify the token and check the bot has
  permissions in the channel.
