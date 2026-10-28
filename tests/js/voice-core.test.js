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
let REV = 0;
const frame = (event, fields) =>
    Object.assign({ type: "voice", event: event, target: T, session: "s1", rev: ++REV }, fields || {});

// Play the head clip as the console does once its download landed.
function playNext(m) {
    for (const c of Core.toFetch(m, 0)) Core.fetching(m, c);
    for (const c of m.queue) Core.fetched(m, c);   // the downloads in flight land
    const clip = Core.nextReady(m);
    if (clip) Core.begin(m, clip);
    return clip;
}

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
    Core.applyFrame(m, frame("say", { id: "a", text: "The tests pass.", turn: 1 }));
    Core.applyFrame(m, frame("state", { state: "speaking" }));
    Core.applyFrame(m, frame("say", { id: "b", text: "All of them.", turn: 1 }));
    assert.equal(Core.mode(m), "rx");
    assert.equal(playNext(m).id, "a");
    Core.applyFrame(m, frame("turn_end", { turn: 1 }));
    Core.applyFrame(m, frame("state", { state: "listening" }));
    assert.equal(Core.mode(m), "rx", "still playing: not listening yet");
    Core.clipDone(m);
    assert.equal(playNext(m).id, "b");
    Core.clipDone(m);
    assert.equal(playNext(m), null);
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

test("only the playback window is fetched, and it moves as clips are heard", () => {
    const m = Core.createModel(T);
    for (let i = 0; i < 100; i++) Core.applyFrame(m, frame("say", { id: "c" + i, text: "piece " + i, turn: 1 }));
    let wanted = Core.toFetch(m, 0);
    assert.deepEqual(wanted.map((c) => c.id), ["c0", "c1"]);
    wanted.forEach((c) => Core.fetching(m, c));
    assert.deepEqual(Core.toFetch(m, 0), [], "nothing more while those download");
    Core.fetched(m, wanted[0]);
    Core.begin(m, Core.nextReady(m));
    assert.deepEqual(Core.toFetch(m, 0).map((c) => c.id), ["c2"], "one more once one plays");
    const heard = [];
    for (let guard = 0; guard < 500 && (m.playing || m.queue.length); guard++) {
        if (m.playing) { heard.push(m.playing.id); Core.clipDone(m); continue; }
        playNext(m);
        const fetchingNow = m.queue.filter((c) => c.status === "fetching" || c.status === "ready").length;
        assert.ok(fetchingNow <= Core.WINDOW, fetchingNow);
    }
    assert.deepEqual(heard, Array.from({ length: 100 }, (_, i) => "c" + i), "each once, in order");
    assert.ok(m.played.length <= Core.MAX_PLAYED);
});

test("a failed download is retried in place, then given up but recoverable", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "first", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "b", text: "second", turn: 1 }));
    const [a] = Core.toFetch(m, 0);
    Core.fetching(m, a);
    const wait = Core.fetchFailed(m, a, 1000);
    assert.ok(wait > 0);
    assert.equal(m.queue[0].id, "a", "still first in line");
    assert.equal(Core.nextReady(m), null, "b never jumps ahead");
    assert.deepEqual(Core.toFetch(m, 1000).map((c) => c.id), ["b"], "a waits for its retry");
    assert.ok(Core.toFetch(m, 1000 + wait).some((c) => c.id === "a"));
    assert.equal(m.captions.length, 0, "nothing captioned before it plays");
    for (let i = 1; i < Core.MAX_TRIES; i++) { Core.fetching(m, a); Core.fetchFailed(m, a, 5000); }
    assert.deepEqual(m.queue.map((c) => c.id), ["b"]);
    assert.match(m.notice, /could not be played/);
    assert.ok(!m.played.includes("a"));
    // A snapshot that still lists it brings it back, ahead of b.
    Core.beginSync(m);
    Core.applySnapshot(m, { active: true, session: "s1", rev: REV, state: "speaking",
        clips: [{ id: "a", text: "first", turn: 1, rev: 1 }, { id: "b", text: "second", turn: 1, rev: 2 }] });
    assert.deepEqual(m.queue.map((c) => c.id), ["a", "b"]);
    assert.equal(m.queue[0].tries, 0);
});

test("a snapshot is folded in before the frames that waited behind it", () => {
    const m = Core.createModel(T);
    const base = REV;
    Core.applyFrame(m, frame("state", { state: "thinking" }));
    // The socket dropped: A was said and missed. The page asks for the snapshot,
    // and meanwhile B and a busy state arrive on the new socket.
    REV++;   // A's say, never received
    Core.beginSync(m);
    const b = frame("say", { id: "B", text: "two", turn: 1 });
    const busy = frame("state", { state: "speaking" });
    Core.applyFrame(m, b);
    Core.applyFrame(m, busy);
    assert.equal(Core.nextReady(m), null, "nothing starts while syncing");
    assert.equal(m.queue.length, 0);
    // The snapshot was taken after A, before B.
    Core.applySnapshot(m, { active: true, session: "s1", rev: base + 2, state: "listening",
        clips: [{ id: "A", text: "one", turn: 1, rev: base + 2 }] });
    assert.deepEqual(m.queue.map((c) => c.id), ["A", "B"]);
    assert.equal(m.server, "speaking", "the newer frame wins over the snapshot");
});

