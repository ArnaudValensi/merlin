/*
 * Tests for app/static/client.js — the app-stream WebRTC client's state
 * machine: a destroyed or superseded connection must never act again, and a
 * hidden page pauses the stream unless it is in picture-in-picture.
 *
 * Run: node --test tests/js/   (also part of `uv run scripts.py validate`)
 *
 * client.js is a browser IIFE; it runs here in a vm context with fake
 * WebSocket, RTCPeerConnection, document, video and a controlled clock.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.join(__dirname, "../../app/static/client.js"),
    "utf8",
);

function makeEnv() {
    let now = 0;
    let nextId = 1;
    const timers = new Map();
    const sockets = [];
    const peers = [];
    const docListeners = {};

    function deferred() {
        let resolve, reject;
        const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
        return { promise, resolve, reject };
    }

    class FakeSocket {
        constructor(url) {
            this.url = url;
            this.readyState = 1;
            this.sent = [];
            this.closed = false;
            sockets.push(this);
        }
        send(text) { this.sent.push(JSON.parse(text)); }
        close() { this.closed = true; this.readyState = 3; }
        deliver(msg) { if (this.onmessage) this.onmessage({ data: JSON.stringify(msg) }); }
        drop(code) {
            this.readyState = 3;
            if (this.onclose) this.onclose({ code: code || 1006 });
        }
    }
    FakeSocket.OPEN = 1;

    class FakePeer {
        constructor() {
            this.remote = deferred();
            this.closed = false;
            this.connectionState = "new";
            peers.push(this);
        }
        setRemoteDescription() { return this.remote.promise; }
        createAnswer() { return Promise.resolve({ type: "answer", sdp: answerSdp }); }
        setLocalDescription(desc) { this.local = desc; return Promise.resolve(); }
        addIceCandidate() { return Promise.resolve(); }
        getStats() { return Promise.resolve(new Map()); }
        close() { this.closed = true; }
    }

    let answerSdp = "answer-sdp";
    const storage = new Map();
    const localStorage = {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => storage.set(k, String(v)),
    };

    const videoListeners = {};
    const parentAttrs = {};
    const video = {
        srcObject: null,
        muted: true,
        plays: 0,
        playResult: null,     // a function returning play()'s promise, when set
        parentElement: { setAttribute(k, v) { parentAttrs[k] = v; } },
        addEventListener(type, cb) { (videoListeners[type] = videoListeners[type] || []).push(cb); },
        removeEventListener(type, cb) {
            videoListeners[type] = (videoListeners[type] || []).filter((f) => f !== cb);
        },
        play() { this.plays++; return this.playResult ? this.playResult(this) : Promise.resolve(); },
        fire(type) { (videoListeners[type] || []).slice().forEach((cb) => cb({})); },
    };

    const document = {
        hidden: false,
        pictureInPictureElement: null,
        addEventListener(type, cb) { (docListeners[type] = docListeners[type] || []).push(cb); },
        removeEventListener(type, cb) {
            docListeners[type] = (docListeners[type] || []).filter((f) => f !== cb);
        },
        fire(type) { (docListeners[type] || []).slice().forEach((cb) => cb({})); },
    };

    const sandbox = {
        window: {},
        document,
        location: { protocol: "http:", host: "merlin.test" },
        WebSocket: FakeSocket,
        RTCPeerConnection: FakePeer,
        RTCRtpReceiver: { getCapabilities: () => ({ codecs: [{ mimeType: "video/H264" }] }) },
        MediaStream: class {
            constructor() { this.tracks = []; }
            addTrack(track) { this.tracks.push(track); }
        },
        localStorage,
        requestAnimationFrame: (fn) => fn(),
        setTimeout(fn, delay) {
            const id = nextId++;
            timers.set(id, { fn, due: now + (delay || 0) });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
        Date,
        JSON,
        Promise,
        String,
        Math,
    };
    vm.createContext(sandbox);
    vm.runInContext(SOURCE, sandbox);

    /** Move the clock forward, running every timer that falls due. */
    function advance(ms) {
        const end = now + ms;
        for (;;) {
            let next = null;
            for (const [id, t] of timers) {
                if (t.due <= end && (!next || t.due < next[1].due)) next = [id, t];
            }
            if (!next) break;
            timers.delete(next[0]);
            now = next[1].due;
            next[1].fn();
        }
        now = end;
    }

    const flush = () => new Promise((resolve) => setImmediate(resolve));

    return {
        MerlinApps: sandbox.window.MerlinApps,
        sockets, peers, video, document, parentAttrs, advance, flush, storage,
        setAnswer(sdp) { answerSdp = sdp; },
    };
}

