/* Tests for the native terminal's pure helpers (terminal/static/native/core.js),
   with the vendored xterm.js parser where rows are decoded. */

const test = require('node:test');
const assert = require('node:assert/strict');

const Core = require('../../terminal/static/native/core.js');
const { Terminal } = require('../../static/vendor/xterm.min.js');
const { Unicode11Addon } = require('../../static/vendor/xterm-addon-unicode11.min.js');

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
    const term = Core.newDecoder(Terminal, Unicode11Addon, 5);
    term.options.scrollback = 50;
    const rows = ['\x1b[31mAAAAA', 'BBBBB', '\x0eqq\x0fx'].map((s) => Buffer.from(s));
    const lines = Core.decodeRows(term, rows, 1);
    assert.deepEqual(lines.map((l) => l.translateToString(true)), ['BBBBB', '──x']);
    assert.equal(lines[0].getCell(0).getFgColor(), 1);
});

test('decodeRows keeps the rows after a skipped prefix longer than the scrollback', () => {
    const term = Core.newDecoder(Terminal, Unicode11Addon, 20);
    term.options.scrollback = 100;
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

// -- rows to DOM text ----------------------------------------------------------------

// Lines of a terminal fed ``data`` (synchronously, as decodeRows does).
function lines(cols, data, rows) {
    const t = Core.newDecoder(Terminal, Unicode11Addon, cols);
    t.resize(cols, rows || 4);
    t._core._inputHandler.parse(data);
    const buf = t.buffer.active;
    const out = [];
    for (let y = 0; y < buf.length; y++) out.push(buf.getLine(y));
    return out;
}

test('an address attached to text opens the web or the Files page, and nothing else', () => {
    const link = (uri, label) => `\x1b]8;;${uri}\x1b\\${label}\x1b]8;;\x1b\\`;
    const term = Core.newDecoder(Terminal, Unicode11Addon, 12);
    const rows = [
        link('https://e.com/a', 'docs') + ' x',
        link('file:///etc/x', 'path'),
        link('javascript:alert(1)', 'bad'),
        link('https://e.com/b', 'https://x.io'),
        link('https://e.com/"q', 'esc'),
        link('https://e.com/w', '日本') + ' y',
        link('file:///a%20b/c.md', 'ja…/c.md'),
        link('file://other/etc/x', 'ssh'),
        '\x1b]8;;https://e.com/c\x1b\\abc',
        'def\x1b]8;;\x1b\\ z',
    ].map((r) => Buffer.from(r));
    const ls = Core.decodeRows(term, rows, 0);
    const out = Core.rowsHtml(ls, 12, '0'.repeat(rows.length), false, '', undefined, term);
    const anchors = out.html.map((h) => [...h.matchAll(/<a class="nt-link"[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/g)].map((m) => [m[1], text(m[2])]));
    assert.deepEqual(anchors[0], [['https://e.com/a', 'docs']]);
    assert.deepEqual(anchors[1], [['/files/etc/x', 'path']]);
    assert.deepEqual(anchors[2], []);
    // The attached address wins over the one the text shows.
    assert.deepEqual(anchors[3], [['https://e.com/b', 'https://x.io']]);
    assert.deepEqual(anchors[4], [['https://e.com/&quot;q', 'esc']]);
    assert.deepEqual(anchors[5], [['https://e.com/w', '日本']]);
    // A link left open carries to the next row of the capture.
    assert.deepEqual(anchors[8], [['https://e.com/c', 'abc']]);
    assert.deepEqual(anchors[9], [['https://e.com/c', 'def']]);
    // The address keeps its escapes; a file on another machine is not ours.
    assert.deepEqual(anchors[6], [['/files/a%20b/c.md', 'ja…/c.md']]);
    assert.deepEqual(anchors[7], []);
    assert.equal(text(out.html[0]), 'docs x');
    // The parser's register holds the last capture's links, not every capture's.
    for (let i = 0; i < 50; i++) Core.decodeRows(term, rows, 0);
    assert.equal(term._core._oscLinkService._dataByLinkId.size, 9);
});

// The text a browser copies from a row's HTML: the text nodes, entities
// decoded. Boxes (span.p) have no text; the markup carries no other text.
function text(html) {
    return html.replace(/<[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

test('rowHtml: the text of a row is its characters, trailing blanks left out', () => {
    const [l] = lines(20, 'ab  cd   ');
    assert.equal(text(Core.rowHtml(l, 20)), 'ab  cd');
    const [e] = lines(20, '');
    assert.equal(Core.rowHtml(e, 20), '');
});

test('rowHtml: a row that wraps keeps every cell, spaces included', () => {
    // 10 columns: "abcde     " then "fgh" on the next row, one logical line.
    const [a, b] = lines(10, 'abcde     fgh');
    assert.equal(text(Core.rowHtml(a, 10, { wraps: true })), 'abcde     ');
    assert.equal(text(Core.rowHtml(b, 10)), 'fgh');
});

test('rowHtml: styled trailing blanks are boxes with no text', () => {
    const [l] = lines(10, 'ab\x1b[41m    \x1b[0m');
    const html = Core.rowHtml(l, 10);
    assert.equal(text(html), 'ab');
    assert.match(html, /<span class="p g1" style="width:4ch;"><\/span>$/);
    // An inverse status bar: the same.
    const [s] = lines(10, '\x1b[7mtop       \x1b[0m');
    const h2 = Core.rowHtml(s, 10);
    assert.equal(text(h2), 'top');
    assert.match(h2, /class="p gi fi"/);
});

test('rowHtml: wide characters once, in a 2ch box; symbols in a 1ch box', () => {
    const [l] = lines(20, '日本x🎉│é');
    const html = Core.rowHtml(l, 20);
    assert.equal(text(html), '日本x🎉│é');
    assert.equal((html.match(/class="w"/g) || []).length, 3);
    assert.match(html, /<span class="n">│<\/span>é/);
});

test('rowHtml: colours, bright bold, inverse, underline styles, escaping', () => {
    const [l] = lines(60, '\x1b[1;31mR\x1b[0m\x1b[38;5;208mO\x1b[0m\x1b[38;2;1;2;3mT\x1b[0m'
        + '\x1b[7mI\x1b[0m\x1b[4:3mC\x1b[0m\x1b[3;9mX\x1b[0m<&>');
    const html = Core.rowHtml(l, 60);
    assert.match(html, /<span class="f9 B">R<\/span>/);
    assert.match(html, /<span style="color:#ff8700;">O<\/span>/);
    assert.match(html, /<span style="color:#010203;">T<\/span>/);
    assert.match(html, /<span class="gi fi">I<\/span>/);
    assert.match(html, /<span class="U3">C<\/span>/);
    assert.match(html, /<span class="I S">X<\/span>/);
    assert.ok(html.endsWith('&lt;&amp;&gt;'));
    assert.equal(text(html), 'ROTICX<&>');
});

test('findLinks: a URL across wrapped rows, and across a boundary drawn earlier', () => {
    const cols = 12;
    // "see https://" | "example.com/" | "a?b=1. ok"
    const ls = lines(cols, 'see https://example.com/a?b=1. ok');
    const texts = ls.slice(0, 3).map((l) => Core.rowColumns(l, cols));
    const href = 'https://example.com/a?b=1';
    const per = Core.findLinks(texts, '');
    assert.deepEqual(per, [[[4, 12, href]], [[0, 12, href]], [[0, 5, href]]]);
    // The rows' HTML carries the anchors, the text unchanged.
    const out = Core.rowsHtml(ls.slice(0, 3), cols, '110', false, '');
    assert.equal(out.html.map(text).join(''), 'see https://example.com/a?b=1. ok');
    assert.equal((out.html.join('').match(/<a class="nt-link"[^>]*href="https:\/\/example.com\/a\?b=1"/g) || []).length, 3);
    // The last row alone, after the rows drawn elsewhere (the history/screen
    // boundary): the whole URL is still found.
    assert.deepEqual(Core.findLinks([texts[2]], texts[0] + texts[1]), [[[0, 5, href]]]);
    // A wide character stops a URL; no URL, no work.
    assert.deepEqual(Core.findLinks(['http://a.b日x'.replace('日', '\u0001')], ''), [[[0, 10, 'http://a.b']]]);
    assert.deepEqual(Core.findLinks(['plain', 'text'], ''), [null, null]);
});

test('groupRows: logical lines from wrap flags', () => {
    assert.deepEqual(Core.groupRows('0110', 4), [[0, 1], [1, 4]]);
    assert.deepEqual(Core.groupRows('', 0), []);
    assert.deepEqual(Core.groupRows('1', 1), [[0, 1]]);
});

test('mouseMode and mouseEncoding from the bridge state', () => {
    assert.equal(Core.mouseMode(null), 'none');
    assert.equal(Core.mouseMode({ standard: true }), 'vt200');
    assert.equal(Core.mouseMode({ standard: true, button: true }), 'drag');
    assert.equal(Core.mouseMode({ all: true }), 'any');
    assert.equal(Core.mouseEncoding({ sgr: true, utf8: true }), 'SGR');
    assert.equal(Core.mouseEncoding({ utf8: true }), 'UTF8');
    assert.equal(Core.mouseEncoding({}), 'DEFAULT');
    assert.equal(Core.encodeMouse('SGR', 64, 3, 4, false), '\x1b[<64;3;4M');
    assert.equal(Core.encodeMouse('UTF8', 0, 200, 1, false), '\x1b[M ' + String.fromCharCode(232) + '!');
});

test('X10 reports are bytes up to column and row 223, nothing past it', () => {
    const bytes = (col, row, release) => Core.mouseBytes(Core.encodeMouse('DEFAULT', 0, col, row, release));
    assert.equal(bytes(95, 3, false), '1b5b4d207f23');   // 32 + 95 = 0x7f
    assert.equal(bytes(96, 3, false), '1b5b4d208023');   // past ASCII: a byte, not text
    assert.equal(bytes(223, 223, false), '1b5b4d20ffff');
    assert.equal(bytes(223, 3, true), '1b5b4d23ff23');   // release: button 3
    assert.equal(Core.encodeMouse('DEFAULT', 0, 224, 3, false), '');
    assert.equal(Core.encodeMouse('DEFAULT', 0, 3, 224, false), '');
    // A release keeps the modifiers: Shift (4) + release (3).
    assert.equal(Core.encodeMouse('DEFAULT', 4, 1, 1, true).charCodeAt(3), 32 + 7);
    // SGR and UTF-8 carry larger values as text.
    assert.equal(Core.encodeMouse('SGR', 0, 300, 400, false), '\x1b[<0;300;400M');
    assert.equal(Core.encodeMouse('UTF8', 0, 2015, 1, false).length, 6);
    assert.equal(Core.encodeMouse('UTF8', 0, 2016, 1, false), '');
});

test('mouseCode: motion adds 32 to a drag and to a move with no button', () => {
    assert.equal(Core.mouseCode('press', 0), 0);
    assert.equal(Core.mouseCode('drag', 0), 32);
    assert.equal(Core.mouseCode('move', 3), 35);
    assert.equal(Core.mouseCode('release', 2), 2);
    assert.equal(Core.mouseCode('wheel', 64, { ctrl: true }), 80);
    assert.equal(Core.mouseCode('press', 0, { shift: true, alt: true }), 12);
    assert.equal(Core.encodeMouse('SGR', Core.mouseCode('move', 3), 5, 3, false), '\x1b[<35;5;3M');
});

