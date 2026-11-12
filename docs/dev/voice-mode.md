# Voice Mode: Internals

Voice mode turns one terminal window into a hands-free voice conversation with
the Claude Code agent running in it. The user talks, the words are transcribed
and submitted to the agent, the agent answers in a style written for the ear,
and Merlin reads the prose of the answer aloud, then listens again. While it is
on, a voice console either covers the terminal (full screen, for the ear only)
or sits as a bar under a terminal that stays in view and in use. User side: [`../terminal.md`](../terminal.md#talk-with-the-agent-voice-mode).

## Architecture

```
Phone (terminal page, voice console)        Merlin server                     TTS server
────────────────────────────────────        ─────────────                     ──────────
tap Voice → POST /api/voice/start ──────→  @voice=on on the window
                                            VoiceSession(target) starts
VAD: a piece of speech ... 1.2 s of silence
POST /api/terminal/transcribe (no target) → the piece's text, back to the page
  ... pieces held until one ends with the end word ("terminé", "over")
POST /api/voice/say {target, text, id} ──→  send-keys the message, Enter (once per id)
                                            Claude's UserPromptSubmit hook
                                              → voice instructions in context
                                            tail the window's transcript:
  ←── \x00{voice: heard}                     user line   → caption
  ←── \x00{voice: activity}                  tool_use    → activity label
                                             text block  → speakable → Chunker
                                               → tts.synthesize() ─────────→  the provider
  ←── \x00{voice: say id, text}  ←──────────── in-memory audio store  ←──────  audio bytes
GET /api/voice/audio/{id} → WebAudio, in order, analyser drives the scope
  ←── \x00{voice: turn_end}                 turn_duration line / state left busy
last clip played → done earcon → listen again
```

Everything lives in the `voice/` core module, mounted like the other core
modules (`mount_module(voice, "voice")` in `main.py`, API under `/api/voice`):

| File | Role |
|---|---|
| `voice/tts.py` | The TTS client: provider selection, config, `synthesize()`, typed errors |
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

`synthesize(text, lang)` is the one way Merlin speaks. Behind it sit providers,
each a small pair of functions in `voice/tts.py`: one turns a text into a
request, one turns a refusal into a fixed sentence. There is no browser
`speechSynthesis` and no fallback from one provider to another. The URL and
the key never reach the page: `/api/voice/status` and `/api/voice/check` say
only whether voice mode is available and why not, and the page fetches audio
from Merlin by clip id.

### The providers

- **OpenAI-compatible** (`openai`): `POST {MERLIN_TTS_URL}/audio/speech`, JSON
  `{"model", "input", "voice", "response_format", "speed"}`, the key (if any) as
  `Authorization: Bearer`. Any server that speaks it works: the reference Kokoro
  server, another local engine behind a compatible shim, a cloud provider.
- **ElevenLabs** (`elevenlabs`), with the user's own key:
  `POST {url}/text-to-speech/{voice_id}?output_format=mp3_44100_128`, header
  `xi-api-key`, JSON `{"text", "model_id", "language_code", "voice_settings":
  {"speed"}}`. The voice id is percent-encoded into the path; the speed is
  clamped to ElevenLabs' range, 0.7 to 1.2. The non-streaming endpoint: Merlin
  plays whole clips.

### Which one runs

`tts_config()` picks, explicit configuration first:

1. `MERLIN_TTS_PROVIDER=elevenlabs` with a non-empty `MERLIN_TTS_API_KEY`:
   ElevenLabs. `MERLIN_TTS_URL` is optional and defaults to
   `https://api.elevenlabs.io/v1`.
2. `MERLIN_TTS_URL` set, provider `openai` or unset: OpenAI-compatible. An
   existing `MERLIN_TTS_URL` setup needs no new key.
3. Otherwise none. `elevenlabs` without a key, or a provider Merlin does not
   know, is a configuration error, not a fallback: `unconfigured_reason()`
   gives the sentence start refuses with (the key is missing, the provider is
   unknown, or how to configure one).

`merlin config tts-provider <openai|elevenlabs|>` and `merlin config
tts-api-key <key|->` set the two new keys (validated, one line each; the key is
never printed back, `merlin config` shows it as `(set)`, and `-` reads it from
stdin).

### Defaults per provider

| Key | OpenAI-compatible | ElevenLabs |
|---|---|---|
| `MERLIN_TTS_URL` | (required) | `https://api.elevenlabs.io/v1` |
| `MERLIN_TTS_API_KEY` | none | (required) |
| `MERLIN_TTS_MODEL` | `kokoro` | `eleven_flash_v2_5` |
| `MERLIN_TTS_VOICE_EN` | `af_heart` | Sarah, `EXAVITQu4vr4xnSDxMaL` (a default voice) |
| `MERLIN_TTS_VOICE_FR` | `ff_siwis` | Lucie, `YxrwjAKoUKULGd0g8K9Y` (a library voice: a paid plan) |
| `MERLIN_TTS_SPEED` | `1.05` | `1.0` (sent clamped to 0.7 to 1.2) |
| `MERLIN_TTS_FORMAT` | `wav`; start refuses one the browser cannot decode (`pcm`) | not read: always mp3 |

Every key is read from the environment and `~/.merlin/config.env` at start.
`TtsConfig.format` is the effective format, so the content type of a clip
(`PLAYABLE_FORMATS`, `audio/mpeg` for mp3) follows the provider.

### A voice per language

`voice_for(lang)` takes `MERLIN_TTS_VOICE_<LANG>` (upper-case ISO 639-1 code,
`MERLIN_TTS_VOICE_DE`) for any language, then the provider's default for it,
then the English voice. Only keys of exactly two letters are read, and a
language that is not two letters gets the English voice. The console's
languages are still `en` and `fr` (`LANGS` in `routes.py`).
`capability()["voices"]` shows the voice for each: a voice Merlin knows by its
name (Lucie, Sarah), any other by its id.

### Errors

`synthesize()` is async (httpx) with an overall 30 s deadline on top of the
per-operation timeouts. It raises `TtsNotConfigured`, `TtsUnreachable`
(connection refused, timed out) or `TtsRefused` (an HTTP error, an empty body).
Each carries `public`, a fixed browser-safe sentence for the console; the
detail (what the service said, OpenAI's `error.message` or ElevenLabs'
`detail.status` and `message`) goes to the log with the URL and key redacted.

| Provider | Answer | Public sentence |
|---|---|---|
| OpenAI-compatible | 401, 403 | The voice server refused the key (`<status>`) |
| OpenAI-compatible | other | The voice server refused the request (`<status>`) |
| ElevenLabs | 402, or `quota_exceeded` / `paid_plan_required` in the detail (ElevenLabs sends these with a 401 too) | The voice service plan does not allow this (quota or paid voice) |
| ElevenLabs | 401, 403 | The voice service refused the key |
| ElevenLabs | other | The voice service refused the request (`<status>`) |

### Warm-up

An ElevenLabs library voice that has not been used for a while answers its
first request in 8 to 10 s (measured on 2026-11-12); a default voice does not.
So when a session starts, and when its language changes, the session sends one
short synthesis in that language ("OK.", "D'accord.") and throws the audio
away (`_start_warm_up()`). It runs in the background: start never waits for
it, a failure is only logged, stop cancels it. It runs for ElevenLabs and not
for an OpenAI-compatible server (`WARM_UP_PROVIDERS`), which has no cold cloud
voice to wake.

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
  on stdout only when its window has `@voice` set to `on` (the console covers
  the terminal: write for the ear, no markdown) or `screen` (the terminal is in
  view: write for the screen, keep the prose short enough to listen to); Claude
  Code adds that stdout to the agent's context. Nothing is prefixed to what the
  user said, and the next prompt after voice mode ends is back to normal. It
  drains stdin, makes one tmux read and always exits 0, so it never blocks or
  fails a prompt.
- `skills.voice_hooks_installed()` checks both are present and current; start
  refuses otherwise, with the fix in the reason. An agent started before the
  hooks were refreshed still runs the old set: restart the agent once.

Window options: `@voice` (`on` or `screen` while a session runs, following the
console's view, set by start and `/api/voice/view`, cleared by stop and at
server startup for windows a previous process left marked),
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

**A message is typed at most once.** The page gives each message an `id` when it
forms it (random, 96 bits) and a retry sends the same one. The session remembers
the last 32 ids (`VoiceSession.message()`, a `Message` record each): a request
whose id was typed answers `{"ok": true}` without typing, one whose text went but
not its Enter sends only the Enter, and one that arrives while the first still
types waits on the record's lock, then finds it done. A request without an id (a
page older than this rule) types every time. Tested on the route in
`tests/unit/test_voice_console_server.py`.

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
| `POST /api/voice/start` `{target, lang, view}` | Start, or return the running session's snapshot |
| `POST /api/voice/view` `{target, view}` | What the page shows (`full`, `dock`): the agent's next prompt follows |
| `POST /api/voice/stop` `{target}` | Stop, clear `@voice` |
| `GET /api/voice/session?target=` | The snapshot for a reconnecting page |
| `POST /api/voice/lang` `{target, lang}` | Switch the voice language |
| `POST /api/voice/skip` `{target, turn}` | Drop the rest of that answer |
| `POST /api/voice/say` `{target, text, id}` | Type the message into the window, then Enter; an `id` already typed is answered `ok` without typing |
| `POST /api/voice/answer` `{target, yes}` | Yes sends Enter, No sends Escape (and writes `idle`) |
| `POST /api/voice/interrupt` `{target}` | Stop agent: drop every answer waiting, send Escape |
| `GET /api/voice/audio/{id}` | A clip's audio |

