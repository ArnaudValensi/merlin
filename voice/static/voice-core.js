/*
 * The voice console's pure parts, shared by the page (voice-console.js) and
 * node's test runner (tests/js/voice-core.test.js): the WAV encoder for an
 * utterance and the console model, which folds the server's voice frames and the page's own playback into the
 * one mode the console shows.
 *
 * The server says what the agent is doing (state frames, the clips to speak);
 * the page knows what it is still playing. Listening comes back only when both
 * are done: the server's turn is over and the last clip has been played.
 *
 * Ordering: every frame carries its session id and a revision that only
 * grows, and a snapshot carries the revision it reflects. Frames older than
 * what the model holds are dropped; while a snapshot is being fetched, frames
 * wait behind it (syncing) and are folded in after it, so a clip missed
 * during a disconnection is queued before the newer ones, and an old state
 * never replaces a newer one.
 *
 * While syncing, nothing starts playing and the page does not listen (the
 * frames that would say the agent is busy are the ones held back). The
 * barrier lifts only with a snapshot: a failed request leaves it up.
 *
 * Clips: queued, fetching, ready, playing, then remembered as played (a
 * bounded memory). Only the first few are fetched (the playback window), so
 * the server's own prefetch bound follows what is actually heard. A failed
 * download is retried a few times in place; past that the clip stays at its
 * place, stalled, and nothing after it plays until the user retries it or
 * skips that answer: speech is never heard out of order.
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

    // -- the end word ---------------------------------------------------------
    //
    // A pause does not end a message: what is said is cut at every pause and
    // transcribed piece by piece, and the message goes to the agent when a
    // piece ends with the end word, radio style ("terminé", "over"), in
    // either language.

    const END_CLAUSE = /^(termin(e|er|ee|es|ees|ez)|over|over and out)( (termin(e|er|ee|es|ees|ez)|over))*$/;
    const LAST_CLAUSE = /([^.!?,;:…—–]*)[.!?,;:…—–\s]*$/;

    function fold(text) {
        return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
            .replace(/[^a-z\s]/g, " ").replace(/\s+/g, " ").trim();
    }

    /** One transcribed piece: {text, end}, what was said without the end
     *  word, and whether it was there. The end word counts only as a clause
     *  of its own at the end of the piece ("Terminé.", "..., terminé"), so
     *  "c'est terminé" in a sentence never sends. */
    function endWord(text) {
        let rest = String(text || "").trim();
        let end = false;
        for (;;) {
            const match = LAST_CLAUSE.exec(rest);
            if (!match || !END_CLAUSE.test(fold(match[1]))) break;
            end = true;
            rest = rest.slice(0, match.index).replace(/[\s,;:—–]+$/, "").trim();
        }
        return { text: rest, end: end };
    }

    // -- the console model -------------------------------------------------------

    const MAX_CAPTIONS = 4;
    // Clips fetched ahead of playback, the one about to play included.
    const WINDOW = 2;
    // Download attempts for one clip, and the wait before each retry.
    const MAX_TRIES = 3;
    const RETRY_MS = [600, 1500];
    // What the model remembers to tell a replayed clip or turn from a new one.
    const MAX_PLAYED = 64;
    const MAX_SKIPPED = 32;
    const MAX_PENDING = 512;

    /**
     * server: the last state frame (listening, thinking, speaking, ask, error, off).
     * local:  what the page itself is doing: idle, hearing (speech detected),
     *         sending (a send was asked: from then until the say request
     *         answered, its pieces still being transcribed included),
     *         waiting (the message reached the window, the server has not
     *         answered yet), error (a local failure, tap to retry).
     */
    function createModel(target) {
        return {
            target: target,
            session: null,      // the server session's id, once known
            rev: 0,             // the newest revision folded in
            syncing: false,     // a snapshot is on its way: frames wait
            pending: [],
            server: "listening",
            local: "idle",
            held: false,        // part of a message said, its end word not yet
            error: "",          // shown in the error mode
            notice: "",         // a passing message (nothing heard, voice server down)
            offReason: null,    // set once the session is over
            heard: "",
            activity: "",
            ask: null,
            queue: [],          // clips to play, by revision: {id, text, turn, rev, status, tries, retryAt}
            playing: null,
            played: [],         // ids heard (bounded), never queued again
            skipped: [],        // turns skipped here (bounded)
            captions: [],       // [{who: 'you' | 'agent', text}]
            pieces: [],         // the message's pieces not folded in yet, in the order said
            words: [],          // the text of the pieces folded in, not sent yet
        };
    }

    // -- the message: its pieces, in the order said ---------------------------
    //
    // Each piece of speech is transcribed as soon as it is cut, while the user
    // goes on, so transcriptions can land in any order and fail. The pieces
    // wait here in the order said: a piece is folded into the message only once
    // every piece before it is, and a failed one holds the rest back until it
    // is retried, so nothing said is lost or reordered by the network.
    //
    // A piece carries `send` when the message ends with it: its text ended with
    // the end word, or a tap asked to send what was said up to it.

    function holding(m) {
        return m.words.length > 0 || m.pieces.length > 0;
    }

    /** A piece was cut: {status: up | done | failed, text, send}. */
    function pieceCut(m, send) {
        const p = { status: "up", text: "", send: !!send };
        m.pieces.push(p);
        m.held = true;
        return p;
    }

    /** Its transcription landed. False when the piece was dropped meanwhile. */
    function pieceHeard(m, p, text) {
        if (m.pieces.indexOf(p) < 0) return false;
        const said = endWord(text);
        p.status = "done";
        p.text = said.text;
        if (said.end) p.send = true;
        return true;
    }

    /** Its transcription failed, retries spent: it waits for a retry. */
    function pieceLost(m, p) {
        if (m.pieces.indexOf(p) < 0) return false;
        p.status = "failed";
        return true;
    }

    /** The failed pieces, in order, set to be transcribed again. */
    function retryPieces(m) {
        const again = m.pieces.filter(function (p) { return p.status === "failed"; });
        again.forEach(function (p) { p.status = "up"; });
        return again;
    }

    /** Whether the message is to go once its pieces are transcribed. */
    function sendAsked(m) {
        return m.pieces.some(function (p) { return p.send; });
    }

    /** A tap: send what was said so far, the pieces still in flight included.
     *  False when nothing was said. */
    function askSend(m) {
        if (m.pieces.length) {
            m.pieces[m.pieces.length - 1].send = true;
            return true;
        }
        if (!m.words.length) return false;
        m.pieces.push({ status: "done", text: "", send: true });
        return true;
    }

    /** Fold the transcribed pieces in, in order. Returns {send: text} when a
     *  message is complete (text may be empty: nothing was said), {failed:
     *  true} when the next piece failed, else null (waiting, or nothing). */
    function nextMessage(m) {
        let out = null;
        while (m.pieces.length && m.pieces[0].status === "done") {
            const p = m.pieces.shift();
            if (p.text) m.words.push(p.text);
            if (p.send) {
                out = { send: m.words.join(" ").trim() };
                m.words = [];
                break;
            }
        }
        m.held = holding(m);
        if (!out && m.pieces.length && m.pieces[0].status === "failed") out = { failed: true };
        return out;
    }

    /** Forget the message (its transcription was refused). */
    function dropMessage(m) {
        m.pieces = [];
        m.words = [];
        m.held = false;
    }

    function remember(list, value, max) {
        if (list.indexOf(value) < 0) list.push(value);
        while (list.length > max) list.shift();
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

    function known(m, id) {
        return m.played.indexOf(id) >= 0
            || (m.playing && m.playing.id === id)
            || m.queue.some(function (c) { return c.id === id; });
    }

    /** Queue a clip in revision order, unless it was heard, is queued, or
     *  belongs to a skipped turn. */
    function enqueue(m, clip) {
        if (!clip || !clip.id || known(m, clip.id) || m.skipped.indexOf(clip.turn) >= 0) return false;
        const item = {
            id: clip.id, text: clip.text || "", turn: clip.turn, rev: clip.rev || 0,
            status: "queued", tries: 0, retryAt: 0,
        };
        let at = m.queue.length;
        while (at > 0 && m.queue[at - 1].rev > item.rev) at--;
        m.queue.splice(at, 0, item);
        return true;
    }

    /** Fold one voice frame in. Returns false for another window's frame. */
    function applyFrame(m, f) {
        if (!f || f.type !== "voice" || f.target !== m.target) return false;
        if (m.syncing) {
            m.pending.push(f);
            if (m.pending.length > MAX_PENDING) m.pending.shift();
            return true;
        }
        if (f.session) {
            if (!m.session) m.session = f.session;
            if (f.session !== m.session) return true;   // an older session's frame
        }
        if (f.rev) {
            if (f.rev <= m.rev) return true;            // already folded in
            m.rev = f.rev;
        }
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

    /** Hold the frames until a snapshot has been folded in. Speech the page
     *  had started detecting is dropped: listening stops with the barrier. */
    function beginSync(m) {
        m.syncing = true;
        if (m.local === "hearing") m.local = "idle";
    }

    /** Fold a snapshot from GET /api/voice/session in, then the frames that
     *  waited behind it, and lift the barrier. */
    function applySnapshot(m, snap) {
        m.syncing = false;
        if (snap.active === false) {
            m.server = "off";
            m.offReason = m.offReason || "Voice mode is off";
        } else {
            if (snap.session && m.session && snap.session !== m.session) {
                // A new session on this window: nothing of the old one holds.
                m.queue = [];
                m.rev = 0;
            }
            if (snap.session) m.session = snap.session;
            const rev = snap.rev || 0;
            if (rev >= m.rev) {
                m.rev = rev;
                m.server = snap.state || m.server;
                m.heard = snap.heard || "";
                m.activity = snap.activity || "";
                m.ask = snap.ask || null;
                if (m.local === "waiting" && m.server !== "listening") m.local = "idle";
            }
            (snap.clips || []).forEach(function (c) { enqueue(m, c); });
        }
        const waiting = m.pending;
        m.pending = [];
        waiting.forEach(function (f) { applyFrame(m, f); });
    }

    /** The mode on screen: off, error, ask, rx, tx, proc or listen. While
     *  syncing the page never listens: what would be MON or TX is PROC. */
    function mode(m) {
        const base = baseMode(m);
        return m.syncing && (base === "listen" || base === "tx") ? "proc" : base;
    }

    function baseMode(m) {
        if (m.offReason) return "off";
        if (m.local === "error") return "error";
        if (m.server === "ask" || m.ask) return "ask";
        if (m.playing || m.queue.length) return "rx";
        if (m.local === "sending" || m.local === "waiting") return "proc";
        // Never listening while the agent works, even on a prompt typed in
        // the terminal while the user was talking.
        if (m.server === "thinking" || m.server === "speaking") return "proc";
        if (m.server === "error") return "error";
        // Speaking, or between two pieces of a message not ended yet.
        if (m.local === "hearing" || m.held) return "tx";
        return "listen";
    }

    /** The clips to download now: the first few in line not fetched yet
     *  (a failed one once its retry time has come). */
    function toFetch(m, now) {
        return m.queue.slice(0, WINDOW).filter(function (c) {
            return c.status === "queued" || (c.status === "failed" && c.retryAt <= now);
        });
    }

    /** The next clip, if it can play now: the head of the line, downloaded. */
    function nextReady(m) {
        if (m.playing || m.syncing) return null;
        const head = m.queue[0];
        return head && head.status === "ready" ? head : null;
    }

    function fetching(m, clip) {
        clip.status = "fetching";
    }

    function fetched(m, clip) {
        if (clip.status === "fetching") clip.status = "ready";
    }

    /** A download failed. Returns the wait before the retry, or -1 when the
     *  tries are spent: the clip stays at its place, stalled, and holds back
     *  what follows until retryStalled or a skip of its answer. */
    function fetchFailed(m, clip, now) {
        clip.tries += 1;
        if (clip.tries >= MAX_TRIES) {
            clip.status = "stalled";
            return -1;
        }
        const wait = RETRY_MS[Math.min(clip.tries - 1, RETRY_MS.length - 1)];
        clip.status = "failed";
        clip.retryAt = now + wait;
        return wait;
    }

    function stalled(m) {
        return m.queue.find(function (c) { return c.status === "stalled"; }) || null;
    }

    /** Try the stalled clips again, from scratch. */
    function retryStalled(m) {
        m.queue.forEach(function (c) {
            if (c.status === "stalled") { c.status = "queued"; c.tries = 0; c.retryAt = 0; }
        });
    }

    /** The head clip starts playing: only now is it captioned. */
    function begin(m, clip) {
        m.queue = m.queue.filter(function (c) { return c !== clip; });
        m.playing = clip;
        if (!clip.captioned) {
            clip.captioned = true;
            caption(m, "agent", clip.text);
        }
    }

    /** The playing clip was heard to its end. */
    function clipDone(m) {
        if (m.playing) remember(m.played, m.playing.id, MAX_PLAYED);
        m.playing = null;
    }

    /** The playing clip was cut (the agent asked something): it goes back to
     *  the head of the line, still downloaded, and plays again afterwards. */
    function requeue(m) {
        const clip = m.playing;
        if (!clip) return;
        m.playing = null;
        m.queue.unshift(clip);
    }

    function dropTurn(m, turn) {
        remember(m.skipped, turn, MAX_SKIPPED);
        m.queue = m.queue.filter(function (c) { return c.turn !== turn; });
        if (m.playing && m.playing.turn === turn) m.playing = null;
    }

    /** Drop the rest of the answer being heard. Returns the skipped turn. */
    function skip(m) {
        const current = m.playing || m.queue[0];
        if (!current) return null;
        dropTurn(m, current.turn);
        return current.turn;
    }

    /** Drop everything waiting to be spoken (the agent was stopped). */
    function silence(m) {
        const turns = m.queue.map(function (c) { return c.turn; });
        if (m.playing) turns.push(m.playing.turn);
        turns.forEach(function (t) { dropTurn(m, t); });
    }

    return {
        encodeWav: encodeWav,
        endWord: endWord,
        createModel: createModel,
        holding: holding,
        pieceCut: pieceCut,
        pieceHeard: pieceHeard,
        pieceLost: pieceLost,
        retryPieces: retryPieces,
        sendAsked: sendAsked,
        askSend: askSend,
        nextMessage: nextMessage,
        dropMessage: dropMessage,
        applyFrame: applyFrame,
        beginSync: beginSync,
        applySnapshot: applySnapshot,
        mode: mode,
        toFetch: toFetch,
        nextReady: nextReady,
        fetching: fetching,
        fetched: fetched,
        fetchFailed: fetchFailed,
        stalled: stalled,
        retryStalled: retryStalled,
        begin: begin,
        clipDone: clipDone,
        requeue: requeue,
        skip: skip,
        dropTurn: dropTurn,
        silence: silence,
        MAX_CAPTIONS: MAX_CAPTIONS,
        WINDOW: WINDOW,
        MAX_TRIES: MAX_TRIES,
        MAX_PLAYED: MAX_PLAYED,
    };
})();

/* Node (tests/js/voice-core.test.js) requires the file directly; browsers get
   MerlinVoiceCore as a plain script-scope const. */
if (typeof module !== "undefined" && module.exports) module.exports = MerlinVoiceCore;
