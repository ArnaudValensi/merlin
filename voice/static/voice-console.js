/*
 * The voice console: a hands-free conversation with the agent of one terminal
 * window, shown full screen (a voice call, nothing to read) or as a bar under a
 * terminal that stays visible. See docs/dev/voice-mode.md.
 *
 * The Voice button starts a session on the window the page shows (its target
 * is captured then, like dictation's). From there the console loops on its
 * own: listen with the VAD, transcribe what is said piece by piece (cut at
 * each pause), send the message once a piece ends with the end word
 * ("terminé", "over") or on a tap, wait while the agent works, play the clips
 * the server announces in order, then listen again. The model in
 * voice-core.js decides the mode; this file runs the effects (mic, VAD, audio,
 * requests, drawing).
 *
 * The mic stream is opened once per session and stays open: reopening it
 * asks Safari's permission again, and a reopen without a tap can be refused.
 * The VAD is paused whenever the console is not listening, so the agent
 * never hears itself.
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
    // A piece of what is said ends after this much silence (the message
    // itself ends with the end word).
    const REDEMPTION_MS = 1200;
    // The e2e tests shorten the delays below (window.MerlinVoiceTimings, set
    // before the page loads) rather than wait them out.
    const TIMINGS = window.MerlinVoiceTimings || {};
    // A message held this long without its end word: one soft reminder.
    const REMIND_MS = TIMINGS.REMIND_MS || 7000;
    // A piece whose transcription failed is tried again after these waits.
    const PIECE_RETRY_MS = [1000, 2000];
    // Every request of a turn has a deadline: on a mobile network that does
    // not answer, a fetch can hang for minutes. A transcription attempt past
    // it counts as failed (and is tried again); a say past it is an error a
    // tap retries, with the same message id, so it is never typed twice.
    const TRANSCRIBE_TIMEOUT_MS = TIMINGS.TRANSCRIBE_TIMEOUT_MS || 20000;
    const SAY_TIMEOUT_MS = TIMINGS.SAY_TIMEOUT_MS || 15000;
    // A send asked this long ago and still not gone: say it is the network.
    const SEND_SLOW_MS = 3000;
    // An open mic puts iOS in call mode, which turns the answer down: Safari
    // (navigator.audioSession) plays louder, with a limiter against clipping.
    const OUT_GAIN = navigator.audioSession ? 2.5 : 1;
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
        tune:   { name: "Connecting",  hint: "" },
        resume: { name: "Paused",      hint: "Tap anywhere to resume" },
        listen: { name: "Listening",   hint: "" },
        tx:     { name: "Hearing you", hint: "" },
        proc:   { name: "Working",     hint: "" },
        rx:     { name: "Speaking",    hint: "Tap anywhere to skip this answer" },
        ask:    { name: "Your answer", hint: "" },
        error:  { name: "Error",       hint: "Tap anywhere to retry" },
    };
    // The bar has no "anywhere" to tap (a click belongs to the terminal): what a
    // tap does in that mode gets a button.
    const TAP_LABELS = { resume: "Resume", tx: "Send", rx: "Skip", error: "Retry" };
    const VIEW_KEY = "voice-view";
    const wide = window.matchMedia ? window.matchMedia("(min-width: 900px) and (pointer: fine)") : { matches: false };
    const finePointer = window.matchMedia ? window.matchMedia("(pointer: fine)") : { matches: false };
    const fullParent = root.parentNode;
    const barSlot = document.getElementById("terminal-status");
    // The text area before anything was said.
    const BUTTONS = {
        tune: ["exit"], resume: ["tap", "exit"], listen: ["exit"], tx: ["tap", "exit"],
        proc: ["stop", "exit"], rx: ["tap", "stop", "exit"], ask: ["yes", "no", "exit"], error: ["tap", "exit"],
    };

    // -- state ----------------------------------------------------------------

    let m = null;              // the console model while open
    let target = "";
    let phase = "off";         // off | tune | resume | live
    let view = savedView();    // full | dock: what this page shows
    let windowName = "";       // the name of the window the page is on
    let lastMode = "";
    let modeSince = 0;
    let retry = null;          // what a tap does in a local error: start | vad | pieces | say | sync | clip
    let saying = null;         // {text, id, inflight}: the message given to say, until it answered ok
    let acked = false;         // the send earcon was played for the message on its way
    let unsent = false;        // a send that had nothing to send: no "done" earcon after it
    let sendingSince = 0;
    let slowTimer = null;
    let remindTimer = null;
    let cutIntent = null;      // "send": the speech a pause is cutting goes with the message
    let listening = false;     // the VAD is running for us
    let muted = false;         // the user muted the mic: the VAD does not run (never stored)
    const earcons = [];        // the last earcons played, for the tests
    let vadStarts = 0;         // how many times the VAD was started, for the tests
    let waitTimer = null;
    let noticeTimer = null;
    let wakeLock = null;
    let opening = null;        // {gen, promise}: the initialization in flight
    let gen = 0;               // the current open; bumped by open, adopt and close
    let syncReq = 0;           // the latest resync request
    let askToken = 0;          // the approval playback allowed to start
    let askFor = null;         // the ask audio id that playback was for
    let askOpen = false;       // the bar shows the whole question
    let paintStale = true;     // the scope's colours are to be read again
    let covered = [];          // what full screen made inert

    const audio = { ctx: null, out: null, outAnalyser: null, mic: null, micSource: null, micAnalyser: null, wave: null };
    let vad = null;
    let vadLoading = null;
    let speech = null;          // {src, kind: answer | ask}: the one voice playing
    const buffers = new Map();  // clip id -> AudioBuffer, the playback window only

    // -- markup ---------------------------------------------------------------

    root.tabIndex = -1;
    root.innerHTML = [
        '<div class="vc-plate">',
        '  <header class="vc-head">',
        '    <div class="vc-brand"><span class="vc-window" data-el="window"></span></div>',
        '    <div class="vc-tools">',
        '      <button type="button" class="vc-mic" data-el="mic" data-mic aria-pressed="false" aria-label="Mute microphone" title="Mute microphone">',
        '        <svg class="vc-mic-on" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>',
        '        <svg class="vc-mic-off" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2 2 20 20"/><path d="M18.89 13.23A7.12 7.12 0 0 0 19 12v-2"/><path d="M5 10v2a7 7 0 0 0 12 5"/><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/><path d="M12 19v3"/></svg>',
        '      </button>',
        '      <div class="vc-lang" role="group" aria-label="Voice language">',
        '        <button type="button" data-lang="en">EN</button><button type="button" data-lang="fr">FR</button>',
        '      </div>',
        '      <button type="button" class="vc-view" data-el="viewToggle" data-view-toggle>',
        '        <span class="vc-view-label" data-el="viewLabel"></span>',
        '        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>',
        '      </button>',
        '    </div>',
        '  </header>',
        '  <section class="vc-main" data-el="main">',
        '    <div class="vc-mode">',
        '      <span class="vc-led"></span>',
        '      <div class="vc-mode-name" data-el="name"></div>',
        '      <span class="vc-clock" data-el="clock">00:00</span>',
        '    </div>',
        '    <div class="vc-scope" data-el="scopeBox"><canvas data-el="scope" aria-hidden="true"></canvas></div>',
        '  </section>',
        '  <footer class="vc-foot" data-el="foot">',
        '    <section class="vc-text" data-el="text">',
        '      <div class="vc-ask" data-el="ask" hidden>',
        '        <div class="vc-ask-text" id="vc-ask-text" data-el="askText"></div>',
        '        <button type="button" class="vc-ask-more" data-el="askMore" aria-controls="vc-ask-text" hidden>',
        '          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
        '        </button>',
        '      </div>',
        '      <div class="vc-error" data-el="error" hidden></div>',
        '      <div class="vc-notice" data-el="notice" role="status"></div>',
        '    </section>',
        '    <div class="vc-hint" data-el="hint"></div>',
        '    <div class="vc-buttons" data-el="buttons">',
        '      <button type="button" class="vc-btn vc-tap" data-act="tap"></button>',
        '      <button type="button" class="vc-btn vc-yes" data-act="yes">Yes</button>',
        '      <button type="button" class="vc-btn vc-no" data-act="no">No</button>',
        '      <button type="button" class="vc-btn vc-stop" data-act="stop" aria-label="Stop agent">Stop<span class="vc-more-words"> agent</span></button>',
        '      <button type="button" class="vc-btn vc-skip" data-act="skip">Skip answer</button>',
        '      <button type="button" class="vc-btn vc-exit" data-act="exit">Exit</button>',
        '    </div>',
        '  </footer>',
        '</div>',
    ].join("\n");
    const el = {};
    root.querySelectorAll("[data-el]").forEach(function (n) { el[n.dataset.el] = n; });
    const actionButtons = {};
    root.querySelectorAll("[data-act]").forEach(function (n) { actionButtons[n.dataset.act] = n; });

    // -- the view -------------------------------------------------------------
    //
    // One session, two ways to show it. Full screen is for the ear only (a phone
    // on a mount, a walk). The bar leaves the terminal in view and in use, with
    // what the answer says on screen. The view is the page's, and the session
    // hears of it: the voice context hook tells the agent what to write for.

    function savedView() {
        let saved = null;
        try { saved = localStorage.getItem(VIEW_KEY); } catch (_) { /* no storage */ }
        return saved === "full" || saved === "dock" ? saved : (wide.matches && barSlot ? "dock" : "full");
    }

    function isolate(modal) {
        covered.forEach(function (n) { n.inert = false; });
        covered = [];
        if (!modal) return;
        for (let n = root; n !== document.body; n = n.parentNode) {
            Array.prototype.forEach.call(n.parentNode.children, function (s) {
                if (s !== n && !s.inert) { s.inert = true; covered.push(s); }
            });
        }
    }

    function applyView() {
        const dock = view === "dock" && !!barSlot;
        root.dataset.view = dock ? "dock" : "full";
        // In the flow of the terminal's column, so xterm refits around it.
        if (dock) barSlot.parentNode.insertBefore(root, barSlot);
        else if (root.parentNode !== fullParent) fullParent.appendChild(root);
        root.setAttribute("role", dock ? "region" : "dialog");
        if (dock) root.removeAttribute("aria-modal"); else root.setAttribute("aria-modal", "true");
        // What the state says sits with the state; in the bar, next to its buttons.
        const before = dock ? el.buttons : el.scopeBox;
        before.parentNode.insertBefore(el.text, before);
        before.parentNode.insertBefore(el.hint, before);
        // Full screen is a dialog: what it covers takes neither keys nor clicks.
        const modal = !root.hidden && !dock;
        isolate(modal);
        if (modal && !root.classList.contains("vc-modal")) root.focus({ preventScroll: true });
        else if (!modal && root.classList.contains("vc-modal") && finePointer.matches && T.focus) setTimeout(T.focus, 0);
        root.classList.toggle("vc-modal", modal);
        paintStale = true;
        document.body.classList.toggle("voice-console-open", !root.hidden && !dock);
        document.body.classList.toggle("voice-console-docked", !root.hidden && dock);
        const toggle = dock ? "Full screen" : "Show terminal";
        el.viewLabel.textContent = toggle;
        el.viewToggle.setAttribute("aria-label", toggle);
        el.viewToggle.title = toggle;
        el.viewToggle.hidden = !barSlot;
    }

    function setView(value) {
        view = value;
        try { localStorage.setItem(VIEW_KEY, value); } catch (_) { /* no storage */ }
        applyView();
        render();
        if (m && phase !== "off") api("POST", "view", { target: target, view: view });
    }

    function renderWindow() {
        el.window.textContent = windowName || target.split(":")[0] || "Voice";
    }

    // -- requests -------------------------------------------------------------

    /** One request and its JSON body: {ok, status, data}, status 0 when it
     *  could not be made or ``ms`` passed first (aborted, body included). */
    async function request(url, options, ms) {
        const ctl = ms && typeof AbortController !== "undefined" ? new AbortController() : null;
        const timer = ctl ? setTimeout(function () { ctl.abort(); }, ms) : null;
        try {
            const res = await fetch(url, ctl ? Object.assign({ signal: ctl.signal }, options) : options);
            let data = {};
            try { data = await res.json(); } catch (_) { /* empty body */ }
            if (ctl && ctl.signal.aborted) throw new Error("timed out");
            return { ok: res.ok, status: res.status, data: data };
        } catch (e) {
            return { ok: false, status: 0, data: { error: "Merlin is unreachable" } };
        } finally {
            clearTimeout(timer);
        }
    }

    function api(method, path, body, ms) {
        return request("/api/voice/" + path, {
            method: method,
            headers: body ? { "Content-Type": "application/json" } : undefined,
            body: body ? JSON.stringify(body) : undefined,
            cache: "no-store",
        }, ms);
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
    }

    // A pick here or in the status bar, whose picker is the bar's too.
    function onLang() {
        renderLang();
        render();   // the hint names the end word of the language
        if (m && phase !== "off") api("POST", "lang", { target: target, lang: lang() });
    }

    function renderMic() {
        const label = muted ? "Unmute microphone" : "Mute microphone";
        el.mic.setAttribute("aria-pressed", muted ? "true" : "false");
        el.mic.setAttribute("aria-label", label);
        el.mic.title = label;
    }

    function renderLang() {
        root.querySelectorAll("[data-lang]").forEach(function (b) {
            b.classList.toggle("active", b.dataset.lang === lang());
            b.setAttribute("aria-pressed", b.dataset.lang === lang() ? "true" : "false");
        });
    }

    // -- audio ----------------------------------------------------------------

    /** iOS plays Web Audio as "ambient", muted by the silent switch. The
     *  session's mic sets play-and-record, which ignores the switch; on exit
     *  the page goes back to "auto", or dictation's mic would be refused. */
    function audioSession(type) {
        const s = navigator.audioSession;
        if (!s) return;
        try { if (s.type !== type) s.type = type; } catch (_) { /* not settable */ }
    }

    /** Create or wake the one AudioContext. Called from a tap, which unlocks it. */
    function ensureAudio() {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) throw new Error("This browser cannot play audio");
        if (!audio.ctx || audio.ctx.state === "closed") {
            const ctx = new AC();
            audio.ctx = ctx;
            audio.out = ctx.createGain();
            audio.out.gain.value = OUT_GAIN;
            const limiter = ctx.createDynamicsCompressor();
            limiter.threshold.value = -3;
            limiter.knee.value = 0;
            limiter.ratio.value = 20;
            limiter.attack.value = 0.002;
            limiter.release.value = 0.12;
            audio.outAnalyser = ctx.createAnalyser();
            audio.outAnalyser.fftSize = 1024;
            audio.out.connect(limiter);
            limiter.connect(audio.outAnalyser);
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
        audioSession("play-and-record");
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
        audioSession("auto");
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
        earcons.push(kind);
        if (earcons.length > 32) earcons.shift();
        const t = audio.ctx.currentTime + 0.02;
        switch (kind) {
            case "listen": tone([660, 990], 0.07, t); tone([990, 1480], 0.11, t + 0.085); return 0.2;
            case "send":   tone([1320], 0.06, t); tone([880], 0.12, t + 0.07); return 0.19;
            case "done":   tone([784], 0.08, t, 0.1); tone([1175], 0.16, t + 0.08, 0.08); return 0.24;
            case "error":  tone([330], 0.1, t); tone([247], 0.18, t + 0.13); return 0.31;
            case "remind": tone([523], 0.08, t, 0.07); tone([523], 0.08, t + 0.16, 0.07); return 0.3;
            // Soft glides, down to close the mic, up to open it again.
            case "mute":   tone([660, 440], 0.14, t, 0.08); return 0.2;
            case "unmute": tone([440, 660], 0.14, t, 0.08); return 0.2;
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
        const mine = own(buf, "answer");
        mine.id = clip.id;
        mine.turn = clip.turn;
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
            // One mic for the session: a pause keeps it open (a reopen asks
            // the permission again on iOS), a start reopens it only if it
            // died (a phone call took it).
            getStream: async function () { await ensureMic(gen); return audio.mic; },
            pauseStream: async function () {},
            resumeStream: async function () { await ensureMic(gen); return audio.mic; },
            startOnLoad: false,
            redemptionMs: REDEMPTION_MS,
            submitUserSpeechOnPause: true,
            onSpeechStart: function () {},
            onSpeechRealStart: function () {
                clearTimeout(remindTimer);
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

    /** Listen only in MON or TX, with the VAD loaded, no speech playing, and
     *  the mic not muted. */
    function syncListening(mode) {
        const want = phase === "live" && !!vad && !speech && !muted && (mode === "listen" || mode === "tx");
        if (want) startListening(); else stopListening();
    }

    function startListening() {
        if (listening || !vad || speech) return;
        listening = true;
        vadStarts++;
        const g = gen;
        vad.start().catch(function (e) {   // the mic could not be reopened
            if (g !== gen || !listening) return;
            listening = false;
            if (e && e.message !== "closed") fail(e.message || "Microphone unavailable", "vad");
        });
    }

    function stopListening() {
        if (!listening) return;
        listening = false;
        if (vad) vad.pause();   // a pause mid-speech hands the audio to onUtterance
    }

    /** Pause the VAD and keep the speech in progress for the message: the
     *  pause hands it to onUtterance after a microtask (vad-web awaits
     *  pauseStream first), so the intent waits for it. Resolves to whether a
     *  piece came of it. */
    function cutForSend() {
        listening = false;
        cutIntent = "send";
        return Promise.resolve(vad ? vad.pause() : null).catch(function () {}).then(function () {
            const cut = cutIntent === null;
            cutIntent = null;
            return cut;
        });
    }

    function onUtterance(samples) {
        const intent = cutIntent;
        cutIntent = null;
        if (!m || phase !== "live") return;
        const blob = function () { return new Blob([Core.encodeWav(samples, 16000)], { type: "audio/wav" }); };
        // The speech a Send tap cut: it ends the message.
        if (intent === "send") { piece(blob(), true); return; }
        if (!listening) return;   // not ours to send
        if (m.syncing) return;   // the agent may be busy: its frames are held back
        if (m.local !== "idle" && m.local !== "hearing") return;
        m.local = "idle";
        piece(blob(), false);
    }

    /** One piece of what is said, cut at a pause: transcribe it now, while
     *  the user goes on. The model keeps the pieces in the order said. */
    function piece(blob, send) {
        const p = Core.pieceCut(m, send);
        p.blob = blob;
        clearTimeout(remindTimer);
        render();
        transcribe(p);
    }

    function transcribe(p) {
        const g = gen;
        const model = m;
        transcribePiece(p.blob).then(function (r) {
            if (g !== gen || m !== model) return;
            if (r.error && !r.retry) {
                // Refused (quota, too long): the message cannot be made whole.
                Core.dropMessage(m);
                unack();   // whatever the console shows: the message is gone
                fail(r.error, null);
                return;
            }
            if (r.error) Core.pieceLost(m, p); else Core.pieceHeard(m, p, r.text);
            advance();
        });
    }

    /** The text of one piece through the transcription route (no target: the
     *  text comes back instead of going to the window). {text} or {error, retry}. */
    async function transcribePiece(blob) {
        for (let attempt = 0; ; attempt++) {
            const form = new FormData();
            form.append("file", blob, "piece.wav");
            form.append("language", lang());
            // Past its deadline, an attempt counts as failed: tried again.
            const res = await request("/api/terminal/transcribe",
                { method: "POST", body: form, cache: "no-store" }, TRANSCRIBE_TIMEOUT_MS);
            const data = res.data || {};
            if (res.status === 200) return { text: String(data.text || "") };
            if (res.status === 422) return { text: "" };   // unreadable or empty audio: nothing said
            if (res.status === 402 || res.status === 413) return { error: data.error || "The transcription was refused", retry: false };
            if (attempt >= PIECE_RETRY_MS.length) {
                return { error: "Could not reach Merlin to send what you said", retry: true };
            }
            await new Promise(function (r) { setTimeout(r, PIECE_RETRY_MS[attempt]); });
        }
    }

    /** Fold in what was transcribed, in the order said, and act on it: send a
     *  whole message, show a piece that failed, or wait. One message at a
     *  time: the next waits until say answered for this one. */
    function advance() {
        if (!m || phase === "off") return;
        if (saying || m.local === "error") { render(); return; }
        const next = Core.nextMessage(m);
        if (next && next.failed) {
            fail("Could not reach Merlin to send what you said", "pieces");
            return;
        }
        if (next) {
            if (next.send) { sendMessage(next.send); return; }
            if (m.local === "sending") unack();   // nothing was said after all
        } else if (Core.sendAsked(m) && m.local === "idle") {
            // The end word was heard, earlier pieces are still on their way.
            ack();
        }
        armRemind();
        render();
    }

    /** A send was asked: say so at once (the earcon, "Sending"), before any
     *  transcription lands. The earcon plays once per message. */
    function ack() {
        clearTimeout(remindTimer);
        m.local = "sending";
        unsent = false;
        if (!acked) { acked = true; earcon("send"); }
        sendingSince = Date.now();
        clearTimeout(slowTimer);
        const g = gen;
        slowTimer = setTimeout(function () { if (g === gen && m) render(); }, SEND_SLOW_MS + 20);
        render();
    }

    function unack() {
        acked = false;
        clearTimeout(slowTimer);
        if (m && m.local === "sending") { m.local = "idle"; unsent = true; }
    }

    /** A tap in TX: send what was said so far, the speech in progress and
     *  the pieces still being transcribed included. A tap while a send is
     *  asked does nothing more. */
    function tapSend() {
        if (m.local === "sending" || m.local === "waiting") return;
        if (m.local === "hearing") {
            const g = gen;
            const model = m;
            cutForSend().then(function (cut) {
                if (g !== gen || m !== model) return;
                // A misfire (too short to be speech): what was said before goes.
                if (!cut && !Core.sendAsked(m) && !Core.askSend(m)) { unack(); render(); return; }
                advance();
            });
            ack();
            return;
        }
        if (!Core.sendAsked(m) && !Core.askSend(m)) return;   // nothing said
        ack();
        advance();
    }

    function newMessageId() {
        const bytes = new Uint8Array(12);
        if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
        else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
        return Array.prototype.map.call(bytes, function (b) { return (b + 256).toString(16).slice(1); }).join("");
    }

    /** A whole message: its id is made now, and a retry sends the same one. */
    function sendMessage(text) {
        if (m.local !== "sending") ack();
        say({ text: text, id: newMessageId(), inflight: false });
    }

    async function say(msg) {
        const g = gen;
        const model = m;
        saying = msg;
        msg.inflight = true;
        const res = await api("POST", "say", { target: target, text: msg.text, id: msg.id }, SAY_TIMEOUT_MS);
        msg.inflight = false;
        if (g !== gen || m !== model) return;
        if (res.ok) {
            saying = null;
            acked = false;
            clearTimeout(slowTimer);
            if (m.local === "sending") {
                // A frame that arrived during the request already answered it.
                m.local = m.server === "listening" ? "waiting" : "idle";
                if (m.local === "waiting") armWait();
            }
            advance();   // a next message may be ready behind this one
            return;
        }
        if (res.status === 0 || res.status >= 500) {
            // Kept: a tap sends it again, same id, and what follows waits.
            fail(res.status === 0 ? "Could not reach Merlin to send what you said"
                : res.data.error || "Could not reach Merlin to send what you said", "say");
            return;
        }
        saying = null;
        acked = false;
        clearTimeout(slowTimer);
        fail(res.data.error || "The message was refused", null);
    }

    /** A message said but not ended: one soft reminder of the end word. */
    function armRemind() {
        clearTimeout(remindTimer);
        if (!m || !m.held || Core.sendAsked(m) || muted) return;
        const g = gen;
        remindTimer = setTimeout(function () {
            if (g === gen && m && m.held && !muted && !Core.sendAsked(m) && m.local === "idle" && Core.mode(m) === "tx") earcon("remind");
        }, REMIND_MS);
    }

    /** Mute or unmute the mic. Muted, the VAD is paused on the open stream
     *  (never closed: a reopen asks iOS's permission again), so nothing is
     *  cut, uploaded or transcribed. The speech in progress is dropped (a
     *  pause hands it to onUtterance, which drops what is not listened for),
     *  the pieces held are kept. The session is not told: the answer plays as
     *  usual. It lasts until the user unmutes, across turns, and a new
     *  console starts unmuted. */
    function setMuted(on) {
        if (!m || muted === on) return;
        muted = on;
        earcon(on ? "mute" : "unmute");
        if (on) {
            clearTimeout(remindTimer);
            if (m.local === "hearing") m.local = "idle";
            stopListening();
        } else {
            armRemind();
        }
        render();   // an unmute listens again where the mode listens
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
        view = savedView();
        applyView();
        renderWindow();
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
                const res = await api("POST", "start", { target: t, lang: lang(), view: view });
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
        saying = null;
        acked = false;
        unsent = false;
        muted = false;
        cutIntent = null;
        clearTimeout(slowTimer);
        clearTimeout(remindTimer);
        // The keys were on the console: they do not fall to the page.
        const hadKeys = root.contains(document.activeElement) && root.dataset.view === "dock";
        root.hidden = true;
        applyView();
        if (hadKeys && finePointer.matches && T.focus) setTimeout(T.focus, 0);
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
        api("POST", "view", { target: target, view: view });
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
            tapSend();
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
            if (kind === "start") { phase = "tune"; render(); connectSession(true); }
            else if (kind === "vad") { phase = "tune"; render(); connectSession(false); }
            else if (kind === "sync") resync();
            else if (kind === "clip") { Core.retryStalled(m); pump(); }
            // Whatever the error was, what the network left behind goes
            // again: the pieces that failed, in the order said, and a
            // message whose say got no answer (the same id: typed once).
            const again = Core.retryPieces(m);
            if (saying) {
                // Still out (the error was another's): it stays "Sending".
                ack();
                if (!saying.inflight) say(saying);
            } else if (Core.sendAsked(m)) ack();
            again.forEach(transcribe);
            advance();
            render();
        }
    }

    root.addEventListener("click", function (e) {
        const b = e.target.closest("button");
        const docked = root.dataset.view === "dock";
        if (!b) {
            if (!docked) tapAnywhere();
            return;
        }
        if (b === el.askMore) { askOpen = !askOpen; render(); return; }
        // Only the mute, never the tap: in full screen a tap anywhere sends.
        if ("mic" in b.dataset) {
            if (docked && e.detail > 0 && finePointer.matches && T.focus) setTimeout(T.focus, 0);
            setMuted(!muted);
            return;
        }
        // A button of the bar clicked with the mouse hands the keys back to the
        // terminal. From the keyboard (no click count), they stay on the bar.
        if (docked && e.detail > 0 && finePointer.matches && T.focus && !("viewToggle" in b.dataset)) setTimeout(T.focus, 0);
        if (b.dataset.lang) { setLang(b.dataset.lang); return; }
        if ("viewToggle" in b.dataset) { setView(docked ? "full" : "dock"); return; }
        const act = b.dataset.act;
        if (act === "tap") tapAnywhere();
        else if (act === "exit") close("", true);
        else if (act === "skip" && m) {
            // The answer whose clip could not be downloaded: drop it here
            // and on the server, and carry on.
            const stuck = Core.stalled(m);
            if (stuck) {
                // An earlier clip of that answer may still be playing.
                if (speech && speech.kind === "answer" && speech.turn === stuck.turn) stopSpeech();
                Core.dropTurn(m, stuck.turn);
                api("POST", "skip", { target: target, turn: stuck.turn });
            }
            dropBuffers();
            if (Core.stalled(m)) {
                // Another answer is stuck too: its retry and skip stay on screen.
                fail("Part of the answer could not be downloaded", "clip");
            } else {
                m.local = "idle";
                m.error = "";
                retry = null;
                render();
            }
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
        // The session stays on its window when the page moves to another one.
        if (phase === "off" || t === target) {
            windowName = d.window || "";
            if (phase !== "off") renderWindow();
        }
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
    if (micLang) micLang.addEventListener("change", onLang);

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
        earcons: function () { return earcons.slice(); },
        muted: function () { return muted; },
        vadStarts: function () { return vadStarts; },
        target: function () { return target; },
    };
    renderButton();
    if (window.ResizeObserver) {
        new ResizeObserver(function () {
            if (m && phase !== "off") renderAsk(currentMode(), root.dataset.view === "dock");
        }).observe(root);
    }

    // -- rendering ------------------------------------------------------------

    function currentMode() {
        if (!m) return "off";
        if (phase === "tune" || phase === "resume") return phase;
        return Core.mode(m);
    }

    function onModeChange(mode, prev) {
        modeSince = Date.now();
        if (prev === "ask") { cancelAskSpeech(); askFor = null; }
        // Back from a Send that had nothing to send: no answer came, so not
        // the earcons of one.
        const quiet = unsent && (mode === "listen" || mode === "tx");
        unsent = false;
        if (quiet) return;
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

    function slowSend(mode) {
        return mode === "proc" && m.local === "sending" && !m.syncing && Date.now() - sendingSince >= SEND_SLOW_MS;
    }

    function micOff(mode) {
        return muted && (mode === "listen" || mode === "tx");
    }

    function hintFor(mode, docked) {
        const endWord = lang() === "fr" ? "terminé" : "over";
        if (micOff(mode)) return "Microphone off";
        if (mode === "listen") return "Speak, then say \u201c" + endWord + "\u201d";
        if (mode === "tx") return "Say \u201c" + endWord + "\u201d to send" + (docked ? "" : ", or tap");
        if (mode === "proc" && m.syncing) return "Reconnecting";
        // The message has not left yet: the pieces or the say are still on
        // their way. Said in the bar too, where it is the only sign of it.
        if (slowSend(mode)) return "Waiting for the connection";
        if (mode === "tune") return "Loading voice detection";
        // An error from the server has nothing to retry, here or anywhere.
        if (mode === "error" && m.local !== "error") return "Exit to start again";
        if (docked) return "";   // the bar's buttons say it
        if (mode === "error" && !retry) return "Tap anywhere to dismiss";
        return MODES[mode].hint;
    }

    // The bar keeps one line for the question, and offers the rest when it does
    // not fit: a tap shows it all, and the bar makes room.
    function renderAsk(mode, docked) {
        if (mode !== "ask") askOpen = false;
        root.toggleAttribute("data-ask-open", docked && askOpen);
        if (mode !== "ask") return;
        el.askMore.hidden = true;   // measured without it: it must not be what cuts the text
        const cut = el.askText.scrollWidth > el.askText.clientWidth + 1;
        const offered = docked && (askOpen || cut);
        el.askMore.hidden = !offered;
        el.askMore.setAttribute("aria-expanded", askOpen ? "true" : "false");
        el.askMore.setAttribute("aria-label", askOpen ? "Show less of the question" : "Show the whole question");
    }

    function render() {
        if (!m) return;
        if (m.offReason && phase !== "off") { close(m.offReason, false); return; }
        const mode = currentMode();
        const spec = MODES[mode];
        root.dataset.mode = mode;
        const docked = root.dataset.view === "dock";
        el.name.textContent = mode === "proc" && m.local === "sending" ? "Sending" : spec.name;
        el.hint.textContent = hintFor(mode, docked);
        el.hint.title = el.hint.textContent;   // the bar may cut it short
        // A hint the bar shows even where it has no room for hints.
        root.toggleAttribute("data-hint-shown", slowSend(mode) || micOff(mode));
        renderMic();

        // No conversation text in the console: full screen reads as a voice
        // call, not something to read while driving, and the bar leaves the
        // terminal in view. Captions stay in the model only.

        el.ask.hidden = mode !== "ask";
        if (mode === "ask" && m.ask) {
            el.askText.textContent = m.ask.text || "The agent needs your answer.";
        }
        el.error.hidden = mode !== "error";
        if (mode === "error") el.error.textContent = m.error || m.notice || "The voice server failed";
        let shown = mode === "error" && retry === "clip" ? ["skip", "exit"] : BUTTONS[mode] || ["exit"];
        // An error the server raised has nothing to retry.
        if (mode === "error" && m.local !== "error") shown = shown.filter(function (k) { return k !== "tap"; });
        actionButtons.tap.textContent = mode === "error" && !retry ? "Dismiss" : TAP_LABELS[mode] || "";
        const focused = document.activeElement;
        Object.keys(actionButtons).forEach(function (k) { actionButtons[k].hidden = shown.indexOf(k) < 0; });
        // The button that had the keys is gone: they go to one that stays.
        if (focused && focused.hidden && focused.classList.contains("vc-btn")) {
            actionButtons[shown[0]].focus();
        }
        // Once the rest of the row is settled: it decides whether the question fits.
        renderAsk(mode, docked);

        showNotice();
        if (mode !== lastMode) {
            const prev = lastMode;
            lastMode = mode;
            onModeChange(mode, prev);
        } else {
            unsent = false;   // no way back to listening came of it: it is spent
        }
        // The dialog's sentence, once per dialog (a new dialog plays its own).
        if (mode === "ask" && m.ask && askFor !== (m.ask.audio || "")) playAsk(m.ask.audio);
        syncListening(mode);
        if (reduceMotion.matches) drawFrame(true);
    }

    // -- the scope ----------------------------------------------------------------

    let rafId = 0;
    let samples = null;
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
    let scopeBg = "";

    function drawFrame(still) {
        if (!m) return;
        const mode = currentMode();
        const dpr = fit();
        const w = canvas.width, h = canvas.height, mid = h / 2;
        if (sigMode !== mode || paintStale) {
            sigColor = signal();
            sigMode = mode;
            scopeBg = getComputedStyle(canvas.parentNode).backgroundColor || "#0f1117";
            paintStale = false;
        }

        const analyser = still ? null
            : (mode === "rx" || mode === "ask") ? audio.outAnalyser
            : (mode === "listen" || mode === "tx") && !muted ? audio.micAnalyser : null;
        let points = null;
        if (analyser) {
            if (!samples || samples.length !== analyser.fftSize) samples = new Float32Array(analyser.fftSize);
            analyser.getFloatTimeDomainData(samples);
            let peak = 0;
            for (let i = 0; i < samples.length; i++) {
                peak = Math.max(peak, Math.abs(samples[i]));
            }
            points = samples;
            // A quiet voice still draws a trace, a loud one stays on screen.
            const want = 0.8 / Math.max(peak, 0.15);
            gain += (want - gain) * (want < gain ? 0.5 : 0.05);
        }
        // Persistence: fade the last frame instead of clearing it.
        g.globalCompositeOperation = "source-over";
        g.fillStyle = scopeBg;
        g.globalAlpha = still ? 1 : 0.5;
        g.fillRect(0, 0, w, h);

        g.beginPath();
        const now = performance.now() / 1000;
        const n = 96;
        for (let i = 0; i <= n; i++) {
            const x = (i / n) * w;
            let y;
            if (points) {
                // Averaged over its stretch: a voice's shape, not its every edge.
                const from = Math.floor((i / n) * (points.length - 1));
                const span = Math.max(1, Math.floor(points.length / n));
                let v = 0;
                for (let k = from; k < Math.min(from + span, points.length); k++) v += points[k];
                v /= span;
                y = Math.max(2, Math.min(h - 2, mid - v * gain * mid * 0.5));
            } else if (still || micOff(mode)) {
                y = mid;   // muted: a flat line, nothing is heard
            } else if (mode === "proc" || mode === "tune") {
                // A pulse sweeping across: the agent is working.
                const p = ((now * 0.45) % 1.2) - 0.1;
                const d = i / n - p;
                y = mid - Math.exp(-d * d * 260) * h * 0.2;
            } else {
                y = mid + Math.sin(i * 0.12 + now * 2) * h * 0.012;
            }
            if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
        }
        g.strokeStyle = sigColor;
        g.lineJoin = "round";
        g.globalAlpha = 1;
        g.lineWidth = 1.5 * dpr;
        g.stroke();

        const s = Math.floor((Date.now() - modeSince) / 1000);
        const clock = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
        if (clock !== lastClock) { lastClock = clock; el.clock.textContent = clock; }
    }
}

if (window.MerlinTerminal) initVoiceConsole();
else document.addEventListener("DOMContentLoaded", initVoiceConsole);