## The console

The Voice button sits in the terminal status bar. It is always shown, dimmed
with the reason when TTS or transcription is missing or the window cannot start
(`/api/voice/check`). Tapping it is the user gesture that creates and resumes
the one `AudioContext` and asks for the mic permission.

### The two views

One session, two ways to show it, chosen per page. **Full screen** (`full`, the
default on a phone) covers the terminal and shows no conversation text: a phone
on a mount or a walk. **The bar** (`dock`, the default with a fine pointer on a
screen at least 900 px wide) moves `#voice-console` into the terminal's column,
over the status bar, so xterm refits around it and the terminal keeps its keys;
what the answer says is on screen in the terminal itself. A button switches the
view at any time, and the choice is kept in `localStorage` (`voice-view`).

The page tells the session its view at start, on a switch and on a resume from
hold, and the session sets `@voice` so the agent writes for the ear or for the
screen. With two pages on one session, the last one to say wins. Both views have
a button named after what a tap does (Send, Skip, Retry, Resume). Full screen is a
dialog: a tap anywhere does the same, the keys come into the console, and what it
covers is inert until it leaves. In the bar a click belongs to the terminal,
dictation's controls are hidden (they would hear what the session hears), and each
of the bar's buttons hands the keys back to the terminal. The session stays on its window if the page moves to
another one; the bar names that window.

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

