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

function makeEnv(options = {}) {
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
        constructor(config) {
            // A browser that refuses the servers (a bad URL) throws here.
            if (options.rejectServers && config && config.iceServers && config.iceServers.length) {
                throw new Error("bad iceServers");
            }
            this.config = config;
            this.remote = deferred();
            this.closed = false;
            this.connectionState = "new";
            peers.push(this);
        }
        setRemoteDescription() { return this.remote.promise; }
        createAnswer() { return Promise.resolve({ type: "answer", sdp: answerSdp }); }
        setLocalDescription(desc) { this.local = desc; return Promise.resolve(); }
        addIceCandidate() { return Promise.resolve(); }
        getStats() { return Promise.resolve(this.report || new Map()); }
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
        location: { protocol: "http:", host: "merlin.test", search: options.search || "" },
        URLSearchParams,
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
        setInterval(fn, every) {
            const id = nextId++;
            timers.set(id, { fn, due: now + every, every });
            return id;
        },
        clearInterval(id) { timers.delete(id); },
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
            const [id, t] = next;
            if (t.every) t.due += t.every; else timers.delete(id);
            now = Math.max(now, t.due - (t.every || 0));
            t.fn();
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

test("a slow server start is not 'unreachable'; ICE gets up to 30 s from the offer", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    env.sockets[0].deliver({ type: "welcome", host: "box", app: { id: "probe" } });
    env.advance(30000);  // the server waits (another app's stop, say): no offer yet
    assert.equal(stream.state, "connecting");
    assert.equal(env.sockets.length, 1);

    env.sockets[0].deliver({ type: "offer", sdp: "offer-sdp" });
    await env.flush();
    env.advance(29000);
    assert.equal(stream.state, "connecting");
    env.advance(2000);  // 30 s after the offer and ICE never decided: give up
    assert.equal(stream.state, "unreachable");
});

test("after 4 s without a connection the state says it is still trying", async () => {
    const env = makeEnv();
    const details = [];
    connect(env, { onState: (s, d) => details.push([s, d]) });
    await offer(env, env.sockets[0]);
    env.advance(3900);
    assert.ok(!details.some(([, d]) => d.slow));
    env.advance(200);
    const last = details[details.length - 1];
    assert.equal(last[0], "connecting");
    assert.equal(last[1].slow, true);
});

test("ICE failing before any connection is unreachable after a short grace", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await offer(env, env.sockets[0]);
    env.peers[0].connectionState = "failed";
    env.peers[0].onconnectionstatechange();
    env.advance(5999);
    assert.equal(stream.state, "connecting", "late candidates may still revive it");
    env.advance(1);
    assert.equal(stream.state, "unreachable");
});

test("late candidates revive an early failure (STUN, the router's mapping)", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await offer(env, env.sockets[0]);
    const peer = env.peers[0];
    peer.remote.resolve();
    await env.flush();
    peer.connectionState = "failed";  // every early pair refused at once
    peer.onconnectionstatechange();
    env.advance(2000);
    peer.connectionState = "connected";  // the public candidate arrived
    peer.onconnectionstatechange();
    env.advance(10000);
    assert.equal(stream.state, "live");
});

async function dropLive(env, peer, how) {
    peer.connectionState = how;
    peer.onconnectionstatechange();
    if (how === "disconnected") env.advance(5000);  // the grace before calling it dropped
}

test("a dropped live connection gets three new sessions, then is unreachable", async () => {
    const env = makeEnv();
    const details = [];
    const { stream } = connect(env, { onState: (s, d) => details.push([s, d]) });
    await goLive(env, env.sockets[0]);
    await dropLive(env, env.peers[0], "disconnected");
    for (const [attempt, delay] of [[1, 1000], [2, 2000], [3, 4000]]) {
        assert.equal(stream.state, "connecting");
        assert.equal(details[details.length - 1][1].reconnecting, true);
        const sockets = env.sockets.length;
        env.advance(delay - 1);
        assert.equal(env.sockets.length, sockets, `not before ${delay} ms`);
        env.advance(1);
        assert.equal(env.sockets.length, sockets + 1, `session ${attempt} after ${delay} ms`);
        await offer(env, env.sockets[env.sockets.length - 1]);
        const fresh = env.peers[env.peers.length - 1];
        fresh.remote.resolve();
        await env.flush();
        // The network is still gone: this session never connects.
        if (attempt === 2) {
            env.advance(30000);  // the bound, this time, rather than ICE's verdict
        } else {
            fresh.connectionState = "failed";
            fresh.onconnectionstatechange();
            env.advance(6000);  // its grace for late candidates
        }
    }
    assert.equal(stream.state, "unreachable");
    env.advance(60000);
    assert.equal(env.sockets.length, 4, "nothing more after the third");
});

