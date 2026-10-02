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
        createAnswer() { return Promise.resolve({ type: "answer", sdp: "answer-sdp" }); }
        setLocalDescription() { return Promise.resolve(); }
        addIceCandidate() { return Promise.resolve(); }
        getStats() { return Promise.resolve(new Map()); }
        close() { this.closed = true; }
    }

    const videoListeners = {};
    const parentAttrs = {};
    const video = {
        srcObject: null,
        parentElement: { setAttribute(k, v) { parentAttrs[k] = v; } },
        addEventListener(type, cb) { (videoListeners[type] = videoListeners[type] || []).push(cb); },
        removeEventListener(type, cb) {
            videoListeners[type] = (videoListeners[type] || []).filter((f) => f !== cb);
        },
        play() { return Promise.resolve(); },
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
        MediaStream: class {},
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
        sockets, peers, video, document, parentAttrs, advance, flush,
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