function connect(env, opts) {
    const states = [];
    const stream = env.MerlinApps.connect({
        id: "probe",
        video: env.video,
        onState: (s) => states.push(s),
        ...(opts || {}),
    });
    return { stream, states };
}

async function offer(env, socket) {
    socket.deliver({ type: "welcome", host: "box", app: { id: "probe" } });
    socket.deliver({ type: "offer", sdp: "offer-sdp" });
    await env.flush();
}

async function goLive(env, socket) {
    await offer(env, socket);
    const peer = env.peers[env.peers.length - 1];
    peer.remote.resolve();
    await env.flush();
    await env.flush();
    peer.connectionState = "connected";
    peer.onconnectionstatechange();
}

test("a destroyed client never reconnects, even when negotiation fails later", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await offer(env, env.sockets[0]);
    assert.equal(env.peers.length, 1);

    stream.destroy();
    env.peers[0].remote.reject(new Error("negotiation failed"));
    await env.flush();
    env.advance(120000);

    assert.equal(env.sockets.length, 1, "no socket opened after destroy()");
    assert.equal(env.sockets[0].closed, true);
});

for (const outcome of ["resolves", "rejects"]) {
    test(`a superseded negotiation that ${outcome} never touches the new connection`, async () => {
        const env = makeEnv();
        const { stream, states } = connect(env);
        await offer(env, env.sockets[0]);

        stream.reconnect();
        assert.equal(env.sockets.length, 2);
        const fresh = env.sockets[1];
        if (outcome === "resolves") env.peers[0].remote.resolve();
        else env.peers[0].remote.reject(new Error("late failure"));
        await env.flush();
        await env.flush();
        env.advance(5000);  // past any automatic retry it might have queued

        assert.equal(fresh.closed, false, "the new socket stays open");
        assert.deepEqual(fresh.sent, [], "nothing sent on the new socket");
        assert.equal(env.sockets.length, 2, "no extra reconnect");
        assert.notEqual(states[states.length - 1], "error");
    });
}

test("an old negotiation settling while the new one is pending leaves it alone", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await offer(env, env.sockets[0]);
    stream.reconnect();
    await offer(env, env.sockets[1]);
    assert.equal(env.peers.length, 2);

    env.peers[0].remote.reject(new Error("late failure"));
    await env.flush();
    env.advance(5000);
    assert.equal(env.peers[1].closed, false, "the new peer is untouched");
    assert.equal(env.sockets.length, 2);

    env.peers[1].remote.resolve();
    await env.flush();
    await env.flush();
    assert.deepEqual(
        env.sockets[1].sent.map((m) => m.type),
        ["hello", "answer"],
        "the new negotiation completes on its own socket",
    );
});

test("a restarted app reconnects at once", async () => {
    const env = makeEnv();
    connect(env);
    await goLive(env, env.sockets[0]);
    env.sockets[0].deliver({ type: "restarted" });
    assert.equal(env.sockets.length, 2);
    assert.equal(env.sockets[0].closed, true);
});

test("leaving picture-in-picture while hidden starts the pause timer", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    await goLive(env, env.sockets[0]);
    assert.equal(states[states.length - 1], "live");
    env.document.hidden = true;
    env.document.pictureInPictureElement = env.video;
    env.document.fire("visibilitychange");
    env.advance(60000);
    assert.notEqual(states[states.length - 1], "paused", "PiP keeps it watched");

    env.document.pictureInPictureElement = null;
    env.video.fire("leavepictureinpicture");
    env.advance(31000);
    assert.equal(states[states.length - 1], "paused");
});