test("a short blip is not a drop, and a successful reconnect resets the count", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await goLive(env, env.sockets[0]);
    const peer = env.peers[0];
    peer.connectionState = "disconnected";
    peer.onconnectionstatechange();
    env.advance(3000);
    peer.connectionState = "connected";
    peer.onconnectionstatechange();
    env.advance(5000);
    assert.equal(stream.state, "live");
    assert.equal(env.sockets.length, 1, "no new session for a blip");

    // Four drops in a row, each followed by a successful reconnect: never
    // unreachable, since each success resets the count.
    for (let i = 0; i < 4; i++) {
        const live = env.peers[env.peers.length - 1];
        await dropLive(env, live, "failed");
        env.advance(1000);
        await goLive(env, env.sockets[env.sockets.length - 1]);
        assert.equal(stream.state, "live");
    }
});

test("the route is named from the machine's address", () => {
    const { routeOf } = makeEnv().MerlinApps;
    const cases = [
        ["192.168.1.12", "host", "LAN"],
        ["10.1.2.3", "host", "LAN"],
        ["172.20.0.5", "prflx", "LAN"],
        ["169.254.3.4", "host", "LAN"],
        ["fe80::1", "host", "LAN"],
        ["fd12:3456::1", "host", "LAN"],
        ["100.101.102.103", "host", "LAN"],
        ["fd7a:115c:a1e0:ab12::1", "host", "LAN"],
        ["2001:861:61c0:8770:a065:d2fb:61b8:6724", "host", "Internet · IPv6"],
        ["176.186.26.141", "srflx", "Internet · IPv4"],
        ["213.239.219.213", "relay", "Internet · IPv4"],  // the relay is a path
        ["abcd.local", "host", "?"],
    ];
    for (const [address, type, route] of cases) {
        assert.equal(routeOf(address, type), route, address);
    }
});

test("the gauge shows where the rate controller stands", () => {
    const { levelOf } = makeEnv().MerlinApps;
    assert.equal(levelOf(null, null), 4, "full until the streamer says otherwise");
    assert.equal(levelOf(5500, 5500), 4);
    assert.equal(levelOf(4700, 5500), 4);
    assert.equal(levelOf(4600, 5500), 3);
    assert.equal(levelOf(3300, 5500), 3);
    assert.equal(levelOf(3200, 5500), 2);
    assert.equal(levelOf(1925, 5500), 2);
    assert.equal(levelOf(1900, 5500), 1);
});

test("the chip names the route and round trip, with the numbers on demand", () => {
    const { chipText } = makeEnv().MerlinApps;
    const s = { route: "Internet · IPv6", rttMs: 48, kbps: 3200, rateKbps: 4800, rateMax: 5500,
                fps: 60, lossPct: 1.24, codec: "H264", encoder: "nvh264enc" };
    assert.equal(chipText(s, false), "Internet · IPv6 · 48 ms");
    assert.equal(chipText(s, true),
        "Internet · IPv6 · 48 ms\n3.2 Mbit/s (target 4.8/5.5) · 60 fps · loss 1.2 % · H264 (nvh264enc)");
});

test("the chip names the path, and its detail what each way of reaching gave", () => {
    const { chipText } = makeEnv().MerlinApps;
    const s = { route: "Internet · IPv6", path: "STUN", rttMs: 48, rateKbps: 4800, rateMax: 5500,
                local: "host udp ipv6", remote: "srflx udp ipv6",
                reach: { stun: "ok 176.186.26.141", upnp: "pinhole", turn: "ok" } };
    assert.equal(chipText(s, false), "Internet · IPv6 · STUN · 48 ms");
    assert.equal(chipText(s, true), [
        "Internet · IPv6 · STUN · 48 ms",
        "target 4.8/5.5 Mbit/s",
        "host udp ipv6 ↔ srflx udp ipv6",
        "STUN ok 176.186.26.141 · UPnP pinhole · TURN ok",
    ].join("\n"));
    // On the LAN a direct path goes without saying.
    assert.equal(chipText({ route: "LAN", path: "Direct", rttMs: 3 }, false), "LAN · 3 ms");
    assert.equal(chipText({ route: "LAN", path: "UPnP", rttMs: 3 }, false), "LAN · UPnP · 3 ms");
});

