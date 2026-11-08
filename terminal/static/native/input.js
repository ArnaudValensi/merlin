// Native terminal input: one real text field, kept holding a sentinel so the
// platform keyboard always has text to delete and a caret to move (held
// backspace, the space-bar trackpad, dictation and predictive text depend on
// it). Every edit is read from beforeinput, mapped to bytes or a tmux key
// name (MerlinNativeCore), and the field is put back to the sentinel.
(function (root) {
  'use strict';
  const Core = root.MerlinNativeCore;
  const SENTINEL = '    ';

  class NativeInput {
    // sink: { input(data), key(name), paste(text) }; opts.modifiers() returns
    // the toolbar's sticky modifiers to apply to the next key, then clears them.
    constructor(field, sink, opts) {
      this.field = field;
      this.sink = sink;
      this.opts = opts || {};
      this.composing = false;
      this.quiet = false;
      this.lastCaret = SENTINEL.length;
      field.setAttribute('autocapitalize', 'off');
      field.setAttribute('autocomplete', 'off');
      field.setAttribute('autocorrect', 'off');
      field.setAttribute('spellcheck', 'false');
      this.reset();
      field.addEventListener('beforeinput', (e) => this.onBeforeInput(e));
      field.addEventListener('compositionstart', () => { this.composing = true; });
      field.addEventListener('compositionend', (e) => this.onCompositionEnd(e));
      field.addEventListener('keydown', (e) => this.onKeyDown(e));
      document.addEventListener('selectionchange', () => this.onSelectionChange());
    }

    reset() {
      this.quiet = true;
      this.field.value = SENTINEL + SENTINEL;
      this.field.setSelectionRange(SENTINEL.length, SENTINEL.length);
      this.lastCaret = SENTINEL.length;
      this.quiet = false;
    }

    // Sticky modifiers from the toolbar apply to the next key or character.
    sticky() {
      return this.opts.modifiers ? this.opts.modifiers() : null;
    }

    deliver(action) {
      if (!action) return;
      if (action.paste != null) this.sink.paste(action.paste);
      else if (action.key) this.sink.key(action.key);
      else if (action.data) {
        const mods = this.sticky();
        if (mods && (mods.ctrl || mods.alt) && action.data.length === 1) {
          const k = Core.keyAction(action.data, mods);
          if (k) { this.deliver(k); return; }
        }
        this.sink.input(action.data);
      }
    }

    onBeforeInput(e) {
      if (this.composing || e.inputType === 'insertCompositionText') return;
      e.preventDefault();
      let data = e.data;
      if (data == null && e.dataTransfer) data = e.dataTransfer.getData('text/plain');
      this.deliver(Core.inputAction(e.inputType, data));
      this.reset();
    }

    onCompositionEnd(e) {
      this.composing = false;
      if (e.data) this.deliver({ data: e.data });
      this.reset();
    }

    onKeyDown(e) {
      if (e.isComposing || e.keyCode === 229) return;
      const sticky = this.opts.peekModifiers ? this.opts.peekModifiers() : null;
      const mods = {
        ctrl: e.ctrlKey || !!(sticky && sticky.ctrl),
        alt: e.altKey || !!(sticky && sticky.alt),
        shift: e.shiftKey,
        meta: e.metaKey,
      };
      if (this.opts.onKey && this.opts.onKey(e) === false) return;
      const action = Core.keyAction(e.key, mods);
      if (!action) return;
      e.preventDefault();
      if (sticky) this.sticky();
      this.deliver(action);
    }

    // The space-bar trackpad (and any caret move) in the pristine field.
    onSelectionChange() {
      if (this.quiet || this.composing || document.activeElement !== this.field) return;
      const f = this.field;
      if (f.value !== SENTINEL + SENTINEL || f.selectionStart !== f.selectionEnd) return;
      const delta = f.selectionStart - this.lastCaret;
      if (!delta) return;
      for (const k of Core.caretAction(delta)) this.sink.key(k);
      this.reset();
    }

    focus() {
      this.field.focus({ preventScroll: true });
    }
  }

  root.MerlinNativeInput = NativeInput;
  root.MerlinNativeInput.SENTINEL = SENTINEL;
})(typeof window !== 'undefined' ? window : globalThis);
