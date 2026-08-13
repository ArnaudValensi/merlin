/*
 * Merlin clipboard core — the one implementation of "read the browser
 * clipboard and tell me what is in it".
 *
 * This file is a Jinja partial, included verbatim by BOTH terminal.html (the
 * real terminal) and clipboard-test.html (the /clipboard-test diagnostic
 * page). That is deliberate: the two used to carry hand-copied versions of the
 * same ladder, they drifted, and the diagnostic page ended up reporting
 * success on paths where the terminal showed "Clipboard blocked". Every
 * clipboard read must go through readClipboard() so what you debug is what
 * runs.
 *
 * It is pure logic: no DOM, no xterm, no globals beyond the clipboard object
 * handed to it. That is what makes tests/js/clipboard-core.test.js able to
 * replay each browser's behaviour without a browser.
 *
 * Keep this file free of Jinja delimiters — it is rendered as a template.
 */

const MerlinClipboard = (() => {
    'use strict';

    // Text flavours we accept from clipboard.read(), best first.
    //
    // text/html is deliberately absent: pasting raw markup into a shell is
    // worse than falling through to readText(), which hands back the plain
    // text flavour of the same content.
    const TEXT_TYPES = ['text/plain', 'text/uri-list'];

    // What the terminal shows when nothing could be pasted. Lives here so the
    // diagnostic page can print the exact status the terminal would display.
    const STATUS_FOR_REASON = {
        blocked: 'Clipboard blocked',
        empty: 'Clipboard empty',
        unsupported: 'Clipboard unavailable',
    };

    const noop = () => {};

    // text/uri-list is a line list in which '#' lines are comments (RFC 2483).
    const normalizeText = (type, text) => {
        if (type !== 'text/uri-list') return text;
        return text
            .split(/\r?\n/)
            .filter((line) => line && line.charAt(0) !== '#')
            .join('\n');
    };

    const describeTypes = (items) =>
        items.map((item) => (item.types || []).join('+') || '(none)').join(', ');

    /*
     * Read the clipboard and report what came back.
     *
     * opts.clipboard — clipboard object to read (defaults to
     *                  navigator.clipboard); injected by the tests.
     * opts.trace     — optional (level, message) sink; levels are
     *                  'ok' | 'warn' | 'err' | 'skip'.
     *
     * Resolves to one of:
     *   { kind: 'image', blob, type, via }
     *   { kind: 'text',  text, type, via }
     *   { kind: 'none',  reason: 'blocked' | 'empty' | 'unsupported', detail }
     *
     * It never rejects: every failure is a 'none' result the caller can act on.
     */
    const readClipboard = async (opts) => {
        const options = opts || {};
        const clipboard =
            'clipboard' in options
                ? options.clipboard
                : globalThis.navigator && globalThis.navigator.clipboard;
        const trace = options.trace || noop;

        if (!clipboard) {
            trace('err', 'navigator.clipboard is unavailable (insecure context?)');
            return {
                kind: 'none',
                reason: 'unsupported',
                detail: 'navigator.clipboard missing',
            };
        }

        let sawSuccess = false; // some API call resolved, so the API works
        let blockedBy = null; // error name of the first rejection
        let detail = '';

        // ---- Rung 1: clipboard.read() — the only API that can return images.
        if (typeof clipboard.read === 'function') {
            let items = null;
            try {
                items = Array.from(await clipboard.read());
                sawSuccess = true;
                trace('ok', `read() resolved with ${items.length} item(s)`);
            } catch (err) {
                blockedBy = (err && err.name) || 'Error';
                trace('err', `read() rejected: ${blockedBy}: ${err && err.message}`);
            }

            if (items) {
                // Images before text: a screenshot on the clipboard often also
                // carries a text flavour (a filename, a URL), so checking text
                // first would never reach the image.
                for (const item of items) {
                    const imgType = (item.types || []).find((t) => t.startsWith('image/'));
                    if (!imgType) continue;
                    try {
                        const blob = await item.getType(imgType);
                        trace('ok', `read() -> image ${imgType} (${blob.size} bytes)`);
                        return { kind: 'image', blob, type: imgType, via: 'read' };
                    } catch (err) {
                        // One unreadable item must not sink the paste: keep
                        // scanning, and still let the text rungs below run.
                        trace('warn', `getType(${imgType}) failed: ${err && err.name}`);
                    }
                }

                for (const item of items) {
                    const types = item.types || [];
                    const textType = TEXT_TYPES.find((t) => types.includes(t));
                    if (!textType) continue;
                    try {
                        const blob = await item.getType(textType);
                        const text = normalizeText(textType, await blob.text());
                        if (text) {
                            trace('ok', `read() -> text ${textType} (${text.length} chars)`);
                            return { kind: 'text', text, type: textType, via: 'read' };
                        }
                        // The clipboard genuinely holds an empty string. Asking
                        // readText() for it would only cost a second iOS paste
                        // prompt for the same nothing.
                        trace('warn', `read() -> ${textType} is empty`);
                        return { kind: 'none', reason: 'empty', detail: `${textType} empty` };
                    } catch (err) {
                        trace('warn', `getType(${textType}) failed: ${err && err.name}`);
                    }
                }

                detail = `read() gave ${items.length} item(s) [${describeTypes(items)}] with nothing usable`;
                trace('warn', detail);
            }
        } else {
            trace('skip', 'clipboard.read() unsupported — going straight to readText()');
        }

        // ---- Rung 2: readText() — text only, but implemented far more widely
        // and far more reliably than read(). This rung is what rescues the iOS
        // case where read() resolves with items we cannot pull any text out of;
        // before it existed the terminal reported "Clipboard blocked" right
        // after the user had tapped the native paste button.
        //
        // Skipped after a NotAllowedError: that means the user dismissed the
        // paste prompt (or the permission is denied outright), and readText()
        // would only raise a second prompt for the same refusal.
        if (blockedBy === 'NotAllowedError') {
            trace('skip', 'readText() skipped — read() was denied, a retry would only re-prompt');
        } else if (typeof clipboard.readText === 'function') {
            try {
                const text = await clipboard.readText();
                sawSuccess = true;
                if (text) {
                    trace('ok', `readText() -> ${text.length} chars`);
                    return { kind: 'text', text, type: 'text/plain', via: 'readText' };
                }
                trace('warn', 'readText() returned an empty string');
                return { kind: 'none', reason: 'empty', detail: 'readText() empty' };
            } catch (err) {
                blockedBy = blockedBy || (err && err.name) || 'Error';
                trace('err', `readText() rejected: ${err && err.name}: ${err && err.message}`);
            }
        } else {
            trace('skip', 'clipboard.readText() unsupported');
        }

        // ---- Nothing worked. Rank the reasons so the status the user sees
        // says which of the three situations they are in.
        if (blockedBy) {
            const why = detail ? `${blockedBy}; ${detail}` : blockedBy;
            return { kind: 'none', reason: 'blocked', detail: why };
        }
        if (sawSuccess) {
            return {
                kind: 'none',
                reason: 'empty',
                detail: detail || 'clipboard held nothing pasteable',
            };
        }
        return { kind: 'none', reason: 'unsupported', detail: 'no clipboard read API' };
    };

    const statusFor = (reason) => STATUS_FOR_REASON[reason] || STATUS_FOR_REASON.blocked;

    return { readClipboard, statusFor, TEXT_TYPES };
})();

/* Node (tests/js/clipboard-core.test.js) requires the file directly; browsers
   get MerlinClipboard as a plain script-scope const. */
if (typeof module !== 'undefined' && module.exports) module.exports = MerlinClipboard;
