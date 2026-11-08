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

  // One capture of tmux's grid decoded by a scratch xterm.js parser (never
  // opened) as one continuous stream, as tmux wrote it: attributes and the
  // shift state carry from row to row. Returns the buffer lines of the rows
  // after the first ``skip``, valid until the next call. The parser must be as
  // wide as the pane. capture-pane -e writes line-drawing cells with SO, hence
  // G1 = DEC graphics.
  const G1_GRAPHICS = [0x1b, 0x29, 0x30];
  function decodeRows(term, rows, skip) {
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

  // Mouse report for a program that asked for the mouse. ``button``: 0 left,
  // 1 middle, 2 right, 3 release (X10 encoding), 64/65 wheel up/down; add 32
  // for motion. ``col``/``row`` are 1-based. ``encoding``: 'SGR' or 'DEFAULT'.
  function encodeMouse(encoding, button, col, row, release) {
    if (encoding === 'SGR') return '\x1b[<' + button + ';' + col + ';' + row + (release ? 'm' : 'M');
    const b = release && button < 64 ? 3 : button;
    if (col > 223 || row > 223) return '';
    return '\x1b[M' + String.fromCharCode(32 + b, 32 + col, 32 + row);
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
  // number of the oldest row the page has, ``collected`` the oldest tmux still
  // holds. Null when there is nothing older.
  function nextHistoryRequest(oldest, collected, page) {
    if (oldest == null || oldest <= collected) return null;
    return { end: oldest, count: Math.min(page, oldest - collected) };
  }

  // Physical keys: what a keydown sends, or null when the text field's
  // beforeinput carries it (printable text, IME, dictation). Special keys and
  // modified keys become tmux key names, so tmux encodes them for the pane's
  // own modes (application cursor, extended keys) and its bindings (the
  // prefix, F2 to F5, Escape) run exactly as for a typed key.
  const NAMED = {
    ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
    Home: 'Home', End: 'End', PageUp: 'PPage', PageDown: 'NPage',
    Insert: 'IC', Delete: 'DC', Escape: 'Escape', Tab: 'Tab',
  };
  for (let i = 1; i <= 12; i++) NAMED['F' + i] = 'F' + i;

  // ``mods``: {ctrl, alt, shift, meta}; ``macOptionIsMeta`` as in the classic
  // terminal (Option is Meta). Cmd/Win combinations stay the browser's.
  function keyAction(key, mods) {
    const m = mods || {};
    if (m.meta) return null;
    const ctrl = !!m.ctrl, alt = !!m.alt, shift = !!m.shift;
    const prefix = (ctrl ? 'C-' : '') + (alt ? 'M-' : '');
    if (NAMED[key]) {
      if (key === 'Tab' && shift && !ctrl && !alt) return { key: 'BTab' };
      return { key: prefix + (shift ? 'S-' : '') + NAMED[key] };
    }
    if (key === 'Enter') return ctrl || alt ? { key: prefix + 'Enter' } : { data: '\r' };
    if (key === 'Backspace') {
      if (alt) return { key: 'M-BSpace' };
      return { data: ctrl ? '\x08' : '\x7f' };
    }
    if (key.length === 1 && (ctrl || alt)) {
      // A letter with Ctrl is case-insensitive for a terminal.
      const k = /[A-Za-z]/.test(key) && ctrl ? key.toLowerCase() : key;
      if (key === ' ') return { key: prefix + 'Space' };
      return { key: prefix + k };
    }
    return null;
  }

  // The sentinel text field (touch keyboards): what a beforeinput event
  // sends. ``data``: the event's data (or the dropped/pasted text).
  function inputAction(inputType, data) {
    switch (inputType) {
      case 'insertText':
      case 'insertReplacementText':
      case 'insertFromDrop':
      case 'insertFromYank':
        return data ? { data: data.replace(/\r?\n/g, '\r') } : null;
      case 'insertFromPaste':
        return data ? { paste: data } : null;
      case 'insertLineBreak':
      case 'insertParagraph':
        return { data: '\r' };
      case 'deleteContentBackward':
        return { data: '\x7f' };
      case 'deleteWordBackward':
        return { data: '\x17' };
      case 'deleteContentForward':
        return { key: 'DC' };
      case 'deleteSoftLineBackward':
      case 'deleteHardLineBackward':
        return { data: '\x15' };
      default:
        return null;
    }
  }

  // A caret move in the pristine sentinel field (the iOS space-bar
  // trackpad): arrow keys, one per character moved.
  function caretAction(delta) {
    if (!delta) return [];
    return new Array(Math.min(Math.abs(delta), 200)).fill(delta < 0 ? 'Left' : 'Right');
  }

  return {
    FRAME_UPDATE, FRAME_HISTORY, parseFrame, splitRows, decodeRows, joinSelection, encodeMouse,
    mouseWants, nextHistoryRequest, keyAction, inputAction, caretAction,
  };
});
