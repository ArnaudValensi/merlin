/*
 * The voice console: a full-screen, hands-free conversation with the agent of
 * one terminal window. See docs/dev/voice-mode.md.
 *
 * The Voice button starts a session on the window the page shows (its target
 * is captured then, like dictation's). From there the console loops on its
 * own: listen with the VAD, upload the utterance through dictation's path
 * (MerlinTerminal.sendVoice, auto-enter on), wait while the agent works, play
 * the clips the server announces in order, then listen again. The model in
 * voice-core.js decides the mode; this file runs the effects (mic, VAD, audio,
 * requests, drawing).
 *
 * The mic stream is opened once per session and stays open; the VAD is paused
 * whenever the console is not listening, so the agent never hears itself.
 */

// The terminal page builds MerlinTerminal on DOMContentLoaded, which fires
// after this deferred script runs: start from the same event, after it.
function initVoiceConsole() {
    "use strict";

    const Core = typeof MerlinVoiceCore !== "undefined" ? MerlinVoiceCore : null;
    const T = window.MerlinTerminal;
    const btn = document.getElementById("voice-btn");
    const root = document.getElementById("voice-console");
    if (!Core || !T || !btn || !root) return;

    let capability = { available: false, reason: "Voice mode is unavailable" };
    try { capability = JSON.parse(btn.dataset.mode || "{}"); } catch (_) { /* keep */ }
    const dictation = btn.dataset.dictation === "true";
    const micLang = document.getElementById("mic-lang");

    const VENDOR = "/static/voice/vendor/";
    const ORT_DIR = VENDOR + "onnxruntime-web-1.22.0/";
    const VAD_DIR = VENDOR + "vad-web-0.0.30/";
    // End of speech after this much silence.
    const REDEMPTION_MS = 1500;
    // An upload the server never answered: ask for the session's state again.
    const WAIT_TIMEOUT_MS = 45000;
    const NOTICE_MS = 5000;
    // A resync whose request fails tries again after these waits, then says
    // so (tap to try again). The barrier stays up all along.
    const SYNC_RETRY_MS = [1000, 2000, 4000, 8000];
    const reduceMotion = window.matchMedia
        ? window.matchMedia("(prefers-reduced-motion: reduce)")
        : { matches: false };

    const MODES = {
        tune:   { badge: "SYNC", name: "TUNING",     hint: "Opening the channel" },
        resume: { badge: "HOLD", name: "ON HOLD",    hint: "Tap anywhere to resume" },
        listen: { badge: "MON",  name: "LISTENING",  hint: "Speak when ready" },
        tx:     { badge: "TX",   name: "TRANSMIT",   hint: "Pause to send, or tap to send now" },
        proc:   { badge: "PROC", name: "PROCESSING", hint: "The agent is working" },
        rx:     { badge: "RX",   name: "RECEIVING",  hint: "Tap anywhere to skip this answer" },
        ask:    { badge: "ASK",  name: "STANDBY",    hint: "Yes sends Enter, No sends Escape" },
        error:  { badge: "ERR",  name: "FAULT",      hint: "Tap anywhere to retry" },
    };
    // The text area before anything was said.
    const IDLE_LINES = {
        tune: "Opening the channel, one moment.",
        resume: "Voice mode is still on for this window. Tap anywhere to pick it up.",
        listen: "Go ahead, I am listening. Pause when you are done.",
        tx: "Go ahead, I am listening. Pause when you are done.",
    };
    const BUTTONS = {
        tune: ["exit"], resume: ["exit"], listen: ["exit"], tx: ["exit"],
        proc: ["stop", "exit"], rx: ["stop", "exit"], ask: ["yes", "no", "exit"], error: ["exit"],
    };

    // -- state ----------------------------------------------------------------

    let m = null;              // the console model while open
    let target = "";
    let phase = "off";         // off | tune | resume | live
    let lastMode = "";
    let modeSince = 0;
    let retry = null;          // what a tap does in a local error: start | vad | upload
    let retryBlob = null;
    let uploadRefusal = "";
    let sendNow = false;
    let listening = false;     // the VAD is running for us
    let waitTimer = null;
    let noticeTimer = null;
    let wakeLock = null;
    let opening = null;        // {gen, promise}: the initialization in flight
    let gen = 0;               // the current open; bumped by open, adopt and close
    let syncReq = 0;           // the latest resync request
    let askToken = 0;          // the approval playback allowed to start
    let askFor = null;         // the ask audio id that playback was for

    const audio = { ctx: null, out: null, outAnalyser: null, mic: null, micSource: null, micAnalyser: null, wave: null };
    let vad = null;
    let vadLoading = null;
    let speech = null;          // {src, kind: answer | ask}: the one voice playing
    const buffers = new Map();  // clip id -> AudioBuffer, the playback window only

    // -- markup ---------------------------------------------------------------

    root.innerHTML = [
        '<div class="vc-plate">',
        '  <header class="vc-head">',
        '    <div class="vc-brand"><span class="vc-led"></span>MERLIN<span class="vc-slash">//</span>VOICE</div>',
        '    <div class="vc-lang" role="group" aria-label="Voice language">',
        '      <button type="button" data-lang="en">EN</button><button type="button" data-lang="fr">FR</button>',
        '    </div>',
        '  </header>',
        '  <div class="vc-id">',
        '    <span>CALL <b data-el="call"></b></span>',
        '    <span><b data-el="freq"></b> MHz</span>',
        '    <span class="vc-clock" data-el="clock">00:00</span>',
        '  </div>',
        '  <section class="vc-main">',
        '    <div class="vc-mode">',
        '      <div class="vc-badge" data-el="badge"></div>',
        '      <div class="vc-mode-text">',
        '        <div class="vc-mode-name" data-el="name"></div>',
        '        <div class="vc-meter" data-el="meter" aria-hidden="true">' + "<i></i>".repeat(16) + '</div>',
        '      </div>',
        '    </div>',
        '    <div class="vc-scope"><canvas data-el="scope" aria-hidden="true"></canvas>',
        '      <div class="vc-readout" data-el="readout"></div></div>',
        '  </section>',
        '  <section class="vc-text">',
        '    <div class="vc-ask" data-el="ask" hidden>',
        '      <div class="vc-ask-title" data-el="askTitle"></div>',
        '      <pre class="vc-excerpt" data-el="excerpt"></pre>',
        '    </div>',
        '    <div class="vc-error" data-el="error" hidden></div>',
        '    <div class="vc-captions" data-el="captions" aria-live="polite"></div>',
        '    <div class="vc-notice" data-el="notice" role="status"></div>',
        '  </section>',
        '  <footer class="vc-foot">',
        '    <div class="vc-hint" data-el="hint"></div>',
        '    <div class="vc-buttons">',
        '      <button type="button" class="vc-btn vc-yes" data-act="yes">YES</button>',
        '      <button type="button" class="vc-btn vc-no" data-act="no">NO</button>',
        '      <button type="button" class="vc-btn vc-stop" data-act="stop">STOP AGENT</button>',
        '      <button type="button" class="vc-btn vc-skip" data-act="skip">SKIP ANSWER</button>',
        '      <button type="button" class="vc-btn vc-exit" data-act="exit">EXIT</button>',
        '    </div>',
        '  </footer>',
        '</div>',
    ].join("\n");
    const el = {};
    root.querySelectorAll("[data-el]").forEach(function (n) { el[n.dataset.el] = n; });
    const actionButtons = {};
    root.querySelectorAll("[data-act]").forEach(function (n) { actionButtons[n.dataset.act] = n; });
    const meterCells = Array.prototype.slice.call(el.meter.children);

    // -- requests -------------------------------------------------------------

    async function api(method, path, body) {
        try {
            const res = await fetch("/api/voice/" + path, {
                method: method,
                headers: body ? { "Content-Type": "application/json" } : undefined,
                body: body ? JSON.stringify(body) : undefined,
                cache: "no-store",
            });
            let data = {};
            try { data = await res.json(); } catch (_) { /* empty body */ }
            return { ok: res.ok, status: res.status, data: data };
        } catch (e) {
            return { ok: false, status: 0, data: { error: "Merlin is unreachable" } };
        }
    }

    function lang() {
        const value = micLang ? micLang.value : "en";
        return value === "fr" ? "fr" : "en";
    }

    function setLang(value) {
        if (micLang && micLang.value !== value) {
            micLang.value = value;
            micLang.dispatchEvent(new Event("change"));   // saves it, like a pick
        }
        renderLang();
        if (m && phase !== "off") api("POST", "lang", { target: target, lang: value });
    }

    function renderLang() {
        root.querySelectorAll("[data-lang]").forEach(function (b) {
            b.classList.toggle("active", b.dataset.lang === lang());
            b.setAttribute("aria-pressed", b.dataset.lang === lang() ? "true" : "false");
        });
    }

    // -- audio ----------------------------------------------------------------

    /** Create or wake the one AudioContext. Called from a tap, which unlocks it. */
    function ensureAudio() {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) throw new Error("This browser cannot play audio");
        if (!audio.ctx || audio.ctx.state === "closed") {
            const ctx = new AC();
            audio.ctx = ctx;
            audio.out = ctx.createGain();
            audio.outAnalyser = ctx.createAnalyser();
            audio.outAnalyser.fftSize = 1024;
            audio.out.connect(audio.outAnalyser);
            audio.outAnalyser.connect(ctx.destination);
            // Nova's earcon timbre: a sine with a little of its second and
            // third harmonics.
            audio.wave = ctx.createPeriodicWave(
                new Float32Array([0, 0, 0, 0]), new Float32Array([0, 1, 0.25, 0.08]),
                { disableNormalization: true });
        }
        if (audio.ctx.state === "suspended") audio.ctx.resume().catch(function () {});
        return audio.ctx;
    }

    function micLive() {
        return !!(audio.mic && audio.mic.getAudioTracks().some(function (t) { return t.readyState === "live"; }));
    }

    /** Open the mic once for the whole session. ``g``: the open it serves; a
     *  stream that arrives after that open ended is closed at once. */
    async function ensureMic(g) {
        if (micLive()) return;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("Voice mode needs HTTPS: the microphone is unavailable over HTTP");
        }
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
            });
        } catch (e) {
            if (e && e.name === "NotAllowedError") throw new Error("Microphone permission denied");
            throw new Error("Microphone unavailable: " + ((e && e.message) || e));
        }
        if (g !== gen || micLive()) {
            // Exit landed while the prompt was up, or another open got there first.
            stream.getTracks().forEach(function (t) { t.stop(); });
            if (g !== gen) throw new Error("closed");
            return;
        }
        audio.mic = stream;
        audio.micSource = audio.ctx.createMediaStreamSource(stream);
        audio.micAnalyser = audio.ctx.createAnalyser();
        audio.micAnalyser.fftSize = 1024;
        audio.micSource.connect(audio.micAnalyser);
    }

    function closeMic() {
        if (audio.micSource) { try { audio.micSource.disconnect(); } catch (_) { /* gone */ } }
        if (audio.mic) audio.mic.getTracks().forEach(function (t) { t.stop(); });
        audio.mic = audio.micSource = audio.micAnalyser = null;
    }

    function tone(freqs, dur, at, amp) {
        const ctx = audio.ctx;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.setPeriodicWave(audio.wave);
        osc.frequency.setValueAtTime(freqs[0], at);
        if (freqs.length > 1) osc.frequency.linearRampToValueAtTime(freqs[freqs.length - 1], at + dur);
        gain.gain.setValueAtTime(0, at);
        gain.gain.linearRampToValueAtTime(amp || 0.18, at + 0.004);
        gain.gain.setTargetAtTime(0, at + 0.004, dur * 0.35);
        osc.connect(gain);
        gain.connect(audio.out);
        osc.start(at);
        osc.stop(at + dur + 0.25);
    }

    /** Nova's earcons, plus an alert for the ask state. Returns their length in s. */
    function earcon(kind) {
        if (!audio.ctx || audio.ctx.state !== "running") return 0;
        const t = audio.ctx.currentTime + 0.02;
        switch (kind) {
            case "listen": tone([660, 990], 0.07, t); tone([990, 1480], 0.11, t + 0.085); return 0.2;
            case "send":   tone([1320], 0.06, t); tone([880], 0.12, t + 0.07); return 0.19;
            case "done":   tone([784], 0.08, t, 0.1); tone([1175], 0.16, t + 0.08, 0.08); return 0.24;
            case "error":  tone([330], 0.1, t); tone([247], 0.18, t + 0.13); return 0.31;
            case "alert":
                tone([880, 1175], 0.08, t); tone([880, 1175], 0.08, t + 0.14); tone([1568], 0.2, t + 0.28);
                return 0.5;
        }
        return 0;
    }

    function decode(data) {
        return new Promise(function (resolve) {
            try {
                const p = audio.ctx.decodeAudioData(data, resolve, function () { resolve(null); });
                if (p && p.catch) p.catch(function () { resolve(null); });
            } catch (_) { resolve(null); }
        });
    }

    /** Download and decode one clip: an AudioBuffer, or null on any failure. */
    async function download(id) {
        try {
            const r = await fetch("/api/voice/audio/" + encodeURIComponent(id), { cache: "no-store" });
            if (!r.ok) return null;
            return await decode(await r.arrayBuffer());
        } catch (_) {
            return null;
        }
    }

    // -- speech: one owner ----------------------------------------------------
    //
    // Answer clips and the ask sentence share one slot, `speech`: never two
    // voices at once, every source stoppable, and the VAD never runs while
    // the slot is taken. Only the playback window is downloaded (the server
    // makes the next clips as these are fetched), failures retry in place.

    function fetchWindow() {
        if (!m || phase !== "live" || !audio.ctx) return;
        const g = gen;
        const model = m;
        Core.toFetch(m, Date.now()).forEach(function (clip) {
            Core.fetching(m, clip);
            download(clip.id).then(function (buf) {
                // Closed, or the clip was skipped while it downloaded.
                if (g !== gen || m !== model || m.queue.indexOf(clip) < 0) return;
                if (buf) {
                    buffers.set(clip.id, buf);
                    Core.fetched(m, clip);
                } else {
                    const wait = Core.fetchFailed(m, clip, Date.now());
                    if (wait >= 0) setTimeout(function () { if (g === gen) pump(); }, wait + 20);
                    else fail("Part of the answer could not be downloaded", "clip");
                }
                render();
                pump();
            });
        });
    }

    function stopSpeech() {
        if (!speech) return;
        const s = speech;
        speech = null;
        try { s.src.onended = null; s.src.stop(); } catch (_) { /* never started */ }
    }

    function own(buf, kind, at) {
        const s = audio.ctx.createBufferSource();
        s.buffer = buf;
        s.connect(audio.out);
        const mine = { src: s, kind: kind };
        speech = mine;
        stopListening();
        s.onended = function () {
            if (speech !== mine) return;
            speech = null;
            if (kind === "answer" && m) Core.clipDone(m);
            if (kind === "answer") buffers.delete(mine.id);
            render();
            pump();
        };
        s.start(at || 0);
        return mine;
    }

    function dropBuffers() {
        const keep = new Set(m ? m.queue.map(function (c) { return c.id; }) : []);
        Array.from(buffers.keys()).forEach(function (id) { if (!keep.has(id)) buffers.delete(id); });
    }

    /** Start the next clip when the slot is free and the head is downloaded. */
    function pump() {
        if (!m || phase !== "live") return;
        fetchWindow();
        if (speech || Core.mode(m) === "ask") return;   // the answer waits while the agent asks
        const clip = Core.nextReady(m);
        if (!clip) { render(); return; }
        const buf = buffers.get(clip.id);
        if (!buf) { clip.status = "queued"; fetchWindow(); return; }
        Core.begin(m, clip);
        own(buf, "answer").id = clip.id;
        render();
        fetchWindow();
    }

    /** The agent asks: cut the answer (it plays again afterwards), then the
     *  alert and the ask sentence, in the slot like any speech. Each attempt
     *  has its own token: an answer, a new dialog, leaving ASK, Stop or Exit
     *  revoke it, so a download that lands late never starts. */
    function playAsk(id) {
        if (speech && speech.kind === "answer") {
            stopSpeech();
            Core.requeue(m);
        }
        cancelAskSpeech();
        askFor = id || "";
        const token = askToken;
        const wait = earcon("alert");
        if (!id) return;
        download(id).then(function (buf) {
            if (!buf || token !== askToken || !m || Core.mode(m) !== "ask" || speech) return;
            own(buf, "ask", audio.ctx.currentTime + wait + 0.1);
        });
    }

    function cancelAskSpeech() {
        askToken++;
        if (speech && speech.kind === "ask") stopSpeech();
    }

    // -- VAD ------------------------------------------------------------------

    function loadScript(src) {
        return new Promise(function (resolve, reject) {
            const s = document.createElement("script");
            s.src = src;
            s.onload = resolve;
            s.onerror = function () { reject(new Error("could not load " + src)); };
            document.head.appendChild(s);
        });
    }

    /** Silero VAD, vendored and loaded the first time voice mode opens. */
    function loadVad() {
        if (window.vad && window.vad.MicVAD) return Promise.resolve(window.vad);
        if (!vadLoading) {
            vadLoading = (async function () {
                if (!window.ort) await loadScript(ORT_DIR + "ort.wasm.min.js");
                // One thread: no cross-origin isolation is needed.
                window.ort.env.wasm.numThreads = 1;
                window.ort.env.wasm.wasmPaths = ORT_DIR;
                await loadScript(VAD_DIR + "bundle.min.js");
                if (!window.vad || !window.vad.MicVAD) throw new Error("the VAD did not load");
                return window.vad;
            })().catch(function (e) { vadLoading = null; throw e; });
        }
        return vadLoading;
    }

    async function ensureVad(g) {
        if (vad) return vad;
        const lib = await loadVad();
        if (g !== gen) throw new Error("closed");
        if (vad) return vad;
        const made = await lib.MicVAD.new({
            model: "legacy",
            baseAssetPath: VAD_DIR,
            onnxWASMBasePath: ORT_DIR,
            audioContext: audio.ctx,
            // The session's one stream: pausing never closes it, so no
            // permission prompt comes back between turns.
            getStream: async function () { return audio.mic; },
            pauseStream: async function () {},
            resumeStream: async function () { return audio.mic; },
            startOnLoad: false,
            redemptionMs: REDEMPTION_MS,
            submitUserSpeechOnPause: true,
            onSpeechStart: function () {},
            onSpeechRealStart: function () {
                if (m && listening && m.local === "idle") { m.local = "hearing"; render(); }
            },
            onVADMisfire: function () {
                if (m && m.local === "hearing") { m.local = "idle"; render(); }
            },
            onSpeechEnd: function (samples) { onUtterance(samples); },
        });
        if (g !== gen || vad) {
            made.destroy();
            if (g !== gen) throw new Error("closed");
            return vad;
        }
        vad = made;
        return vad;
    }

    /** Listen only in MON or TX, with the VAD loaded and no speech playing. */
    function syncListening(mode) {
        const want = phase === "live" && !!vad && !speech && (mode === "listen" || mode === "tx");
        if (want) startListening(); else stopListening();
    }

    function startListening() {
        if (listening || !vad || speech) return;
        listening = true;
        vad.start();
    }

    function stopListening() {
        if (!listening) return;
        listening = false;
        if (vad) vad.pause();   // a pause mid-speech hands the audio to onUtterance
    }

    function onUtterance(samples) {
        const forced = sendNow;
        sendNow = false;
        if (!m || phase !== "live" || (!listening && !forced)) return;   // not ours to send
        if (m.syncing) return;   // the agent may be busy: its frames are held back
        if (m.local !== "idle" && m.local !== "hearing") return;
        m.local = "sending";
        earcon("send");
        render();
        upload(new Blob([Core.encodeWav(samples, 16000)], { type: "audio/wav" }));
    }

    async function upload(blob) {
        const t = target;
        const g = gen;
        retryBlob = null;
        uploadRefusal = "";
        const hooks = {
            progress: function (f) { root.style.setProperty("--vc-upload", String(f)); },
            done: function () { root.style.removeProperty("--vc-upload"); },
            refused: function (message) { uploadRefusal = message || ""; },
        };
        const outcome = await T.sendVoice(blob, lang(), t, hooks);
        if (g !== gen || !m || m.local !== "sending") return;
        if (outcome === "accepted") {
            // A frame that arrived during the upload already answered it.
            m.local = m.server === "listening" ? "waiting" : "idle";
            if (m.local === "waiting") armWait();
        } else if (outcome === "refused") {
            fail(uploadRefusal || "The transcription was refused", null);
        } else if (outcome === "failed") {
            retryBlob = blob;
            fail("Could not reach Merlin to send what you said", "upload");
        } else {
            m.local = "idle";
        }
        render();
    }

    function armWait() {
        clearTimeout(waitTimer);
        const g = gen;
        waitTimer = setTimeout(function () {
            if (g === gen && m && m.local === "waiting") { m.local = "idle"; resync(); }
        }, WAIT_TIMEOUT_MS);
    }

    // -- lifecycle ------------------------------------------------------------
    //
    // Every open (and every close) starts a new generation. Work begun for an
    // older one (a mic prompt, the VAD load, a request) finds the generation
    // changed when it completes, disposes what it made and stops there.

    function show() {
        root.hidden = false;
        document.body.classList.add("voice-console-open");
        el.call.textContent = Core.callSign(target);
        el.freq.textContent = Core.frequency(target);
        renderLang();
        startDrawing();
    }

    async function acquireWakeLock() {
        if (!("wakeLock" in navigator) || wakeLock || document.hidden) return;
        try {
            const lock = await navigator.wakeLock.request("screen");
            if (phase === "off") { lock.release().catch(function () {}); return; }
            wakeLock = lock;
            wakeLock.addEventListener("release", function () { if (wakeLock === lock) wakeLock = null; });
        } catch (_) { /* refused (battery saver): the screen may sleep */ }
    }

    function fail(message, kind) {
        if (!m) return;
        m.local = "error";
        m.error = message;
        retry = kind;
        render();
    }

    /** Tap on the Voice button: open the console and start a session. */
    async function open() {
        const t = T.currentTarget();
        if (!t) { T.notice("Voice mode: not connected to a window yet"); return; }
        try { ensureAudio(); } catch (e) { T.notice(e.message); return; }
        gen++;
        target = t;
        m = Core.createModel(t);
        phase = "tune";
        show();
        acquireWakeLock();
        render();
        await connectSession(true);
    }

    /** Mic, session and VAD for the current generation, in parallel where
     *  they can be. A second call in the same generation joins the first. */
    function connectSession(starting) {
        if (opening && opening.gen === gen) return opening.promise;
        const g = gen;
        const t = target;
        const model = m;
        const current = function () { return g === gen && m === model; };
        const promise = (async function () {
            const ready = (async function () { await ensureMic(g); await ensureVad(g); })();
            ready.catch(function () {});   // reported below
            if (starting) {
                const res = await api("POST", "start", { target: t, lang: lang() });
                if (!current()) {
                    // Exit came first: a session this start made is nobody's,
                    // unless the console reopened on that same window.
                    if (res.ok && !(phase !== "off" && target === t)) api("POST", "stop", { target: t });
                    return;
                }
                if (!res.ok) { phase = "live"; fail(res.data.error || "Voice mode could not start", "start"); return; }
                Core.applySnapshot(m, res.data);
            }
            try {
                await ready;
            } catch (e) {
                if (!current()) return;
                phase = "live";
                const reason = e.message === "closed" ? "" : e.message;
                fail(/Microphone|HTTPS/.test(reason) ? reason : "Voice detection failed to load: " + reason, "vad");
                return;
            }
            if (!current()) return;
            phase = "live";
            render();
            pump();
        })();
        opening = { gen: g, promise: promise };
        return promise.finally(function () { if (opening && opening.promise === promise) opening = null; });
    }

    /** Exit, or the session ended on the server's side (post = false). */
    function close(reason, post) {
        if (phase === "off") return;
        const t = target;
        if (post) api("POST", "stop", { target: t });
        gen++;
        phase = "off";
        opening = null;
        listening = false;
        stopSpeech();
        askToken++;
        askFor = null;
        if (vad) { try { vad.destroy(); } catch (_) { /* gone */ } vad = null; }
        closeMic();
        buffers.clear();
        clearTimeout(waitTimer);
        clearTimeout(noticeTimer);
        if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
        if (audio.ctx && audio.ctx.state === "running") audio.ctx.suspend().catch(function () {});
        stopDrawing();
        m = null;
        lastMode = "";
        retry = null;
        retryBlob = null;
        root.hidden = true;
        document.body.classList.remove("voice-console-open");
        if (reason) T.notice("Voice mode: " + reason);
    }

    /** Take the session's state again (a reconnected socket may have missed
     *  frames). Until a snapshot is folded in, frames wait behind it, nothing
     *  starts playing and the page does not listen; a failed request is tried
     *  again, then reported (tap to try again). Only the latest of
     *  overlapping resyncs completes. */
    async function resync() {
        if (!m || phase === "off") return;
        const g = gen;
        const t = target;
        const req = ++syncReq;
        const current = function () { return g === gen && m && req === syncReq; };
        Core.beginSync(m);
        render();
        for (let attempt = 0; ; attempt++) {
            const res = await api("GET", "session?target=" + encodeURIComponent(t));
            if (!current()) return;
            if (res.ok) {
                if (res.data.active === false) { close("the session ended", false); return; }
                Core.applySnapshot(m, res.data);
                render();
                pump();
                return;
            }
            if (attempt >= SYNC_RETRY_MS.length) { fail("Lost contact with Merlin", "sync"); return; }
            await new Promise(function (r) { setTimeout(r, SYNC_RETRY_MS[attempt]); });
            if (!current()) return;
        }
    }

    /** A page opened on a window whose session is running: hold until a tap
     *  unlocks audio and the mic again. */
    async function adopt(t) {
        const g = ++gen;
        const res = await api("GET", "session?target=" + encodeURIComponent(t));
        if (g !== gen || phase !== "off" || !res.ok || !res.data.active) return;
        target = t;
        m = Core.createModel(t);
        Core.applySnapshot(m, res.data);
        phase = "resume";
        show();
        render();
    }

    async function resumeFromHold() {
        try { ensureAudio(); } catch (e) { fail(e.message, null); return; }
        acquireWakeLock();
        phase = "tune";
        render();
        await connectSession(false);
        resync();
    }

    // -- taps -----------------------------------------------------------------

    function tapAnywhere() {
        if (!m) return;
        if (phase === "resume") { resumeFromHold(); return; }
        if (phase !== "live") return;
        if (audio.ctx && audio.ctx.state === "suspended") audio.ctx.resume().catch(function () {});
        const mode = Core.mode(m);
        if (mode === "tx" && vad) {
            // Send now, without waiting for the silence.
            sendNow = true;
            listening = false;
            vad.pause();
            sendNow = false;
            if (m.local === "hearing") m.local = "idle";
            render();
        } else if (mode === "rx") {
            const turn = Core.skip(m);
            if (speech && speech.kind === "answer") stopSpeech();
            dropBuffers();
            api("POST", "skip", { target: target, turn: turn });
            render();
            pump();
        } else if (mode === "error" && m.local === "error") {
            const kind = retry;
            retry = null;
            m.local = "idle";
            m.error = "";
            render();
            if (kind === "start") { phase = "tune"; render(); connectSession(true); }
            else if (kind === "vad") { phase = "tune"; render(); connectSession(false); }
            else if (kind === "upload" && retryBlob) { m.local = "sending"; render(); upload(retryBlob); }
            else if (kind === "sync") resync();
            else if (kind === "clip") { Core.retryStalled(m); pump(); }
        }
    }

    root.addEventListener("click", function (e) {
        const b = e.target.closest("button");
        if (!b) { tapAnywhere(); return; }
        if (b.dataset.lang) { setLang(b.dataset.lang); return; }
        const act = b.dataset.act;
        if (act === "exit") close("", true);
        else if (act === "skip" && m) {
            // The answer whose clip could not be downloaded: drop it here
            // and on the server, and carry on.
            const stuck = Core.stalled(m);
            if (stuck) {
                Core.dropTurn(m, stuck.turn);
                api("POST", "skip", { target: target, turn: stuck.turn });
            }
            m.local = "idle";
            m.error = "";
            retry = null;
            dropBuffers();
            render();
            pump();
        }
        else if (act === "stop" && m) {
            Core.silence(m);
            stopSpeech();
            dropBuffers();
            api("POST", "interrupt", { target: target });
            render();
        } else if ((act === "yes" || act === "no") && m) {
            cancelAskSpeech();
            b.disabled = true;
            api("POST", "answer", { target: target, yes: act === "yes" }).then(function (res) {
                b.disabled = false;
                if (!res.ok && m) { m.notice = res.data.error || "The answer did not go through"; render(); }
            });
        }
    });

    // -- the Voice button -------------------------------------------------------

    let check = { target: "", available: true, reason: "" };

    function unavailableReason() {
        if (!capability.available) return capability.reason || "Voice mode is unavailable";
        if (!dictation) return "Voice mode needs transcription: set OPENAI_API_KEY or MERLIN_SAAS_TOKEN";
        return "";
    }

    function renderButton() {
        const reason = unavailableReason() || (check.available ? "" : check.reason);
        btn.classList.toggle("voice-mode-unavailable", !!reason);
        btn.title = reason ? "Voice mode: " + reason : "Voice mode: talk with this window's agent";
    }

    async function checkWindow(t) {
        if (!t || unavailableReason()) { renderButton(); return check; }
        const res = await api("GET", "check?target=" + encodeURIComponent(t));
        if (res.ok) check = { target: t, available: !!res.data.available, reason: res.data.reason || "" };
        renderButton();
        return check;
    }

    btn.addEventListener("click", async function () {
        const fixed = unavailableReason();
        if (fixed) { T.notice("Voice mode: " + fixed); return; }
        if (phase !== "off") return;
        const t = T.currentTarget();
        if (!check.available && check.target === t) {
            // The window may have changed since (an agent started): look again.
            ensureAudio();
            const again = await checkWindow(t);
            if (!again.available) { T.notice("Voice mode: " + again.reason); return; }
        }
        open();
    });

    let adopted = false;
    document.addEventListener("merlin:terminal-window", function (e) {
        const d = e.detail || {};
        const t = d.session && d.windowId ? d.session + ":" + d.windowId : "";
        checkWindow(t);
        if (!adopted && t && phase === "off" && !unavailableReason()) {
            adopted = true;
            adopt(t);
        }
    });
    document.addEventListener("merlin:terminal-socket", function () {
        if (phase === "live") resync();
    });
    document.addEventListener("visibilitychange", function () {
        if (document.hidden || phase === "off") return;
        acquireWakeLock();
        if (phase === "live") resync();
    });
    if (micLang) micLang.addEventListener("change", renderLang);

    window.MerlinVoice = {
        onFrame: function (f) {
            if (!m || phase === "off") return;
            if (!Core.applyFrame(m, f)) return;
            render();
            pump();
        },
        // For tests and the screenshots: the console's mode and model.
        mode: function () { return m ? (phase === "live" ? Core.mode(m) : phase) : "off"; },
        model: function () { return m; },
        speaking: function () { return speech ? speech.kind : null; },
        listening: function () { return listening; },
        target: function () { return target; },
    };
    renderButton();

    // -- rendering ------------------------------------------------------------

    function currentMode() {
        if (!m) return "off";
        if (phase === "tune" || phase === "resume") return phase;
        return Core.mode(m);
    }

    function onModeChange(mode, prev) {
        modeSince = Date.now();
        if (prev === "ask") { cancelAskSpeech(); askFor = null; }
        if (mode === "listen" && (prev === "rx" || prev === "proc")) {
            const wait = earcon("done");
            setTimeout(function () { if (m && currentMode() === "listen") earcon("listen"); }, wait * 1000 + 120);
        } else if (mode === "listen" && prev !== "tx") {
            earcon("listen");
        } else if (mode === "error") {
            earcon("error");
        }
        if (prev === "ask") pump();
    }

    function showNotice() {
        if (!m || !m.notice) return;
        el.notice.textContent = m.notice;
        el.notice.classList.add("on");
        m.notice = "";
        earcon("error");
        clearTimeout(noticeTimer);
        noticeTimer = setTimeout(function () {
            el.notice.classList.remove("on");
            noticeTimer = setTimeout(function () { el.notice.textContent = ""; }, 400);
        }, NOTICE_MS);
    }

    function render() {
        if (!m) return;
        if (m.offReason && phase !== "off") { close(m.offReason, false); return; }
        const mode = currentMode();
        const spec = MODES[mode];
        root.dataset.mode = mode;
        el.badge.textContent = spec.badge;
        el.name.textContent = mode === "proc" && m.local === "sending" ? "UPLINK" : spec.name;
        el.hint.textContent = spec.hint;
        let readout = "";
        if (mode === "proc" && m.syncing) readout = "reconnecting";
        else if (mode === "proc") readout = m.local === "sending" ? "sending what you said" : (m.activity || "thinking");
        else if (mode === "rx") readout = m.activity ? m.activity : "voice";
        else if (mode === "listen") readout = "mic open";
        else if (mode === "tx") readout = "voice detected";
        else if (mode === "tune") readout = "loading voice detection";
        else if (mode === "resume") readout = "audio paused";
        el.readout.textContent = readout ? "▸ " + readout : "";

        const idle = IDLE_LINES[mode];
        const lines = m.captions.length && mode !== "resume" ? m.captions
            : idle ? [{ who: "idle", text: idle }] : m.captions;
        el.captions.replaceChildren.apply(el.captions, lines.map(function (c, i) {
            const line = document.createElement("div");
            line.className = "vc-line vc-" + c.who + (i === lines.length - 1 ? " vc-last" : "");
            const who = document.createElement("span");
            who.className = "vc-who";
            who.textContent = c.who === "you" ? "YOU" : c.who === "agent" ? "AGENT" : "";
            const text = document.createElement("span");
            text.className = "vc-said";
            text.textContent = c.text;
            line.append(who, text);
            return line;
        }));

        el.ask.hidden = mode !== "ask";
        if (mode === "ask" && m.ask) {
            el.askTitle.textContent = m.ask.text || "The agent needs your answer.";
            el.excerpt.textContent = m.ask.excerpt || "";
            // The question sits at the bottom of the pane: keep it in view.
            el.excerpt.scrollTop = el.excerpt.scrollHeight;
        }
        el.error.hidden = mode !== "error";
        if (mode === "error") el.error.textContent = m.error || m.notice || "The voice server failed";
        const shown = mode === "error" && retry === "clip" ? ["skip", "exit"] : BUTTONS[mode] || ["exit"];
        Object.keys(actionButtons).forEach(function (k) { actionButtons[k].hidden = shown.indexOf(k) < 0; });

        showNotice();
        if (mode !== lastMode) {
            const prev = lastMode;
            lastMode = mode;
            onModeChange(mode, prev);
        }
        // The dialog's sentence, once per dialog (a new dialog plays its own).
        if (mode === "ask" && m.ask && askFor !== (m.ask.audio || "")) playAsk(m.ask.audio);
        syncListening(mode);
        if (reduceMotion.matches) drawFrame(true);
    }

    // -- the scope ----------------------------------------------------------------

    let rafId = 0;
    let samples = null;
    let level = 0;
    let gain = 3;   // display gain, follows the signal's peak
    let lastClock = "";
    const canvas = el.scope;
    const g = canvas.getContext("2d");

    function startDrawing() {
        if (rafId || reduceMotion.matches) { drawFrame(true); return; }
        const loop = function () { rafId = requestAnimationFrame(loop); drawFrame(false); };
        rafId = requestAnimationFrame(loop);
    }

    function stopDrawing() {
        if (rafId) cancelAnimationFrame(rafId);
        rafId = 0;
    }

    function fit() {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
        const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
        if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
        return dpr;
    }

    function signal() {
        return getComputedStyle(root).getPropertyValue("--vc-sig").trim() || "#4ade80";
    }

    let sigColor = "";
    let sigMode = "";

    function drawFrame(still) {
        if (!m) return;
        const mode = currentMode();
        const dpr = fit();
        const w = canvas.width, h = canvas.height, mid = h / 2;
        if (sigMode !== mode) { sigColor = signal(); sigMode = mode; }

        const analyser = still ? null
            : (mode === "rx" || mode === "ask") ? audio.outAnalyser
            : (mode === "listen" || mode === "tx") ? audio.micAnalyser : null;
        let points = null;
        let rms = 0;
        if (analyser) {
            if (!samples || samples.length !== analyser.fftSize) samples = new Float32Array(analyser.fftSize);
            analyser.getFloatTimeDomainData(samples);
            let peak = 0;
            for (let i = 0; i < samples.length; i++) {
                rms += samples[i] * samples[i];
                peak = Math.max(peak, Math.abs(samples[i]));
            }
            rms = Math.sqrt(rms / samples.length);
            points = samples;
            // A quiet voice still draws a trace, a loud one stays on screen.
            const want = 0.8 / Math.max(peak, 0.15);
            gain += (want - gain) * (want < gain ? 0.5 : 0.05);
        }
        level = level * 0.7 + Math.min(1, rms * 4) * 0.3;

        // Phosphor persistence: fade the last frame instead of clearing it.
        g.globalCompositeOperation = "source-over";
        g.fillStyle = still ? "rgb(4, 9, 7)" : "rgba(4, 9, 7, 0.32)";
        g.fillRect(0, 0, w, h);

        g.beginPath();
        const now = performance.now() / 1000;
        const n = 160;
        for (let i = 0; i <= n; i++) {
            const x = (i / n) * w;
            let y;
            if (points) {
                const v = points[Math.floor((i / n) * (points.length - 1))];
                y = Math.max(2, Math.min(h - 2, mid - v * gain * mid * 0.5));
            } else if (still) {
                y = mid;
            } else if (mode === "proc" || mode === "tune") {
                // A pulse sweeping across: the agent is working.
                const p = ((now * 0.45) % 1.2) - 0.1;
                const d = i / n - p;
                y = mid - Math.exp(-d * d * 900) * h * 0.28 * Math.sin(i * 0.9);
            } else {
                y = mid + Math.sin(i * 0.12 + now * 2) * h * 0.012;
            }
            if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
        }
        g.strokeStyle = sigColor;
        g.globalAlpha = 0.22;
        g.lineWidth = 6 * dpr;
        g.stroke();
        g.globalAlpha = 1;
        g.lineWidth = 1.6 * dpr;
        g.stroke();

        const lit = Math.round(level * meterCells.length);
        meterCells.forEach(function (c, i) { c.classList.toggle("on", i < lit); });

        const s = Math.floor((Date.now() - modeSince) / 1000);
        const clock = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
        if (clock !== lastClock) { lastClock = clock; el.clock.textContent = clock; }
    }
}

if (window.MerlinTerminal) initVoiceConsole();
else document.addEventListener("DOMContentLoaded", initVoiceConsole);
