// Native terminal toolbar: the classic terminal's keys (Esc, Tab, Shift+Tab,
// the sticky Ctrl, Alt and Shift, Enter, the window keys F3, F4 and F5, the
// new-window button, the arrows that repeat while held). A sticky modifier
// applies to the next key from the toolbar or from a keyboard (the input
// field reads the same MerlinNativeInput.Modifiers), then clears.
//
// Buttons never take the focus: the input field keeps it, so a phone's
// keyboard stays open while the toolbar is used.
(function (root) {
  'use strict';
  const Core = root.MerlinNativeCore;
  const REPEAT_DELAY = 400;
  const REPEAT_EVERY = 60;

  class NativeToolbar {
    // el: the toolbar; sink: { input(data), key(name) }; opts: { modifiers,
    // focus(), newWindow(), onWindowKey() }
    constructor(el, sink, opts) {
      this.el = el;
      this.sink = sink;
      this.opts = opts || {};
      this.modifiers = this.opts.modifiers;
      this.modifiers.onChange = (active) => this.paint(active);
      // No button takes the focus from the input field.
      el.addEventListener('mousedown', (e) => e.preventDefault());
      for (const btn of el.querySelectorAll('[data-mod]')) {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          this.modifiers.toggle(btn.dataset.mod);
          this.focus();
        });
      }
      for (const btn of el.querySelectorAll('[data-key]:not([data-repeat])')) {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          this.press(btn.dataset.key);
          this.focus();
        });
      }
      for (const btn of el.querySelectorAll('[data-repeat]')) this.bindRepeat(btn);
      const nw = el.querySelector('[data-action="new-window"]');
      if (nw) {
        nw.addEventListener('click', (e) => {
          e.preventDefault();
          this.modifiers.clear();
          if (this.opts.newWindow) this.opts.newWindow(nw);
          this.focus();
        });
      }
    }

    focus() { if (this.opts.focus) this.opts.focus(); }

    paint(active) {
      for (const btn of this.el.querySelectorAll('[data-mod]')) {
        btn.classList.toggle('active', active.has(btn.dataset.mod));
      }
    }

    // One key, with the sticky modifiers, then they clear. ``key``: a
    // KeyboardEvent key name, or ``ShiftTab``.
    press(key) {
      const m = this.modifiers.peek() || {};
      this.modifiers.clear();
      const action = key === 'ShiftTab' ? { key: 'BTab' } : Core.keyAction(key, m, null);
      if (!action) return;
      if (action.key) this.sink.key(action.key);
      else if (action.data) this.sink.input(action.data);
      if (/^F[345]$/.test(key) && this.opts.onWindowKey) this.opts.onWindowKey(key);
    }

    // Held, the key repeats: once at once, then every REPEAT_EVERY ms after
    // REPEAT_DELAY. The sticky modifiers apply to every repeat of one hold.
    bindRepeat(btn) {
      let delay = null, every = null, mods = null;
      const fire = () => {
        const action = Core.keyAction(btn.dataset.key, mods || {}, null);
        if (action && action.key) this.sink.key(action.key);
      };
      const start = (e) => {
        e.preventDefault();
        stop();
        mods = this.modifiers.peek();
        this.modifiers.clear();
        fire();
        delay = setTimeout(() => { every = setInterval(fire, REPEAT_EVERY); }, REPEAT_DELAY);
      };
      const stop = () => {
        clearTimeout(delay);
        clearInterval(every);
        delay = every = null;
      };
      btn.addEventListener('touchstart', start, { passive: false });
      btn.addEventListener('touchend', (e) => { e.preventDefault(); stop(); this.focus(); });
      btn.addEventListener('touchcancel', stop);
      btn.addEventListener('mousedown', (e) => { if (e.button === 0) start(e); });
      btn.addEventListener('mouseup', () => { stop(); this.focus(); });
      btn.addEventListener('mouseleave', stop);
    }
  }

  root.MerlinNativeToolbar = NativeToolbar;
})(typeof window !== 'undefined' ? window : globalThis);
