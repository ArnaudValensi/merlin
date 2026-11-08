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
//         .nt-hist          history, in chunks of CHUNK logical lines, each
//           .nt-chunk       content-visibility: auto
//             .l            one logical line
//               .r          one visual row, an inline-block as wide as the pane
//         .nt-screen        the live main screen, the same .l > .r structure
//     .nt-alt               the alternate screen, a layer over the scroller
//
// Every visual row is exactly one row height (an integer number of pixels),
// so the spacer shrinks by exactly the height of the rows that replace it:
// loading older history never moves what the user looks at, and never sets
// scrollTop (which would stop a momentum scroll on iOS).
//
// A soft-wrapped line is one .l element whose rows are inline-blocks, so the
// platform's own selection copies it joined (what iOS needs: no script runs
// on its Copy). A line stays one element when its rows reach history in two
// updates, and when it straddles history and screen (the screen rows that
// continue the last history line are "borrowed" into it).
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
      this.screen = el('div', 'nt-screen');
      this.alt = el('div', 'nt-alt');
      this.alt.hidden = true;
      this.cursor = el('span', 'nt-cursor');
      this.cursor.setAttribute('aria-hidden', 'true');
      this.content.append(this.spacer, this.hist, this.screen);
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
      this.screen.textContent = '';
      this.alt.textContent = '';
      this.alt.hidden = true;
      this.content.style.minWidth = Math.ceil(cols * this.cellW) + 'px';
      this.tail = null; // the last chunk
      this.tailLines = 0;
      this.lastLine = null; // the last history .l
      this.borrowed = [];
      this.histRows = 0; // visual rows in the DOM history
      this.oldest = null; // absolute number of the first DOM history row
      this.firstHeld = null; // the oldest row tmux holds
      this.next = null; // one past the last
      this.pendingGap = 0;
      this.spacerRows = 0;
      this.spacer.style.height = '0px';
      this.screenSig = null;
      this.screenHtml = [];
      this.rowEls = [];
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

    // -- history -------------------------------------------------------------------

    // A chunk knows the absolute number of its first row (``start``), its
    // rows, its logical lines, and its gap markers (one row high each).
    newChunk(before, start) {
      const c = el('div', 'nt-chunk');
      if (before) this.hist.insertBefore(c, before);
      else this.hist.appendChild(c);
      c._start = start;
      c._rows = 0;
      c._lines = 0;
      c._gaps = 0;
      return c;
    }

    sizeChunk(c) {
      c.style.containIntrinsicSize = 'auto ' + (c._rows + c._gaps) * this.rowH + 'px';
    }

    dropBorrowed() {
      for (const r of this.borrowed) r.remove();
      this.borrowed = [];
      this.screenSig = null;
    }

    // Rows ``start``.. appended to history (they scrolled off the screen).
    // ``html``/``texts`` from rowsHtml, ``wrapped`` their flags, ``cont``: the
    // first one continues the last history line. ``gap``: rows before
    // ``start`` tmux dropped before the page got them.
    appendHistory(start, html, texts, wrapped, cont, gap) {
      if (!html.length) return;
      this.dropBorrowed();
      if (this.oldest == null) { this.oldest = start; this.next = start; }
      gap = (gap || 0) + this.pendingGap;
      this.pendingGap = 0;
      if (gap > 0 || start !== this.next) {
        // A hole: say so (not as text, nothing to copy), and start a new line.
        const g = el('div', 'nt-gap');
        g.dataset.rows = String(gap || start - this.next);
        if (!this.tail) { this.tail = this.newChunk(null, start); this.tailLines = 0; }
        this.tail.appendChild(g);
        this.tail._gaps++;
        cont = false;
        this.lastLine = null;
      }
      let i = 0;
      if (cont && this.lastLine) {
        // Rows that continue the last line join its element.
        let e = 0;
        while (e < html.length) { e++; if (wrapped[e - 1] !== '1') break; }
        this.lastLine.insertAdjacentHTML('beforeend', rowSpans(html, 0, e));
        this.lastLine._t += texts.slice(0, e).join('');
        this.lastLine.parentNode._rows += e;
        i = e;
      }
      while (i < html.length) {
        if (!this.tail || this.tailLines >= this.opts.chunk) {
          if (this.tail) this.sizeChunk(this.tail);
          this.tail = this.newChunk(null, start + i);
          this.tailLines = 0;
        }
        let out = '';
        const lineTexts = [];
        let rows = 0;
        while (i < html.length && this.tailLines < this.opts.chunk) {
          let e = i;
          while (e < html.length - 1 && wrapped[e] === '1') e++;
          e++;
          out += '<div class="l">' + rowSpans(html, i, e) + '</div>';
          lineTexts.push(texts.slice(i, e).join(''));
          rows += e - i;
          this.tailLines++;
          i = e;
        }
        const firstNew = this.tail.childElementCount;
        this.tail.insertAdjacentHTML('beforeend', out);
        const kids = this.tail.children;
        for (let k = 0; k < lineTexts.length; k++) kids[firstNew + k]._t = lineTexts[k];
        this.tail._rows += rows;
        this.tail._lines = this.tailLines;
        this.lastLine = this.tail.lastElementChild;
      }
      this.sizeChunk(this.tail);
      this.histRows += html.length;
      this.next = start + html.length;
      this.trim();
    }

    // Older rows ``start``.. (``html.length`` of them, ending where the DOM
    // history begins) above everything. The spacer gives back their height.
    prependHistory(start, html, texts, wrapped) {
      if (!html.length) return;
      if (this.oldest == null) { this.appendHistory(start, html, texts, wrapped, false, 0); return; }
      let end = html.length;
      const first = this.hist.querySelector('.l');
      if (wrapped[end - 1] === '1' && first && start + html.length === this.oldest) {
        // The last prepended line continues into the first one we have: its
        // rows go to the start of that element.
        let s = end - 1;
        while (s > 0 && wrapped[s - 1] === '1') s--;
        first.insertAdjacentHTML('afterbegin', rowSpans(html, s, end));
        first._t = texts.slice(s, end).join('') + first._t;
        first.parentNode._rows += end - s;
        first.parentNode._start = start + s;
        this.sizeChunk(first.parentNode);
        end = s;
      }
      let before = this.hist.firstChild;
      let i = 0;
      // Chunks in order, inserted before what we have.
      while (i < end) {
        const c = this.newChunk(before, start + i);
        let out = '';
        const lineTexts = [];
        while (i < end && c._lines < this.opts.chunk) {
          let e = i;
          while (e < end - 1 && wrapped[e] === '1') e++;
          e++;
          out += '<div class="l">' + rowSpans(html, i, e) + '</div>';
          lineTexts.push(texts.slice(i, e).join(''));
          c._rows += e - i;
          c._lines++;
          i = e;
        }
        c.innerHTML = out;
        for (let k = 0; k < lineTexts.length; k++) c.children[k]._t = lineTexts[k];
        this.sizeChunk(c);
        before = c.nextSibling;
      }
      if (!this.tail) { this.tail = this.hist.lastElementChild; this.tailLines = this.tail._lines; }
      if (!this.lastLine) this.lastLine = this.tail.lastElementChild;
      this.histRows += html.length;
      this.oldest = start;
      this.spacerRows = Math.max(0, this.spacerRows - html.length);
      this.spacer.style.height = this.spacerRows * this.rowH + 'px';
    }

    // Keep at most maxRows rows: the oldest chunks go back to the spacer (the
    // same height: nothing moves), except one a selection touches or one in
    // view. A chunk with a gap marker is not as high as the rows it stands
    // for, so it goes only while the view follows the bottom.
    trim() {
      const sel = typeof getSelection === 'function' ? getSelection() : null;
      while (this.histRows > this.opts.maxRows) {
        const first = this.hist.firstElementChild;
        if (!first || first === this.tail) break;
        if (sel && sel.rangeCount && sel.getRangeAt(0).intersectsNode(first)) break;
        const top = this.spacerHeight() + (first._rows + first._gaps) * this.rowH;
        if (!this.stuck && (first._gaps || top > this.scroller.scrollTop - this.scroller.clientHeight)) break;
        const following = first.nextElementSibling;
        this.histRows -= first._rows;
        this.oldest = following ? following._start : this.next;
        this.spacerRows = this.firstHeld == null ? 0 : Math.max(0, this.oldest - this.firstHeld);
        this.spacer.style.height = this.spacerRows * this.rowH + 'px';
        first.remove();
      }
    }

    // The column text of the last history line, for links that cross into the
    // screen rows that continue it.
    lastLineText() {
      return this.lastLine ? this.lastLine._t : '';
    }

    // -- the screen ----------------------------------------------------------------

    // The live screen: ``html`` one entry per row, ``wrapped`` their flags,
    // ``cont``: its first row continues the last history line. ``cursor``:
    // {x, y, visible, shape, blink, ch, wide}. With ``alternate`` the rows go
    // to the alternate layer and the main screen stays as it was.
    setScreen(html, wrapped, cont, cursor, alternate) {
      this.alternate = !!alternate;
      if (alternate) {
        this.alt.hidden = false;
        this.renderLayer(html, wrapped);
      } else {
        this.alt.hidden = true;
        this.renderMain(html, wrapped, cont);
      }
      this.placeCursor(cursor);
      if (this.stuck && !this.touching) this.scrollToBottom();
    }

    renderMain(html, wrapped, cont) {
      const n = html.length;
      let lend = 0;
      if (cont && this.lastLine) {
        while (lend < n) { lend++; if (wrapped[lend - 1] !== '1') break; }
      }
      const sig = lend + ':' + n + ':' + wrapped.slice(lend, n - 1);
      if (sig !== this.screenSig) {
        this.dropBorrowed();
        if (lend) {
          this.lastLine.insertAdjacentHTML('beforeend', rowSpans(html, 0, lend));
          const kids = this.lastLine.children;
          this.borrowed = Array.prototype.slice.call(kids, kids.length - lend);
        }
        let out = '';
        for (const [a, b] of groups(wrapped, lend, n)) out += '<div class="l">' + rowSpans(html, a, b) + '</div>';
        this.screen.innerHTML = out;
        this.rowEls = this.borrowed.concat(Array.prototype.slice.call(this.screen.querySelectorAll('.r')));
        this.screenHtml = html.slice();
        this.screenSig = sig;
        return;
      }
      for (let y = 0; y < n; y++) {
        if (html[y] !== this.screenHtml[y]) {
          this.rowEls[y].innerHTML = html[y];
          this.screenHtml[y] = html[y];
        }
      }
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
      const rows = this.alternate ? this.altEls : this.rowEls;
      const row = c && c.visible ? rows[c.y] : null;
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
