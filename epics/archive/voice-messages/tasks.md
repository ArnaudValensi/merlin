# Voice Messages — Tasks

## T1: Install ffmpeg
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: —
- **Description**: Install ffmpeg system package (`pacman -S --noconfirm ffmpeg`). Required by faster-whisper to decode .ogg audio files.

## T2: Create `transcribe.py`
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1
- **Description**: Create `merlin-bot/transcribe.py` with PEP 723 inline deps (`faster-whisper`). Implement:
  - `transcribe(audio_path: str) -> str` function that returns transcription text
  - Uses `faster-whisper` with `WhisperModel("base", compute_type="int8")` for CPU
  - Language hardcoded to `"en"` — forces English output, auto-translates French
  - CLI interface: `uv run transcribe.py <audio_file>` prints transcription to stdout
  - Self-documenting with `--help`
  - Handle errors gracefully (return empty string or raise with descriptive message)

## T3: Add voice message detection to `merlin.py`
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T2
- **Description**: In `on_message`, before building the prompt:
  - Check `message.flags.voice` — if True, it's a voice message
  - Download `message.attachments[0]` to a temp file (use `tempfile.NamedTemporaryFile`)
  - Call `transcribe()` to get the text
  - Clean up temp file in a `finally` block
  - Log: "Voice message from {author}, transcribing..." and "Transcription: {text[:80]}"
  - If transcription fails, log the error and set transcription to a failure notice

## T4: Update `build_prompt()` for voice messages
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T3
- **Description**: Modify `build_prompt()` to accept an optional `transcription: str | None` parameter:
  - If set, use `[Discord voice message from ...]` header instead of `[Discord message from ...]`
  - Append `[Transcribed audio]: {transcription}` as the content
  - If the original message also has text content (unlikely but possible), include both

## T5: Add ffmpeg to `_validate_config()`
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1
- **Description**: Add a check for `ffmpeg` in `_validate_config()`:
  - `shutil.which("ffmpeg")` — if missing, add error with install instructions (`pacman -S ffmpeg`)

## T6: Unit tests
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T2, T3, T4
- **Description**: Write pytest tests:
  - `transcribe.py`: mock faster-whisper, verify it's called with correct params (language="en", model="base")
  - `merlin.py`: mock a voice message (flags.voice=True, attachment with .ogg), verify transcription is called and prompt includes `[Transcribed audio]`
  - `build_prompt()`: verify voice message prompt format vs regular prompt format
  - Error handling: transcription failure doesn't crash the bot
  - Also fixed pre-existing Python 3.14 compatibility issue (asyncio.get_event_loop → asyncio.run)

## T7: Live validation
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T5, T6
- **Description**: Start merlin.py, send a voice message in Discord, verify:
  - Merlin transcribes and responds to the voice message
  - French voice messages get transcribed in French (language=fr)
  - Transcription appears in logs and structured log (dashboard)
  - Regular text messages still work normally
  - Transcription posted to Discord thread as italic blockquote with duration
  - 🎤 → 🤔 → ✅ reaction flow works correctly