test("pausing cancels a pending reconnect", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    await goLive(env, env.sockets[0]);
    env.document.hidden = true;
    env.document.fire("visibilitychange");      // pause due at t=30000
    env.advance(29500);
    env.sockets[0].drop(1006);                  // reconnect due at t=30500
    env.advance(600);                           // the pause fires first
    assert.equal(states[states.length - 1], "paused");
    const opened = env.sockets.length;
    env.advance(60000);
    assert.equal(env.sockets.length, opened, "the reconnect was cancelled");
});

test("a lost server is retried with backoff, then reported closed", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    for (let i = 0; i < 7; i++) {
        env.sockets[env.sockets.length - 1].drop(1006);
        env.advance(5000);
    }
    assert.equal(env.sockets.length, 8);
    env.sockets[env.sockets.length - 1].drop(1006);
    env.advance(60000);
    assert.equal(env.sockets.length, 8);
    assert.equal(states[states.length - 1], "closed");
});

test("an unauthorized socket is an error, not a retry", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    env.sockets[0].drop(4401);
    env.advance(60000);
    assert.equal(env.sockets.length, 1);
    assert.equal(states[states.length - 1], "error");
});

test("a slow server start is not 'unreachable'; ICE gets 8 s from the offer", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    env.sockets[0].deliver({ type: "welcome", host: "box", app: { id: "probe" } });
    env.advance(30000);  // the server waits (another app's stop, say): no offer yet
    assert.equal(states[states.length - 1], "connecting");
    assert.equal(env.sockets.length, 1);

    env.sockets[0].deliver({ type: "offer", sdp: "offer-sdp" });
    await env.flush();
    env.advance(7000);
    assert.equal(states[states.length - 1], "connecting");
    env.advance(2000);  // 8 s after the offer, still no ICE: not the same network
    assert.equal(states[states.length - 1], "unreachable");
});

test("no offer at all within a minute is a failed start, retried", async () => {
    const env = makeEnv();
    const { states } = connect(env);
    env.advance(61000);
    assert.ok(states.includes("connecting"));
    assert.ok(!states.includes("unreachable"));
    assert.equal(env.sockets.length, 2, "one automatic retry");
});

// ---- sound -----------------------------------------------------------------

const CHROME_ANSWER = [
    "v=0",
    "m=video 9 UDP/TLS/RTP/SAVPF 96",
    "a=rtpmap:96 VP8/90000",
    "m=audio 9 UDP/TLS/RTP/SAVPF 97",
    "a=rtpmap:97 opus/48000/2",
    "a=fmtp:97 minptime=10;useinbandfec=1",
    "",
].join("\r\n");

test("the answer asks for stereo sound, and only once", () => {
    const { preferStereo } = makeEnv().MerlinApps;
    const stereo = preferStereo(CHROME_ANSWER);
    assert.match(stereo, /a=fmtp:97 minptime=10;useinbandfec=1;stereo=1\r\n/);
    assert.equal(preferStereo(stereo), stereo);
    const noFmtp = CHROME_ANSWER.replace("a=fmtp:97 minptime=10;useinbandfec=1\r\n", "");
    assert.match(preferStereo(noFmtp), /a=rtpmap:97 opus\/48000\/2\r\na=fmtp:97 stereo=1\r\n/);
    const videoOnly = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 VP8/90000\r\n";
    assert.equal(preferStereo(videoOnly), videoOnly);
    const mono = CHROME_ANSWER.replace("useinbandfec=1", "stereo=0");
    assert.equal(preferStereo(mono), mono, "an explicit choice is kept");
});

test("the stereo answer is the one set locally and sent to the streamer", async () => {
    const env = makeEnv();
    env.setAnswer(CHROME_ANSWER);
    connect(env);
    await goLive(env, env.sockets[0]);
    const sent = env.sockets[0].sent.find((m) => m.type === "answer");
    assert.match(sent.sdp, /stereo=1/);
    assert.equal(env.peers[0].local.sdp, sent.sdp);
});

