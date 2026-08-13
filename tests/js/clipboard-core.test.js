/*
 * Tests for terminal/templates/clipboard-core.js — the paste ladder shared by
 * the terminal and /clipboard-test.
 *
 * Run: node --test tests/js/   (also part of `uv run scripts.py validate`)
 *
 * Each case replays a browser behaviour we have actually hit in the field, so
 * a future clipboard change has to keep every platform working, not just the
 * one being fixed. When a new browser quirk shows up, add it here first.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const MerlinClipboard = require('../../terminal/templates/clipboard-core.js');
const { readClipboard, statusFor } = MerlinClipboard;

// --- fakes ---------------------------------------------------------------

const err = (name) => Object.assign(new Error(name), { name });

// A ClipboardItem: `data` maps a MIME type to a string (text), a blob-ish
// object (image), or an Error to raise from getType().
const item = (types, data = {}) => ({
    types,
    getType: async (type) => {
        const value = data[type];
        if (value === undefined) throw err('NotFoundError');
        if (value instanceof Error) throw value;
        if (typeof value === 'string') return { text: async () => value };
        return value;
    },
});

// A navigator.clipboard. `read`/`readText` may be a value, an Error to reject
// with, or omitted entirely to model a browser lacking that API.
function clipboardOf({ read, readText } = {}) {
    const calls = { read: 0, readText: 0 };
    const cb = { calls };
    if (read !== undefined) {
        cb.read = async () => {
            calls.read++;
            if (read instanceof Error) throw read;
            return read;
        };
    }
    if (readText !== undefined) {
        cb.readText = async () => {
            calls.readText++;
            if (readText instanceof Error) throw readText;
            return readText;
        };
    }
    return cb;
}

// --- the happy paths we must not break -----------------------------------

test('desktop: read() returns text/plain', async () => {
    const clipboard = clipboardOf({ read: [item(['text/plain'], { 'text/plain': 'hello' })] });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'hello');
    assert.equal(r.via, 'read');
    assert.equal(clipboard.calls.readText, 0, 'readText() must not be called when read() succeeds');
});

test('image on the clipboard is uploaded, not pasted as text', async () => {
    const blob = { size: 42 };
    const clipboard = clipboardOf({ read: [item(['image/png'], { 'image/png': blob })] });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'image');
    assert.equal(r.blob, blob);
    assert.equal(r.type, 'image/png');
});

test('image wins over the text flavour that rides along with it', async () => {
    const blob = { size: 7 };
    const clipboard = clipboardOf({
        read: [item(['text/plain', 'image/png'], { 'text/plain': 'screenshot.png', 'image/png': blob })],
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'image');
});

test('old Firefox: no read(), readText() carries the paste', async () => {
    const clipboard = clipboardOf({ readText: 'from readText' });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.via, 'readText');
    assert.equal(r.text, 'from readText');
});

// --- the iOS Brave bug this ladder was rebuilt for ------------------------
//
// The user long-presses, taps the Merlin paste pill, taps the native iOS
// paste button — so read() is allowed and resolves — but the items it hands
// back carry no flavour we can use. The old code fell straight past the
// readText() fallback to "Clipboard blocked" with a perfectly readable
// clipboard sitting right there.

test('read() resolves with unusable items: readText() still runs', async () => {
    const clipboard = clipboardOf({
        read: [item(['text/html'], { 'text/html': '<b>hi</b>' })],
        readText: 'hi',
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'hi');
    assert.equal(r.via, 'readText');
});

test('read() resolves with zero items: readText() still runs', async () => {
    const clipboard = clipboardOf({ read: [], readText: 'rescued' });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'rescued');
});

test('read() resolves but getType() throws: readText() still runs', async () => {
    const clipboard = clipboardOf({
        read: [item(['text/plain'], { 'text/plain': err('NotAllowedError') })],
        readText: 'rescued',
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'rescued');
});

test('an unreadable image item does not sink the text on the clipboard', async () => {
    const clipboard = clipboardOf({
        read: [
            item(['image/png'], { 'image/png': err('DataError') }),
            item(['text/plain'], { 'text/plain': 'still here' }),
        ],
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'still here');
    assert.equal(r.via, 'read');
});

test('text/uri-list is accepted, with RFC 2483 comment lines stripped', async () => {
    const clipboard = clipboardOf({
        read: [item(['text/uri-list'], { 'text/uri-list': '# comment\nhttps://example.com' })],
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'https://example.com');
});

// --- no double paste prompts ---------------------------------------------

test('a denied read() does not re-prompt through readText()', async () => {
    const clipboard = clipboardOf({ read: err('NotAllowedError'), readText: 'unreachable' });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'none');
    assert.equal(r.reason, 'blocked');
    assert.equal(clipboard.calls.readText, 0, 'a refused paste prompt must not be raised twice');
});

test('an explicitly empty clipboard does not re-prompt through readText()', async () => {
    const clipboard = clipboardOf({
        read: [item(['text/plain'], { 'text/plain': '' })],
        readText: 'unreachable',
    });
    const r = await readClipboard({ clipboard });
    assert.equal(r.reason, 'empty');
    assert.equal(clipboard.calls.readText, 0);
});

test('read() failing for a non-permission reason does fall through', async () => {
    const clipboard = clipboardOf({ read: err('NotSupportedError'), readText: 'ok' });
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'text');
    assert.equal(r.text, 'ok');
});

// --- the three ways nothing gets pasted ----------------------------------

test('both APIs rejected reports blocked', async () => {
    const clipboard = clipboardOf({ read: err('DataError'), readText: err('NotAllowedError') });
    const r = await readClipboard({ clipboard });
    assert.equal(r.reason, 'blocked');
    assert.match(r.detail, /Error/);
});

test('readText() returning an empty string reports empty, not blocked', async () => {
    const r = await readClipboard({ clipboard: clipboardOf({ readText: '' }) });
    assert.equal(r.reason, 'empty');
});

test('no clipboard object at all reports unsupported', async () => {
    const r = await readClipboard({ clipboard: null });
    assert.equal(r.reason, 'unsupported');
});

test('a clipboard object with neither API reports unsupported', async () => {
    const r = await readClipboard({ clipboard: {} });
    assert.equal(r.reason, 'unsupported');
});

// --- contract -------------------------------------------------------------

test('readClipboard never rejects, whatever the browser throws', async () => {
    const clipboard = {
        read: () => {
            throw err('SecurityError');
        },
        readText: () => {
            throw err('SecurityError');
        },
    };
    const r = await readClipboard({ clipboard });
    assert.equal(r.kind, 'none');
});

test('every failure reason maps to a status string', () => {
    for (const reason of ['blocked', 'empty', 'unsupported']) {
        assert.equal(typeof statusFor(reason), 'string');
        assert.ok(statusFor(reason).length > 0);
    }
    assert.equal(statusFor('nonsense'), 'Clipboard blocked');
});

test('the trace sink sees every rung the ladder walks', async () => {
    const lines = [];
    const clipboard = clipboardOf({ read: [], readText: 'x' });
    await readClipboard({ clipboard, trace: (level, message) => lines.push([level, message]) });
    assert.ok(lines.length >= 2, 'trace should describe read() and readText()');
    assert.ok(lines.every(([level]) => ['ok', 'warn', 'err', 'skip'].includes(level)));
});
