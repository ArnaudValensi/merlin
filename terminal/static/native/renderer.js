// Native terminal renderer: tmux's grid of one pane as real DOM text in a
// native scrolling container (merlin/docs/dev/web-terminal.md, "Native
// terminal"). It receives rows as HTML (MerlinNativeCore.rowsHtml) and knows
// nothing of the socket.
//
//   host
//     .nt-scroller          overflow: auto, the platform scrolls it
//       .nt-content
//         .nt-spacer        the height of the history rows tmux holds and the
//                           page has not loaded yet (one row each)
//         .nt-hist          every row the page has, history then the screen,
//           .nt-chunk       in chunks of CHUNK logical lines (content-visibility)
//             .l            one logical line
//               .r          one visual row, an inline-block as wide as the pane
//     .nt-alt               the alternate screen, a layer over the scroller
//
// Rows never move and are never rebuilt. Each row element stands for one row
// of tmux's grid by its absolute number; the screen is just the last
// ``rows`` of them. When output scrolls, the rows that leave the screen stay
// where they are (they are history now) and new rows are appended below; a
// row's HTML is rewritten only when its content changed. A selection, whose
// ends live in text nodes, survives the output that scrolls it: the DOM
// collapses a range whose node is removed, even to be put back elsewhere, so
// not moving rows is the only way.
//
// Rows the page has no content for yet (the screen moved down by more than a
// screen while history was still on its way) are a placeholder of exactly
// their height (.nt-pending), filled in order as history arrives. Rows tmux
// dropped before the page got them become a gap marker (.nt-gap).
//
// Every visual row is exactly one row height (an integer number of pixels),
// so a spacer or a placeholder shrinks by exactly the height of the rows that
// replace it: loading rows never moves what the user looks at, and never sets
// scrollTop (which would stop a momentum scroll on iOS).
//
// A soft-wrapped line is one .l element whose rows are inline-blocks, so the
// platform's own selection copies it joined (what iOS needs: no script runs on
// its Copy).
(function (root) {
  'use strict';

  const CHUNK = 200;
  // Visual rows kept in the DOM; older chunks go back to the spacer (and can
  // be loaded again from tmux).
  const MAX_ROWS = 60000;

  class NativeRenderer {
    constructor(host, opts) {
      this.opts = Object.assign({ chunk: CHUNK, maxRows: MAX_ROWS }, opts || {});
      this.host = host;
      host.classList.add('nt-term');
      this.scroller = el('div', 'nt-scroller');
      this.content = el('div', 'nt-content');
      this.spacer = el('div', 'nt-spacer');
      this.hist = el('div', 'nt-hist');
      this.alt = el('div', 'nt-alt');
      this.alt.hidden = true;
      this.cursor = el('span', 'nt-cursor');
      this.cursor.setAttribute('aria-hidden', 'true');
      this.content.append(this.spacer, this.hist);
      this.scroller.appendChild(this.content);
      host.append(this.scroller, this.alt);
      this.rowH = 14;
      this.cellW = 7;
      this.cols = 80;
      this.rows = 24;
      this.onStuck = null;
      // Whether the view follows new output. Only the user changes it: content
      // growing under the view does not, so output that arrives while the user
      // reads history leaves the view where it is.
      this.stuck = true;
      this.touching = false;
      this.ourTop = -1;
      this.scroller.addEventListener('scroll', () => this.onScroll(), { passive: true });
      this.scroller.addEventListener('touchstart', () => { this.touching = true; }, { passive: true });
      const release = () => {
        this.touching = false;
        // A fling keeps scrolling after the finger lifts: its scroll events
        // decide. Only a release at the bottom sticks at once.
        if (this.atBottom()) this.setStuck(true);
      };
      this.scroller.addEventListener('touchend', release, { passive: true });
      this.scroller.addEventListener('touchcancel', release, { passive: true });
      this.scroller.addEventListener('wheel', (e) => {
        if (e.deltaY < 0) this.setStuck(false);
      }, { passive: true });
      this.reset(80, 24);
    }

    // -- metrics -----------------------------------------------------------------

    // The cell size of the current font: the width from a run of text, the row
    // height an integer (rows must add up exactly, see the spacer).
    measure() {
      const probe = el('span', 'r nt-probe');
      probe.textContent = 'W'.repeat(100);
      this.content.appendChild(probe);
      const r = probe.getBoundingClientRect();
      probe.remove();
      const fs = parseFloat(getComputedStyle(this.host).fontSize) || 11;
      this.rowH = Math.ceil(fs * 1.2);
      this.host.style.setProperty('--nt-row-h', this.rowH + 'px');
      if (r.width > 0) this.cellW = r.width / 100;
      this.spacer.style.height = this.spacerRows * this.rowH + 'px';
      for (const p of this.pendings) p.el.style.height = (p.b - p.a) * this.rowH + 'px';
      for (const c of this.hist.children) this.sizeChunk(c);
      this.content.style.minWidth = Math.ceil(this.cols * this.cellW) + 'px';
      return { w: this.cellW, h: this.rowH };
    }

    // Columns and rows that fit the scroller (FitAddon's behaviour).
    fit() {
      const w = this.scroller.clientWidth;
      const h = this.scroller.clientHeight;
      return {
        cols: Math.max(2, Math.floor(w / this.cellW)),
        rows: Math.max(1, Math.floor(h / this.rowH)),
      };
    }

    // -- state -------------------------------------------------------------------

    // Start a pane afresh at ``cols`` x ``rows``.
    reset(cols, rows) {
      this.cols = cols;
      this.rows = rows;
      this.hist.textContent = '';
      this.alt.textContent = '';
      this.alt.hidden = true;
      this.content.style.minWidth = Math.ceil(cols * this.cellW) + 'px';
      this.tail = null; // the chunk new rows go to (null: start one)
      this.tailLines = 0;
      this.lastEl = null; // the last row in DOM order
      this.endBreak = false; // a placeholder or a gap after lastEl
      this.rowCount = 0; // row elements in the DOM
      this.oldest = null; // absolute number of the first row the page has
      this.next = null; // the first row not confirmed as history yet
      // Rows [next, next + live.length): the unconfirmed history rows and the
      // screen; null for a row in a placeholder.
      this.live = [];
      this.lastConfirmed = null; // the row element of next - 1
      this.pendings = []; // placeholders, in order: {el, a, b} (rows [a, b))
      this.deferred = new Set(); // rows whose line boundary waits for a selection to go
      this.screenTop = null;
      this.firstHeld = null; // the oldest row tmux holds
      this.spacerRows = 0;
      this.spacer.style.height = '0px';
      this.altSig = null;
      this.altHtml = [];
      this.altEls = [];
      this.alternate = false;
      this.cursorState = null;
      this.stuck = true;
    }

    // Rows tmux holds before the oldest the page has (``first`` is the
    // oldest tmux still holds): reserved as one row height each.
    setFirstHeld(first) {
      this.firstHeld = first;
      const n = this.oldest == null ? 0 : Math.max(0, this.oldest - first);
      if (n === this.spacerRows) return;
      const removed = this.spacerRows - n;
      this.spacerRows = n;
      this.spacer.style.height = n * this.rowH + 'px';
      // tmux dropped rows the page never had: what is above the view shrank.
      if (removed > 0 && !this.stuck) this.scroller.scrollTop -= removed * this.rowH;
    }

    spacerHeight() {
      return this.spacerRows * this.rowH;
    }

    // Kept for the tests and the measurements: rows the page holds.
    get histRows() { return this.rowCount; }

    // -- rows ------------------------------------------------------------------------

    // The row element of absolute number ``n``, if the page has one.
    rowEl(n) {
      if (this.next == null) return null;
      if (n === this.next - 1) return this.lastConfirmed;
      const i = n - this.next;
      return i >= 0 && i < this.live.length ? this.live[i] : null;
    }

    // The column text of the rows before ``n`` in its logical line (for links
    // that cross into row ``n``), '' when row n - 1 does not wrap into it.
    prefixFor(n) {
      const prev = this.rowEl(n - 1);
      if (!prev || prev._w !== '1') return '';
      let out = '';
      for (const r of prev.parentNode.children) {
        out += r._t || '';
        if (r === prev) break;
      }
      return out;
    }

    newChunk(after) {
      const c = el('div', 'nt-chunk');
      if (after) after.after(c);
      else this.hist.appendChild(c);
      c._rows = 0;
      c._lines = 0;
      c._gaps = 0;
      return c;
    }

    // The height a chunk takes while it is skipped (off screen): exactly its
    // rows, gap markers and placeholder, so a chunk that changes while off
    // screen (a placeholder filling above the view) or that was never drawn
    // (lazy history) moves nothing. No ``auto``: a remembered size would be
    // stale for a chunk that changed while skipped.
    sizeChunk(c) {
      if (!c) return;
      let n = c._rows + c._gaps;
      for (const p of this.pendings) if (p.el.parentNode === c) n += p.b - p.a;
      c.style.containIntrinsicSize = '1px ' + n * this.rowH + 'px';
    }

    // Row elements parsed from ``markup`` (row spans), numbered from ``n``,
    // their content and flags from index ``from`` of the arrays.
    parseRows(markup, n, html, texts, wrapped, from) {
      const t = document.createElement('template');
      t.innerHTML = markup;
      const els = Array.from(t.content.querySelectorAll('.r'));
      tagRows(els, n, html, texts, wrapped, from);
      return els;
    }

    // Rows (``{n, html, text, w}``) as a fragment: the first ``join`` of them
    // bare (they continue a line already in the DOM), the others in logical
    // lines, at most ``maxLines`` lines. One parse for the whole batch.
    // Returns {frag, used, lines, last}.
    buildRows(rows, from, join, maxLines) {
      let html = '';
      let i = from;
      for (; i < from + join; i++) html += '<span class="r">' + rows[i].html + '</span>';
      let lines = 0;
      while (i < rows.length && lines < maxLines) {
        html += '<div class="l">';
        do { html += '<span class="r">' + rows[i].html + '</span>'; i++; } while (i < rows.length && rows[i - 1].w === '1');
        html += '</div>';
        lines++;
      }
      const t = document.createElement('template');
      t.innerHTML = html;
      const els = t.content.querySelectorAll('.r');
      for (let k = 0; k < els.length; k++) {
        const row = rows[from + k], r = els[k];
        r._n = row.n; r._h = row.html; r._t = row.text; r._w = row.w;
        this.live[row.n - this.next] = r;
      }
      return { frag: t.content, used: i - from, lines, last: els[els.length - 1] || null };
    }

    // How many rows from ``from`` continue the line of ``prev`` (it wraps).
    joining(rows, from, prev) {
      if (!prev || prev._w !== '1') return 0;
      let e = from;
      while (e < rows.length) { e++; if (rows[e - 1].w !== '1') break; }
      return e - from;
    }

    // New rows after everything, in as few insertions as chunks allow.
    appendRows(rows) {
      let i = 0;
      const prev = this.endBreak ? null : this.lastEl;
      const join = this.joining(rows, 0, prev);
      let last = null;
      if (join) {
        const b = this.buildRows(rows, 0, join, 0);
        prev.parentNode.append(b.frag);
        prev.parentNode.parentNode._rows += join;
        this.sizeChunk(prev.parentNode.parentNode);
        last = b.last;
        i = join;
      }
      while (i < rows.length) {
        if (!this.tail || this.tailLines >= this.opts.chunk) {
          if (this.tail) this.sizeChunk(this.tail);
          this.tail = this.newChunk(this.hist.lastElementChild);
          this.tailLines = 0;
        }
        const b = this.buildRows(rows, i, 0, this.opts.chunk - this.tailLines);
        this.tail.append(b.frag);
        this.tailLines += b.lines;
        this.tail._lines += b.lines;
        this.tail._rows += b.used;
        this.sizeChunk(this.tail);
        last = b.last;
        i += b.used;
      }
      this.rowCount += rows.length;
      if (last) { this.lastEl = last; this.endBreak = false; }
    }

    // Rows that fill the first placeholder from its first row, inserted before
    // it; a full chunk passes the placeholder (no text) on to a new one.
    fillRows(p, rows) {
      let i = 0;
      const prev = this.rowBefore(p.el);
      const join = this.joining(rows, 0, prev);
      let last = null;
      if (join) {
        const b = this.buildRows(rows, 0, join, 0);
        prev.parentNode.append(b.frag);
        prev.parentNode.parentNode._rows += join;
        this.sizeChunk(prev.parentNode.parentNode);
        last = b.last;
        i = join;
      }
      let c = p.el.parentNode;
      while (i < rows.length) {
        if (c._lines >= this.opts.chunk) {
          const full = c;
          c = this.newChunk(full);
          c.appendChild(p.el);
          this.sizeChunk(full);
        }
        const b = this.buildRows(rows, i, 0, this.opts.chunk - c._lines);
        c.insertBefore(b.frag, p.el);
        c._lines += b.lines;
        c._rows += b.used;
        last = b.last;
        i += b.used;
      }
      this.rowCount += rows.length;
      p.a += rows.length;
      if (p.a === p.b) {
        if (this.atEnd(p.el)) { this.lastEl = last; this.endBreak = false; }
        p.el.remove();
        this.pendings.shift();
        if (!c.firstChild) c.remove();
        else this.sizeChunk(c);
        // The row after the placeholder may continue the last one filled: the
        // two lines become one, moving only rows no selection touches.
        if (last && last._w === '1') this.settle(last);
      } else {
        p.el.style.height = (p.b - p.a) * this.rowH + 'px';
        this.sizeChunk(c);
      }
    }

    // A placeholder for rows [a, b) after everything, in a chunk of its own
    // (rows that fill it are inserted before it; what follows it goes to a
    // new chunk, never moved).
    appendPending(a, b) {
      const last = this.pendings[this.pendings.length - 1];
      if (last && last.b === a && this.endBreak && this.atEnd(last.el)) {
        last.b = b;
        last.el.style.height = (last.b - last.a) * this.rowH + 'px';
        this.sizeChunk(last.el.parentNode);
        return;
      }
      if (this.tail) this.sizeChunk(this.tail);
      const c = this.newChunk(this.hist.lastElementChild);
      const p = { el: el('div', 'nt-pending'), a, b };
      p.el.style.height = (b - a) * this.rowH + 'px';
      c.appendChild(p.el);
      this.pendings.push(p);
      this.sizeChunk(c);
      this.tail = null; // the next row starts a chunk after it
      this.endBreak = true;
    }

    // Whether ``node`` is the last thing in the DOM sequence.
    atEnd(node) {
      return node.parentNode === this.hist.lastElementChild && !node.nextElementSibling;
    }

    // Rows ``start``.. now show ``html``. A row the page has is rewritten
    // only when its content changed; rows past the end are appended (after a
    // placeholder for rows not received); rows of the first placeholder are
    // filled (history arrives in order). New rows go in batches.
    applyRows(start, html, texts, wrapped) {
      let k = 0;
      const n0 = start;
      const row = (j) => ({ n: n0 + j, html: html[j], text: texts[j], w: wrapped[j] || '0' });
      while (k < html.length) {
        const n = n0 + k;
        const i = n - this.next;
        if (i < 0) { k++; continue; }
        if (i < this.live.length && this.live[i]) {
          this.updateRow(this.live[i], html[k], texts[k], wrapped[k] || '0');
          k++;
          continue;
        }
        if (i < this.live.length) {
          // In a placeholder: only its first row can be filled (in order).
          const p = this.pendings[0];
          if (!p || p.a !== n) { k++; continue; }
          const batch = [];
          while (k < html.length && n0 + k < p.b) batch.push(row(k++));
          this.fillRows(p, batch);
          continue;
        }
        if (i > this.live.length) {
          const a = this.next + this.live.length;
          this.appendPending(a, n);
          for (let x = a; x < n; x++) this.live.push(null);
        }
        const batch = [];
        while (k < html.length) batch.push(row(k++));
        this.live.length = batch[batch.length - 1].n - this.next + 1;
        this.appendRows(batch);
      }
    }

    updateRow(r, html, text, w) {
      if (r._h !== html) {
        r.innerHTML = html;
        r._h = html;
        r._t = text;
      }
      if (r._w !== w) this.rewrap(r, w);
    }

    // The row element just before ``node`` (a placeholder or a gap marker) in
    // DOM order, or null when a placeholder or gap comes first.
    rowBefore(node) {
      let prev = node.previousElementSibling;
      if (!prev) {
        const pc = node.parentNode.previousElementSibling;
        prev = pc ? pc.lastElementChild : null;
      }
      if (!prev || !prev.classList.contains('l')) return null;
      return lastRow(prev);
    }

    // The row element right after ``r``, if nothing (placeholder, gap) is in
    // between.
    rowAfter(r) {
      let s = r.nextElementSibling;
      while (s && !s.classList.contains('r')) s = s.nextElementSibling;
      if (s) return s;
      let l = r.parentNode.nextElementSibling;
      if (!l) {
        const nc = r.parentNode.parentNode.nextElementSibling;
        l = nc ? nc.firstElementChild : null;
      }
      return l && l.classList.contains('l') ? firstRow(l) : null;
    }

    // Regrouping logical lines (a wrap flag changed, a placeholder completed
    // a line that continues into rows already shown, rows dropped from the
    // middle of a line) moves rows between line elements. The DOM collapses a
    // range whose node is removed, so only the side no selection touches
    // moves; when a selection touches both, the regrouping waits until it is
    // gone (``deferred``, retried with every update).
    selected(node) {
      const sel = typeof getSelection === 'function' ? getSelection() : null;
      if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
      try { return sel.getRangeAt(0).intersectsNode(node); } catch (e) { return false; }
    }

    // Counters of the chunks rows moved between, read from the DOM.
    recount(...chunks) {
      for (const c of new Set(chunks)) {
        if (!c || !c.classList || !c.classList.contains('nt-chunk')) continue;
        c._rows = c.querySelectorAll('.r').length;
        c._lines = 0;
        c._gaps = 0;
        for (const k of c.children) {
          if (k.classList.contains('l')) c._lines++;
          else if (k.classList.contains('nt-gap')) c._gaps++;
        }
        if (c === this.tail) this.tailLines = c._lines;
        if (!c.firstChild && c !== this.tail) c.remove();
        else this.sizeChunk(c);
      }
    }

    // A row whose wrap flag changed.
    rewrap(r, w) {
      r._w = w;
      this.settle(r);
    }

    // Make the line boundary after ``r`` match its wrap flag now, or remember
    // the row (``deferred``) when a selection touches both sides. What is
    // deferred is the row, not an operation: it is reconciled with its flag
    // as it is when the selection goes, so a flag that changed and changed
    // back leaves nothing to do.
    settle(r) {
      if (this.reconcile(r)) this.deferred.delete(r);
      else this.deferred.add(r);
    }

    // True when the boundary after ``r`` matches its flag (now, or after the
    // regrouping done here); false when a selection blocks the regrouping.
    reconcile(r) {
      if (!r.isConnected) return true;
      if (r._w === '1') {
        const nx = this.rowAfter(r);
        if (!nx || nx.parentNode === r.parentNode || firstRow(nx.parentNode) !== nx) return true;
        return this.mergeLines(r.parentNode, nx.parentNode);
      }
      const after = this.rowAfterInLine(r);
      return after ? this.splitBefore(after) : true;
    }

    rowAfterInLine(r) {
      for (let s = r.nextElementSibling; s; s = s.nextElementSibling) if (s.classList.contains('r')) return s;
      return null;
    }

    // Lines ``l1`` and ``l2`` (right after it) become one; false when a
    // selection touches both (nothing moved).
    mergeLines(l1, l2) {
      const c1 = l1.parentNode, c2 = l2.parentNode;
      if (!this.selected(l1)) {
        // l1's rows go to the start of l2 (inserted before: nothing in l2 moves).
        l2.prepend(...rowsOf(l1));
        l1.remove();
      } else if (!this.selected(l2)) {
        l1.append(...rowsOf(l2));
        l2.remove();
      } else {
        return false;
      }
      this.recount(c1, c2);
      return true;
    }

    // The line of ``r`` ends before ``r``: r and the rows after it become a
    // line of their own (or the rows before it do, whichever no selection
    // touches); false when a selection touches both (nothing moved).
    splitBefore(r) {
      const l = r.parentNode;
      const rows = rowsOf(l);
      const at = rows.indexOf(r);
      if (at <= 0) return true;
      const head = rows.slice(0, at), tail = rows.slice(at);
      const touches = (list) => list.some((x) => this.selected(x));
      const l2 = el('div', 'l');
      if (!touches(tail)) {
        l2.append(...tail);
        l.after(l2);
      } else if (!touches(head)) {
        l2.append(...head);
        l.before(l2);
      } else {
        return false;
      }
      this.recount(l2.parentNode);
      return true;
    }

    retryDeferred() {
      for (const r of Array.from(this.deferred)) this.settle(r);
    }

    // -- history -----------------------------------------------------------------------

    // History rows ``start``.. confirmed by the bridge (each row once, in
    // order). Rows before ``start`` the page never got (tmux dropped them)
    // become a gap marker.
    applyHistory(start, html, texts, wrapped) {
      if (this.oldest == null) { this.oldest = start; this.next = start; }
      if (start > this.next) this.dropRows(start);
      this.applyRows(start, html, texts, wrapped);
      this.confirm(start + html.length);
    }

    confirm(upTo) {
      const k = upTo - this.next;
      if (k <= 0) return;
      for (let i = 0; i < k && i < this.live.length; i++) if (this.live[i]) this.lastConfirmed = this.live[i];
      this.live.splice(0, k);
      this.next = upTo;
    }

    // Rows [next, upTo) will never come: removed (placeholders shrink), and a
    // marker (no text, nothing to copy) stands for them.
    dropRows(upTo) {
      const count = upTo - this.next;
      let anchor = null; // the node the marker goes before
      for (let i = 0; i < count && i < this.live.length; i++) {
        const r = this.live[i];
        if (!r) continue;
        const l = r.parentNode, c = l.parentNode;
        r.remove();
        c._rows--;
        this.rowCount--;
        if (!firstRow(l)) { l.remove(); c._lines--; if (c === this.tail) this.tailLines--; }
        this.sizeChunk(c);
        if (this.lastEl === r) this.lastEl = null;
      }
      while (this.pendings.length && this.pendings[0].a < upTo) {
        const p = this.pendings[0];
        p.a = Math.min(upTo, p.b);
        if (p.a < p.b) { p.el.style.height = (p.b - p.a) * this.rowH + 'px'; break; }
        const c = p.el.parentNode;
        p.el.remove();
        this.pendings.shift();
        if (!c.firstChild) c.remove();
      }
      this.live.splice(0, count);
      this.next = upTo;
      this.lastConfirmed = null;
      // The marker goes before the first row (or placeholder) still to come,
      // else at the end.
      const first = this.live.find((x) => x) || null;
      const p0 = this.pendings[0];
      if (p0 && (!first || p0.a <= first._n)) anchor = p0.el;
      else if (first) anchor = first.parentNode; // its line starts with it now
      const g = el('div', 'nt-gap');
      g.dataset.rows = String(count);
      if (anchor) {
        anchor.parentNode.insertBefore(g, anchor);
        anchor.parentNode._gaps++;
        this.sizeChunk(anchor.parentNode);
        if (first && firstRow(first.parentNode) !== first) {
          // The rows before it in its line come before the gap: the row before
          // it no longer continues into anything, and its line ends there.
          const rows = rowsOf(first.parentNode);
          const before = rows[rows.indexOf(first) - 1];
          before._w = '0';
          this.settle(before);
        }
      } else {
        if (!this.tail) this.tail = this.newChunk(this.hist.lastElementChild);
        this.tail.appendChild(g);
        this.tail._gaps++;
        this.sizeChunk(this.tail);
        this.endBreak = true;
      }
    }

    // Older rows ``start``.. (``html.length`` of them, ending where the page's
    // rows begin) above everything. The spacer gives back their height.
    prependHistory(start, html, texts, wrapped) {
      if (!html.length) return;
      if (this.oldest == null) { this.applyHistory(start, html, texts, wrapped); return; }
      let end = html.length;
      const firstL = this.hist.querySelector('.l');
      const head = firstL ? firstRow(firstL) : null;
      if (wrapped[end - 1] === '1' && head && head._n === this.oldest && start + html.length === this.oldest) {
        // The last prepended line continues into the first one we have: its
        // rows go to the start of that element (inserted, nothing removed).
        let s = end - 1;
        while (s > 0 && wrapped[s - 1] === '1') s--;
        const c = firstL.parentNode;
        const rows = this.parseRows(rowSpans(html, s, end), start + s, html, texts, wrapped, s);
        firstL.prepend(...rows);
        c._rows += rows.length;
        this.sizeChunk(c);
        this.rowCount += rows.length;
        end = s;
      }
      let before = this.hist.firstElementChild;
      let i = 0;
      while (i < end) {
        const c = el('div', 'nt-chunk');
        this.hist.insertBefore(c, before);
        c._rows = 0;
        c._lines = 0;
        c._gaps = 0;
        let out = '';
        const from = i;
        while (i < end && c._lines < this.opts.chunk) {
          let e = i;
          while (e < end - 1 && wrapped[e] === '1') e++;
          e++;
          out += '<div class="l">' + rowSpans(html, i, e) + '</div>';
          c._rows += e - i;
          c._lines++;
          i = e;
        }
        c.innerHTML = out;
        tagRows(c.querySelectorAll('.r'), start + from, html, texts, wrapped, from);
        this.sizeChunk(c);
        before = c.nextElementSibling;
      }
      this.rowCount += end;
      this.oldest = start;
      this.spacerRows = Math.max(0, this.spacerRows - html.length);
      this.spacer.style.height = this.spacerRows * this.rowH + 'px';
    }

    // Keep at most maxRows rows: the oldest chunks go back to the spacer (the
    // same height: nothing moves), only chunks of confirmed history, never one
    // a selection touches or one in view. A chunk with a gap marker is not as
    // high as the rows it stands for, so it goes only while the view follows
    // the bottom.
    trim() {
      const sel = typeof getSelection === 'function' ? getSelection() : null;
      while (this.rowCount > this.opts.maxRows) {
        const first = this.hist.firstElementChild;
        if (!first || !first.nextElementSibling || first === this.tail) break;
        // Only confirmed history: its last row comes before ``next``.
        const rows = first.querySelectorAll('.r');
        if (rows.length && rows[rows.length - 1]._n >= this.next) break;
        if (this.pendings.some((p) => p.el.parentNode === first)) break;
        if (sel && sel.rangeCount && sel.getRangeAt(0).intersectsNode(first)) break;
        const top = this.spacerHeight() + (first._rows + first._gaps) * this.rowH;
        if (!this.stuck && (first._gaps || top > this.scroller.scrollTop - this.scroller.clientHeight)) break;
        if (this.lastConfirmed && first.contains(this.lastConfirmed)) break;
        this.rowCount -= rows.length;
        first.remove();
        // The oldest row is the first one left (counted, not remembered: lines
        // regroup across chunks).
        const head = this.hist.querySelector('.r');
        this.oldest = head ? head._n : this.next;
        this.spacerRows = this.firstHeld == null ? 0 : Math.max(0, this.oldest - this.firstHeld);
        this.spacer.style.height = this.spacerRows * this.rowH + 'px';
      }
    }

    // -- the screen ----------------------------------------------------------------

    // The live screen: rows ``top``.. (``html`` one entry per row, ``wrapped``
    // their flags). ``cursor``: {x, y, visible, shape, blink, ch, wide}. With
    // ``alternate`` the rows go to the alternate layer and the main screen
    // stays as it was.
    applyScreen(top, html, texts, wrapped, cursor, alternate) {
      this.alternate = !!alternate;
      if (alternate) {
        this.alt.hidden = false;
        this.renderLayer(html, wrapped);
      } else {
        this.alt.hidden = true;
        if (this.oldest == null) { this.oldest = top; this.next = top; }
        this.applyRows(top, html, texts, wrapped);
        this.screenTop = top;
      }
      this.placeCursor(cursor);
      this.retryDeferred();
      this.trim();
      if (this.stuck && !this.touching) this.scrollToBottom();
    }

    // The text of the screen rows, one line per row (for tests and checks).
    screenText() {
      const out = [];
      if (this.screenTop == null) return '';
      for (let y = 0; y < this.rows; y++) {
        const r = this.rowEl(this.screenTop + y);
        out.push(r ? r.textContent : '');
      }
      return out.join('\n');
    }

    renderLayer(html, wrapped) {
      const n = html.length;
      const sig = n + ':' + wrapped.slice(0, n - 1);
      this.alt.style.width = Math.ceil(this.cols * this.cellW) + 'px';
      if (sig !== this.altSig) {
        let out = '';
        for (const [a, b] of groups(wrapped, 0, n)) out += '<div class="l">' + rowSpans(html, a, b) + '</div>';
        this.alt.innerHTML = out;
        this.altEls = Array.prototype.slice.call(this.alt.querySelectorAll('.r'));
        this.altHtml = html.slice();
        this.altSig = sig;
        return;
      }
      for (let y = 0; y < n; y++) {
        if (html[y] !== this.altHtml[y]) {
          this.altEls[y].innerHTML = html[y];
          this.altHtml[y] = html[y];
        }
      }
    }

    // The cursor is an overlay inside its row (no text of its own: the
    // character it shows comes from generated content, never copied).
    placeCursor(c) {
      this.cursorState = c || null;
      let row = null;
      if (c && c.visible) row = this.alternate ? this.altEls[c.y] : this.rowEl(this.screenTop + c.y);
      if (!row) { this.cursor.remove(); return; }
      const k = this.cursor;
      k.style.left = c.x + 'ch';
      k.style.width = (c.wide ? 2 : 1) + 'ch';
      k.dataset.ch = c.ch && c.ch !== ' ' ? c.ch : '';
      k.dataset.shape = c.shape === 'underline' || c.shape === 'bar' ? c.shape : 'block';
      k.classList.toggle('nt-blink', !!c.blink);
      if (k.parentNode !== row) row.appendChild(k);
    }

    // -- scrolling -------------------------------------------------------------------

    atBottom() {
      const s = this.scroller;
      return s.scrollTop + s.clientHeight >= s.scrollHeight - 4;
    }

    setStuck(stuck) {
      if (stuck === this.stuck) return;
      this.stuck = stuck;
      if (this.onStuck) this.onStuck(stuck);
    }

    onScroll() {
      // Our own scroll to the bottom fires a scroll event later, when more rows
      // may have arrived: an event at the position we set is ours.
      if (this.scroller.scrollTop === this.ourTop) return;
      this.ourTop = -1;
      this.setStuck(this.atBottom());
      if (this.onScrolled) this.onScrolled();
    }

    scrollToBottom() {
      const s = this.scroller;
      s.scrollTop = s.scrollHeight;
      this.ourTop = s.scrollTop;
      this.setStuck(true);
    }
  }

  function el(tag, cls) {
    const e = document.createElement(tag);
    e.className = cls;
    return e;
  }

  function rowsOf(l) {
    const out = [];
    for (let s = l.firstElementChild; s; s = s.nextElementSibling) if (s.classList.contains('r')) out.push(s);
    return out;
  }

  function firstRow(l) {
    for (let s = l.firstElementChild; s; s = s.nextElementSibling) if (s.classList.contains('r')) return s;
    return null;
  }

  function lastRow(l) {
    for (let s = l.lastElementChild; s; s = s.previousElementSibling) if (s.classList.contains('r')) return s;
    return null;
  }

  function tagRows(els, n, html, texts, wrapped, from) {
    for (let k = 0; k < els.length; k++) {
      const r = els[k];
      r._n = n + k; r._h = html[from + k]; r._t = texts[from + k]; r._w = wrapped[from + k] || '0';
    }
  }

  function rowSpans(html, a, b) {
    let out = '';
    for (let i = a; i < b; i++) out += '<span class="r">' + html[i] + '</span>';
    return out;
  }

  // Logical lines of rows [from, to) from their wrap flags.
  function groups(wrapped, from, to) {
    const out = [];
    let s = from;
    for (let i = from; i < to; i++) {
      if (i === to - 1 || wrapped[i] !== '1') { out.push([s, i + 1]); s = i + 1; }
    }
    return out;
  }

  root.MerlinNativeRenderer = NativeRenderer;
})(typeof window !== 'undefined' ? window : globalThis);
