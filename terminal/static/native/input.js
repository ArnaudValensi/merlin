// Native terminal input: one real text field, kept holding a sentinel on each
// side of the caret so a software keyboard always has text to delete and a
// caret to move (held backspace, the space-bar trackpad, dictation and
// predictive text depend on it).
//
// The field keeps what was typed since the last reset between the two
// sentinels, and every change is diffed against what the terminal already got
// (MerlinNativeCore.fieldDiff): DEL back to the first changed character, then
// the new tail. iOS can then rewrite its own text in place (a dictation
// hypothesis, an autocorrection, a prediction) and the terminal follows it
// exactly, never doubling a word: the model the founder validated on his
// iPhone (journal, 2026-11-08, variant C). Edits the diff cannot read (Enter,
// a word deleted with nothing typed, forward delete, paste, undo) are mapped
// explicitly. Physical keys with no text of their own (arrows, Escape, Tab,
// function keys, anything with Ctrl or Alt) are encoded from keydown as tmux
// key names.
(function (root) {
  'use strict';
  const Core = root.MerlinNativeCore;
  const SENTINEL = '\u00a0\u00a0\u00a0\u00a0';
  // After a finger leaves the field, how long a caret change is still the
  // system placing the caret for that touch.
  const TOUCH_GRACE = 500;
  const IS_MAC = typeof navigator !== 'undefined'
    && (/Mac/.test(navigator.platform || '') || /Mac/.test(navigator.userAgent || ''));

  function editable(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  // The toolbar's sticky modifiers: Ctrl, Alt and Shift apply to the next key
  // from the toolbar or a keyboard, then clear.
  class Modifiers {
    constructor() { this.active = new Set(); this.onChange = null; }
    toggle(name) {
      if (this.active.has(name)) this.active.delete(name); else this.active.add(name);
      if (this.onChange) this.onChange(this.active);
    }
    peek() {
      if (!this.active.size) return null;
      return { ctrl: this.active.has('ctrl'), alt: this.active.has('alt'), shift: this.active.has('shift') };
    }
    clear() {
      if (!this.active.size) return;
      this.active.clear();
      if (this.onChange) this.onChange(this.active);
    }
  }

  class NativeInput {
    // sink: { input(data), key(name), paste(text) }
    // opts: { modifiers: Modifiers, onPasteFiles(files), copy(), hasSelection(),
    //         cursorRect(), isMac }
    constructor(field, sink, opts) {
      this.field = field;
      this.sink = sink;
      this.opts = opts || {};
      this.modifiers = this.opts.modifiers || new Modifiers();
      this.isMac = this.opts.isMac != null ? this.opts.isMac : IS_MAC;
      this.typed = '';
      this.sent = '';
      this.composing = false;
      this.resetPending = false;
      // A composition the page dropped (its context changed) that the browser
      // may still be running: every edit of it (composing input, its commit,
      // its end) is swallowed until a normal edit, a key or a new
      // composition.
      this.cancelled = false;
      this.deadMeta = false;
      this.deadNext = null;
      this.preedit = null;
      // A finger on the field itself (over the Paste button on a touch
      // screen): the caret it places is the system's Paste menu's, not a
      // cursor move for the terminal.
      this.touched = false;
      this.touchEnd = null;
      this.touchAnchor = 0;
      this.now = () => Date.now();
      field.setAttribute('autocapitalize', 'off');
      field.setAttribute('enterkeyhint', 'enter');
      // No autocorrect, predictions or spellcheck: they rewrite commands.
      field.setAttribute('autocorrect', 'off');
      field.setAttribute('autocomplete', 'off');
      field.setAttribute('spellcheck', 'false');
      this.reset();
      field.addEventListener('beforeinput', (e) => this.onBeforeInput(e));
      field.addEventListener('input', () => this.onInput());
      field.addEventListener('compositionstart', () => this.onCompositionStart());
      field.addEventListener('compositionupdate', (e) => { if (!this.cancelled) this.showPreedit(e.data); });
      field.addEventListener('compositionend', () => this.onCompositionEnd());
      field.addEventListener('keydown', (e) => this.onKeyDown(e));
      field.addEventListener('paste', (e) => this.onPaste(e));
      field.addEventListener('touchstart', () => { this.touched = true; this.touchEnd = null; }, { passive: true });
      const released = () => { this.touchEnd = this.now(); this.touchAnchor = field.selectionStart; };
      field.addEventListener('touchend', released, { passive: true });
      field.addEventListener('touchcancel', released, { passive: true });
      field.addEventListener('blur', () => this.settleTouch());
      // The system's menu also offers Cut: it would delete the sentinels.
      field.addEventListener('cut', (e) => { e.preventDefault(); this.reset(); });
      document.addEventListener('touchstart', (e) => { if (e.target !== field) this.settleTouch(); }, { passive: true });
      document.addEventListener('selectionchange', () => this.onSelectionChange());
      document.addEventListener('keydown', (e) => this.onDocumentKeyDown(e));
      document.addEventListener('paste', (e) => { if (e.target !== field && !editable(e.target)) this.onPaste(e); });
    }

    value() { return SENTINEL + this.typed + SENTINEL; }

    // The field back to two sentinels with the caret between them. Never
    // while composing (setting the value would cancel the composition): then
    // once it ends.
    reset() {
      if (this.composing) { this.resetPending = true; return; }
      this.resetPending = false;
      this.touched = false;
      this.typed = '';
      this.sent = '';
      this.field.value = this.value();
      this.field.setSelectionRange(SENTINEL.length, SENTINEL.length);
    }

    // -- sending ---------------------------------------------------------------

    send(action) {
      if (!action || action.none) return;
      if (action.paste != null) this.sink.paste(action.paste);
      else if (action.key) this.sink.key(action.key);
      else if (action.data) this.sink.input(action.data);
    }

    // A key or an explicit edit: it consumes the sticky modifiers and the
    // field starts afresh (what was typed is no longer where the terminal's
    // cursor is). A composition in progress is committed first, where it was
    // typed, so the key comes after it as it did for the user.
    sendKey(action) {
      if (this.composing) this.commitComposition();
      this.modifiers.clear();
      this.send(action);
      this.reset();
    }

    // Ends the composition now: a blur makes the browser commit it (its
    // compositionend runs the diff); one that does not is dropped, never sent
    // later against a moved cursor.
    commitComposition() {
      const f = this.field;
      const focused = document.activeElement === f;
      f.blur();
      if (focused) this.focus();
      if (this.composing) this.dropComposition();
    }

    dropComposition() {
      this.composing = false;
      this.cancelled = true;
      this.deadMeta = false;
      this.hidePreedit();
    }

    // The terminal's target changed (another pane or window): what the field
    // holds, a composition included, belongs to the old one and is dropped,
    // nothing sent.
    invalidate() {
      if (this.composing) this.dropComposition();
      this.reset();
    }

    // Keys from elsewhere on the page (the toolbar): the same boundary as a
    // physical key, so the field never keeps text that no longer sits before
    // the terminal's cursor.
    keys() {
      return {
        key: (name) => this.sendKey({ key: name }),
        input: (data) => this.sendKey({ data }),
      };
    }

    diff(transform) {
      const d = Core.fieldDiff(this.sent, this.field.value, SENTINEL);
      if (transform) { d.text = transform(d.text); d.reset = true; }
      const bytes = Core.diffBytes(d);
      if (bytes) {
        this.modifiers.clear();
        this.sink.input(bytes);
      }
      if (d.reset) this.reset();
      else { this.typed = d.typed; this.sent = d.sent; }
    }

    // -- the field ---------------------------------------------------------------

    onBeforeInput(e) {
      this.settleTouch();
      const t = e.inputType;
      const composition = e.isComposing || /Composition/.test(t || '');
      if (this.cancelled) {
        if (composition) return;
        // A normal edit: the dropped composition is over, and whatever of
        // it is still in the field goes before this edit applies.
        this.cancelled = false;
        this.reset();
      }
      if (this.composing || composition) return;
      let data = e.data;
      if (data == null && e.dataTransfer) data = e.dataTransfer.getData('text/plain');
      const sticky = Core.stickyAction(t, data, this.modifiers.peek());
      const action = sticky || Core.inputAction(t, data, this.typed);
      if (!action) return; // the field applies it; the diff reads it on input
      e.preventDefault();
      this.sendKey(action);
    }

    // Every input event while a dropped composition runs is one of its edits
    // (a normal edit ends that state in beforeinput first): never read.
    onInput() {
      if (this.cancelled) { this.reset(); return; }
      if (!this.composing) this.diff();
    }

    onCompositionStart() {
      // Before it begins: a composition is written at the caret.
      this.settleTouch();
      this.composing = true;
      this.cancelled = false;
      this.showPreedit('');
    }

    onCompositionEnd() {
      if (this.cancelled) {
        // Its commit may still follow as an input event: still swallowed.
        this.reset();
        return;
      }
      this.composing = false;
      this.hidePreedit();
      // After an Option dead key already sent as Meta, the accent it
      // composed is not sent again: what follows it is the key typed after
      // the dead key (a Space included), else what the composition gives.
      const dead = this.deadMeta;
      const next = this.deadNext;
      this.deadMeta = false;
      this.deadNext = null;
      this.diff(dead ? (text) => (next != null ? next : Core.afterDeadKey(text)) : null);
      if (this.resetPending) this.reset();
    }

    // The composition being typed (an IME, an accent held on a key), shown
    // at the terminal's cursor until it commits: only the committed text
    // reaches the terminal, once.
    showPreedit(text) {
      if (!this.preedit) {
        this.preedit = document.createElement('span');
        this.preedit.className = 'nt-preedit';
        this.preedit.setAttribute('aria-hidden', 'true');
        (this.field.parentNode || document.body).appendChild(this.preedit);
      }
      const p = this.preedit;
      p.textContent = text || '';
      p.hidden = !text;
      const rect = this.opts.cursorRect ? this.opts.cursorRect() : null;
      const base = p.offsetParent ? p.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
      if (rect) {
        p.style.left = (rect.left - base.left) + 'px';
        p.style.top = (rect.top - base.top) + 'px';
      }
    }

    hidePreedit() {
      if (this.preedit) { this.preedit.hidden = true; this.preedit.textContent = ''; }
    }

    // A caret move away from the end of what was typed: the iOS space-bar
    // trackpad (or a tap in the field). Arrows, one per character, then a
    // fresh field.
    onSelectionChange() {
      if (this.composing || document.activeElement !== this.field) return;
      const f = this.field;
      if (f.value !== this.value() || f.selectionStart !== f.selectionEnd) return;
      const end = SENTINEL.length + this.typed.length;
      const caret = f.selectionStart;
      let from = end;
      if (this.touched) {
        // The finger is on the field, or just left it: this caret is the
        // system's for that touch, and later moves count from it.
        if (this.touchEnd === null || this.now() - this.touchEnd < TOUCH_GRACE) {
          this.touchAnchor = caret;
          return;
        }
        from = this.touchAnchor;
      }
      if (caret === from) return;
      const moved = Core.caretMove(f.value.slice(0, from), f.value.slice(0, caret));
      this.modifiers.clear();
      for (const k of Core.caretAction(moved)) this.sink.key(k);
      this.reset();
    }

    // The finger is done with the field: the caret goes back after what was
    // typed, before any edit applies, and nothing is sent for where it was.
    settleTouch() {
      if (!this.touched) return;
      this.touched = false;
      if (this.composing) return;
      const end = SENTINEL.length + this.typed.length;
      this.field.setSelectionRange(end, end);
    }

    mods(e) {
      const s = this.modifiers.peek() || {};
      return {
        ctrl: e.ctrlKey || !!s.ctrl,
        alt: e.altKey || !!s.alt,
        shift: e.shiftKey || !!s.shift,
        meta: e.metaKey,
        altGraph: !!(e.getModifierState && e.getModifierState('AltGraph')),
      };
    }

    // Copy and paste shortcuts (docs/merlin-cli/clipboard.md). True when the
    // key is the browser's: nothing is sent for it.
    shortcut(e) {
      const plain = !e.altKey && !e.metaKey;
      const k = e.key ? e.key.toLowerCase() : '';
      // Ctrl+Shift+C copies the selection (or the last text a program
      // copied); Ctrl+Shift+V pastes (the field's native paste event).
      if (e.ctrlKey && e.shiftKey && plain && (k === 'c' || e.code === 'KeyC')) {
        e.preventDefault();
        if (this.opts.copy) this.opts.copy();
        return true;
      }
      if (e.ctrlKey && e.shiftKey && plain && (k === 'v' || e.code === 'KeyV')) return this.toPaste();
      // Ctrl+V pastes, except on a Mac, where Cmd+V does and Ctrl+V is the
      // running program's (Claude Code reads its own clipboard with it).
      if (!this.isMac && e.ctrlKey && !e.shiftKey && plain && (k === 'v' || e.code === 'KeyV')) return this.toPaste();
      if (e.metaKey && !e.ctrlKey && !e.altKey && (k === 'v' || e.code === 'KeyV')) return this.toPaste();
      // Ctrl+C over a selection in the terminal copies it (the browser's
      // copy, through the page's copy handler); without one it is ^C.
      if (e.ctrlKey && !e.shiftKey && plain && (k === 'c' || e.code === 'KeyC')
          && this.opts.hasSelection && this.opts.hasSelection()) {
        if (document.activeElement === this.field) this.field.blur();
        return true;
      }
      return false;
    }

    // The browser's own paste follows the key: the paste event (text and
    // images, no permission prompt) lands on the field or the document.
    toPaste() {
      this.modifiers.clear();
      return true;
    }

    // An Option (Alt) dead key: Meta with the key's letter, like any Alt
    // letter. The browser may still compose its accent (see onCompositionEnd).
    deadKey(e) {
      return e.key === 'Dead' && e.altKey && !e.ctrlKey && !e.metaKey && /^Key[A-Z]$/.test(e.code || '');
    }

    // The key typed after an Option dead key, while the browser composes.
    noteDeadNext(e) {
      if (this.deadMeta && this.deadNext == null && e.key && Array.from(e.key).length === 1) {
        this.deadNext = Core.typedText(e.key);
      }
    }

    onKeyDown(e) {
      if (e.isComposing) { this.noteDeadNext(e); return; }
      this.settleTouch();
      if (this.cancelled && e.keyCode !== 229) { this.cancelled = false; this.reset(); }
      const dead = this.deadKey(e);
      if (e.keyCode === 229 && !dead) return;
      this.deadMeta = dead;
      this.deadNext = null;
      if (this.shortcut(e)) return;
      const action = Core.fieldKeyAction(e.key, this.mods(e), e.code);
      if (!action) return;
      e.preventDefault();
      this.sendKey(action);
    }

    // Keys typed while no text field has the focus (a mouse selection takes
    // it off the field): they go to the terminal, and the field takes the
    // focus back for what follows.
    onDocumentKeyDown(e) {
      if (e.defaultPrevented || e.target === this.field || editable(e.target)) return;
      if (e.isComposing) return;
      const dead = this.deadKey(e);
      if (e.keyCode === 229 && !dead) return;
      this.deadMeta = dead;
      this.deadNext = null;
      if (this.shortcut(e)) return;
      const action = Core.documentKeyAction(e.key, this.mods(e), e.code);
      if (!action) return;
      e.preventDefault();
      this.sendKey(action);
      this.focus();
    }

    onPaste(e) {
      const dt = e.clipboardData;
      if (!dt) return;
      const files = [];
      for (const item of Array.from(dt.items || [])) {
        if (item.kind === 'file' && item.type && item.type.startsWith('image/')) {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      e.preventDefault();
      e.stopPropagation();
      this.modifiers.clear();
      if (files.length) {
        if (this.opts.onPasteFiles) this.opts.onPasteFiles(files);
      } else {
        const text = dt.getData('text/plain');
        if (text) this.sink.paste(text);
      }
      this.reset();
    }

    focus() {
      this.field.focus({ preventScroll: true });
    }
  }

  root.MerlinNativeInput = NativeInput;
  root.MerlinNativeInput.Modifiers = Modifiers;
  root.MerlinNativeInput.SENTINEL = SENTINEL;
})(typeof window !== 'undefined' ? window : globalThis);
