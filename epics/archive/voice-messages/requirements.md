# Voice Messages — Requirements

## Goal

Allow Merlin to understand Discord voice messages by transcribing them to text using faster-whisper, then processing them like normal text messages.

## Context

Discord voice messages arrive as regular `Message` objects with `message.flags.voice == True` and an `.ogg` audio attachment. Currently, Merlin ignores these because `message.content` is empty and there's no attachment handling.

By transcribing voice messages locally with faster-whisper (language forced to English), Merlin can process them seamlessly. When the language is set to English and the speaker uses French, Whisper automatically translates to English — which fits the bilingual setup.

## Requirements

### R1: Audio transcription script (`transcribe.py`)
- **Status**: `proposed`
- New self-documenting script with `--help`
- Takes an audio file path or URL, returns transcription text to stdout
- Uses `faster-whisper` with language hardcoded to `"en"` (forces English output; auto-translates French)
- Model size: `base` (good speed/accuracy tradeoff for short voice messages on CPU)
- PEP 723 inline dependencies (`faster-whisper`)
- System dependency: `ffmpeg` (needed by faster-whisper to decode .ogg)

### R2: Voice message detection in `merlin.py`
- **Status**: `proposed`
- In `on_message`, detect voice messages via `message.flags.voice`
- Download the `.ogg` attachment to a temp file
- Call `transcribe.py` (or import directly) to get the text
- Clean up the temp file after transcription

### R3: Prompt format for voice messages
- **Status**: `proposed`
- Modify `build_prompt()` to accept optional transcription text
- Format:
  ```
  [Discord voice message from "username" in thread 123, channel 456, message ID 789]
  [Transcribed audio]: Hey Merlin, can you check the cron jobs for me?
  ```
- If transcription fails, still send the prompt but note the failure so Claude can ask the user to type it out

### R4: System dependency (`ffmpeg`)
- **Status**: `proposed`
- Install `ffmpeg` via `pacman -S ffmpeg`
- Add to `_validate_config()` in `merlin.py` so it fails fast if missing

### R5: Model caching
- **Status**: `proposed`
- faster-whisper downloads the model on first use (~150MB for `base`)
- Model is cached in `~/.cache/huggingface/` by default
- First invocation will be slow (download), subsequent ones fast
- No custom caching logic needed — just document the behavior

## Out of Scope

- Regular audio file attachments (only voice messages)
- Image/video attachments
- Streaming transcription
- Speaker diarization
- Custom Whisper model fine-tuning
- Language selection (hardcoded to English)
