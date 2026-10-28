/*
 * Tests for voice/static/voice-core.js, the voice console's pure parts: the
 * WAV encoder, the call sign and frequency, and the model that folds the
 * server's frames and the page's playback into the mode on screen.
 *
 * Run: node --test tests/js/   (also part of `uv run scripts.py validate`)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const Core = require("../../voice/static/voice-core.js");

const T = "main:@3";
const frame = (event, fields) => Object.assign({ type: "voice", event: event, target: T }, fields || {});

test("encodeWav writes a 16-bit mono PCM header and clamps samples", () => {
    const buf = Core.encodeWav(new Float32Array([0, 1, -1, 2, -2, 0.5]), 16000);
    const v = new DataView(buf);
    const ascii = (o, n) => String.fromCharCode(...new Uint8Array(buf, o, n));
    assert.equal(ascii(0, 4), "RIFF");
    assert.equal(ascii(8, 4), "WAVE");
    assert.equal(ascii(36, 4), "data");
    assert.equal(v.getUint32(4, true), 36 + 12);
    assert.equal(v.getUint16(20, true), 1);
    assert.equal(v.getUint16(22, true), 1);
    assert.equal(v.getUint32(24, true), 16000);
    assert.equal(v.getUint32(28, true), 32000);
    assert.equal(v.getUint16(34, true), 16);
    assert.equal(v.getUint32(40, true), 12);
    assert.equal(buf.byteLength, 56);
    const s = (i) => v.getInt16(44 + i * 2, true);
    assert.deepEqual([0, 1, 2, 3, 4].map(s), [0, 32767, -32768, 32767, -32768]);
    assert.equal(s(5), 16383);
});

test("callSign and frequency are stable per window", () => {
    assert.equal(Core.callSign("main:@3"), "MAIN-3");
    assert.equal(Core.callSign("my-project_2:@17"), "MYPROJ-17");
    assert.equal(Core.callSign(""), "MRLN");
    assert.equal(Core.frequency("main:@3"), Core.frequency("main:@3"));
    assert.notEqual(Core.frequency("main:@3"), Core.frequency("main:@4"));
    const f = parseFloat(Core.frequency("main:@3"));
    assert.ok(f >= 144 && f < 148, f);
    assert.match(Core.frequency("x:@1"), /^14[4-7]\.\d\d[05]$/);
});

test("a fresh model listens; another window's frames are ignored", () => {
    const m = Core.createModel(T);
    assert.equal(Core.mode(m), "listen");
    assert.equal(Core.applyFrame(m, { type: "voice", event: "state", state: "thinking", target: "main:@4" }), false);
    assert.equal(Core.applyFrame(m, { type: "session", target: T }), false);
    assert.equal(Core.mode(m), "listen");
});

test("a turn: speech, upload, thinking, clips in order, then listening once played", () => {
    const m = Core.createModel(T);
    m.local = "hearing";
    assert.equal(Core.mode(m), "tx");
    m.local = "sending";
    assert.equal(Core.mode(m), "proc");
    m.local = "waiting";
    Core.applyFrame(m, frame("state", { state: "thinking" }));
    assert.equal(m.local, "idle", "the server's frame answers the upload");
    Core.applyFrame(m, frame("heard", { text: "run the tests" }));
    Core.applyFrame(m, frame("activity", { label: "command" }));
    assert.equal(Core.mode(m), "proc");
    assert.equal(m.activity, "command");
    Core.applyFrame(m, frame("say", { id: "a", text: "The tests pass.", turn: 1 }));
    Core.applyFrame(m, frame("state", { state: "speaking" }));
    Core.applyFrame(m, frame("say", { id: "b", text: "All of them.", turn: 1 }));
    assert.equal(Core.mode(m), "rx");
    assert.equal(Core.nextClip(m).id, "a");
    Core.applyFrame(m, frame("turn_end", { turn: 1 }));
    Core.applyFrame(m, frame("state", { state: "listening" }));
    assert.equal(Core.mode(m), "rx", "still playing: not listening yet");
    Core.clipDone(m);
    assert.equal(Core.nextClip(m).id, "b");
    Core.clipDone(m);
    assert.equal(Core.nextClip(m), null);
    assert.equal(Core.mode(m), "listen");
    assert.deepEqual(m.captions.map((c) => c.who + ":" + c.text),
        ["you:run the tests", "agent:The tests pass. All of them."]);
});

test("never listening while the agent works, even mid-speech", () => {
    const m = Core.createModel(T);
    m.local = "hearing";
    Core.applyFrame(m, frame("state", { state: "thinking" }));
    assert.equal(Core.mode(m), "proc");
});

test("a clip is queued once, even when a snapshot lists it again", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    Core.applySnapshot(m, { active: true, state: "speaking", clips: [{ id: "a", text: "one", turn: 1 }, { id: "b", text: "two", turn: 1 }] });
    assert.deepEqual(m.queue.map((c) => c.id), ["a", "b"]);
    Core.nextClip(m);
    Core.clipDone(m);
    Core.applySnapshot(m, { active: true, state: "speaking", clips: [{ id: "a", text: "one", turn: 1 }, { id: "b", text: "two", turn: 1 }] });
    assert.deepEqual(m.queue.map((c) => c.id), ["b"]);
});

test("skip drops the turn being heard and its late clips, not the next turn", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "b", text: "two", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "c", text: "next", turn: 2 }));
    Core.nextClip(m);
    assert.equal(Core.skip(m), 1);
    assert.equal(m.playing, null);
    assert.deepEqual(m.queue.map((c) => c.id), ["c"]);
    Core.applyFrame(m, frame("say", { id: "d", text: "late", turn: 1 }));
    assert.deepEqual(m.queue.map((c) => c.id), ["c"]);
    assert.equal(Core.skip(Core.createModel(T)), null);
});

test("silence drops everything waiting (Stop agent)", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "b", text: "two", turn: 2 }));
    Core.nextClip(m);
    Core.silence(m);
    assert.equal(m.queue.length, 0);
    assert.equal(m.playing, null);
    Core.applyFrame(m, frame("say", { id: "c", text: "late", turn: 2 }));
    assert.equal(m.queue.length, 0);
});

test("ask wins over the answer and clears when the state moves on", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    Core.applyFrame(m, frame("ask", { excerpt: "Allow Bash?", text: "The agent needs your answer.", audio: "z" }));
    Core.applyFrame(m, frame("state", { state: "ask" }));
    assert.equal(Core.mode(m), "ask");
    assert.equal(m.ask.excerpt, "Allow Bash?");
    Core.applyFrame(m, frame("state", { state: "speaking" }));
    assert.equal(m.ask, null);
    assert.equal(Core.mode(m), "rx");
});

test("errors: a local fault shows until retried; a server error passes as a notice", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("error", { message: "Nothing heard, try again" }));
    Core.applyFrame(m, frame("state", { state: "listening" }));
    assert.equal(m.notice, "Nothing heard, try again");
    assert.equal(Core.mode(m), "listen");
    Core.applyFrame(m, frame("state", { state: "error" }));
    assert.equal(Core.mode(m), "error");
    Core.applyFrame(m, frame("state", { state: "listening" }));
    m.local = "error";
    assert.equal(Core.mode(m), "error");
});

test("off ends the console, with the server's reason", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("state", { state: "off", reason: "The window was closed" }));
    assert.equal(Core.mode(m), "off");
    assert.equal(m.offReason, "The window was closed");
    const n = Core.createModel(T);
    Core.applySnapshot(n, { active: false, target: T });
    assert.equal(Core.mode(n), "off");
});

test("captions keep the last few lines", () => {
    const m = Core.createModel(T);
    for (let i = 0; i < 10; i++) Core.applyFrame(m, frame("heard", { text: "line " + i }));
    assert.equal(m.captions.length, Core.MAX_CAPTIONS);
    assert.equal(m.captions[m.captions.length - 1].text, "line 9");
});
