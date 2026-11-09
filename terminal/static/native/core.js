// Native terminal: pure helpers, shared by the page and tests/js (node's own
// runner, no dependencies). No DOM, no WebSocket here.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MerlinNativeCore = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  const FRAME_UPDATE = 1, FRAME_HISTORY = 3;

  // A binary frame from /ws/terminal2 (terminal/native.py): kind byte, JSON
  // header, NUL, then rows of tmux's grid (capture-pane -e lines) separated by
  // LF. An update carries two captures, the history's then the screen's; each
  // starts with ``skip`` rows to decode and not keep (a capture carries
  // attributes from row to row, so it is decoded from its first row).
  function parseFrame(buffer) {
    const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    const kind = bytes[0];
    const nul = bytes.indexOf(0, 1);
    if (nul < 0 || (kind !== FRAME_UPDATE && kind !== FRAME_HISTORY)) return null;
    let header;
    try { header = JSON.parse(new TextDecoder().decode(bytes.subarray(1, nul))); } catch (e) { return null; }
    if (kind === FRAME_HISTORY) {
      return { kind, header, rows: splitRows(bytes.subarray(nul + 1), header.skip + header.wrapped.length) };
    }
    const nHist = header.hist.count ? header.hist.skip + header.hist.count : 0;
    const nScreen = header.screen.skip + header.screen.wrapped.length;
    const rows = splitRows(bytes.subarray(nul + 1), nHist + nScreen);
    return { kind, header, histRows: rows.slice(0, nHist), screenRows: rows.slice(nHist) };
  }

  // ``count`` rows separated by LF (an empty body is no row, not one empty row).
  function splitRows(body, count) {
    const rows = [];
    if (!count) return rows;
    let start = 0;
    for (let i = 0; i < body.length && rows.length < count - 1; i++) {
      if (body[i] === 10) { rows.push(body.subarray(start, i)); start = i + 1; }
    }
    rows.push(body.subarray(start));
    return rows;
  }

  // The scratch parser rows are decoded with: as wide as the pane, never
  // opened, and counting character widths as tmux does (Unicode 11: an emoji
  // such as U+1F389 takes two columns, which xterm.js's default Unicode 6
  // tables count as one, shifting every column after it).
  function newDecoder(Terminal, Unicode11Addon, cols) {
    const term = new Terminal({ cols, rows: 1, scrollback: 4000, allowProposedApi: true });
    term.loadAddon(new Unicode11Addon());
    term.unicode.activeVersion = '11';
    // A new row is blank. xterm.js would fill it with the running background,
    // and tmux sends neither a reset nor the cells a row leaves empty: the
    // colour a row ends in would spill over the next one. A capture holds text
    // and colours only, so this erase serves nothing but new rows.
    const parser = term._core._inputHandler;
    parser._eraseAttrData = () => parser._eraseAttrDataInternal;
    return term;
  }

  // One capture of tmux's grid decoded by a scratch xterm.js parser (never
  // opened) as one continuous stream, as tmux wrote it: attributes and the
  // shift state carry from row to row. Returns the buffer lines of the rows
  // after the first ``skip``, valid until the next call. The parser must be as
  // wide as the pane. capture-pane -e writes line-drawing cells with SO, hence
  // G1 = DEC graphics.
  const G1_GRAPHICS = [0x1b, 0x29, 0x30];
  function decodeRows(term, rows, skip) {
    // The parser keeps an entry per hyperlink of every capture: only the
    // current capture's are ever read.
    const links = term._core._oscLinkService;
    links._dataByLinkId.clear();
    links._entriesWithId.clear();
    term.reset();
    if (!rows.length) return [];
    // Every row of the capture must stay in the parser's buffer until read:
    // the skipped prefix is decoded (its attributes carry) but may be long.
    if (term.options.scrollback < rows.length) term.options.scrollback = rows.length;
    let size = G1_GRAPHICS.length;
    for (const r of rows) size += r.length + 2;
    const all = new Uint8Array(size);
    let o = 0;
    all.set(G1_GRAPHICS, o); o += G1_GRAPHICS.length;
    rows.forEach((r, i) => {
      all.set(r, o); o += r.length;
      if (i < rows.length - 1) { all[o++] = 13; all[o++] = 10; }
    });
    term._core._inputHandler.parse(all.subarray(0, o));
    const buf = term.buffer.active;
    const lines = [];
    // The capture's rows are the buffer's last rows.length lines.
    const first = buf.length - rows.length + (skip || 0);
    if (first < 0) throw new Error('decodeRows: rows were trimmed from the parser');
    for (let y = first; y < buf.length; y++) lines.push(buf.getLine(y));
    return lines;
  }

  // The copied text of a selection: one string per logical line touched,
  // trailing blanks trimmed, lines joined by a newline.
  function joinSelection(lines) {
    return lines.map((l) => l.replace(/[  ]+$/, '')).join('\n');
  }

  // -- rows to DOM text --------------------------------------------------------
  //
  // One visual row becomes the inner HTML of a ``span.r`` (an inline-block as
  // wide as the pane, ``white-space: pre``). What the platform copies is that
  // HTML's text, so it must be exactly the row's text: no cursor, no padding.
  //
  // - Trailing blanks of a row that ends its logical line are not text: plain
  //   ones are left out, styled ones (a background, an inverse status bar)
  //   become an empty box ``span.p`` as wide as they are.
  // - A row that wraps into the next keeps every cell as text (its spaces are
  //   part of the line).
  // - A wide character is one character in a 2ch box (``span.w``); a character
  //   from U+2000 on (box drawing, symbols, Nerd Font icons, emoji) is in a 1ch
  //   box (``span.n``) so a fallback font cannot shift the columns after it.
  //
  // Classes: f0..f15 / g0..g15 the 16 theme colours (fg / bg), fi / gi the
  // default colours swapped by an inverse, B bold, I italic, D dim, H hidden,
  // S strike, O overline, U1..U5 the underline styles (single, double, curly,
  // dotted, dashed). 256 and true colours are inline styles.

  const CM_DEFAULT = 0, CM_P16 = 0x1000000, CM_RGB = 0x3000000;

  const P256 = (function () {
    const out = new Array(256);
    const v = [0, 95, 135, 175, 215, 255];
    const hx = (n) => n.toString(16).padStart(2, '0');
    for (let i = 0; i < 216; i++) {
      out[16 + i] = '#' + hx(v[Math.floor(i / 36)]) + hx(v[Math.floor(i / 6) % 6]) + hx(v[i % 6]);
    }
    for (let i = 0; i < 24; i++) { const c = 8 + i * 10; out[232 + i] = '#' + hx(c) + hx(c) + hx(c); }
    return out;
  })();

  function esc(s) {
    return s.replace(/[&<>]/g, (c) => (c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;'));
  }
  function attrEsc(s) { return esc(s).replace(/"/g, '&quot;'); }

  // A colour as a theme index (0..15) or a CSS colour, null for the default.
  function colour(mode, value) {
    if (mode === CM_DEFAULT) return null;
    if (mode === CM_RGB) return { css: '#' + value.toString(16).padStart(6, '0') };
    if (mode === CM_P16 || value < 16) return { n: value };
    return { css: P256[value] };
  }

  // The class list and inline style of one cell.
  function cellStyle(cell) {
    let fg = colour(cell.getFgColorMode(), cell.getFgColor());
    const bg = colour(cell.getBgColorMode(), cell.getBgColor());
    const bold = cell.isBold();
    // Bold text in the 8 base colours is drawn bright, as the classic terminal
    // does (xterm.js drawBoldTextInBrightColors).
    if (bold && fg && fg.n != null && fg.n < 8) fg = { n: fg.n + 8 };
    let c = '';
    let s = '';
    if (cell.isInverse()) {
      c += fg ? (fg.n != null ? ' g' + fg.n : '') : ' gi';
      if (fg && fg.css) s += 'background:' + fg.css + ';';
      c += bg ? (bg.n != null ? ' f' + bg.n : '') : ' fi';
      if (bg && bg.css) s += 'color:' + bg.css + ';';
    } else {
      if (fg) { if (fg.n != null) c += ' f' + fg.n; else s += 'color:' + fg.css + ';'; }
      if (bg) { if (bg.n != null) c += ' g' + bg.n; else s += 'background:' + bg.css + ';'; }
    }
    if (bold) c += ' B';
    if (cell.isItalic()) c += ' I';
    if (cell.isDim()) c += ' D';
    if (cell.isInvisible()) c += ' H';
    if (cell.isStrikethrough()) c += ' S';
    if (cell.isOverline && cell.isOverline()) c += ' O';
    if (cell.isUnderline()) {
      const ext = cell.extended;
      const style = ext && ext.underlineStyle ? ext.underlineStyle : 1;
      c += ' U' + Math.min(Math.max(style, 1), 5);
      if (cell.isUnderlineColorDefault && !cell.isUnderlineColorDefault()) {
        const u = colour(cell.getUnderlineColorMode(), cell.getUnderlineColor());
        if (u) s += 'text-decoration-color:' + (u.css || 'var(--nt-c' + u.n + ')') + ';';
      }
    }
    return { cls: c.trim(), style: s };
  }

  function blank(cell) {
    const ch = cell.getChars();
    return ch === '' || ch === ' ';
  }

  // Whether a blank cell shows anything (a background or an inverse).
  function painted(cell) {
    return cell.getBgColorMode() !== CM_DEFAULT || cell.isInverse();
  }

  function wrapChar(ch, width) {
    if (width === 2) return '<span class="w">' + ch + '</span>';
    if (ch.codePointAt(0) >= 0x2000) return '<span class="n">' + ch + '</span>';
    return ch;
  }

  // The inner HTML of one row. ``line``: an xterm.js buffer line; ``cols``:
  // the pane's width; ``opts.wraps``: the row continues on the next one;
  // ``opts.links``: [[fromCol, toCol, href], ...], sorted, not overlapping.
  // ``scratch``: an optional cell object reused across calls.
  function rowHtml(line, cols, opts, scratch) {
    const o = opts || {};
    const cell = line.getCell(0, scratch);
    // The end of the row's text: after the last non-blank cell, unless the
    // row wraps (every cell is then part of the line).
    let textEnd = cols;
    if (!o.wraps) {
      while (textEnd > 0) {
        line.getCell(textEnd - 1, cell);
        if (cell.getWidth() === 0 || !blank(cell)) break;
        textEnd--;
      }
    }
    // Styled trailing blanks become empty boxes, up to the last painted one.
    let paintEnd = textEnd;
    for (let x = cols - 1; x >= textEnd; x--) {
      line.getCell(x, cell);
      if (painted(cell)) { paintEnd = x + 1; break; }
    }
    const links = o.links || null;
    let out = '';
    let run = '';
    let key = null;
    let cls = '';
    let style = '';
    let li = 0;
    let inLink = false;
    const flush = () => {
      if (run === '') return;
      out += cls || style
        ? '<span' + (cls ? ' class="' + cls + '"' : '') + (style ? ' style="' + style + '"' : '') + '>' + run + '</span>'
        : run;
      run = '';
    };
    for (let x = 0; x < textEnd; x++) {
      line.getCell(x, cell);
      const w = cell.getWidth();
      if (w === 0) continue;
      if (links && li < links.length) {
        if (inLink && x >= links[li][1]) { flush(); out += '</a>'; inLink = false; li++; }
        if (!inLink && li < links.length && x >= links[li][0] && x < links[li][1]) {
          flush();
          out += '<a class="nt-link" draggable="false" href="' + attrEsc(links[li][2])
            + '" target="_blank" rel="noopener noreferrer">';
          inLink = true;
        }
      }
      const st = cellStyle(cell);
      const k = st.cls + '|' + st.style;
      if (k !== key) { flush(); key = k; cls = st.cls; style = st.style; }
      run += wrapChar(esc(cell.getChars() || ' '), w);
    }
    flush();
    if (inLink) out += '</a>';
    // Painted trailing blanks: boxes with no text, one per style run.
    let x = textEnd;
    while (x < paintEnd) {
      line.getCell(x, cell);
      const st = cellStyle(cell);
      const k = st.cls + '|' + st.style;
      let n = 0;
      while (x < paintEnd) {
        line.getCell(x, cell);
        const s2 = cellStyle(cell);
        if (s2.cls + '|' + s2.style !== k) break;
        const w = Math.max(cell.getWidth(), 1);
        n += w;
        x += w;
      }
      out += '<span class="p' + (st.cls ? ' ' + st.cls : '') + '" style="width:' + n + 'ch;' + st.style + '"></span>';
    }
    return out;
  }

  // One string per row in which column x is character x (a wide or combined
  // character becomes U+0001, its second column too), for finding links.
  function rowColumns(line, cols, scratch) {
    const cell = line.getCell(0, scratch);
    let s = '';
    for (let x = 0; x < cols; x++) {
      line.getCell(x, cell);
      const ch = cell.getChars();
      if (cell.getWidth() !== 1) s += '\u0001';
      else s += ch.length === 1 ? ch : (ch === '' ? ' ' : '\u0001');
    }
    return s;
  }

  // The classic terminal's WebLinksAddon pattern (U+0001 marks a column no
  // URL can cross).
  const URL_RE = /https?:[/]{2}[^\s"'!*(){}|\\^<>`\u0001]*[^\s"':,.!?{}|\\^~[\]`()<>\u0001]/g;

  // Links of a logical line: ``texts`` are its rows' column strings
  // (rowColumns), ``prefix`` the text of its rows already drawn elsewhere (so
  // a URL crossing that boundary is found whole). Per row: [[from, to, href]]
  // or null when the row has none.
  function findLinks(texts, prefix) {
    const pre = prefix || '';
    const text = pre + texts.join('');
    const per = texts.map(() => null);
    if (text.indexOf('http') < 0) return per;
    const starts = [];
    let at = pre.length;
    for (const t of texts) { starts.push(at); at += t.length; }
    URL_RE.lastIndex = 0;
    let m;
    while ((m = URL_RE.exec(text))) {
      const from = m.index, to = m.index + m[0].length;
      for (let r = 0; r < texts.length; r++) {
        const a = Math.max(from, starts[r]), b = Math.min(to, starts[r] + texts[r].length);
        if (a < b) (per[r] || (per[r] = [])).push([a - starts[r], b - starts[r], m[0]]);
      }
    }
    return per;
  }

  // Where an address attached to text leads: a web address as it is, a file of
  // this machine in the Files page, anything else nowhere. A file on a named
  // host is not one of ours (a shell over ssh).
  function linkTarget(uri) {
    if (/^https?:/i.test(uri)) return uri;
    if (!/^file:/i.test(uri)) return null;
    try {
      const u = new URL(uri);
      return !u.hostname || u.hostname === 'localhost' ? '/files' + u.pathname : null;
    } catch (e) {
      return null;
    }
  }

  // The addresses a program attached to a row's text (OSC 8), whatever the
  // text says: [[from, to, href]], for those that lead somewhere (linkTarget).
  // ``term``: the decoder the row came from, before its next capture.
  function attachedLinks(line, cols, term, scratch) {
    const service = term._core._oscLinkService;
    const cell = line.getCell(0, scratch);
    const out = [];
    let last = null;
    for (let x = 0; x < cols; x++) {
      line.getCell(x, cell);
      const w = cell.getWidth();
      if (w === 0) continue;
      const id = cell.hasExtendedAttrs() ? cell.extended.urlId : 0;
      const data = id ? service.getLinkData(id) : null;
      const uri = data ? linkTarget(data.uri) : null;
      if (uri && last && last[1] === x && last[2] === uri) last[1] = x + w;
      else if (uri) out.push(last = [x, x + w, uri]);
      else last = null;
    }
    return out;
  }

  // A row's links: the attached ones, then the addresses found in its text
  // that they do not cover. Sorted, not overlapping.
  function mergeLinks(found, attached) {
    if (!attached.length) return found;
    const free = (found || []).filter((f) => !attached.some((a) => f[0] < a[1] && a[0] < f[1]));
    return free.concat(attached).sort((a, b) => a[0] - b[0]);
  }

  // Logical lines from wrap flags: ``wrapped[i]`` is '1' when row i continues
  // on row i + 1. Returns [[firstRow, endRow), ...].
  function groupRows(wrapped, count) {
    const groups = [];
    let start = 0;
    for (let i = 0; i < count; i++) {
      if (i === count - 1 || wrapped[i] !== '1') {
        groups.push([start, i + 1]);
        start = i + 1;
      }
    }
    return groups;
  }

  // The inner HTML of rows decoded from one capture, with the links of each
  // logical line. ``cont``: the first row continues a line drawn before, whose
  // column text is ``prefix``. ``term``: the decoder the rows came from, for the
  // links programs attached to their text. Returns {html: [...], texts: [...]}.
  function rowsHtml(lines, cols, wrapped, cont, prefix, scratch, term) {
    const texts = lines.map((l) => rowColumns(l, cols, scratch));
    const html = new Array(lines.length);
    for (const [a, b] of groupRows(wrapped, lines.length)) {
      const links = findLinks(texts.slice(a, b), a === 0 && cont ? prefix : '');
      for (let i = a; i < b; i++) {
        const rowLinks = term ? mergeLinks(links[i - a], attachedLinks(lines[i], cols, term, scratch)) : links[i - a];
        html[i] = rowHtml(lines[i], cols, { wraps: wrapped[i] === '1', links: rowLinks }, scratch);
      }
    }
    return { html, texts };
  }

  // The mouse tracking mode a pane asked for, from the bridge's state
  // (mouse_all_flag, mouse_button_flag, mouse_standard_flag).
  function mouseMode(mouse) {
    if (!mouse) return 'none';
    if (mouse.all) return 'any';
    if (mouse.button) return 'drag';
    if (mouse.standard) return 'vt200';
    return 'none';
  }

  function mouseEncoding(mouse) {
    if (mouse && mouse.sgr) return 'SGR';
    if (mouse && mouse.utf8) return 'UTF8';
    return 'DEFAULT';
  }

  // Mouse report for a program that asked for the mouse. ``button``: 0 left,
  // 1 middle, 2 right, 3 release (X10 encoding), 64/65 wheel up/down; add 32
  // for motion. ``col``/``row`` are 1-based. ``encoding``: 'SGR' or 'DEFAULT'.
  // 'SGR' (mode 1006) is text. 'UTF8' (mode 1005) writes each value as one
  // character, which the socket carries as UTF-8: exactly that encoding, up
  // to 2015. 'DEFAULT' (X10) writes each value as one byte, 32 + value, so up
  // to 223; the string returned then holds bytes (char codes below 256), to
  // send as bytes (``mouseBytes``), never as text. Past an encoding's range
  // there is nothing to send: '' (xterm.js does the same).
  function encodeMouse(encoding, button, col, row, release) {
    if (encoding === 'SGR') return '\x1b[<' + button + ';' + col + ';' + row + (release ? 'm' : 'M');
    const b = release && button < 64 ? 3 + (button & ~3) : button;
    const limit = encoding === 'UTF8' ? 2015 : 223;
    if (col > limit || row > limit || col < 1 || row < 1) return '';
    return '\x1b[M' + String.fromCharCode(32 + b, 32 + col, 32 + row);
  }

  // The bytes of an X10 report, as hex for the bridge's byte input.
  function mouseBytes(report) {
    let hex = '';
    for (let i = 0; i < report.length; i++) hex += report.charCodeAt(i).toString(16).padStart(2, '0');
    return hex;
  }

  // The button code of a report: ``button`` 0 left, 1 middle, 2 right, 3 none
  // (a move with no button down), 64/65 wheel up/down; 32 for motion (a drag
  // or a move), plus the modifiers (Shift 4, Alt/Meta 8, Ctrl 16).
  function mouseCode(kind, button, mods) {
    const m = mods || {};
    let code = button;
    if (kind === 'drag' || kind === 'move') code += 32;
    if (m.shift) code += 4;
    if (m.alt || m.meta) code += 8;
    if (m.ctrl) code += 16;
    return code;
  }

  // Whether a mouse event of this kind is reported in this tracking mode
  // (xterm's term.modes.mouseTrackingMode).
  function mouseWants(mode, kind) {
    if (!mode || mode === 'none') return false;
    if (kind === 'press' || kind === 'wheel') return true;
    if (kind === 'release') return mode !== 'x10';
    if (kind === 'drag') return mode === 'drag' || mode === 'any';
    return mode === 'any';
  }

  // Lazy history: which rows to ask for next. ``oldest`` is the absolute
  // number of the oldest row the page has, ``first`` the oldest tmux still
  // holds. Null when there is nothing older.
  function nextHistoryRequest(oldest, first, page) {
    if (oldest == null || oldest <= first) return null;
    return { end: oldest, count: Math.min(page, oldest - first) };
  }

  // Physical keys: what a keydown sends. Special keys and modified keys
  // become tmux key names, so tmux encodes them for the pane's own modes
  // (application cursor, the xterm modifier forms such as ``\x1b[1;5D``) and
  // its bindings (the prefix, F2 to F5, Escape) run exactly as for a typed key
  // (measured on tmux 3.8 for every name: ``tests/unit/test_terminal_native.py``).
  const NAMED = {
    ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
    Home: 'Home', End: 'End', PageUp: 'PPage', PageDown: 'NPage',
    Insert: 'IC', Delete: 'DC', Escape: 'Escape', Tab: 'Tab',
  };
  for (let i = 1; i <= 12; i++) NAMED['F' + i] = 'F' + i;

  function oneChar(s) {
    return typeof s === 'string' && s.length > 0 && s.length <= 2 && Array.from(s).length === 1;
  }

  // ``mods``: {ctrl, alt, shift, meta, altGraph}; ``code``: the event's
  // physical key (``KeyA``), for Alt and Ctrl on keys whose character is not
  // the letter (Option on a Mac composes: Option+h is ``˙``; a Cyrillic
  // layout). Alt is Meta (ESC prefix), as in the classic terminal; Cmd/Win
  // combinations and AltGr characters stay the browser's (null). Returns
  // {key: <tmux name>} or {data: <bytes as text>} or null.
  function keyAction(key, mods, code) {
    const m = mods || {};
    if (m.meta || m.altGraph || typeof key !== 'string') return null;
    const ctrl = !!m.ctrl, alt = !!m.alt, shift = !!m.shift;
    const prefix = (ctrl ? 'C-' : '') + (alt ? 'M-' : '');
    if (NAMED[key]) {
      if (key === 'Tab' && shift && !ctrl && !alt) return { key: 'BTab' };
      // tmux has no Ctrl+Escape (it would type the name): Ctrl is dropped.
      if (key === 'Escape') return { key: (alt ? 'M-' : '') + (shift ? 'S-' : '') + 'Escape' };
      return { key: prefix + (shift ? 'S-' : '') + NAMED[key] };
    }
    if (key === 'Enter') return ctrl || alt ? { key: prefix + 'Enter' } : { data: '\r' };
    if (key === 'Backspace') {
      // tmux has no Ctrl+BSpace either: ^H, as xterm sends it.
      if (ctrl) return { data: alt ? '\x1b\x08' : '\x08' };
      return alt ? { key: 'M-BSpace' } : { data: '\x7f' };
    }
    if (!(ctrl || alt)) return null;
    let ch = key;
    const letter = code ? /^Key([A-Z])$/.exec(code) : null;
    const digit = code ? /^Digit([0-9])$/.exec(code) : null;
    // From the key's code before anything else: Option on a Mac composes a
    // character or starts a dead key (Option+e is ``Dead``), and Alt is Meta.
    if (alt && !ctrl && letter) ch = shift ? letter[1] : letter[1].toLowerCase();
    else if (alt && !ctrl && digit && !shift) ch = digit[1];
    if (ctrl && !/^[\x21-\x7e ]$/.test(ch) && letter) ch = letter[1];
    if (!oneChar(ch)) return null;
    if (ch === ' ') return { key: prefix + 'Space' };
    // A letter with Ctrl is case-insensitive for a terminal.
    if (ctrl && /^[A-Za-z]$/.test(ch)) ch = ch.toLowerCase();
    // Nor Ctrl+# (typed as its name too): the character, with Alt if any.
    if (ctrl && ch === '#') return alt ? { key: 'M-#' } : { data: '#' };
    return { key: prefix + ch };
  }

  // A keydown in the text field: only what the field cannot carry itself.
  // Printable text, plain Enter and plain Backspace go through the field
  // (beforeinput / input), which is what the iOS keyboard needs: its held
  // delete accelerates from the field's own content.
  function fieldKeyAction(key, mods, code) {
    const m = mods || {};
    if ((key === 'Enter' || key === 'Backspace') && !m.ctrl && !m.alt) return null;
    return keyAction(key, mods, code);
  }

  // A keydown with no text field focused (after a mouse selection took the
  // focus): every key, printable text included.
  function documentKeyAction(key, mods, code) {
    const m = mods || {};
    // An AltGr character (``@`` on many layouts) is text.
    if (m.altGraph) return !m.meta && oneChar(key) ? { data: typedText(key) } : null;
    const a = keyAction(key, m, code);
    if (a || m.meta || m.ctrl || m.alt) return a;
    return oneChar(key) ? { data: typedText(key) } : null;
  }

  // Typed text as the terminal gets it. iOS smart punctuation curls quotes
  // and makes dashes, which breaks shell commands (the founder's test on the
  // iPhone); a no-break space (the sentinel's, or iOS's own around French
  // punctuation) is a space; a line break is Enter. Every mapping is per
  // character, so a prefix of the field maps to a prefix of what was sent.
  const TYPED = {
    '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'", '\u2032': "'",
    '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"', '\u2033': '"',
    '\u2014': '--', '\u2013': '-', '\u2026': '...',
    '\u00a0': ' ', '\u202f': ' ', '\u2007': ' ', '\n': '\r',
  };
  const TYPED_RE = /[\u2018\u2019\u201a\u201b\u2032\u201c\u201d\u201e\u201f\u2033\u2014\u2013\u2026\u00a0\u202f\u2007\n]/g;
  function typedText(s) {
    return s.replace(/\r\n/g, '\n').replace(TYPED_RE, (c) => TYPED[c]);
  }

  // The text field's diff model (the founder's iPhone test, variant C). The
  // field holds ``sentinel + typed + sentinel``, with the caret after
  // ``typed``: what was typed since the last reset. iOS edits that text in
  // place (dictation streams a hypothesis and rewrites it, autocorrect and
  // predictions replace a word, held delete removes characters then words),
  // so each change is compared with what the terminal already got (``sent``):
  // DEL back to the first character that differs, then the new tail. Deleting
  // into the leading sentinel deletes in the terminal too (held backspace on
  // an empty field), and the field is then reset. Returns {dels, text, typed,
  // sent, reset}; counts are in characters (code points), as a line editor
  // deletes them.
  function fieldDiff(sent, value, sentinel) {
    if (!value.endsWith(sentinel)) return { dels: 0, text: '', typed: '', sent: '', reset: true };
    const head = value.slice(0, value.length - sentinel.length);
    const before = Array.from(typedText(sentinel) + sent);
    const after = Array.from(typedText(head));
    let p = 0;
    const max = Math.min(before.length, after.length);
    while (p < max && before[p] === after[p]) p++;
    const out = { dels: before.length - p, text: after.slice(p).join(''), typed: '', sent: '', reset: false };
    if (!head.startsWith(sentinel)) { out.reset = true; return out; }
    out.typed = head.slice(sentinel.length);
    out.sent = typedText(out.typed);
    return out;
  }

  // The bytes for a diff: DELs then the new text, in one message.
  function diffBytes(d) {
    return '\x7f'.repeat(d.dels) + d.text;
  }

  // Edits the field must not apply (the diff model would misread them), and
  // what they send instead. ``typed``: the field's typed text (a word deleted
  // with nothing typed is the terminal's word: Ctrl+W).
  function inputAction(inputType, data, typed) {
    switch (inputType) {
      case 'insertLineBreak':
      case 'insertParagraph':
        return { data: '\r' };
      case 'deleteWordBackward':
        return typed ? null : { data: '\x17' };
      case 'deleteContentForward':
      case 'deleteWordForward':
        return { key: 'DC' };
      case 'deleteSoftLineBackward':
      case 'deleteHardLineBackward':
        return { data: '\x15' };
      case 'insertFromPaste':
      case 'insertFromDrop':
      case 'insertFromPasteAsQuotation':
        return data ? { paste: data } : { none: true };
      case 'historyUndo':
      case 'historyRedo':
        return { none: true };
      default:
        return null;
    }
  }

  // The toolbar's sticky Ctrl or Alt on the next thing typed on a software
  // keyboard: a character, Enter or delete become that key with the
  // modifiers. Null when the modifiers do not apply.
  function stickyAction(inputType, data, mods) {
    if (!mods || !(mods.ctrl || mods.alt)) return null;
    const m = { ctrl: !!mods.ctrl, alt: !!mods.alt };
    if (inputType === 'insertText' && oneChar(data)) return keyAction(typedText(data), m, null);
    if (inputType === 'insertLineBreak' || inputType === 'insertParagraph') return keyAction('Enter', m, null);
    if (inputType === 'deleteContentBackward') return keyAction('Backspace', m, null);
    return null;
  }

  // A caret move in the field (the iOS space-bar trackpad): arrow keys, one
  // per character the terminal's cursor moves. ``before`` and ``after``: the
  // field's text before the caret, where it was and where it is; counted as
  // the terminal got them (an ellipsis is three characters there).
  function caretMove(before, after) {
    return Array.from(typedText(after)).length - Array.from(typedText(before)).length;
  }

  function caretAction(delta) {
    if (!delta) return [];
    return new Array(Math.abs(delta)).fill(delta < 0 ? 'Left' : 'Right');
  }

  // What a composition commits after an Option dead key the page already
  // sent as Meta (Option+e is M-e): the browser may still compose, giving the
  // accent alone or on the next letter (``´``, ``´x``, ``á``). The accent is
  // the dead key's, already sent; the rest is what was typed after it. The
  // accent alone is what a Space after a dead key commits: that Space.
  const DEAD_ACCENTS = '\u00b4`\u02c6^\u02dc~\u00a8\u02da\u00b8';
  function afterDeadKey(text) {
    const chars = Array.from(text || '');
    if (!chars.length) return '';
    if (chars.length === 1 && DEAD_ACCENTS.includes(chars[0])) return ' ';
    if (DEAD_ACCENTS.includes(chars[0])) return chars.slice(1).join('');
    chars[0] = chars[0].normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    return chars.join('');
  }

  return {
    mouseBytes, mouseCode,
    FRAME_UPDATE, FRAME_HISTORY, parseFrame, splitRows, newDecoder, decodeRows, joinSelection, encodeMouse,
    mouseWants, nextHistoryRequest, keyAction, fieldKeyAction, documentKeyAction, inputAction, caretAction,
    typedText, fieldDiff, diffBytes, stickyAction, caretMove, afterDeadKey,
    P256, rowHtml, rowColumns, findLinks, groupRows, rowsHtml, mouseMode, mouseEncoding,
  };
});