test("the browser names the path from its own pair until the streamer says", () => {
    const { pathOf } = makeEnv().MerlinApps;
    assert.equal(pathOf("host", "relay"), "TURN");
    assert.equal(pathOf("relay", "host"), "TURN");
    assert.equal(pathOf("srflx", "prflx"), "STUN");
    assert.equal(pathOf("host", "prflx"), "Direct");
    assert.equal(pathOf("", ""), "");
});

test("the servers from the server go into the peer, the relay switch too", async () => {
    const env = makeEnv();
    connect(env);
    const servers = [{ urls: ["stun:s:3478"] },
                     { urls: ["turn:t:3478"], username: "1:lisa", credential: "pw" }];
    env.sockets[0].deliver({ type: "welcome", host: "box", app: { id: "probe" } });
    env.sockets[0].deliver({ type: "servers", iceServers: servers, policy: "all", turn: "ok" });
    env.sockets[0].deliver({ type: "offer", sdp: "offer-sdp" });
    assert.deepEqual(JSON.parse(JSON.stringify(env.peers[0].config)), { iceServers: servers });
    env.sockets[0].deliver({ type: "servers", iceServers: servers, policy: "relay", turn: "ok" });
    env.sockets[0].deliver({ type: "offer", sdp: "offer-sdp" });
    assert.equal(env.peers[1].config.iceTransportPolicy, "relay");
});

test("a browser that refuses the servers still gets a peer, for the LAN", async () => {
    const env = makeEnv({ rejectServers: true });
    const { states } = connect(env);
    env.sockets[0].deliver({ type: "welcome", host: "box", app: { id: "probe" } });
    env.sockets[0].deliver({ type: "servers", iceServers: [{ urls: ["stun:bad"] }] });
    env.sockets[0].deliver({ type: "offer", sdp: "offer-sdp" });
    assert.equal(env.peers.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(env.peers[0].config)), { iceServers: [] });
    assert.ok(!states.includes("error"));
});

test("?ice=relay on the page asks for a relay session, nothing else does", () => {
    for (const [search, ice] of [["?ice=relay", "relay"], ["?ice=nonsense", undefined], ["", undefined]]) {
        const env = makeEnv({ search });
        connect(env);
        env.sockets[0].deliver({ type: "welcome", host: "box", app: { id: "probe" } });
        const hello = env.sockets[0].sent.find((m) => m.type === "hello");
        assert.equal(hello.ice, ice, search);
    }
});

test("path and reach reach the stats; a new session forgets them and the servers", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    env.sockets[0].deliver({ type: "servers", iceServers: [{ urls: ["stun:s:1"] }], policy: "relay" });
    await goLive(env, env.sockets[0]);
    env.peers[0].report = new Map([
        ["T", { id: "T", type: "transport", selectedCandidatePairId: "P" }],
        ["P", { id: "P", type: "candidate-pair", remoteCandidateId: "R", localCandidateId: "L" }],
        ["R", { id: "R", type: "remote-candidate", address: "2001:861::1", candidateType: "prflx" }],
        ["L", { id: "L", type: "local-candidate", candidateType: "srflx" }],
    ]);
    assert.equal((await stream.stats()).path, "STUN", "the browser's own reading first");
    env.sockets[0].deliver({ type: "path", path: "UPnP", local: "host udp ipv6", remote: "srflx udp ipv6" });
    env.sockets[0].deliver({ type: "reach", stun: "ok 1.2.3.4", upnp: "pinhole", turn: "no access" });
    const s = await stream.stats();
    assert.equal(s.path, "UPnP");
    assert.equal(s.local, "host udp ipv6");
    assert.equal(s.remote, "srflx udp ipv6");
    assert.deepEqual(JSON.parse(JSON.stringify(s.reach)), { stun: "ok 1.2.3.4", upnp: "pinhole", turn: "no access" });
    stream.reconnect();
    assert.equal(stream.info.path, null);
    assert.equal(stream.info.reach, null);
    assert.deepEqual(JSON.parse(JSON.stringify(stream.info.iceServers)), []);
    assert.equal(stream.info.policy, "all");
});