test("a stale snapshot never replaces a newer state", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("state", { state: "thinking" }));
    Core.beginSync(m);
    Core.applySnapshot(m, { active: true, session: "s1", rev: REV - 5, state: "listening", clips: [] });
    assert.equal(m.server, "thinking");
});

test("frames already folded in, or from another session, are ignored", () => {
    const m = Core.createModel(T);
    const f = frame("say", { id: "a", text: "one", turn: 1 });
    Core.applyFrame(m, f);
    playNext(m);
    Core.clipDone(m);
    Core.applyFrame(m, f);
    assert.equal(m.queue.length, 0, "a replayed frame");
    Core.applyFrame(m, Object.assign(frame("say", { id: "x", text: "old", turn: 9 }), { session: "s0" }));
    assert.equal(m.queue.length, 0, "another session");
});

test("skip drops the turn being heard and its late clips, not the next turn", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "b", text: "two", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "c", text: "next", turn: 2 }));
    playNext(m);
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
    playNext(m);
    Core.silence(m);
    assert.equal(m.queue.length, 0);
    assert.equal(m.playing, null);
    Core.applyFrame(m, frame("say", { id: "c", text: "late", turn: 2 }));
    assert.equal(m.queue.length, 0);
});

test("ask cuts the answer, which plays again afterwards, captioned once", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "one", turn: 1 }));
    playNext(m);
    Core.applyFrame(m, frame("ask", { excerpt: "Allow Bash?", text: "The agent needs your answer.", audio: "z" }));
    Core.applyFrame(m, frame("state", { state: "ask" }));
    assert.equal(Core.mode(m), "ask");
    Core.requeue(m);
    assert.equal(m.playing, null);
    assert.equal(m.queue[0].id, "a");
    assert.equal(m.queue[0].status, "ready", "still downloaded");
    Core.applyFrame(m, frame("state", { state: "speaking" }));
    assert.equal(m.ask, null);
    assert.equal(Core.mode(m), "rx");
    Core.begin(m, Core.nextReady(m));
    assert.equal(m.captions.filter((c) => c.who === "agent").length, 1);
    assert.equal(m.captions[0].text, "one");
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

test("captions, played ids and skipped turns stay bounded", () => {
    const m = Core.createModel(T);
    for (let i = 0; i < 10; i++) Core.applyFrame(m, frame("heard", { text: "line " + i }));
    assert.equal(m.captions.length, Core.MAX_CAPTIONS);
    assert.equal(m.captions[m.captions.length - 1].text, "line 9");
    for (let i = 0; i < 200; i++) {
        Core.applyFrame(m, frame("say", { id: "p" + i, text: "x", turn: 1000 + i }));
        playNext(m);
        if (i % 2) Core.skip(m); else Core.clipDone(m);
    }
    assert.ok(m.played.length <= Core.MAX_PLAYED);
    assert.ok(m.skipped.length <= 32);
});

test("a download that fails once plays after its retry, each clip once, in order", () => {
    const m = Core.createModel(T);
    Core.applyFrame(m, frame("say", { id: "a", text: "first", turn: 1 }));
    Core.applyFrame(m, frame("say", { id: "b", text: "second", turn: 1 }));
    const [a, b] = Core.toFetch(m, 0);
    Core.fetching(m, a);
    Core.fetching(m, b);
    Core.fetched(m, b);
    const wait = Core.fetchFailed(m, a, 0);
    assert.equal(Core.nextReady(m), null, "b is ready but waits for a");
    // A snapshot during the failure changes nothing in line.
    Core.beginSync(m);
    Core.applySnapshot(m, { active: true, session: "s1", rev: REV, state: "speaking",
        clips: [{ id: "a", text: "first", turn: 1, rev: 1 }, { id: "b", text: "second", turn: 1, rev: 2 }] });
    assert.deepEqual(m.queue.map((c) => c.id), ["a", "b"]);
    const [again] = Core.toFetch(m, wait);
    assert.equal(again.id, "a");
    Core.fetching(m, again);
    Core.fetched(m, again);
    const heard = [];
    let clip;
    while ((clip = Core.nextReady(m))) { Core.begin(m, clip); heard.push(clip.id); Core.clipDone(m); }
    assert.deepEqual(heard, ["a", "b"]);
    assert.equal(m.captions.filter((c) => c.who === "agent")[0].text, "first second");
});
