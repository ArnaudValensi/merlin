/*
 * Merlin audio audition core — the one implementation of "play at most one
 * sample at a time from the File Browser listing, inline, without navigating".
 *
 * It is pure state: no DOM, no globals beyond the audio elements it is handed a
 * factory for. All visible effects go through the onStart / onStop callbacks the
 * caller supplies, so tests/js/audio-audition.test.js can replay start, stop,
 * switch, natural end, and rejected play() without a browser or real audio.
 *
 * files/static/files.js owns the DOM side: it builds the per-row play button,
 * turns onStart/onStop into row and button classes, and calls stop() when the
 * user navigates away. Playback itself reuses /api/files/raw, the same URL the
 * detail-view audio player streams from.
 */

const MerlinAudioAudition = (() => {
    'use strict';

    // Build a controller. Options:
    //   createAudio(src) -> an <audio>-like object with play() (may return a
    //                       Promise), pause(), addEventListener/removeEventListener
    //                       for the 'ended' event. Defaults to `new Audio(src)`.
    //   onStart(path)     -> called when `path` begins playing.
    //   onStop(path)      -> called when `path` stops for any reason: toggled
    //                        off, replaced by another sample, ended on its own,
    //                        stopped by navigation, or failed to start.
    function createAuditionController(options) {
        options = options || {};
        const createAudio =
            options.createAudio ||
            function (src) {
                return new Audio(src);
            };
        const onStart = options.onStart || function () {};
        const onStop = options.onStop || function () {};

        // The one sample currently loaded, or null. `token` guards against a
        // stale play() rejection or 'ended' event arriving after the user has
        // already switched to a different sample.
        let current = null;
        let seq = 0;

        function detach() {
            if (!current) return;
            try {
                current.audio.pause();
            } catch (_e) {
                // A fake or a torn-down element may not implement pause(). The
                // caller's onStop still needs to run, so swallow and continue.
            }
            current.audio.removeEventListener("ended", current.onEnded);
            current = null;
        }

        // Stop whatever is playing and fire onStop for it. No-op when idle.
        function stop() {
            if (!current) return;
            const stopped = current.path;
            detach();
            onStop(stopped);
        }

        function rollback(token, path) {
            // A play() attempt failed. Only roll back if it is still the current
            // one; if the user already switched, the newer sample owns state.
            if (!current || current.token !== token) return;
            detach();
            onStop(path);
        }

        function start(path, src) {
            // Replacing the current sample fires its onStop first.
            stop();
            const audio = createAudio(src != null ? src : path);
            const token = ++seq;
            const onEnded = function () {
                if (!current || current.token !== token) return;
                const ended = current.path;
                detach();
                onStop(ended);
            };
            audio.addEventListener("ended", onEnded);
            current = { path: path, audio: audio, onEnded: onEnded, token: token };
            onStart(path);

            let result;
            try {
                result = audio.play();
            } catch (_e) {
                rollback(token, path);
                return;
            }
            if (result && typeof result.then === "function") {
                result.then(undefined, function () {
                    rollback(token, path);
                });
            }
        }

        // Play `path` if it is not the one already playing, otherwise stop it.
        // Returns true if this call started playback, false if it stopped it.
        function toggle(path, src) {
            if (current && current.path === path) {
                stop();
                return false;
            }
            start(path, src);
            return true;
        }

        function playingPath() {
            return current ? current.path : null;
        }

        return {
            toggle: toggle,
            start: start,
            stop: stop,
            playingPath: playingPath,
        };
    }

    return { createAuditionController: createAuditionController };
})();

/* Node (tests/js/audio-audition.test.js) requires the file directly; browsers
   get MerlinAudioAudition as a plain script-scope const. */
if (typeof module !== "undefined" && module.exports) module.exports = MerlinAudioAudition;
