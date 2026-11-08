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
    //         onControl(msg), onClipboard(text), onStuck(stuck), onUpdate() }
    constructor(opts) {
      this.opts = opts;
      this.renderer = new root.MerlinNativeRenderer(opts.container);
      this.scroller = this.renderer.scroller;
      this.alt = this.renderer.alt;
      this.renderer.onStuck = (stuck) => { if (opts.onStuck) opts.onStuck(stuck); };
      this.renderer.onScrolled = () => this.maybeLoadHistory();
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
      this.decoder = null;
      this.cell = null;
      this.renderer.measure();
      document.addEventListener('copy', (e) => this.onCopy(e));
      this.bindAltMouse();
      if (typeof ResizeObserver === 'function') {
        new ResizeObserver(() => this.fit()).observe(this.scroller);
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
        this.pane = h.pane;
        this.generation = h.history.generation;
        this.loading = false;
        r.reset(h.cols, h.rows);
      }
      r.rows = h.rows;
      const hist = h.hist;
      if (hist.count) {
        const out = this.decode(h.cols, frame.histRows, hist.skip, hist.wrapped, hist.cont, r.lastLineText());
        r.appendHistory(hist.start, out.html, out.texts, hist.wrapped, hist.cont, hist.gap);
      } else if (r.oldest == null) {
        // No history yet: the first rows to come start here.
        r.oldest = hist.start;
        r.next = hist.start;
      } else if (hist.gap) {
        // Rows tmux dropped and nothing after them yet: the hole shows with
        // the next rows.
        r.pendingGap += hist.gap;
        r.next = hist.start;
      }
      r.setFirstHeld(h.history.first);
      const sc = h.screen;
      const out = this.decode(h.cols, frame.screenRows, sc.skip, sc.wrapped, sc.cont, r.lastLineText());
      const c = h.cursor;
      let ch = ' ', wide = false;
      const line = out.lines[c.y];
      if (line) {
        const cell = line.getCell(c.x);
        if (cell) { ch = cell.getChars() || ' '; wide = cell.getWidth() === 2; }
      }
      // Decoded lines are only valid until the next decode: read them first.
      r.setScreen(out.html, sc.wrapped, sc.cont, {
        x: c.x, y: c.y, visible: c.visible, shape: c.shape, blink: c.blink, ch, wide,
      }, h.alternate);
      this.state = h;
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
    // last change (``now``: at once).
    fit(now) {
      const { cols, rows } = this.renderer.fit();
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
    // did not ask for this kind.
    report(kind, button, ev) {
      const m = this.mouse();
      if (!Core.mouseWants(Core.mouseMode(m), kind)) return false;
      const { col, row } = this.cellAt(ev);
      const b = button + (kind === 'drag' ? 32 : 0) + (ev.shiftKey ? 4 : 0) + (ev.altKey ? 8 : 0) + (ev.ctrlKey ? 16 : 0);
      const seq = Core.encodeMouse(Core.mouseEncoding(m), b, col, row, kind === 'release');
      if (seq) this.send({ type: 'input', pane: this.pane, data: seq });
      return true;
    }

    // A wheel notch or a swipe step: a wheel report when the program asked for
    // the mouse, else an arrow key, as native terminals do.
    scrollStep(up, ev) {
      if (!this.report('wheel', up ? 64 : 65, ev)) this.send({ type: 'key', pane: this.pane, key: up ? 'Up' : 'Down' });
    }

    bindAltMouse() {
      const alt = this.alt;
      let down = -1;
      // Shift keeps the browser's own selection, as in xterm.
      alt.addEventListener('mousedown', (ev) => {
        if (ev.shiftKey) return;
        if (this.report('press', ev.button, ev)) { down = ev.button; ev.preventDefault(); }
      });
      alt.addEventListener('mousemove', (ev) => {
        if (down >= 0) this.report('drag', down, ev);
        else this.report('move', 3, ev);
      });
      window.addEventListener('mouseup', (ev) => {
        if (down >= 0) { this.report('release', down, ev); down = -1; }
      });
      alt.addEventListener('wheel', (ev) => {
        ev.preventDefault();
        const steps = Math.max(1, Math.round(Math.abs(ev.deltaY) / Math.max(this.renderer.rowH, 1)));
        for (let i = 0; i < Math.min(steps, 5); i++) this.scrollStep(ev.deltaY < 0, ev);
      }, { passive: false });
      // A vertical swipe: one step per row height moved. A finger moving down
      // pulls earlier lines into view: wheel up.
      let lastY = null, acc = 0, moved = false;
      alt.addEventListener('touchstart', (ev) => {
        if (ev.touches.length !== 1) { lastY = null; return; }
        lastY = ev.touches[0].clientY; acc = 0; moved = false;
      }, { passive: true });
      alt.addEventListener('touchmove', (ev) => {
        if (lastY == null) return;
        const t = ev.touches[0];
        acc += t.clientY - lastY;
        lastY = t.clientY;
        const step = Math.max(this.renderer.rowH, 12);
        while (Math.abs(acc) >= step) {
          moved = true;
          this.scrollStep(acc > 0, t);
          acc += acc > 0 ? -step : step;
        }
        if (moved) ev.preventDefault();
      }, { passive: false });
      alt.addEventListener('touchend', (ev) => {
        // A tap (no swipe) is a click for a program that asked for the mouse.
        if (lastY != null && !moved && ev.changedTouches.length) {
          const t = ev.changedTouches[0];
          if (this.report('press', 0, t)) this.report('release', 0, t);
        }
        lastY = null;
      }, { passive: true });
    }
  }

  root.MerlinNativeTerminal = NativeTerminal;
})(typeof window !== 'undefined' ? window : globalThis);