test("picture and sound tracks play in one element", async () => {
    const env = makeEnv();
    connect(env);
    await goLive(env, env.sockets[0]);
    const peer = env.peers[0];
    peer.ontrack({ track: { kind: "video" }, streams: [{ id: "a" }] });
    const media = env.video.srcObject;
    peer.ontrack({ track: { kind: "audio" }, streams: [{ id: "b" }] });
    assert.equal(env.video.srcObject, media, "the second track joins, never replaces");
    assert.deepEqual(media.tracks.map((t) => t.kind), ["video", "audio"]);
});

test("a browser that refuses sound without a gesture still shows the picture, muted", async () => {
    const env = makeEnv();
    env.video.muted = false;
    env.video.playResult = (v) => (v.muted
        ? Promise.resolve()
        : Promise.reject(Object.assign(new Error("gesture"), { name: "NotAllowedError" })));
    connect(env);
    await goLive(env, env.sockets[0]);
    env.peers[0].ontrack({ track: { kind: "video" }, streams: [] });
    await env.flush();
    assert.equal(env.video.muted, true);
    assert.equal(env.video.plays, 2, "played again, muted");
});

test("ready says whether the stream carries sound", () => {
    const env = makeEnv();
    let ready = null;
    const { stream } = connect(env, { onReady: (info) => { ready = info.audio; } });
    env.sockets[0].deliver({ type: "ready", encoder: "vp8enc", codec: "VP8", audio: true });
    assert.equal(ready, true);
    assert.equal(stream.info.audio, true);
    assert.equal(env.parentAttrs["data-audio"], "1");
    env.sockets[0].deliver({ type: "ready", encoder: "vp8enc", codec: "VP8" });
    assert.equal(ready, false);
});

function gestureTarget() {
    const listeners = {};
    return {
        addEventListener(type, cb) { (listeners[type] = listeners[type] || []).push(cb); },
        removeEventListener(type, cb) {
            listeners[type] = (listeners[type] || []).filter((f) => f !== cb);
        },
        fire(type, target) {
            (listeners[type] || []).slice().forEach((cb) => cb({ target: target || {} }));
        },
        count: () => Object.values(listeners).reduce((n, l) => n + l.length, 0),
    };
}

test("sound is on where you play, off in the mini-player, until the user says otherwise", () => {
    for (const [surface, on] of [["player", true], ["panel", true], ["mini", false]]) {
        const env = makeEnv();
        const target = gestureTarget();
        const sound = env.MerlinApps.sound(env.video, surface, target);
        assert.equal(sound.wanted, on, surface);
        assert.equal(env.video.muted, true, "nothing changes before a gesture");
        target.fire("pointerup");
        assert.equal(env.video.muted, !on, `${surface} after the first gesture`);
    }
});

test("the toggle is remembered per surface and left alone by the gesture hook", () => {
    const env = makeEnv();
    const target = gestureTarget();
    const sound = env.MerlinApps.sound(env.video, "player", target);
    const toggle = { closest: (sel) => (sel === "[data-sound]" ? toggle : null) };

    // Pressing the toggle while muted: the hook must not unmute first (the
    // click would then read "on" and mute again).
    target.fire("pointerup", toggle);
    assert.equal(env.video.muted, true);
    sound.set(!sound.on);
    assert.equal(env.video.muted, false);

    sound.set(false);
    assert.equal(env.video.muted, true);
    assert.equal(env.storage.get("app-sound-player"), "0");
    target.fire("keydown");
    assert.equal(env.video.muted, true, "a muted choice stays muted");

    const again = env.MerlinApps.sound(env.video, "player", gestureTarget());
    assert.equal(again.wanted, false, "remembered");
    assert.equal(env.MerlinApps.sound(env.video, "panel", gestureTarget()).wanted, true,
        "another surface keeps its own");
});

test("sound comes back on the next gesture after a forced mute", () => {
    const env = makeEnv();
    const target = gestureTarget();
    env.video.srcObject = {};
    const sound = env.MerlinApps.sound(env.video, "panel", target);
    sound.apply();
    assert.equal(env.video.muted, false);
    env.video.muted = true;          // the browser's autoplay fallback
    target.fire("touchend");
    assert.equal(env.video.muted, false);
    assert.ok(env.video.plays >= 1, "playback resumed with sound");
    sound.destroy();
    assert.equal(target.count(), 0);
});
