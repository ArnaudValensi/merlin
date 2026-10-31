# Voice Mode: Internals

Voice mode turns one terminal window into a hands-free voice conversation with
the Claude Code agent running in it. The user talks, the words are transcribed
and submitted to the agent, the agent answers in a style written for the ear,
and Merlin reads the prose of the answer aloud, then listens again. While it is
on, a full-screen voice console covers the terminal (the terminal keeps running
under it). User side: [`../terminal.md`](../terminal.md#talk-with-the-agent-voice-mode).

## Architecture

```
Phone (terminal page, voice console)        Merlin server                     TTS server
────────────────────────────────────        ─────────────                     ──────────
tap Voice → POST /api/voice/start ──────→  @voice=on on the window
                                            VoiceSession(target) starts
VAD: a piece of speech ... 1.2 s of silence
POST /api/terminal/transcribe (no target) → the piece's text, back to the page
  ... pieces held until one ends with the end word ("terminé", "over")
POST /api/voice/say {target, text} ──────→  send-keys the message, Enter
                                            Claude's UserPromptSubmit hook
                                              → voice instructions in context
                                            tail the window's transcript:
  ←── \x00{voice: heard}                     user line   → caption
  ←── \x00{voice: activity}                  tool_use    → activity label
                                             text block  → speakable → Chunker
                                               → POST {base}/audio/speech ──→  OpenAI-compatible
  ←── \x00{voice: say id, text}  ←──────────── in-memory audio store  ←──────  audio bytes
GET /api/voice/audio/{id} → WebAudio, in order, analyser drives the scope
  ←── \x00{voice: turn_end}                 turn_duration line / state left busy
last clip played → done earcon → listen again
```

Everything lives in the `voice/` core module, mounted like the other core
modules (`mount_module(voice, "voice")` in `main.py`, API under `/api/voice`):

| File | Role |
|---|---|
| `voice/tts.py` | The TTS client: config, `synthesize()`, typed errors |
| `voice/speech.py` | `speakable()` and `Chunker`: markdown to spoken text, sentence-sized pieces |
| `voice/transcript.py` | The transcript reader (the Codex seam) and `find_transcript()` |
| `voice/session.py` | `VoiceSession` per window, the audio store, start refusals, the registry |
| `voice/routes.py` | `/api/voice/*` |
| `voice/static/voice-core.js` | The console's model (modes, clip queue, resync), pure, node tested |
| `voice/static/voice-console.js`, `.css` | The console: DOM, VAD, mic, playback, earcons, canvas |
| `voice/static/vendor/` | Silero VAD (`vad-web` 0.0.30) and `onnxruntime-web` 1.22.0, pinned, with sha256 in `README.md` |
| `terminal/hooks/agent-voice-context.sh` | The `UserPromptSubmit` hook that injects the voice instructions |
| `tools/kokoro-tts-server.py` | The reference TTS server (a separate script, never imported) |

## The TTS seam

Merlin speaks one API and nothing else: OpenAI's `POST {base}/audio/speech`,
JSON `{"model", "input", "voice", "response_format", "speed"}` in, audio bytes
out. Any server that speaks it works: the reference Kokoro server, another
local engine behind a compatible shim, or a cloud provider. There is no
provider-specific code in Merlin, no browser `speechSynthesis`, and no other
fallback. The URL and key never reach the page: `/api/voice/status` and
`/api/voice/check` say only whether voice mode is available and why not, and
the page fetches audio from Merlin by clip id.

Config keys, read from the environment and `~/.merlin/config.env`
(`tts_config()`):

| Key | Default | Meaning |
|---|---|---|
| `MERLIN_TTS_URL` | (off) | API root, for example `http://127.0.0.1:8880/v1`. `merlin config tts-url <url>` sets it (validated, no whitespace or control characters) |
| `MERLIN_TTS_API_KEY` | none | Sent as `Authorization: Bearer` |
| `MERLIN_TTS_MODEL` | `kokoro` | The `model` field |
| `MERLIN_TTS_VOICE_EN` | `af_heart` | Voice for English |
| `MERLIN_TTS_VOICE_FR` | `ff_siwis` | Voice for French |
| `MERLIN_TTS_SPEED` | `1.05` | The `speed` field |
| `MERLIN_TTS_FORMAT` | `wav` | `response_format`; start refuses one the browser cannot decode (`pcm`) |

`synthesize(text, lang)` is async (httpx) with an overall 30 s deadline on top
of the per-operation timeouts. It raises `TtsNotConfigured`, `TtsUnreachable`
(connection refused, timed out) or `TtsRefused` (an HTTP error, an empty body).
Each carries `public`, a fixed browser-safe sentence for the console; the
detail goes to the log with the URL and key redacted.

## Speech text

`speech.speakable(text, lang)` is Nova's function, adapted: fenced code becomes
one sentence in the voice language ("The code is in the terminal." / "Le code
est dans le terminal."), links keep their label, URLs, markdown marks, list
markers, table rows, rules, images and emoji go, `snake_case` is read with
spaces. It keeps line breaks so a whole text block can be cleaned before it is
cut. `speech.Chunker` cuts the cleaned text into pieces: a short first piece
(12 characters minimum) for a fast first sound, then 40, a line break always
cuts, and no piece is longer than 400 characters. Each assistant text block is
flushed whole when it lands, so the sentence the agent says before a long tool
run is heard before the run.

## The hooks and the tmux options

Voice mode rides on the agent-state hooks that `lib/skills.py` reconciles into
Claude Code's settings (same marker, same `AGENT_STATE_HOOKS` consent, version
`_HOOK_VERSION` 6):

- **`agent-session-init.sh`** (SessionStart) also stamps `@agent_transcript`
  from the hook JSON's `transcript_path` (bash regex, no jq), and with it
  `@agent_transcript_mark`, `"<bytes> <epoch>"`: the file's size and the time
  at this SessionStart, set in the same tmux call (mark first), next to
  `@agent_sid`, `@agent_conv`, `@agent_provider`. Whatever the file held at
  the mark is history (a resumed conversation); what comes after is new.
- **`agent-voice-context.sh`** (UserPromptSubmit) prints the voice instructions
  on stdout only when its window has `@voice on`; Claude Code adds that stdout
  to the agent's context. Nothing is prefixed to what the user said, and the
  next prompt after voice mode ends is back to normal. It drains stdin, makes
  one tmux read and always exits 0, so it never blocks or fails a prompt.
- `skills.voice_hooks_installed()` checks both are present and current; start
  refuses otherwise, with the fix in the reason. An agent started before the
  hooks were refreshed still runs the old set: restart the agent once.

Window options: `@voice` (`on` while a session runs, set by start, cleared by
stop and at server startup for windows a previous process left marked),
`@agent_state` (`busy`, `ask`, `idle`, `done`, from the state hooks),
`@agent_transcript`, `@agent_transcript_mark`, `@agent_conv`,
`@agent_provider` (all from session-init).

## The transcript reader and the Codex seam

`transcript.open_reader(provider, path, ...)` returns a `TranscriptReader`
yielding typed events: `user_prompt`, `assistant_text`, `tool_use(name)`,
`turn_end`. Only `claude` is implemented (`ClaudeTranscriptReader`); any other
provider raises `UnsupportedProvider`, and a Codex window shows voice mode as
unavailable. A Codex reader plugs in here and nowhere else.

The Claude reader reads the JSONL at `~/.claude/projects/<slug>/<session>.jsonl`:
main chain only (`isSidechain` lines are ignored), a partial last line is
buffered until its newline, unknown line types are skipped, a `user` line whose
content is tool results is not a prompt, `system`/`turn_duration` ends the turn.
A file truncated in place is treated as unread from its start.

Where the transcript is: `find_transcript()` takes `@agent_transcript`, and
falls back to the conversation id (`@agent_conv`) under the projects dir. The
session looks again when a turn starts, so `/clear` and resume are followed.
Where reading starts (`VoiceSession._open_reader`, `_boundary`):

- **At session start**, from the end of the current file: what was said before
  voice mode is history and is never read aloud.
- **A conversation that changes later** (`/clear`, resume), from its
  `@agent_transcript_mark`: the byte offset it holds (0 if the file is now
  shorter than the mark), with the mark's time as a `since` filter on the
  lines' timestamps. However late the watch notices the new file, its first
  turn is not lost and the history a resume brings is not spoken.
- **A file the session saw absent** (a `/clear` transcript appears with its
  first prompt), from offset 0 with no filter: everything in it is new.
- **No valid mark** (an agent whose hooks predate it): an existing file starts
  at its current size, a missing one at 0.

## The voice session

One `VoiceSession` per target window (`session:@id`, the dictation rule).
Start (`POST /api/voice/start {target, lang}`) refuses, with a reason the
console shows: not a terminal window, no voice server configured, a format the
browser cannot play, the window is gone, Codex, no Claude Code session in the
window, hooks missing or old. Start and stop are serialized per window; a
session is registered only once its start completed.

While running, two clocks work side by side:

- **The watch** polls (every 250 ms, off the event loop) the transcript and
  `@agent_state`. A turn starts with a prompt or the agent going busy and ends
  with `turn_duration`, or when the state has stayed out of busy for 2 s (Esc
  writes `idle`). `ask` sends the `ask` frame with the bottom of the pane
  (`capture-pane`, cleaned, 8 lines) and the fixed sentence ("The agent needs
  your answer." / "L'agent a besoin de ta réponse."), synthesized within 5 s
  or left out. A voice prompt that no turn
  picked up within 20 s gives an error and listening again.
- **The speaker** drains a FIFO of `(turn, piece)` and `(turn, END)`: every
  turn is spoken in order and gets its `turn_end`, nothing is dropped unless
  the user skips it. It synthesizes at most 3 clips ahead of what the page has
  fetched, and waits, however long, for a fetch. Backpressure: while 64
  pieces wait to be synthesized or 8 turns wait to be spoken, the watch stops
  reading the transcript (the reader keeps its place); a single text block that
  still pushes past 256 waiting pieces says "The rest is in the terminal." and
  drops what follows.

The **audio store** holds clips by unguessable id (`secrets.token_urlsafe(18)`),
bounded to 32 MB and 10 minutes, served by `GET /api/voice/audio/{id}`
(`no-store`).

A dropped page socket does not stop the session: `GET /api/voice/session`
gives a reconnecting page a snapshot (state, captions, ask, the clips still in
the store), with a revision so the page folds it in against live frames. A
window that vanished stops its session. Server shutdown stops every session.

Dictation is unchanged. The console transcribes each piece of speech through
the same route without a target (the text comes back in the response), holds
the pieces, and sends the whole message with `POST /api/voice/say`, which types
it into the window like dictation's injection (copy-mode cancelled, the text,
Enter apart) and tells the session (`note_dictation`). See "The end word".

### Control frames

Every frame is a terminal WebSocket text frame `\x00` + JSON, sent to every
connected page through `terminal.routes.broadcast_control` (a per-page ordered
outbox, so a stalled page never blocks the others); the page filters by
`target`. Each carries `type: "voice"`, `event`, `target`, `session` and `rev`
(grows only).

| `event` | Fields | Meaning |
|---|---|---|
| `state` | `state`: `listening`, `thinking`, `speaking`, `ask`, `error`, `off` (+ `reason`) | The session's state |
| `heard` | `text` | The user's prompt, from the transcript |
| `activity` | `label` | A tool label (`command`, `editing`, `reading`, ...) |
| `say` | `id`, `text`, `turn`, `rev` | A clip is ready to fetch |
| `turn_end` | `turn` | The turn is over, its last clip announced |
| `ask` | `text`, `excerpt`, `audio` | The agent blocks on a dialog |
| `error` | `message` | A passing error (nothing heard, voice server down, prompt not taken) |

### Routes

| Route | Does |
|---|---|
| `GET /api/voice/status` | Whether a TTS URL is configured (never the URL) |
| `GET /api/voice/check?target=` | Whether this window can start, and why not |
| `POST /api/voice/start` `{target, lang}` | Start, or return the running session's snapshot |
| `POST /api/voice/stop` `{target}` | Stop, clear `@voice` |
| `GET /api/voice/session?target=` | The snapshot for a reconnecting page |
| `POST /api/voice/lang` `{target, lang}` | Switch the voice language |
| `POST /api/voice/skip` `{target, turn}` | Drop the rest of that answer |
| `POST /api/voice/say` `{target, text}` | Type the message into the window, then Enter |
| `POST /api/voice/answer` `{target, yes}` | Yes sends Enter, No sends Escape (and writes `idle`) |
| `POST /api/voice/interrupt` `{target}` | Stop agent: drop every answer waiting, send Escape |
| `GET /api/voice/audio/{id}` | A clip's audio |

## The console

The Voice button sits in the terminal status bar. It is always shown, dimmed
with the reason when TTS or transcription is missing or the window cannot start
(`/api/voice/check`). Tapping it is the user gesture that creates and resumes
the one `AudioContext` and asks for the mic permission.

One mic stream for the whole session: `getUserMedia` once, when the console
opens (or after a hold, on the tap), closed on Exit. The VAD's `pauseStream`
keeps it open and its `resumeStream` reopens it only if it died (a phone call
took it). Closing it while the agent answered (tried on 2026-10-29) made iOS
Safari ask the permission at every turn, and a reopen without a tap could be
refused for the rest of the page, dictation's mic included. The round-trip e2e
test asserts one `getUserMedia` per session.

An open mic puts iOS in call mode, which turns the answer down (WebKit bug
311451, with or without `echoCancellation`). The console compensates: on
Safari (`navigator.audioSession` present) the output goes through a gain of
2.5 into a limiter (`DynamicsCompressorNode`, threshold -3 dB, ratio 20).

The silent switch: Safari plays Web Audio as "ambient", which the switch
mutes. The console sets `navigator.audioSession.type` (Safari 17+) to
`"play-and-record"` before `getUserMedia`, which ignores the switch, and back
to `"auto"` on Exit (a page left in `"playback"` gets its next `getUserMedia`
refused; dictation also resets a `"playback"` session before recording).
Browsers without the API keep their default (Android does not mute media in
silent mode). The round-trip test stubs the API and asserts the answer plays
under `"play-and-record"` and Exit leaves `"auto"`.

The VAD (Silero through `vad-web`, one wasm thread, no cross-origin isolation
needed) loads lazily the first time the console opens; the vendored files are
served immutable for a year (`IMMUTABLE_STATIC_PREFIXES` in `main.py`), safe
because each directory names its version. A piece of speech ends after 1.2 s
of silence and goes up as a 16 kHz WAV.

### The end word

A pause does not end a message: people pause to think. Each piece of speech
(cut at a pause) is transcribed at once, while the user goes on, and the pieces
are handled in the order said. The message goes when a piece ends with the end
word, "terminé" or "over" (either one, whatever the language; Whisper's
variants of "terminé" count), or when the user taps the screen in `TX`. The end
word counts only as a clause of its own at the end of a piece ("..., terminé",
"Terminé."), so "c'est terminé" inside a sentence never sends
(`MerlinVoiceCore.endWord`, unit-tested). It is stripped from the message.
While pieces are held the mode stays `TX`, the hint names the end word, and
after 7 s without it a soft double tone reminds the user once. A piece whose
transcription fails is tried twice more, then shown as an error a tap retries.
The e2e test `test_a_message_waits_for_its_end_word` holds two pieces, checks
nothing reached the window, then releases the end word.

Modes (radio names): `SYNC` (starting), `MON` (listening), `TX` (speech
detected), `PROC` (the agent works, also while resyncing), `RX` (the answer
plays), `ASK` (Yes / No / Exit), `ERR` (tap to retry), `HOLD` (after a reload,
a tap unlocks audio). The VAD runs only in `MON` and `TX` with nothing playing:
no listening while the agent is busy or the answer plays (no voice barge-in).
Clips are fetched two ahead of playback, ordered by revision, retried twice;
a clip that still fails stalls the line with "Part of the answer could not be
downloaded" (tap retries, Skip answer drops that answer). Earcons are Nova's,
synthesized with WebAudio, plus an alert for `ASK`. Wake lock while open;
`prefers-reduced-motion` draws a still trace.

**The look is Merlin's.** `voice-console.css` uses the dashboard's variables
(`dashboard.css`: Geist and Geist Mono, the `#0f1117` palette, bordered cards,
tinted states) and keeps the radio only in what it shows: the mode code, the
call sign, the meter and the scope. Each mode's signal colour (`--vc-sig`) is
the accent the Sessions board gives that agent state: green while it waits on
the user (`MON`, `TX` filled), yellow while it works (`PROC`), blue on a
question (`ASK`, filled), purple for the answer (`RX`), red on `ERR`. The scope
fades each frame into its card's own background, read from the CSS.

**No conversation text on screen.** The console shows no captions (neither what
the user said nor the answer), no tool label and no pane excerpt on `ASK`, only
the radio mode, the scope, a short hint and the buttons. It reads as a voice
call, not something to read while driving (the founder's call, 2026-10-28). The
`heard`, `say` and `activity` frames still feed the model (`m.captions`,
`m.activity`), which is bounded and tested, so captions can come back as an
option without touching the server.

## The reference server

`tools/kokoro-tts-server.py` is a PEP 723 script (`uv run --script`), stdlib
HTTP on top of `kokoro-onnx` with the GPU onnxruntime. It implements
`POST /v1/audio/speech` (`wav`, or `pcm` raw 16-bit mono 24 kHz; anything else
is a clear 400; the language comes from the voice id's first letter, `f`
French, `a` American English, `b` British English), `GET /v1/models`,
`GET /v1/audio/voices`, `GET /health` (no auth), and an optional bearer key.
Requests are synthesized one at a time.

It pins its model: `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the
kokoro-onnx `model-files-v1.0` release, with their sizes and sha256 in the
script. Startup fails fast, cheapest check first, each failure saying the fix:

1. **The port is bound** before anything slow (a taken port fails in about
   0.1 s). The handler is installed only once the model is ready; until then a
   connection waits in the listen backlog.
2. **The model files** in `--models` (default `~/.local/share/kokoro`): every
   present file is checked (size, sha256) before anything is downloaded; a
   wrong file is refused and never overwritten. Missing files are downloaded
   (about 354 MB, once) under a lock on the directory
   (`.kokoro-download.lock`, `flock`), so a second server started on the same
   directory waits, looks again, and finds them there. Each goes through a
   temporary file in the same directory, checked for size and sha256, then
   published with a hard link, which never replaces a file that appeared
   meanwhile (a right one is kept, a wrong one stops the start); a filesystem
   without hard links stops the start rather than risk a replacing rename. Free space
   and a writable directory are checked first; temp files left by a killed run
   are removed only while holding the lock. `--no-download` stops instead,
   with the URLs.
3. **The device**: `--device cuda` stops if onnxruntime has no CUDA provider or
   the session falls back to the CPU; `auto` (default) falls back with a loud
   warning; `cpu` forces it.
4. **A warm-up synthesis** must return audio, or the server exits non-zero.

A fresh machine needs only uv and the NVIDIA driver. Measured on an RTX 3090
Ti: ready in about 2 s with the files present, a sentence synthesized in about
0.2 s.

## Testing

- Unit: `tests/unit/test_voice_*.py` (client against a fake server and the
  reference server's HTTP layer, the server's download and preflight,
  `speakable`/`Chunker`, the reader on real transcript lines, the hooks against
  a private tmux server, the session lifecycle and bounds, the console server
  side), `tests/js/voice-core.test.js` (the console model).
- E2E: `tests/e2e/test_voice_console.py`: Chromium's fake mic plays a spoken
  WAV so the real VAD fires, `MERLIN_TRANSCRIBE_FAKE` for transcription, a fake
  TTS server, a fake agent writing a Claude transcript.
- Tests never write the real `~/.claude/settings.json`: the e2e servers run on
  their own `HOME`, the unit tests redirect the settings path.
