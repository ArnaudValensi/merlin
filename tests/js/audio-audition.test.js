/*
 * Tests for files/static/audio-audition.js — the single-clip inline audition
 * controller shared by the File Browser listing.
 *
 * Run: node --test tests/js/   (also part of `uv run scripts.py validate`)
 *
 * The controller is pure state with injected effects, so every case here drives
 * it with a fake audio element and asserts on the onStart / onStop callbacks and
 * on playingPath(). No browser, no real audio.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { createAuditionController } = require("../../files/static/audio-audition.js");

// --- fakes ---------------------------------------------------------------

// A minimal <audio>-like fake. `playImpl` decides what play() does:
//   'resolve' (default) — resolves, i.e. playback started
//   'reject'            — returns a rejected Promise (autoplay blocked, error)
//   'throw'             — throws synchronously
// Call fake.end() to simulate the clip finishing on its own.
function makeAudio(playImpl) {
    const listeners = {};
    return {
        src: null,
        paused: true,
        playImpl: playImpl || "resolve",
        addEventListener(type, cb) {
            (listeners[type] = listeners[type] || []).push(cb);
        },
        removeEventListener(type, cb) {
            listeners[type] = (listeners[type] || []).filter((f) => f !== cb);
        },
        play() {
            if (this.playImpl === "throw") throw new Error("play threw");
            if (this.playImpl === "reject") return Promise.reject(new Error("blocked"));
            this.paused = false;
            return Promise.resolve();
        },
        pause() {
            this.paused = true;
        },
        end() {
            this.paused = true;
            (listeners["ended"] || []).slice().forEach((f) => f());
        },
    };
}

// Build a controller wired to a shared event log and a per-path audio registry
// so a test can reach the fake element created for a given path.
function harness() {
    const events = [];
    const audios = {};
    const controller = createAuditionController({
        createAudio(src) {
            const impl = harness._nextImpl || "resolve";
            harness._nextImpl = null;
            const a = makeAudio(impl);
            a.src = src;
            audios[src] = a;
            return a;
        },
        onStart(path) {
            events.push(["start", path]);
        },
        onStop(path) {
            events.push(["stop", path]);
        },
    });
    return { controller, events, audios };
}

// --- tests ---------------------------------------------------------------

test("toggle on idle starts the path and reports it playing", () => {
    const { controller, events } = harness();
    const started = controller.toggle("a.wav");
    assert.equal(started, true);
    assert.deepEqual(events, [["start", "a.wav"]]);
    assert.equal(controller.playingPath(), "a.wav");
});

test("toggle on the playing path stops it and clears state", () => {
    const { controller, events } = harness();
    controller.toggle("a.wav");
    const started = controller.toggle("a.wav");
    assert.equal(started, false);
    assert.deepEqual(events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
    ]);
    assert.equal(controller.playingPath(), null);
});

test("toggle to another path stops the old and starts the new, never both", () => {
    const { controller, events, audios } = harness();
    controller.toggle("a.wav");
    controller.toggle("b.wav");
    assert.deepEqual(events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
        ["start", "b.wav"],
    ]);
    assert.equal(controller.playingPath(), "b.wav");
    assert.equal(audios["a.wav"].paused, true);
    assert.equal(audios["b.wav"].paused, false);
});

test("a clip that ends on its own fires stop and clears state", () => {
    const { controller, events, audios } = harness();
    controller.toggle("a.wav");
    audios["a.wav"].end();
    assert.deepEqual(events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
    ]);
    assert.equal(controller.playingPath(), null);
});

test("a rejected play() rolls back so no path stays marked playing", async () => {
    const h = harness();
    harness._nextImpl = "reject";
    h.controller.toggle("a.wav");
    // start effect fired optimistically before play() settled
    assert.deepEqual(h.events, [["start", "a.wav"]]);
    // let the rejected promise's handler run
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(h.events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
    ]);
    assert.equal(h.controller.playingPath(), null);
});

test("a synchronously throwing play() rolls back cleanly", () => {
    const h = harness();
    harness._nextImpl = "throw";
    h.controller.toggle("a.wav");
    assert.deepEqual(h.events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
    ]);
    assert.equal(h.controller.playingPath(), null);
});

test("a stale play() rejection after switching does not disturb the new sample", async () => {
    const h = harness();
    harness._nextImpl = "reject"; // a.wav will reject
    h.controller.toggle("a.wav");
    h.controller.toggle("b.wav"); // switch before a's rejection settles
    await Promise.resolve();
    await Promise.resolve();
    // a's late rejection must not stop b or clear state
    assert.equal(h.controller.playingPath(), "b.wav");
    const stops = h.events.filter((e) => e[0] === "stop").map((e) => e[1]);
    assert.deepEqual(stops, ["a.wav"]); // only the switch-out stop for a
});

test("a stale ended event after switching does not stop the new sample", () => {
    const { controller, audios } = harness();
    controller.toggle("a.wav");
    controller.toggle("b.wav");
    audios["a.wav"].end(); // a already detached; its ended must be inert
    assert.equal(controller.playingPath(), "b.wav");
    assert.equal(audios["b.wav"].paused, false);
});

test("stop while idle is a safe no-op", () => {
    const { controller, events } = harness();
    controller.stop();
    assert.deepEqual(events, []);
    assert.equal(controller.playingPath(), null);
});

test("stop while playing fires stop once and clears state", () => {
    const { controller, events } = harness();
    controller.toggle("a.wav");
    controller.stop();
    controller.stop();
    assert.deepEqual(events, [
        ["start", "a.wav"],
        ["stop", "a.wav"],
    ]);
    assert.equal(controller.playingPath(), null);
});
