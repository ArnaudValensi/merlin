/*
 * The voice console's pure parts, shared by the page (voice-console.js) and
 * node's test runner (tests/js/voice-core.test.js): the WAV encoder for an
 * utterance, the call sign and frequency of a window, and the console model,
 * which folds the server's voice frames and the page's own playback into the
 * one mode the console shows.
 *
 * The server says what the agent is doing (state frames, the clips to speak);
 * the page knows what it is still playing. Listening comes back only when both
 * are done: the server's turn is over and the last clip has been played.
 */

const MerlinVoiceCore = (function () {
    "use strict";

    // -- WAV ---------------------------------------------------------------

    /** 16-bit PCM mono WAV of float samples in [-1, 1]. */
    function encodeWav(samples, sampleRate) {
        const n = samples.length;
        const buffer = new ArrayBuffer(44 + n * 2);
        const view = new DataView(buffer);
        function ascii(offset, text) {
            for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
        }
        ascii(0, "RIFF");
        view.setUint32(4, 36 + n * 2, true);
        ascii(8, "WAVE");
        ascii(12, "fmt ");
        view.setUint32(16, 16, true);         // fmt chunk size
        view.setUint16(20, 1, true);          // PCM
        view.setUint16(22, 1, true);          // mono
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * 2, true);
        view.setUint16(32, 2, true);          // block align
        view.setUint16(34, 16, true);         // bits per sample
        ascii(36, "data");
        view.setUint32(40, n * 2, true);
        for (let i = 0; i < n; i++) {
            const s = Math.max(-1, Math.min(1, samples[i] || 0));
            view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        }
        return buffer;
    }

    // -- identity -------------------------------------------------------------

    function hash(text) {
        let h = 2166136261;
        for (let i = 0; i < text.length; i++) {
            h ^= text.charCodeAt(i);
            h = Math.imul(h, 16777619);
        }
        return h >>> 0;
    }

    /** "MAIN-7" for session "main", window "@7": the radio name of a window. */
    function callSign(target) {
        const parts = String(target || "").split(":");
        const session = (parts[0] || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 6) || "MRLN";
        const win = (parts[1] || "").replace(/[^0-9]/g, "");
        return win ? session + "-" + win : session;
    }

    /** A stable "frequency" in the 2 m band, on a 5 kHz raster: "146.355". */
    function frequency(target) {
        const steps = hash(String(target || "")) % 800;   // 144.000 .. 147.995
        const khz = 144000 + steps * 5;
        return (khz / 1000).toFixed(3);
    }

    // -- the console model -------------------------------------------------------

    const MAX_CAPTIONS = 4;

    /**
     * server: the last state frame (listening, thinking, speaking, ask, error, off).
     * local:  what the page itself is doing: idle, hearing (speech detected),
     *         sending (uploading the utterance), waiting (uploaded, the server
     *         has not answered yet), error (a local failure, tap to retry).
     */
    function createModel(target) {
        return {
            target: target,
            server: "listening",
            local: "idle",
            error: "",          // shown in the error mode
            notice: "",         // a passing message (nothing heard, voice server down)
            offReason: null,    // set once the session is over
            heard: "",
            activity: "",
            ask: null,
            queue: [],          // clips to play, in say order: {id, text, turn}
            playing: null,
            seen: new Set(),    // clip ids ever queued: a snapshot never replays them
            skipped: new Set(), // turns skipped here
            captions: [],       // [{who: 'you' | 'agent', text}]
        };
    }

    function caption(m, who, text) {
        text = String(text || "").trim();
        if (!text) return;
        const last = m.captions[m.captions.length - 1];
        if (last && last.who === who && who === "agent") {
            last.text = (last.text + " " + text).slice(-600);
        } else {
            m.captions.push({ who: who, text: text });
        }
        while (m.captions.length > MAX_CAPTIONS) m.captions.shift();
    }

    function enqueue(m, clip) {
        if (!clip || !clip.id || m.seen.has(clip.id) || m.skipped.has(clip.turn)) return false;
        m.seen.add(clip.id);
        m.queue.push({ id: clip.id, text: clip.text || "", turn: clip.turn });
        return true;
    }

    /** Fold one voice frame in. Returns false for another window's frame. */
    function applyFrame(m, f) {
        if (!f || f.type !== "voice" || f.target !== m.target) return false;
        // Nothing runs while the page listens, so any frame after an upload
        // is the server's answer to it (or says why there is none).
        if (m.local === "waiting") m.local = "idle";
        switch (f.event) {
            case "state":
                m.server = f.state;
                if (f.state === "off") m.offReason = f.reason || "Voice mode turned off";
                if (f.state !== "ask") m.ask = null;
                break;
            case "heard":
                m.heard = f.text || "";
                caption(m, "you", m.heard);
                break;
            case "activity":
                m.activity = f.label || "";
                break;
            case "say":
                enqueue(m, f);
                break;
            case "turn_end":
                m.activity = "";
                break;
            case "ask":
                m.ask = { excerpt: f.excerpt || "", text: f.text || "", audio: f.audio || null };
                break;
            case "error":
                m.notice = f.message || "";
                break;
        }
        return true;
    }

    /** A (re)connected page picks the session up from GET /api/voice/session. */
    function applySnapshot(m, snap) {
        if (!snap) return;
        if (snap.active === false) {
            m.server = "off";
            m.offReason = m.offReason || "Voice mode is off";
            return;
        }
        m.server = snap.state || m.server;
        m.heard = snap.heard || "";
        m.activity = snap.activity || "";
        m.ask = snap.ask || null;
        if (m.local === "waiting" && m.server !== "listening") m.local = "idle";
        (snap.clips || []).forEach(function (c) { enqueue(m, c); });
    }

    /** The mode on screen: off, error, ask, rx, tx, proc or listen. */
    function mode(m) {
        if (m.offReason) return "off";
        if (m.local === "error") return "error";
        if (m.server === "ask" || m.ask) return "ask";
        if (m.playing || m.queue.length) return "rx";
        if (m.local === "sending" || m.local === "waiting") return "proc";
        // Never listening while the agent works, even on a prompt typed in
        // the terminal while the user was talking.
        if (m.server === "thinking" || m.server === "speaking") return "proc";
        if (m.server === "error") return "error";
        if (m.local === "hearing") return "tx";
        return "listen";
    }

    /** Take the next clip to play, or null. */
    function nextClip(m) {
        m.playing = m.queue.shift() || null;
        if (m.playing) caption(m, "agent", m.playing.text);
        return m.playing;
    }

    function clipDone(m) {
        m.playing = null;
    }

    /** Drop the rest of the answer being heard. Returns the skipped turn. */
    function skip(m) {
        const current = m.playing || m.queue[0];
        if (!current) return null;
        const turn = current.turn;
        m.skipped.add(turn);
        m.queue = m.queue.filter(function (c) { return c.turn !== turn; });
        m.playing = null;
        return turn;
    }

    /** Drop everything waiting to be spoken (the agent was stopped). */
    function silence(m) {
        if (m.playing) m.skipped.add(m.playing.turn);
        m.queue.forEach(function (c) { m.skipped.add(c.turn); });
        m.queue = [];
        m.playing = null;
    }

    return {
        encodeWav: encodeWav,
        callSign: callSign,
        frequency: frequency,
        createModel: createModel,
        applyFrame: applyFrame,
        applySnapshot: applySnapshot,
        mode: mode,
        nextClip: nextClip,
        clipDone: clipDone,
        skip: skip,
        silence: silence,
        MAX_CAPTIONS: MAX_CAPTIONS,
    };
})();

/* Node (tests/js/voice-core.test.js) requires the file directly; browsers get
   MerlinVoiceCore as a plain script-scope const. */
if (typeof module !== "undefined" && module.exports) module.exports = MerlinVoiceCore;
