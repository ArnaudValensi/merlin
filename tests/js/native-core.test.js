/* Tests for the native terminal's pure helpers (terminal/static/native/core.js),
   with the vendored xterm.js parser where rows are decoded. */

const test = require('node:test');
const assert = require('node:assert/strict');

const Core = require('../../terminal/static/native/core.js');
const { Terminal } = require('../../static/vendor/xterm.min.js');

function frame(kind, header, rows) {
    return new Uint8Array(Buffer.concat([
        Buffer.from([kind]), Buffer.from(JSON.stringify(header)), Buffer.from([0]),
        Buffer.from(rows.join('\n')),
    ]));
}

test('parseFrame splits an update into its two captures, skips included', () => {
    const f = Core.parseFrame(frame(Core.FRAME_UPDATE, {
        hist: { start: 5, count: 2, skip: 1, wrapped: '10', cont: false, gap: 0 },
        screen: { skip: 1, wrapped: '00' },
    }, ['h0', 'h1', 'h2', 'o', 's0', '']));
    assert.deepEqual(f.histRows.map((r) => Buffer.from(r).toString()), ['h0', 'h1', 'h2']);
    assert.deepEqual(f.screenRows.map((r) => Buffer.from(r).toString()), ['o', 's0', '']);
    // No history kept: no history rows on the wire.
    const g = Core.parseFrame(frame(Core.FRAME_UPDATE, {
        hist: { start: 5, count: 0, skip: 0, wrapped: '', cont: false, gap: 0 },
        screen: { skip: 0, wrapped: '0' },
    }, ['only']));
    assert.equal(g.histRows.length, 0);
    assert.equal(g.screenRows.length, 1);
    const h = Core.parseFrame(frame(Core.FRAME_HISTORY, { start: 0, end: 2, skip: 1, wrapped: '00' }, ['p', 'a', 'b']));
    assert.equal(h.rows.length, 3);
    assert.equal(Core.parseFrame(new Uint8Array([1, 123])), null);
});

test('decodeRows decodes a capture as one stream: attributes and shift carry across rows', () => {
    const term = new Terminal({ cols: 5, rows: 1, scrollback: 50, allowProposedApi: true });
    const rows = ['\x1b[31mAAAAA', 'BBBBB', '\x0eqq\x0fx'].map((s) => Buffer.from(s));
    const lines = Core.decodeRows(term, rows, 1);
    assert.deepEqual(lines.map((l) => l.translateToString(true)), ['BBBBB', '──x']);
    assert.equal(lines[0].getCell(0).getFgColor(), 1);
});

test('decodeRows keeps the rows after a skipped prefix longer than the scrollback', () => {
    const term = new Terminal({ cols: 20, rows: 1, scrollback: 100, allowProposedApi: true });
    const rows = [Buffer.from('\x1b[32mstart')];
    for (let i = 1; i < 12003; i++) rows.push(Buffer.from('p' + i));
    for (let i = 0; i < 2000; i++) rows.push(Buffer.from('k' + i));
    const lines = Core.decodeRows(term, rows, 12003);
    assert.equal(lines.length, 2000);
    assert.equal(lines[0].translateToString(true), 'k0');
    assert.equal(lines[1999].translateToString(true), 'k1999');
    // The colour set on the first, skipped, row still applies.
    assert.equal(lines[1999].getCell(0).getFgColor(), 2);
});

test('joinSelection trims each logical line and joins with newlines', () => {
    assert.equal(Core.joinSelection(['a  ', 'b ', '  c']), 'a\nb\n  c');
});

test('keyAction and inputAction map keys and edits', () => {
    assert.deepEqual(Core.keyAction('ArrowLeft', { ctrl: true }), { key: 'C-Left' });
    assert.deepEqual(Core.keyAction('.', { alt: true }), { key: 'M-.' });
    assert.equal(Core.keyAction('a', {}), null);
    assert.deepEqual(Core.inputAction('deleteWordBackward'), { data: '\x17' });
    assert.deepEqual(Core.caretAction(-2), ['Left', 'Left']);
});