test("rate messages reach the stats; a new session forgets them", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await goLive(env, env.sockets[0]);
    env.sockets[0].deliver({ type: "rate", kbps: 2000, max: 5500 });
    const s = await stream.stats();
    assert.equal(s.rateKbps, 2000);
    assert.equal(s.rateMax, 5500);
    stream.reconnect();
    assert.equal(stream.info.rate, null);
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

// ---- a second offer on the same socket (the streamer dropped its sound) ----

for (const outcome of ["rejects", "resolves"]) {
    test(`the first peer finishing late (${outcome}) never touches the second`, async () => {
        const env = makeEnv();
        const { states } = connect(env);
        const socket = env.sockets[0];
        socket.deliver({ type: "welcome", host: "box", app: { id: "probe" } });
        socket.deliver({ type: "offer", sdp: "offer-1" });
        await env.flush();
        socket.deliver({ type: "offer", sdp: "offer-2" });
        await env.flush();
        const [first, second] = env.peers;
        assert.equal(first.closed, true, "the replaced peer is closed");

        if (outcome === "rejects") first.remote.reject(new Error("closed"));
        else first.remote.resolve();
        await env.flush();
        await env.flush();

        assert.equal(second.closed, false, "the new peer lives on");
        assert.equal(socket.closed, false, "and so does the socket");
        assert.ok(!states.includes("error"));
        assert.deepEqual(
            socket.sent.filter((m) => m.type === "answer"), [],
            "no answer for the old offer",
        );

        second.remote.resolve();
        await env.flush();
        await env.flush();
        assert.equal(socket.sent.filter((m) => m.type === "answer").length, 1);
        second.connectionState = "connected";
        second.onconnectionstatechange();
        assert.equal(states[states.length - 1], "live");
        first.connectionState = "failed";
        first.onconnectionstatechange();
        assert.equal(states[states.length - 1], "live", "the old peer's failure is ignored");
    });
}

test("the browser's round trip goes to the streamer every 2 s while live", async () => {
    const env = makeEnv();
    connect(env);
    await goLive(env, env.sockets[0]);
    const peer = env.peers[0];
    const sent = [];
    peer.ondatachannel({ channel: { readyState: "open", send: (t) => sent.push(JSON.parse(t)), close() {} } });
    peer.report = new Map([
        ["T", { id: "T", type: "transport", selectedCandidatePairId: "P" }],
        ["P", { id: "P", type: "candidate-pair", currentRoundTripTime: 0.048 }],
    ]);
    env.advance(1999);
    await env.flush();
    assert.deepEqual(sent, []);
    env.advance(1);
    await env.flush();
    assert.deepEqual(sent, [{ t: "net", rtt: 48 }]);
    env.advance(2000);
    await env.flush();
    assert.equal(sent.length, 2);
});

test("a sub-millisecond round trip reads as <1 ms", () => {
    const { chipText } = makeEnv().MerlinApps;
    assert.equal(chipText({ route: "LAN", rttMs: 0 }, false), "LAN · <1 ms");
});

test("the streamer's route wins over the browser's own reading", async () => {
    const env = makeEnv();
    const { stream } = connect(env);
    await goLive(env, env.sockets[0]);
    env.peers[0].report = new Map([
        ["T", { id: "T", type: "transport", selectedCandidatePairId: "P" }],
        ["P", { id: "P", type: "candidate-pair", remoteCandidateId: "R", localCandidateId: "L" }],
        ["R", { id: "R", type: "remote-candidate", address: "2001:861::1", candidateType: "host" }],
        ["L", { id: "L", type: "local-candidate", candidateType: "host" }],
    ]);
    assert.equal((await stream.stats()).route, "Internet · IPv6", "alone, a public IPv6");
    env.sockets[0].deliver({ type: "route", route: "LAN" });
    assert.equal((await stream.stats()).route, "LAN");
    stream.reconnect();
    assert.equal(stream.info.route, "");
});

test("a Retry, or a resume, starts fresh: not a reconnection, the full budget back", async () => {
    const env = makeEnv();
    const details = [];
    const { stream } = connect(env, { onState: (s, d) => details.push([s, d]) });
    await goLive(env, env.sockets[0]);
    env.peers[0].connectionState = "failed";
    env.peers[0].onconnectionstatechange();  // a drop: reconnecting
    assert.equal(details[details.length - 1][1].reconnecting, true);
    stream.reconnect();  // the user does not wait
    const last = details[details.length - 1];
    assert.equal(last[0], "connecting");
    assert.ok(!last[1].reconnecting, "a Retry says Connecting, not Reconnecting");
    // and three automatic sessions again after the next drop
    await goLive(env, env.sockets[env.sockets.length - 1]);
    for (let i = 0; i < 3; i++) {
        const peer = env.peers[env.peers.length - 1];
        peer.connectionState = "failed";
        peer.onconnectionstatechange();
        assert.equal(stream.state, "connecting", `try ${i + 1} of 3`);
        env.advance(4000);
        await offer(env, env.sockets[env.sockets.length - 1]);
    }
});

test("during recovery, sessions that never get an offer spend the tries too", async () => {
    const env = makeEnv();
    const details = [];
    const { stream } = connect(env, { onState: (s, d) => details.push([s, d]) });
    await goLive(env, env.sockets[0]);
    env.peers[0].connectionState = "failed";
    env.peers[0].onconnectionstatechange();
    // Each new session opens its socket and then hears nothing for a minute.
    for (const delay of [1000, 2000, 4000]) {
        env.advance(delay);
        assert.equal(stream.state, "connecting");
        assert.equal(details[details.length - 1][1].reconnecting, true);
        env.sockets[env.sockets.length - 1].deliver({ type: "welcome", host: "box" });
        env.advance(60000);  // no offer: the setup deadline
    }
    assert.equal(stream.state, "unreachable");
    const opened = env.sockets.length;
    env.advance(600000);
    assert.equal(env.sockets.length, opened, "bounded: nothing more after the third");
    assert.equal(opened, 4);
});

test("a streamer error during recovery spends a try, and says Reconnecting", async () => {
    const env = makeEnv();
    const details = [];
    const { stream } = connect(env, { onState: (s, d) => details.push([s, d]) });
    await goLive(env, env.sockets[0]);
    env.peers[0].connectionState = "failed";
    env.peers[0].onconnectionstatechange();
    env.advance(1000);
    env.sockets[1].deliver({ type: "error", message: "The streamer stopped." });
    assert.equal(stream.state, "connecting");
    assert.equal(details[details.length - 1][1].reconnecting, true);
    env.advance(2000);
    assert.equal(env.sockets.length, 3, "the second try, on its own schedule");
});

function deferredStats(peer) {
    let resolve;
    peer.getStats = () => new Promise((r) => { resolve = r; });
    return (report) => resolve(report);
}

const RTT_1200 = new Map([
    ["T", { id: "T", type: "transport", selectedCandidatePairId: "P" }],
    ["P", { id: "P", type: "candidate-pair", currentRoundTripTime: 1.2 }],
]);

for (const how of ["reconnect", "pause", "destroy"]) {
    test(`an RTT answered after a ${how} is dropped, never sent to another session`, async () => {
        const env = makeEnv();
        const { stream } = connect(env);
        await goLive(env, env.sockets[0]);
        const old = env.peers[0];
        const oldSent = [];
        old.ondatachannel({ channel: { readyState: "open", send: (t) => oldSent.push(t), close() {} } });
        const answer = deferredStats(old);
        env.advance(2000);  // the report is asked, its answer pending
        await env.flush();
        const fresh = [];
        if (how === "reconnect") {
            stream.reconnect();
            await goLive(env, env.sockets[env.sockets.length - 1]);
            env.peers[env.peers.length - 1].ondatachannel({
                channel: { readyState: "open", send: (t) => fresh.push(t), close() {} },
            });
        } else if (how === "pause") {
            env.document.hidden = true;
            env.document.fire("visibilitychange");
            env.advance(30000);
            assert.equal(stream.state, "paused");
        } else {
            stream.destroy();
        }
        answer(RTT_1200);
        await env.flush();
        assert.deepEqual(oldSent, [], "the old channel is closed");
        assert.deepEqual(fresh, [], "the new session never hears the old path");
    });
}
