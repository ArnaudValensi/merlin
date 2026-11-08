// Native terminal engine: one WebSocket to /ws/terminal2 (terminal/native.py),
// tmux's grid of the displayed pane decoded by a scratch xterm.js parser
// (never opened) and drawn by MerlinNativeRenderer.
//
// The page around it (status bar, toolbar, panels, input field) talks to it
// through its methods and callbacks only.
(function (root) {
  'use strict';
  const Core = root.MerlinNativeCore;

  const HISTORY_PAGE = 2000;
  const RESIZE_DELAY = 120;
  // Ask for older rows when the view is this many screens from the rows the
  // page has.
  const HISTORY_AHEAD = 3;
  const DRAIN_BUDGET = 12;

  class NativeTerminal {
    // opts: { container, wsUrl: () => string, onOpen(ws), onClose(event),
    //         onControl(msg), onClipboard(text), onStuck(stuck), onUpdate(),
    //         onPane(pane): the displayed pane changed (another window, or
    //         another pane of the window) }
    constructor(opts) {
      this.opts = opts;
      this.renderer = new root.MerlinNativeRenderer(opts.container);
      this.scroller = this.renderer.scroller;
      this.alt = this.renderer.alt;
      this.renderer.onStuck = (stuck) => { if (opts.onStuck) opts.onStuck(stuck); };
      this.renderer.onScrolled = () => this.maybeLoadHistory();
      this.scroller.addEventListener('scroll', () => {
        const s = this.scroller;
        if (s.clientHeight === this.renderer.viewH) this.viewBottom = s.scrollTop + s.clientHeight;
      }, { passive: true });
      this.ws = null;
      this.pane = '';
      this.state = null; // the last update's pane state
      this.generation = null;
      this.loading = false;
      this.queue = [];
      this.scheduled = false;
      this.handledBytes = 0;
      this.ackedBytes = 0;
      this.ackTimer = null;
      this.size = { cols: 0, rows: 0 };
      this.resizeTimer = null;
      // A software keyboard is up (set by the page): it shortens the view, not
      // the window (see fit), and fullRows is the height the window keeps.
      this.keyboard = false;
      this.fullRows = 0;
      this.viewBottom = 0;
      this.decoder = null;
      this.cell = null;
      this.renderer.measure();
      document.addEventListener('copy', (e) => this.onCopy(e));
      this.bindAltMouse();
      if (typeof ResizeObserver === 'function') {
        new ResizeObserver(() => {
          // Whatever was at the bottom of the view stays there when its
          // height changes (the keyboard opening or closing): the last rows
          // when it follows the output, else the row the user was reading.
          // (From where the view last was: a taller view has been clamped by
          // now.)
          const h = this.scroller.clientHeight;
          if (this.renderer.stuck) this.toBottom();
          else if (this.renderer.viewH && h !== this.renderer.viewH) {
            this.renderer.moveTo(Math.min(this.viewBottom, this.renderer.followTop() + h) - h);
          }
          this.renderer.viewH = h;
          this.viewBottom = this.scroller.scrollTop + h;
          this.fit();
        }).observe(this.scroller);
      }
      // The terminal font, loaded now (a web font is only fetched once text
      // uses it, and document.fonts.ready does not wait for that), and the
      // cell measured again whenever a font finishes loading.
      this.fontReady = Promise.resolve();
      if (document.fonts && document.fonts.load) {
        const family = getComputedStyle(this.renderer.host).fontFamily;
        const size = getComputedStyle(this.renderer.host).fontSize;
        this.fontReady = Promise.all([
          document.fonts.load(size + ' ' + family),
          document.fonts.load('bold ' + size + ' ' + family),
        ]).catch(() => null).then(() => { this.renderer.measure(); });
        document.fonts.addEventListener('loadingdone', () => { this.renderer.measure(); this.fit(); });
      }
    }

    // -- connection --------------------------------------------------------------

    connect() {
      if (this.ws) { try { this.ws.close(); } catch (e) { /* closing anyway */ } }
      const ws = new WebSocket(this.opts.wsUrl());
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      this.queue = [];
      this.handledBytes = 0;
      this.ackedBytes = 0;
      this.pane = '';
      this.loading = false;
      ws.onopen = () => {
        if (this.ws !== ws) return;
        this.size = { cols: 0, rows: 0 };
        // The first size once the font is there: a size measured with a
        // fallback font would reflow the pane twice.
        this.fontReady.then(() => {
          if (this.ws !== ws) return;
          this.renderer.measure();
          this.fit(true);
        });
        if (this.opts.onOpen) this.opts.onOpen(ws);
      };
      ws.onmessage = (e) => {
        if (this.ws !== ws) return;
        if (typeof e.data === 'string') {
          // Control frames are handled at once (session, windows, clipboard,
          // pong): the flow-control window counts them too.
          this.handled(e.data.length, false);
          if (e.data.charCodeAt(0) !== 0) return;
          let msg;
          try { msg = JSON.parse(e.data.slice(1)); } catch (x) { return; }
          if (msg.type === 'clipboard') { if (this.opts.onClipboard) this.opts.onClipboard(msg.text || ''); return; }
          if (this.opts.onControl) this.opts.onControl(msg);
          return;
        }
        // Grid frames are drawn on the next animation frame (within a time
        // budget, the rest on the following ones) and acknowledged once
        // drawn: a hidden page draws nothing and acknowledges nothing, so the
        // bridge stops sending (pending history stays in tmux).
        this.queue.push(e.data);
        this.schedule();
      };
      ws.onclose = (e) => {
        if (this.ws !== ws) return;
        if (this.opts.onClose) this.opts.onClose(e);
      };
      return ws;
    }

    send(msg) {
      if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
      return false;
    }

    schedule() {
      if (this.scheduled) return;
      this.scheduled = true;
      requestAnimationFrame(() => { this.scheduled = false; this.drain(); });
    }

    drain() {
      // A time budget per animation frame: what does not fit waits for the
      // next one (and is acknowledged then), so a flood never stacks several
      // large frames into one long task.
      const t0 = performance.now();
      let drawn = false;
      while (this.queue.length) {
        if (drawn && performance.now() - t0 > DRAIN_BUDGET) { this.schedule(); break; }
        const data = this.queue.shift();
        try {
          const frame = Core.parseFrame(data);
          if (frame && frame.kind === Core.FRAME_UPDATE) { this.onUpdate(frame); this.updatedSince = true; }
          else if (frame && frame.kind === Core.FRAME_HISTORY) this.onHistory(frame);
        } catch (err) {
          // A frame we cannot draw: start the pane afresh rather than show a
          // wrong grid.
          if (root.console) console.error('[native terminal]', err);
          this.send({ type: 'resync' });
        }
        this.handled(data.byteLength, true);
        drawn = true;
      }
      if (this.updatedSince && this.opts.onUpdate) { this.updatedSince = false; this.opts.onUpdate(); }
      this.maybeLoadHistory();
    }

    handled(size, now) {
      this.handledBytes += size;
      if (now) { this.ack(); return; }
      if (!this.ackTimer) this.ackTimer = setTimeout(() => { this.ackTimer = null; this.ack(); }, 50);
    }

    ack() {
      if (this.handledBytes === this.ackedBytes) return;
      if (this.send({ type: 'ack', bytes: this.handledBytes })) this.ackedBytes = this.handledBytes;
    }

    // -- frames ------------------------------------------------------------------

    scratch(cols) {
      if (!this.decoder || this.decoder.cols !== cols) {
        if (this.decoder) this.decoder.dispose();
        this.decoder = Core.newDecoder(root.Terminal, root.Unicode11Addon.Unicode11Addon, cols);
        this.cell = null;
      }
      return this.decoder;
    }

    decode(cols, rows, skip, wrapped, cont, prefix) {
      const lines = Core.decodeRows(this.scratch(cols), rows, skip);
      if (!this.cell && lines.length) this.cell = lines[0].getCell(0);
      const out = Core.rowsHtml(lines, cols, wrapped, cont, prefix, this.cell);
      out.lines = lines;
      return out;
    }

    onUpdate(frame) {
      const h = frame.header;
      const r = this.renderer;
      if (h.reset || h.pane !== this.pane || h.cols !== r.cols) {
        const moved = h.pane !== this.pane;
        this.pane = h.pane;
        this.generation = h.history.generation;
        this.loading = false;
        r.reset(h.cols, h.rows);
        if (moved && this.opts.onPane) this.opts.onPane(h.pane);
      }
      r.rows = h.rows;
      // History first (the rows it confirms, in order; a gap before them for
      // rows tmux dropped), then the screen, whose first row may continue the
      // last of them: the decoder's rows are only valid until the next decode,
      // so each capture is drawn before the next is decoded.
      const hist = h.hist;
      const prefix = (n) => r.prefixFor(n);
      if (hist.count) {
        const p = prefix(hist.start);
        const out = this.decode(h.cols, frame.histRows, hist.skip, hist.wrapped, !!p, p);
        r.applyHistory(hist.start, out.html, out.texts, hist.wrapped);
      } else {
        r.applyHistory(hist.start, [], [], '');
      }
      r.setFirstHeld(h.history.first);
      const sc = h.screen;
      const top = h.history.added;
      const sp = h.alternate ? '' : prefix(top);
      const out = this.decode(h.cols, frame.screenRows, sc.skip, sc.wrapped, !!sp, sp);
      const c = h.cursor;
      let ch = ' ', wide = false;
      const line = out.lines[c.y];
      if (line) {
        const cell = line.getCell(c.x);
        if (cell) { ch = cell.getChars() || ' '; wide = cell.getWidth() === 2; }
      }
      const wasAlternate = !!r.alternate;
      r.applyScreen(top, out.html, out.texts, sc.wrapped, {
        x: c.x, y: c.y, visible: c.visible, shape: c.shape, blink: c.blink, ch, wide,
      }, h.alternate);
      this.state = h;
      if (this.keyboard && !!r.alternate !== wasAlternate) this.fit();
    }

    onHistory(frame) {
      const h = frame.header;
      const r = this.renderer;
      if (h.pane !== this.pane) return;
      this.loading = false;
      if (h.busy) {
        // The bridge had no room: ask again shortly.
        setTimeout(() => this.maybeLoadHistory(), 300);
        return;
      }
      if (h.generation != null && h.generation !== this.generation) return; // a reset follows
      if (h.end !== r.oldest || h.start >= h.end) {
        // Nothing older (tmux holds no more), or an answer for rows we no
        // longer lack.
        if (h.start >= h.end && h.end === r.oldest) {
          this.state.history.first = h.start;
          r.setFirstHeld(h.start);
        }
        return;
      }
      const out = this.decode(r.cols, frame.rows, h.skip, h.wrapped, false, '');
      r.prependHistory(h.start, out.html, out.texts, h.wrapped);
      this.maybeLoadHistory();
    }

    // -- history -------------------------------------------------------------------

    maybeLoadHistory() {
      const r = this.renderer;
      if (this.loading || !this.pane || !this.state || !r.spacerRows) return;
      const s = this.scroller;
      if (s.scrollTop > r.spacerHeight() + HISTORY_AHEAD * s.clientHeight) return;
      const req = Core.nextHistoryRequest(r.oldest, this.state.history.first, HISTORY_PAGE);
      if (req && this.send({ type: 'history', pane: this.pane, end: req.end, count: req.count })) this.loading = true;
    }

    // -- scrolling ---------------------------------------------------------------

    get stuck() { return this.renderer.stuck; }

    toBottom() { this.renderer.scrollToBottom(); }

    // -- size --------------------------------------------------------------------

    // Columns and rows that fit, sent to tmux once, RESIZE_DELAY after the
    // last change (``now``: at once). A keyboard leaves the window its height:
    // the view shows its last rows, and the program is not redrawn each time
    // the keyboard moves. A full-screen program draws for the rows it is told
    // about and cannot be scrolled, so it gets the shorter height.
    fit(now) {
      const got = this.renderer.fit();
      const cols = got.cols;
      let rows = got.rows;
      if (!this.keyboard) this.fullRows = rows;
      else if (!this.renderer.alternate && this.fullRows) rows = this.fullRows;
      if (cols === this.size.cols && rows === this.size.rows) return;
      this.size = { cols, rows };
      clearTimeout(this.resizeTimer);
      const go = () => this.send({ type: 'resize', cols, rows });
      if (now) go(); else this.resizeTimer = setTimeout(go, RESIZE_DELAY);
    }

    // -- input ---------------------------------------------------------------------

    input(data) {
      if (!data || !this.pane) return;
      this.send({ type: 'input', pane: this.pane, data });
      this.toBottom();
    }

    key(name) {
      if (!this.pane) return;
      this.send({ type: 'key', pane: this.pane, key: name });
      this.toBottom();
    }

    paste(text) {
      if (!text || !this.pane) return;
      this.send({ type: 'paste', pane: this.pane, data: text });
      this.toBottom();
    }

    // -- copy ----------------------------------------------------------------------

    // The selection's text from the rows themselves: wrapped rows joined, hard
    // lines broken once, trailing blanks trimmed, on every browser (Firefox's
    // own serialisation is empty over content-visibility chunks). iOS copies
    // the DOM's own text, which carries the same thing by construction.
    selectionText() {
      const sel = getSelection();
      if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
      const range = sel.getRangeAt(0);
      const host = this.alt.hidden ? this.renderer.content : this.alt;
      if (!range.intersectsNode(host)) return null;
      const lines = [];
      for (const l of host.querySelectorAll('.l')) {
        if (!range.intersectsNode(l)) continue;
        const part = document.createRange();
        part.selectNodeContents(l);
        if (range.compareBoundaryPoints(Range.START_TO_START, part) > 0) part.setStart(range.startContainer, range.startOffset);
        if (range.compareBoundaryPoints(Range.END_TO_END, part) < 0) part.setEnd(range.endContainer, range.endOffset);
        lines.push(part.toString());
      }
      return lines.length ? Core.joinSelection(lines) : null;
    }

    onCopy(e) {
      let text = null;
      try { text = this.selectionText(); } catch (x) { text = null; }
      if (text == null || !e.clipboardData) return;
      e.clipboardData.setData('text/plain', text);
      e.preventDefault();
    }

    // -- the mouse, in the alternate screen ---------------------------------------------

    mouse() { return this.state ? this.state.mouse : null; }

    cellAt(ev) {
      const r = this.alt.getBoundingClientRect();
      const cols = this.renderer.cols, rows = this.renderer.rows;
      const col = Math.min(cols, Math.max(1, Math.floor((ev.clientX - r.left) / this.renderer.cellW) + 1));
      const row = Math.min(rows, Math.max(1, Math.floor((ev.clientY - r.top) / this.renderer.rowH) + 1));
      return { col, row };
    }

    // Report one mouse event to a program that asked for it; false when it
    // did not ask for this kind. ``button``: 0 left, 1 middle, 2 right, 3 none,
    // 64/65 the wheel. X10 reports are bytes above 127 for columns past 95:
    // they go as bytes, not text.
    report(kind, button, ev) {
      const m = this.mouse();
      if (!Core.mouseWants(Core.mouseMode(m), kind)) return false;
      const { col, row } = this.cellAt(ev);
      const code = Core.mouseCode(kind, button, { shift: ev.shiftKey, alt: ev.altKey, meta: ev.metaKey, ctrl: ev.ctrlKey });
      const encoding = Core.mouseEncoding(m);
      const seq = Core.encodeMouse(encoding, code, col, row, kind === 'release');
      if (!seq) return true;
      if (encoding === 'DEFAULT') this.send({ type: 'input', pane: this.pane, hex: Core.mouseBytes(seq) });
      else this.send({ type: 'input', pane: this.pane, data: seq });
      return true;
    }

    // A wheel notch or a swipe step: a wheel report when the program asked for
    // the mouse, else an arrow key, as native terminals do.
    scrollStep(up, ev) {
      if (!this.report('wheel', up ? 64 : 65, ev)) this.send({ type: 'key', pane: this.pane, key: up ? 'Up' : 'Down' });
    }

    // One path for the mouse and the finger in the alternate screen: pointer
    // events. (Touch also fires compatibility mouse events after a tap; with no
    // mouse listener here, a tap is reported once.) A mouse: presses, drags,
    // releases, and motion when the program asked for it (one report per cell
    // entered); Shift keeps the browser's own selection for the whole gesture.
    // A finger: a vertical swipe is one wheel step (or arrow key) per row
    // height, a tap is a click.
    bindAltMouse() {
      const alt = this.alt;
      let g = null; // the gesture in progress
      const cellKey = (ev) => { const c = this.cellAt(ev); return c.col + ':' + c.row; };
      alt.addEventListener('pointerdown', (ev) => {
        if (g && g.id !== ev.pointerId) return; // one pointer at a time
        if (ev.pointerType === 'touch') {
          g = { id: ev.pointerId, touch: true, lastY: ev.clientY, acc: 0, moved: false };
          return;
        }
        if (ev.shiftKey) { g = { id: ev.pointerId, select: true }; return; }
        if (this.report('press', ev.button, ev)) {
          g = { id: ev.pointerId, button: ev.button, cell: cellKey(ev) };
          try { alt.setPointerCapture(ev.pointerId); } catch (e) { /* not capturable */ }
          ev.preventDefault();
        }
      });
      alt.addEventListener('pointermove', (ev) => {
        if (g && g.id !== ev.pointerId) return;
        if (g && g.touch) {
          g.acc += ev.clientY - g.lastY;
          g.lastY = ev.clientY;
          const step = Math.max(this.renderer.rowH, 12);
          // A finger moving down pulls earlier lines into view: wheel up.
          while (Math.abs(g.acc) >= step) {
            g.moved = true;
            this.scrollStep(g.acc > 0, ev);
            g.acc += g.acc > 0 ? -step : step;
          }
          return;
        }
        if (g && g.select) return;
        if (ev.pointerType === 'touch') return;
        const key = cellKey(ev);
        if (g) {
          if (key !== g.cell) { g.cell = key; this.report('drag', g.button, ev); }
        } else if (key !== this.moveCell) {
          this.moveCell = key;
          this.report('move', 3, ev);
        }
      });
      const end = (ev, cancelled) => {
        if (!g || g.id !== ev.pointerId) return;
        const done = g;
        g = null;
        if (done.touch) {
          if (!done.moved && !cancelled && this.report('press', 0, ev)) this.report('release', 0, ev);
          return;
        }
        if (done.select) return;
        this.report('release', done.button, ev);
      };
      alt.addEventListener('pointerup', (ev) => end(ev, false));
      alt.addEventListener('pointercancel', (ev) => end(ev, true));
      alt.addEventListener('wheel', (ev) => {
        ev.preventDefault();
        const steps = Math.max(1, Math.round(Math.abs(ev.deltaY) / Math.max(this.renderer.rowH, 1)));
        for (let i = 0; i < Math.min(steps, 5); i++) this.scrollStep(ev.deltaY < 0, ev);
      }, { passive: false });
    }
  }

  root.MerlinNativeTerminal = NativeTerminal;
})(typeof window !== 'undefined' ? window : globalThis);