**What iOS allows, measured on 2026-11-05** (an iPhone on iOS 26.6.1, the
home-screen app, older AirPods without Conversation Awareness). Don't try these
again without a new reason. With the mic open, the answer drops whenever the user
speaks or there is noise, which makes a conversation in wind or music unusable.
In a quiet room its level is about the same as with no mic. Nothing below gives
hands-free without that drop:

| While the answer plays | Does it drop? | What stops it |
|---|---|---|
| Mic open, with or without voice processing | Yes (founder's report, with no per-step recheck of voice processing) | |
| Track `enabled = false` | Yes | |
| Mic open, session `playback` | Yes | |
| `mediaSession.setMicrophoneActive(false)` | No | `setMicrophoneActive(true)` throws `InvalidStateError: Activating capture must be called from a user gesture handler` |
| Mic closed, session `playback` | No | Reopening takes 3 to 4 s, and the permission comes back now and then |

A few quick reopens did not prompt, which proves nothing about the prompts of
October: WebKit grants a reopen without a gesture only while the capture has not
been inactive beyond a reprompt interval in minutes, and never on a muted page
(`hasGrantedRequest` in `UserMediaPermissionRequestManagerProxy.cpp`; the interval's
value on iOS was not checked). A longer closure was never measured.

With the screen locked in the home-screen app, iOS mutes the mic track on lock and
ends it after repeated locks, while the page's timers and network keep running. Web
Audio clips did not finish, and stayed stuck after unlocking until a reload. An
`<audio>` clip already playing carried on, and one started while locked seemed to
wait for the unlock. The AirPods stem did reach `mediaSession` handlers while
locked. These runs were not locked from end to end, and a Safari tab was not tried.

A native app is what would remain, for the lock and the permission. That is
untested, and it may not shorten the reopen. The founder's call: not now.

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
after 7 s without it a soft double tone reminds the user once.

The pieces wait in the model in the order said (`m.pieces`, then `m.words` once
folded in; `MerlinVoiceCore.pieceCut`, `pieceHeard`, `nextMessage`, unit-tested),
whatever order their transcriptions land in. A piece is folded in only once every
piece before it is, and it carries `send` when the message ends with it: its text
ended with the end word, or a tap asked to send what was said up to it. One
message is on its way at a time: the next waits until `say` answered for this one.

**Send answers at once.** A tap in `TX` (or an end word heard while earlier pieces
are still on their way) plays the `send` earcon and shows "Sending" (`proc`) right
away, before any transcription lands; the speech in progress is cut and goes with
the message (vad-web hands it over a microtask after `pause()`, hence
`cutForSend`). The message goes by itself once its pieces are transcribed, and
the earcon is not played again then. A tap while a send is asked does nothing
more. After 3 s (`SEND_SLOW_MS`) still sending, the hint says "Waiting for the
connection", in the bar too (`data-hint-shown`). Nothing claims the message
reached the agent before `say` answered `ok`: until then it is "Sending", then
"Working".

**Every request of a turn has a deadline** (`AbortController`, the constants at
the top of `voice-console.js`): a transcription attempt is cut after 20 s
(`TRANSCRIBE_TIMEOUT_MS`) and counts as failed, tried twice more (`PIECE_RETRY_MS`,
1 s and 2 s), then the piece is marked failed and the error "Could not reach
Merlin to send what you said" offers a tap to retry. A failed piece holds back
the ones after it, never dropped: the retry transcribes every failed piece again,
in order, and a send asked before (by a tap, or by the end word in a later piece)
goes once they are all transcribed, without another tap. `say` is cut after 15 s
(`SAY_TIMEOUT_MS`) into the same error; its retry sends the same id. The e2e tests
lower the deadlines through `window.MerlinVoiceTimings`, set before the page
loads. A refused transcription (402, 413) drops the message with its error.

The e2e tests: `test_a_message_waits_for_its_end_word` holds two pieces, checks
nothing reached the window, then releases the end word;
`test_a_send_tap_answers_at_once_and_the_message_goes_once_transcribed` taps while
the transcription is held; `test_a_transcription_that_never_answers_ends_in_a_retry_that_keeps_every_piece`
and `test_a_say_whose_answer_is_lost_is_retried_and_typed_once` cover the two
deadlines.

Modes (`MerlinVoice.mode()`, shown name): `tune` (Connecting), `listen`
(Listening), `tx` (Hearing you, speech detected or a message held), `proc`
(Working, also while resyncing; Sending from the Send tap or the end word until
`say` answered), `rx` (Speaking, the answer plays), `ask` (Your answer: Yes / No / Exit), `error` (Error, tap to
retry), `resume` (Paused, after a reload a tap unlocks audio). The VAD runs only
in `listen` and `tx` with nothing playing: no listening while the agent is busy
or the answer plays (no voice barge-in).
Clips are fetched two ahead of playback, ordered by revision, retried twice;
a clip that still fails stalls the line with "Part of the answer could not be
downloaded" (tap retries, Skip answer drops that answer). Earcons are Nova's,
synthesized with WebAudio, plus an alert for `ASK`. Wake lock while open;
`prefers-reduced-motion` draws a still trace.

**The look is Merlin's.** `voice-console.css` uses the dashboard's variables
(`dashboard.css`: Geist and Geist Mono, the `#0f1117` palette, bordered cards). It
names the window and says what it is doing in plain words; the first version's
radio dress (mode codes, a call sign, a made-up frequency, the level meter and the
framed oscilloscope) is gone, the founder's call of 2026-11-05. The chrome stays
neutral, and the state's word is the screen, anchored from the top so it stays put,
with what it needs to say (the question, the error, the instruction) right under it
and the buttons at the thumb. A clock shows only how long the agent has been working.
One signal colour (`--vc-sig`) says whose turn it is, in the Sessions board's
accents: green the user's (`listen`, `tx`), yellow the agent's (`proc`, `rx`), blue a
question (`ask`), red an error. It goes on the light and on a thin trace, the quiet
sign of life, which fades each frame into its own background, read from the CSS. A
tint is kept for what needs the user: the question and the error, and Yes is the one
action that wears its colour at rest. There is no trace on a question or an error.
Nothing is filled.

**The bar keeps its height.** It is laid out on its own width (a container query,
since the sidebar and the side panels decide how much of the screen it gets), on one
row at every width, an iPhone's included: the light and the state's word, the trace
in the room left over, the actions, the switch to full screen. From 640 px it also
names the window and shows the hint and how long the agent has been working. The
language is the status bar's picker, right under the bar, which also tells the
session. The state changes what is in the row, not the row, and the terminal does
not refit through a turn. A question keeps one line next to its answers and, when it
does not fit, offers the rest behind a tap (the session may be on a window the page
has left). An error has to be read: it shows in full on a line of its own, the one
thing that grows the bar unasked, along with a passing notice that does not fit. On a
touch screen the controls are 36 px tall, with 44 px targets drawn in the gaps and
never over a neighbour.

**No conversation text in the console.** It shows no captions (neither what
the user said nor the answer), no tool label and no pane excerpt on `ask`, only
the mode, the scope, a short hint and the buttons. Full screen reads as a voice
call, not something to read while driving (the founder's call, 2026-10-28); the
bar leaves the terminal in view, which already shows everything. The `heard`,
`say` and `activity` frames still feed the model (`m.captions`, `m.activity`),
which is bounded and tested, so captions can come back as an option without
touching the server.

### The mute

The mic button (`.vc-mic`, `data-mic`, in the header tools so both views have it)
is a real `<button>` with `aria-pressed` and the labels "Mute microphone" /
"Unmute microphone", a mic or a struck-through mic icon. It is outside the
`data-act` buttons: a full-screen tap on it only toggles the mute, never the main
action, and in the bar a mouse click hands the keys back like the other buttons.

Muted (`setMuted`), `syncListening` keeps the VAD paused on the open stream, so
no piece is cut, uploaded or transcribed. The speech in progress is dropped: the
pause hands it to `onUtterance`, which drops what is not listened for and no
Send asked (`hearing` goes back to `idle`). The pieces held are kept, a Send tap
still sends them, and after unmuting the message can go on to its end word. The
session is not told: the server state, the answer and its level are unchanged.
The mute lasts across turns and is not stored: a new console starts unmuted.
The hint says "Microphone off" in `listen` and `tx` (in the bar too, through
`data-hint-shown`), the trace is a flat line, the end-word reminder does not
fire, and soft glides (`mute`, `unmute` earcons) mark the change.

The iOS constraints hold: one stream, never closed or reopened by the mute, no
`getUserMedia`, no change to `navigator.audioSession`; unmute restarts the VAD
only where it runs (`listen`, or `tx`, with nothing playing). The track's
`enabled` is left alone: the table above shows it does not keep iOS from turning
the answer down, and the paused VAD already hears nothing. The e2e tests
`test_the_mute_drops_what_is_being_said_and_keeps_what_was_said`,
`test_the_mute_lasts_through_a_turn_and_send_still_sends` and
`test_the_bar_has_the_mic_button_and_says_the_mic_is_off` assert no transcription
request while muted, one live and enabled track, and the audio session kept;
`test_unmuting_listens_again_only_where_the_console_listens` counts VAD starts
(`MerlinVoice.vadStarts()`) through an unmute in `proc` and `rx`, and
`test_no_end_word_reminder_while_muted` lowers `REMIND_MS` through
`window.MerlinVoiceTimings` and checks the reminder stays quiet muted and comes
back unmuted.

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
  reference server's HTTP layer, provider selection and the ElevenLabs request
  and refusals through an injected `httpx.MockTransport`, the warm-up, the server's download and preflight,
  `speakable`/`Chunker`, the reader on real transcript lines, the hooks against
  a private tmux server, the session lifecycle and bounds, the console server
  side), `tests/js/voice-core.test.js` (the console model).
- E2E: `tests/e2e/test_voice_console.py`: Chromium's fake mic plays a spoken
  WAV so the real VAD fires, `MERLIN_TRANSCRIBE_FAKE` for transcription, a fake
  TTS server, a fake agent writing a Claude transcript.
- Tests never write the real `~/.claude/settings.json`: the e2e servers run on
  their own `HOME`, the unit tests redirect the settings path.
