/* Tests for the native terminal's input (terminal/static/native/core.js and
   input.js): the key encoder per key, the text field's diff model, and the
   field driven by the event sequences a phone produces. What tmux makes of
   each key name is pinned by tests/unit/test_terminal_native.py
   (KEY_BYTES). */

const test = require('node:test');
const assert = require('node:assert/strict');

const Core = require('../../terminal/static/native/core.js');

const S = '\u00a0\u00a0\u00a0\u00a0';

// -- the key encoder -------------------------------------------------------------

test('keyAction: special keys are tmux names with the xterm modifiers', () => {
  const k = (key, mods, code) => Core.keyAction(key, mods || {}, code || null);
  const cases = [
    ['ArrowUp', {}, 'Up'], ['ArrowDown', {}, 'Down'], ['ArrowLeft', {}, 'Left'], ['ArrowRight', {}, 'Right'],
    ['Home', {}, 'Home'], ['End', {}, 'End'], ['PageUp', {}, 'PPage'], ['PageDown', {}, 'NPage'],
    ['Insert', {}, 'IC'], ['Delete', {}, 'DC'], ['Escape', {}, 'Escape'], ['Tab', {}, 'Tab'],
    ['F1', {}, 'F1'], ['F5', {}, 'F5'], ['F12', {}, 'F12'],
    ['ArrowLeft', { ctrl: true }, 'C-Left'], ['ArrowLeft', { shift: true }, 'S-Left'],
    ['ArrowLeft', { alt: true }, 'M-Left'], ['ArrowUp', { ctrl: true, shift: true }, 'C-S-Up'],
    ['ArrowRight', { ctrl: true, alt: true }, 'C-M-Right'], ['Home', { shift: true }, 'S-Home'],
    ['Delete', { ctrl: true }, 'C-DC'], ['F5', { shift: true }, 'S-F5'], ['F1', { ctrl: true }, 'C-F1'],
    ['Tab', { shift: true }, 'BTab'], ['Tab', { ctrl: true }, 'C-Tab'],
    ['Escape', { alt: true }, 'M-Escape'], ['Escape', { ctrl: true }, 'Escape'],
    ['Enter', { ctrl: true }, 'C-Enter'], ['Enter', { alt: true }, 'M-Enter'],
    ['Backspace', { alt: true }, 'M-BSpace'],
    [' ', { ctrl: true }, 'C-Space'], [' ', { alt: true }, 'M-Space'],
  ];
  for (const [key, mods, name] of cases) assert.deepEqual(k(key, mods), { key: name }, key + JSON.stringify(mods));
});

test('keyAction: Ctrl and Alt on characters', () => {
  const k = (key, mods, code) => Core.keyAction(key, mods, code || null);
  assert.deepEqual(k('c', { ctrl: true }, 'KeyC'), { key: 'C-c' });
  assert.deepEqual(k('C', { ctrl: true, shift: true }, 'KeyC'), { key: 'C-c' });
  assert.deepEqual(k('a', { ctrl: true, alt: true }, 'KeyA'), { key: 'C-M-a' });
  assert.deepEqual(k('[', { ctrl: true }, 'BracketLeft'), { key: 'C-[' });
  assert.deepEqual(k('\\', { ctrl: true }, 'Backslash'), { key: 'C-\\' });
  assert.deepEqual(k('/', { ctrl: true }, 'Slash'), { key: 'C-/' });
  assert.deepEqual(k('2', { ctrl: true }, 'Digit2'), { key: 'C-2' });
  assert.deepEqual(k(';', { ctrl: true }, 'Semicolon'), { key: 'C-;' });
  assert.deepEqual(k('a', { alt: true }, 'KeyA'), { key: 'M-a' });
  assert.deepEqual(k('A', { alt: true, shift: true }, 'KeyA'), { key: 'M-A' });
  assert.deepEqual(k('.', { alt: true }, 'Period'), { key: 'M-.' });
  // Option on a Mac composes a character: the key's own letter or digit.
  assert.deepEqual(k('˙', { alt: true }, 'KeyH'), { key: 'M-h' });
  assert.deepEqual(k('Ó', { alt: true, shift: true }, 'KeyH'), { key: 'M-H' });
  assert.deepEqual(k('¡', { alt: true }, 'Digit1'), { key: 'M-1' });
  // Ctrl on a non-Latin layout: the key's position.
  assert.deepEqual(k('с', { ctrl: true }, 'KeyC'), { key: 'C-c' });
  // Names tmux would type as text are never produced.
  assert.deepEqual(k('#', { ctrl: true }, 'Digit3'), { data: '#' });
  assert.deepEqual(k('#', { ctrl: true, alt: true }, 'Digit3'), { key: 'M-#' });
  assert.deepEqual(k('Backspace', { ctrl: true }), { data: '\x08' });
  assert.deepEqual(k('Backspace', { ctrl: true, alt: true }), { data: '\x1b\x08' });
  // Plain keys.
  assert.deepEqual(k('Enter', {}), { data: '\r' });
  assert.deepEqual(k('Backspace', {}), { data: '\x7f' });
  assert.equal(k('a', {}), null);
  assert.equal(k('a', { shift: true }), null);
  // The browser's: Cmd/Win, AltGr, modifier keys alone, dead keys.
  assert.equal(k('c', { meta: true }, 'KeyC'), null);
  assert.equal(k('@', { ctrl: true, alt: true, altGraph: true }, 'Digit0'), null);
  for (const key of ['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'Dead', 'Unidentified', 'Process']) {
    assert.equal(k(key, { ctrl: true }), null, key);
  }
});

test('every name the encoder produces is one the bridge accepts', () => {
  // Mirrors terminal/native.py valid_key_name: no Ctrl with Escape, BSpace or #.
  const bases = /^(?:[CMS]-){0,3}(Up|Down|Left|Right|Home|End|PPage|NPage|IC|DC|Escape|Tab|BTab|Enter|Space|BSpace|F1[0-2]|F[1-9]|[^\x00-\x20\x7f])$/u;
  const keys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown',
    'Insert', 'Delete', 'Escape', 'Tab', 'Enter', 'Backspace', ' ', 'a', 'Z', '#', ';', '\\', 'é', '1',
    ...Array.from({ length: 12 }, (_, i) => 'F' + (i + 1))];
  for (const key of keys) {
    for (let bits = 0; bits < 8; bits++) {
      const mods = { ctrl: !!(bits & 1), alt: !!(bits & 2), shift: !!(bits & 4) };
      const a = Core.keyAction(key, mods, null);
      if (!a || !a.key) continue;
      const m = bases.exec(a.key);
      assert.ok(m, a.key);
      assert.ok(!(a.key.includes('C-') && ['Escape', 'BSpace', '#'].includes(m[1])), a.key);
    }
  }
});

test('fieldKeyAction leaves text, plain Enter and plain Backspace to the field', () => {
  assert.equal(Core.fieldKeyAction('a', {}), null);
  assert.equal(Core.fieldKeyAction('Enter', {}), null);
  assert.equal(Core.fieldKeyAction('Enter', { shift: true }), null);
  assert.equal(Core.fieldKeyAction('Backspace', {}), null);
  assert.deepEqual(Core.fieldKeyAction('Backspace', { alt: true }), { key: 'M-BSpace' });
  assert.deepEqual(Core.fieldKeyAction('Enter', { ctrl: true }), { key: 'C-Enter' });
  assert.deepEqual(Core.fieldKeyAction('ArrowUp', {}), { key: 'Up' });
});

test('documentKeyAction sends everything, text and AltGr characters included', () => {
  assert.deepEqual(Core.documentKeyAction('a', {}), { data: 'a' });
  assert.deepEqual(Core.documentKeyAction('é', {}), { data: 'é' });
  assert.deepEqual(Core.documentKeyAction('Enter', {}), { data: '\r' });
  assert.deepEqual(Core.documentKeyAction('Backspace', {}), { data: '\x7f' });
  assert.deepEqual(Core.documentKeyAction('@', { ctrl: true, alt: true, altGraph: true }), { data: '@' });
  assert.deepEqual(Core.documentKeyAction('’', {}), { data: "'" });
  assert.deepEqual(Core.documentKeyAction('c', { ctrl: true }, 'KeyC'), { key: 'C-c' });
  assert.equal(Core.documentKeyAction('c', { meta: true }), null);
  assert.equal(Core.documentKeyAction('Shift', { shift: true }), null);
});

// -- typed text --------------------------------------------------------------------

test('typedText undoes smart punctuation and no-break spaces, character for character', () => {
  assert.equal(Core.typedText('C’est ‘ok’ “yes” „x‟'), 'C\'est \'ok\' "yes" "x"');
  assert.equal(Core.typedText('a—b–c…'), 'a--b-c...');
  assert.equal(Core.typedText('a\u00a0b\u202fc'), 'a b c');
  assert.equal(Core.typedText('one\ntwo\r\nthree'), 'one\rtwo\rthree');
  assert.equal(Core.typedText('日本 🎉 é'), '日本 🎉 é');
});

// -- the diff model ------------------------------------------------------------------

function field(typed) { return S + typed + S; }

test('fieldDiff: typing appends, a rewrite erases back to the first change', () => {
  let d = Core.fieldDiff('', field('l'), S);
  assert.deepEqual([d.dels, d.text, d.typed, d.reset], [0, 'l', 'l', false]);
  d = Core.fieldDiff('l', field('ls'), S);
  assert.deepEqual([d.dels, d.text], [0, 's']);
  // An autocorrection replaces the word.
  d = Core.fieldDiff('teh', field('the'), S);
  assert.deepEqual([d.dels, d.text, d.typed], [2, 'he', 'the']);
  // Nothing changed.
  d = Core.fieldDiff('abc', field('abc'), S);
  assert.deepEqual([d.dels, d.text, d.reset], [0, '', false]);
});

test('fieldDiff: iOS dictation rewriting its hypothesis never doubles a word', () => {
  // The founder's sequence: "bonne nuit" then "salut", as iOS streams it.
  const steps = ['bonne', 'Bonne nuit', 'Bonne nuit', 'Bonne nuit sa', 'Bonne nuit salut'];
  let sent = '', line = '';
  for (const typed of steps) {
    const d = Core.fieldDiff(sent, field(typed), S);
    line = line.slice(0, line.length - d.dels) + d.text; // a line editor
    sent = d.sent;
  }
  assert.equal(line, 'Bonne nuit salut');
});

test('fieldDiff: a no-break space iOS puts in place of a space costs no erase', () => {
  const d = Core.fieldDiff('bonne nuit', field('Bonne\u00a0nuit'), S);
  assert.deepEqual([d.dels, d.text], [10, 'Bonne nuit']);
  const e = Core.fieldDiff('bonne nuit', field('bonne\u00a0nuit'), S);
  assert.deepEqual([e.dels, e.text], [0, '']);
  // A smart dash replacing "-" adds one character, nothing erased.
  const f = Core.fieldDiff('ls -', field('ls —'), S);
  assert.deepEqual([f.dels, f.text, f.sent], [0, '-', 'ls --']);
});

test('fieldDiff: deleting typed text, then into the sentinel (held backspace)', () => {
  let d = Core.fieldDiff('ab', field('a'), S);
  assert.deepEqual([d.dels, d.text, d.reset], [1, '', false]);
  d = Core.fieldDiff('', S.slice(1) + S, S);
  assert.deepEqual([d.dels, d.text, d.reset], [1, '', true]);
  // A word deleted from the typed text (iOS accelerating): one DEL per character.
  d = Core.fieldDiff('echo hello', field('echo '), S);
  assert.deepEqual([d.dels, d.text], [5, '']);
});

test('fieldDiff: characters are counted as code points', () => {
  const d = Core.fieldDiff('a🎉', field('a'), S);
  assert.equal(d.dels, 1);
  const e = Core.fieldDiff('日本', field('日本語'), S);
  assert.deepEqual([e.dels, e.text], [0, '語']);
});

test('fieldDiff: an edit after the caret (forward delete) resets without sending', () => {
  const d = Core.fieldDiff('ab', S + 'ab' + S.slice(1), S);
  assert.deepEqual([d.dels, d.text, d.reset], [0, '', true]);
});

test('diffBytes: DELs then the text, one message', () => {
  assert.equal(Core.diffBytes({ dels: 2, text: 'he' }), '\x7f\x7fhe');
  assert.equal(Core.diffBytes({ dels: 0, text: '' }), '');
});

test('inputAction: the edits the field must not apply', () => {
  assert.deepEqual(Core.inputAction('insertLineBreak', null, 'x'), { data: '\r' });
  assert.deepEqual(Core.inputAction('insertParagraph', null, ''), { data: '\r' });
  assert.deepEqual(Core.inputAction('deleteWordBackward', null, ''), { data: '\x17' });
  assert.equal(Core.inputAction('deleteWordBackward', null, 'echo'), null);
  assert.deepEqual(Core.inputAction('deleteContentForward', null, ''), { key: 'DC' });
  assert.deepEqual(Core.inputAction('deleteHardLineBackward', null, ''), { data: '\x15' });
  assert.deepEqual(Core.inputAction('insertFromPaste', 'a\nb', ''), { paste: 'a\nb' });
  assert.deepEqual(Core.inputAction('historyUndo', null, ''), { none: true });
  for (const t of ['insertText', 'insertReplacementText', 'deleteContentBackward', 'insertCompositionText']) {
    assert.equal(Core.inputAction(t, 'x', 'x'), null, t);
  }
});

test('stickyAction: the toolbar Ctrl or Alt on the next thing a software keyboard types', () => {
  assert.deepEqual(Core.stickyAction('insertText', 'c', { ctrl: true }), { key: 'C-c' });
  assert.deepEqual(Core.stickyAction('insertText', 'b', { alt: true }), { key: 'M-b' });
  assert.deepEqual(Core.stickyAction('insertText', 'C', { ctrl: true, alt: true }), { key: 'C-M-c' });
  assert.deepEqual(Core.stickyAction('insertLineBreak', null, { alt: true }), { key: 'M-Enter' });
  assert.deepEqual(Core.stickyAction('deleteContentBackward', null, { alt: true }), { key: 'M-BSpace' });
  assert.equal(Core.stickyAction('insertText', 'hello', { ctrl: true }), null);
  assert.equal(Core.stickyAction('insertText', 'c', { shift: true }), null);
  assert.equal(Core.stickyAction('insertText', 'c', null), null);
});

test('caretAction: one arrow per character, all of them', () => {
  assert.deepEqual(Core.caretAction(-2), ['Left', 'Left']);
  assert.deepEqual(Core.caretAction(1), ['Right']);
  assert.deepEqual(Core.caretAction(0), []);
  assert.equal(Core.caretAction(-1000).length, 1000);
});

test('caretMove counts what the terminal got: an ellipsis is three characters', () => {
  assert.equal(Core.caretMove(S + 'a\u2026', S + 'a'), -3);
  assert.equal(Core.caretMove(S + 'a\u2014b', S + 'a'), -3);
  assert.equal(Core.caretMove(S + 'a', S + 'a\u2026'), 3);
  assert.equal(Core.caretMove(S + '\u65e5', S), -1);
});

test('keyAction: an Option dead key is Meta with its letter', () => {
  assert.deepEqual(Core.keyAction('Dead', { alt: true }, 'KeyE'), { key: 'M-e' });
  assert.deepEqual(Core.keyAction('Dead', { alt: true, shift: true }, 'KeyU'), { key: 'M-U' });
  assert.deepEqual(Core.fieldKeyAction('Dead', { alt: true }, 'KeyN'), { key: 'M-n' });
  // A dead key without Alt is the keyboard's composition.
  assert.equal(Core.keyAction('Dead', {}, 'BracketLeft'), null);
  assert.equal(Core.fieldKeyAction('Dead', { shift: true }, 'Digit6'), null);
  assert.equal(Core.keyAction('Dead', { alt: true }, 'Quote'), null);
});

test('afterDeadKey: the accent was the dead key, the rest is what followed', () => {
  assert.equal(Core.afterDeadKey('\u00b4'), ' ');
  assert.equal(Core.afterDeadKey('\u00b4x'), 'x');
  assert.equal(Core.afterDeadKey('\u00e1'), 'a');
  assert.equal(Core.afterDeadKey('\u00f1'), 'n');
  assert.equal(Core.afterDeadKey('\u02c6'), ' ');
  assert.equal(Core.afterDeadKey(''), '');
});

// -- the field, driven by event sequences ---------------------------------------------

// A minimal DOM: a text field with listeners, a document with the focus and
// selectionchange. Events are dispatched synchronously, as a browser does for
// beforeinput / input; selectionchange is queued.
function harness() {
  const listeners = {};
  const docListeners = {};
  const fieldEl = {
    nodeType: 1, tagName: 'TEXTAREA', value: '', selectionStart: 0, selectionEnd: 0, attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    focus() { doc.activeElement = fieldEl; },
    // A browser that commits a composition on blur sets onBlur.
    blur() { doc.activeElement = null; if (this.onBlur) this.onBlur(); },
    parentNode: null,
  };
  const doc = {
    activeElement: fieldEl,
    addEventListener(t, f) { (docListeners[t] = docListeners[t] || []).push(f); },
    createElement: () => ({ style: {}, setAttribute() {}, hidden: true, textContent: '', offsetParent: null }),
    body: { appendChild() {} },
  };
  global.document = doc;
  global.window = undefined;
  const sent = [];
  const sink = {
    input: (d) => sent.push(['input', d]),
    key: (k) => sent.push(['key', k]),
    paste: (t) => sent.push(['paste', t]),
  };
  // input.js attaches to globalThis when there is no window.
  globalThis.MerlinNativeCore = Core;
  delete require.cache[require.resolve('../../terminal/static/native/input.js')];
  require('../../terminal/static/native/input.js');
  const Input = globalThis.MerlinNativeInput;
  const input = new Input(fieldEl, sink, { isMac: false });
  function fire(type, ev) {
    const e = Object.assign({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {}, target: fieldEl }, ev || {});
    for (const f of listeners[type] || []) f(e);
    return e;
  }
  // What a browser does for one edit: beforeinput, then (unless cancelled)
  // the new value with the caret after the change, then input.
  function edit(inputType, data, apply) {
    const e = fire('beforeinput', { inputType, data });
    if (e.defaultPrevented) return e;
    apply();
    fire('input', { inputType, data });
    return e;
  }
  function caretEnd() { return fieldEl.value.length - S.length; }
  function type(text) {
    edit('insertText', text, () => {
      const c = fieldEl.selectionStart;
      fieldEl.value = fieldEl.value.slice(0, c) + text + fieldEl.value.slice(c);
      fieldEl.setSelectionRange(c + text.length, c + text.length);
    });
  }
  // iOS rewriting the text before the caret (dictation, autocorrect).
  function rewrite(typed) {
    edit('insertReplacementText', typed, () => {
      fieldEl.value = S + typed + S;
      fieldEl.setSelectionRange(caretEnd(), caretEnd());
    });
  }
  function backspace() {
    edit('deleteContentBackward', null, () => {
      const c = fieldEl.selectionStart;
      const chars = Array.from(fieldEl.value.slice(0, c));
      chars.pop();
      const head = chars.join('');
      fieldEl.value = head + fieldEl.value.slice(c);
      fieldEl.setSelectionRange(head.length, head.length);
    });
  }
  function selectionchange() { for (const f of docListeners.selectionchange || []) f({}); }
  function keydown(key, ev) {
    return fire('keydown', Object.assign({ key, code: '', ctrlKey: false, altKey: false, shiftKey: false,
      metaKey: false, isComposing: false, keyCode: 0 }, ev || {}));
  }
  // The real toolbar on a fake element: its buttons and their listeners.
  function toolbar() {
    delete require.cache[require.resolve('../../terminal/static/native/toolbar.js')];
    require('../../terminal/static/native/toolbar.js');
    const button = (dataset) => {
      const ls = {};
      return { dataset, ls, classList: { toggle() {} },
        addEventListener(t, f) { (ls[t] = ls[t] || []).push(f); } };
    };
    const buttons = [
      button({ key: 'ArrowLeft', repeat: '' }), button({ key: 'Enter' }), button({ key: 'F4' }),
      button({ mod: 'alt' }), button({ action: 'new-window' }),
    ];
    const el = {
      addEventListener() {},
      querySelectorAll(sel) {
        if (sel === '[data-mod]') return buttons.filter((b) => 'mod' in b.dataset);
        if (sel === '[data-key]:not([data-repeat])') return buttons.filter((b) => 'key' in b.dataset && !('repeat' in b.dataset));
        if (sel === '[data-repeat]') return buttons.filter((b) => 'repeat' in b.dataset);
        return [];
      },
      querySelector(sel) { return sel === '[data-action="new-window"]' ? buttons[4] : null; },
    };
    const tb = new globalThis.MerlinNativeToolbar(el, input.keys(), { modifiers: input.modifiers, focus() {} });
    const ev = () => ({ preventDefault() {}, button: 0 });
    const run = (b, type) => { for (const f of b.ls[type] || []) f(ev()); };
    return {
      tb,
      tapLeft() { run(buttons[0], 'mousedown'); run(buttons[0], 'mouseup'); },
      enter() { run(buttons[1], 'click'); },
      alt() { run(buttons[3], 'click'); },
    };
  }
  return { field: fieldEl, input, sent, fire, edit, type, rewrite, backspace, selectionchange, keydown, docListeners, toolbar };
}

// The bytes a sequence sent, applied to a line editor (DEL erases before
// the cursor, arrows move it).
function line(sent) {
  let l = '', cur = 0;
  for (const [kind, d] of sent) {
    if (kind === 'key') {
      if (d === 'Left') cur = Math.max(0, cur - 1);
      else if (d === 'Right') cur = Math.min(l.length, cur + 1);
      continue;
    }
    for (const ch of Array.from(d)) {
      if (ch === '\r') { l = ''; cur = 0; continue; }
      if (ch === '\x7f') { if (cur > 0) { l = l.slice(0, cur - 1) + l.slice(cur); cur--; } }
      else { l = l.slice(0, cur) + ch + l.slice(cur); cur += ch.length; }
    }
  }
  return l;
}

test('the field: typed text reaches the terminal once, the sentinel never', () => {
  const h = harness();
  assert.equal(h.field.value, S + S);
  for (const ch of 'echo hi') h.type(ch);
  assert.equal(line(h.sent), 'echo hi');
  assert.ok(h.sent.every(([, d]) => !d.includes('\u00a0')));
  h.edit('insertLineBreak', null, () => { throw new Error('Enter must be cancelled'); });
  assert.deepEqual(h.sent[h.sent.length - 1], ['input', '\r']);
  assert.equal(h.field.value, S + S);
});

test('the field: dictation streamed and rewritten gives the text once', () => {
  const h = harness();
  h.type('bonne');
  h.rewrite('Bonne\u00a0nuit');
  h.rewrite('Bonne nuit');
  h.type(' ');
  h.type('sa');
  h.rewrite('Bonne nuit salut');
  assert.equal(line(h.sent), 'Bonne nuit salut');
  // The NBSP-for-space rewrite sent nothing.
  assert.equal(h.sent.filter(([, d]) => d.includes('\x7f')).length, 1);
});

test('the field: held backspace deletes typed text, then keeps deleting from an empty field', () => {
  const h = harness();
  for (const ch of 'abc') h.type(ch);
  for (let i = 0; i < 6; i++) h.backspace();
  const dels = h.sent.filter(([, d]) => d === '\x7f').length;
  assert.equal(dels, 6);
  // The field was refilled each time the sentinel was reached.
  assert.equal(h.field.value, S + S);
  // iOS accelerating to words on an empty field: Ctrl+W, the field kept.
  h.edit('deleteWordBackward', null, () => { throw new Error('must be cancelled'); });
  assert.deepEqual(h.sent[h.sent.length - 1], ['input', '\x17']);
});

test('the field: a word deleted from typed text is one DEL per character', () => {
  const h = harness();
  for (const ch of 'git status') h.type(ch);
  h.edit('deleteWordBackward', null, () => {
    h.field.value = S + 'git ' + S;
    h.field.setSelectionRange(S.length + 4, S.length + 4);
  });
  assert.equal(line(h.sent), 'git ');
});

test('the field: the space-bar trackpad moves the cursor with arrows', () => {
  const h = harness();
  for (const ch of 'abc') h.type(ch);
  h.field.setSelectionRange(S.length + 1, S.length + 1);
  h.selectionchange();
  assert.deepEqual(h.sent.slice(-2), [['key', 'Left'], ['key', 'Left']]);
  assert.equal(h.field.value, S + S);
  // Then right, past what was typed (into the trailing sentinel).
  h.field.setSelectionRange(S.length + 2, S.length + 2);
  h.selectionchange();
  assert.deepEqual(h.sent.slice(-2), [['key', 'Right'], ['key', 'Right']]);
  // Typing then inserts at the terminal's cursor.
  h.type('X');
  assert.equal(line(h.sent), 'abcX');
  // A caret already at the end, or a non-collapsed selection, sends nothing.
  const n = h.sent.length;
  h.selectionchange();
  h.field.setSelectionRange(0, 2);
  h.selectionchange();
  assert.equal(h.sent.length, n);
});

test('the field: a composition commits once, at its end', () => {
  const h = harness();
  h.fire('compositionstart', {});
  // While composing, the field changes and nothing is sent.
  for (const v of ['k', 'か', 'かん', '漢']) {
    h.fire('beforeinput', { inputType: 'insertCompositionText', data: v, isComposing: true });
    h.field.value = S + v + S;
    h.field.setSelectionRange(S.length + v.length, S.length + v.length);
    h.fire('input', { inputType: 'insertCompositionText', isComposing: true });
  }
  assert.deepEqual(h.sent, []);
  h.fire('compositionend', { data: '漢' });
  assert.deepEqual(h.sent, [['input', '漢']]);
  // An input event after the end (WebKit's order) sends nothing more.
  h.fire('input', { inputType: 'insertFromComposition' });
  assert.deepEqual(h.sent, [['input', '漢']]);
});

test('the field: smart punctuation reaches the terminal as plain quotes', () => {
  const h = harness();
  for (const ch of ['e', 'c', 'h', 'o', ' ', '’', 'h', 'i', '’']) h.type(ch);
  assert.equal(line(h.sent), "echo 'hi'");
});

test('the field: sticky modifiers apply to the next character, then clear', () => {
  const h = harness();
  h.input.modifiers.toggle('ctrl');
  h.edit('insertText', 'c', () => { throw new Error('must be cancelled'); });
  assert.deepEqual(h.sent, [['key', 'C-c']]);
  assert.equal(h.input.modifiers.peek(), null);
  h.type('x');
  assert.deepEqual(h.sent[1], ['input', 'x']);
  // A physical key with a sticky Alt.
  h.input.modifiers.toggle('alt');
  const e = h.keydown('b', { code: 'KeyB' });
  assert.ok(e.defaultPrevented);
  assert.deepEqual(h.sent[2], ['key', 'M-b']);
  // Sticky Shift on a typed character only clears.
  h.input.modifiers.toggle('shift');
  h.type('y');
  assert.deepEqual(h.sent[3], ['input', 'y']);
  assert.equal(h.input.modifiers.peek(), null);
});

test('the field: physical keys with no text are sent from keydown, the field reset', () => {
  const h = harness();
  h.type('a');
  const e = h.keydown('ArrowUp');
  assert.ok(e.defaultPrevented);
  assert.deepEqual(h.sent[1], ['key', 'Up']);
  assert.equal(h.field.value, S + S);
  // Text, Enter and Backspace go through the field.
  for (const key of ['a', 'Enter', 'Backspace']) assert.equal(h.keydown(key).defaultPrevented, false, key);
  // IME keydowns are left alone.
  assert.equal(h.keydown('ArrowUp', { isComposing: true }).defaultPrevented, false);
  assert.equal(h.keydown('a', { keyCode: 229, ctrlKey: true }).defaultPrevented, false);
});

test('the field: copy and paste shortcuts are the browser\'s, never sent', () => {
  const h = harness();
  let copied = 0;
  h.input.opts.copy = () => { copied++; };
  // Ctrl+V outside a Mac: the native paste event follows.
  assert.equal(h.keydown('v', { ctrlKey: true, code: 'KeyV' }).defaultPrevented, false);
  assert.equal(h.keydown('V', { ctrlKey: true, shiftKey: true, code: 'KeyV' }).defaultPrevented, false);
  assert.equal(h.keydown('v', { metaKey: true, code: 'KeyV' }).defaultPrevented, false);
  assert.ok(h.keydown('C', { ctrlKey: true, shiftKey: true, code: 'KeyC' }).defaultPrevented);
  assert.equal(copied, 1);
  assert.deepEqual(h.sent, []);
  // Ctrl+C: ^C without a selection, the browser's copy with one.
  h.keydown('c', { ctrlKey: true, code: 'KeyC' });
  assert.deepEqual(h.sent, [['key', 'C-c']]);
  h.input.opts.hasSelection = () => true;
  assert.equal(h.keydown('c', { ctrlKey: true, code: 'KeyC' }).defaultPrevented, false);
  assert.equal(h.sent.length, 1);
  // On a Mac, Ctrl+V is the program's.
  h.input.isMac = true;
  h.keydown('v', { ctrlKey: true, code: 'KeyV' });
  assert.deepEqual(h.sent[1], ['key', 'C-v']);
});

test('the field: a paste event sends its text through the paste path, images to the page', () => {
  const h = harness();
  const files = [];
  h.input.opts.onPasteFiles = (f) => files.push(...f);
  const text = { kind: 'string', type: 'text/plain' };
  let e = h.fire('paste', { clipboardData: { items: [text], getData: () => 'a\nb' } });
  assert.ok(e.defaultPrevented);
  assert.deepEqual(h.sent, [['paste', 'a\nb']]);
  const img = { kind: 'file', type: 'image/png', getAsFile: () => 'PNG' };
  e = h.fire('paste', { clipboardData: { items: [img], getData: () => '' } });
  assert.deepEqual(files, ['PNG']);
  assert.equal(h.sent.length, 1);
});

test('the field: suggestions are off by default and follow the setting', () => {
  const h = harness();
  assert.equal(h.field.attrs.autocorrect, 'off');
  assert.equal(h.field.attrs.autocapitalize, 'off');
  assert.equal(h.field.attrs.spellcheck, 'false');
  h.input.setSuggestions(true, false);
  assert.equal(h.field.attrs.autocorrect, 'on');
  assert.equal(h.field.attrs.autocomplete, 'on');
  assert.equal(h.field.attrs.spellcheck, 'true');
  assert.equal(h.field.attrs.autocapitalize, 'off');
});

test('the toolbar: an arrow resets the field like a physical arrow', () => {
  const h = harness();
  const t = h.toolbar();
  for (const ch of 'abc') h.type(ch);
  t.tapLeft();
  assert.deepEqual(h.sent[h.sent.length - 1], ['key', 'Left']);
  assert.equal(h.field.value, S + S);
  assert.equal(h.input.typed, '');
  // What iOS does next (a rewrite, a delete) is about the new position.
  h.rewrite('X');
  h.backspace();
  h.backspace();
  assert.equal(line(h.sent), 'ac');
  // The same through a physical arrow gives the same line.
  const p = harness();
  for (const ch of 'abc') p.type(ch);
  p.keydown('ArrowLeft');
  p.rewrite('X');
  p.backspace();
  p.backspace();
  assert.equal(line(p.sent), line(h.sent));
});

test('the toolbar: Enter ends the command, the field starts afresh', () => {
  const h = harness();
  const t = h.toolbar();
  for (const ch of 'ls') h.type(ch);
  t.enter();
  assert.equal(h.field.value, S + S);
  h.backspace();
  // One DEL at the new prompt, nothing of "ls" erased again.
  assert.deepEqual(h.sent.slice(-2), [['input', '\r'], ['input', '\x7f']]);
});

test('the toolbar: a held arrow keeps the hold\'s sticky modifier, then it clears', () => {
  const h = harness();
  const t = h.toolbar();
  t.alt();
  t.tapLeft();
  assert.deepEqual(h.sent, [['key', 'M-Left']]);
  assert.equal(h.input.modifiers.peek(), null);
});

// A composition replacing typed text (b becomes 漢), then a toolbar key
// before it commits.
function composeOverB(h) {
  h.type('a');
  h.type('b');
  h.fire('compositionstart', {});
  h.field.value = S + 'a\u6f22' + S;
  h.field.setSelectionRange(S.length + 2, S.length + 2);
}

test('the toolbar during a composition: the composition commits where it was typed, then the key', () => {
  const h = harness();
  const t = h.toolbar();
  // This browser commits a composition on blur (Chromium, WebKit).
  h.field.onBlur = () => h.fire('compositionend', { data: '\u6f22' });
  composeOverB(h);
  t.tapLeft();
  assert.deepEqual(h.sent, [['input', 'a'], ['input', 'b'], ['input', '\x7f\u6f22'], ['key', 'Left']]);
  assert.equal(h.field.value, S + S);
  assert.equal(h.input.composing, false);
  h.type('X');
  assert.equal(line(h.sent), 'aX\u6f22');
});

test('the toolbar during a composition the browser keeps: it is dropped, never sent later', () => {
  const h = harness();
  const t = h.toolbar();
  composeOverB(h);
  t.tapLeft();
  assert.deepEqual(h.sent, [['input', 'a'], ['input', 'b'], ['key', 'Left']]);
  // The browser's own end of that composition, later, sends nothing.
  h.field.value = S + 'a\u6f22' + S;
  h.fire('compositionend', { data: '\u6f22' });
  assert.equal(h.sent.length, 3);
  assert.equal(h.field.value, S + S);
  h.type('X');
  assert.equal(line(h.sent), 'aXb');
});

test('toolbar Enter during a composition: the composition belongs to the command it ends', () => {
  const h = harness();
  const t = h.toolbar();
  h.field.onBlur = () => h.fire('compositionend', { data: '\u6f22' });
  composeOverB(h);
  t.enter();
  assert.deepEqual(h.sent.slice(2), [['input', '\x7f\u6f22'], ['input', '\r']]);
  h.backspace();
  assert.deepEqual(h.sent[h.sent.length - 1], ['input', '\x7f']);
});

test('another pane: what the field holds is dropped, nothing sent to the new pane', () => {
  // Typed text, then the displayed pane changes.
  const h = harness();
  for (const ch of 'abc') h.type(ch);
  h.input.invalidate();
  assert.equal(h.field.value, S + S);
  h.rewrite('ABC');
  assert.deepEqual(h.sent.slice(3), [['input', 'ABC']]); // no DEL into the new pane
  // A composition in progress: dropped, and its later end ignored.
  const c = harness();
  composeOverB(c);
  c.input.invalidate();
  c.field.value = S + 'a\u6f22' + S;
  c.fire('compositionend', { data: '\u6f22' });
  assert.deepEqual(c.sent, [['input', 'a'], ['input', 'b']]);
  c.type('x');
  assert.deepEqual(c.sent[2], ['input', 'x']);
});

test('the field: a caret move over an ellipsis or a dash moves the terminal by what it got', () => {
  const h = harness();
  h.type('a');
  h.type('\u2026');
  assert.equal(line(h.sent), 'a...');
  h.field.setSelectionRange(S.length + 1, S.length + 1);
  h.selectionchange();
  h.type('X');
  assert.equal(line(h.sent), 'aX...');
  const d = harness();
  for (const ch of ['a', '\u2014', 'b']) d.type(ch);
  d.field.setSelectionRange(S.length + 1, S.length + 1);
  d.selectionchange();
  d.type('X');
  assert.equal(line(d.sent), 'aX--b');
});

test('the field: a caret move longer than 200 characters is sent whole', () => {
  const h = harness();
  const long = 'x'.repeat(250);
  h.type(long);
  h.field.setSelectionRange(S.length, S.length);
  h.selectionchange();
  assert.equal(h.sent.filter(([k, d]) => k === 'key' && d === 'Left').length, 250);
  h.type('Y');
  assert.equal(line(h.sent), 'Y' + long);
});

test('the field: an Option dead key sends Meta once and its accent never', () => {
  // The browser composes anyway: the accent on the next letter.
  const h = harness();
  const e = h.keydown('Dead', { altKey: true, code: 'KeyE', keyCode: 229 });
  assert.ok(e.defaultPrevented);
  assert.deepEqual(h.sent, [['key', 'M-e']]);
  h.fire('compositionstart', {});
  h.keydown('a', { isComposing: true, code: 'KeyA' });
  h.field.value = S + '\u00e1' + S;
  h.fire('compositionend', { data: '\u00e1' });
  assert.deepEqual(h.sent, [['key', 'M-e'], ['input', 'a']]);
  assert.equal(h.field.value, S + S);
  // The dead key then a Space: the accent alone is committed, the Space is
  // what reaches the program.
  const g = harness();
  g.keydown('Dead', { altKey: true, code: 'KeyE', keyCode: 229 });
  g.fire('compositionstart', {});
  g.keydown(' ', { isComposing: true, code: 'Space' });
  g.field.value = S + '\u00b4' + S;
  g.fire('compositionend', { data: '\u00b4' });
  assert.deepEqual(g.sent, [['key', 'M-e'], ['input', ' ']]);
  // The same without the keydown of the Space (a browser that hides it).
  const u = harness();
  u.keydown('Dead', { altKey: true, code: 'KeyU' });
  u.fire('compositionstart', {});
  u.field.value = S + '\u00a8' + S;
  u.fire('compositionend', { data: '\u00a8' });
  assert.deepEqual(u.sent, [['key', 'M-u'], ['input', ' ']]);
  // The dead key then a letter that does not combine: the letter.
  const x = harness();
  x.keydown('Dead', { altKey: true, code: 'KeyE' });
  x.fire('compositionstart', {});
  x.keydown('x', { isComposing: true, code: 'KeyX' });
  x.field.value = S + '\u00b4x' + S;
  x.fire('compositionend', { data: '\u00b4x' });
  assert.deepEqual(x.sent, [['key', 'M-e'], ['input', 'x']]);
  // The browser honours the cancelled keydown: the next key is plain text.
  const c = harness();
  c.keydown('Dead', { altKey: true, code: 'KeyE' });
  c.keydown('a', { code: 'KeyA' });
  c.type('a');
  assert.deepEqual(c.sent, [['key', 'M-e'], ['input', 'a']]);
});

test('the field: a dead key without Option is the keyboard\'s composition, sent once', () => {
  const h = harness();
  const e = h.keydown('Dead', { code: 'BracketLeft' });
  assert.equal(e.defaultPrevented, false);
  h.fire('compositionstart', {});
  h.field.value = S + '\u00ea' + S;
  h.fire('compositionend', { data: '\u00ea' });
  assert.deepEqual(h.sent, [['input', '\u00ea']]);
});

test('the document: an Option dead key after a mouse selection is Meta, the field focused again', () => {
  const h = harness();
  h.field.blur();
  const docKey = (key, ev) => {
    const e = Object.assign({ key, code: '', ctrlKey: false, altKey: false, shiftKey: false, metaKey: false,
      isComposing: false, keyCode: 0, defaultPrevented: false, target: { nodeType: 1, tagName: 'BODY' },
      preventDefault() { this.defaultPrevented = true; } }, ev);
    for (const f of h.docListeners.keydown || []) f(e);
    return e;
  };
  const e = docKey('Dead', { altKey: true, code: 'KeyE', keyCode: 229 });
  assert.ok(e.defaultPrevented);
  assert.deepEqual(h.sent, [['key', 'M-e']]);
  assert.equal(globalThis.document.activeElement, h.field);
  // A composition the browser starts in the field then: the dead key's
  // accent is not sent, the letter typed after it is.
  h.fire('compositionstart', {});
  h.keydown('a', { isComposing: true, code: 'KeyA' });
  h.field.value = S + '\u00e1' + S;
  h.fire('compositionend', { data: '\u00e1' });
  assert.deepEqual(h.sent, [['key', 'M-e'], ['input', 'a']]);
  // Real IME keydowns on the document stay ignored.
  assert.equal(docKey('a', { keyCode: 229 }).defaultPrevented, false);
  assert.equal(docKey('Dead', { altKey: true, code: 'KeyE', isComposing: true }).defaultPrevented, false);
});

// The edits a browser that keeps a dropped composition still delivers: a
// composing beforeinput/input pair with the field grown, then its commit.
function lateComposition(h, value) {
  h.fire('compositionupdate', { data: value });
  const b = h.fire('beforeinput', { inputType: 'insertCompositionText', data: value, isComposing: true });
  assert.equal(b.defaultPrevented, false);
  h.field.value = S + 'a' + value + S;
  h.fire('input', { inputType: 'insertCompositionText', isComposing: true });
  h.fire('compositionend', { data: value });
  // WebKit's order: the commit's own input after the end.
  h.field.value = S + 'a' + value + S;
  h.fire('beforeinput', { inputType: 'insertFromComposition', data: value });
  h.fire('input', { inputType: 'insertFromComposition' });
}

test('a dropped composition sends nothing of its late edits, after a toolbar key', () => {
  const h = harness();
  const t = h.toolbar();
  composeOverB(h);
  t.tapLeft(); // this browser keeps the composition through the blur
  lateComposition(h, '漢字');
  assert.deepEqual(h.sent, [['input', 'a'], ['input', 'b'], ['key', 'Left']]);
  assert.equal(h.field.value, S + S);
  assert.equal(h.input.preedit && !h.input.preedit.hidden, false);
  // Fresh typing, then a fresh composition, are sent as usual.
  h.type('X');
  h.fire('compositionstart', {});
  h.field.value = S + 'Xか' + S;
  h.fire('input', { inputType: 'insertCompositionText', isComposing: true });
  h.fire('compositionend', { data: 'か' });
  assert.deepEqual(h.sent.slice(3), [['input', 'X'], ['input', 'か']]);
  assert.equal(line(h.sent), 'aXかb');
});

test('a dropped composition sends nothing of its late edits to a new pane', () => {
  const h = harness();
  composeOverB(h);
  h.input.invalidate(); // the displayed pane changed
  lateComposition(h, '漢字');
  assert.deepEqual(h.sent, [['input', 'a'], ['input', 'b']]);
  assert.equal(h.field.value, S + S);
  h.type('y');
  assert.deepEqual(h.sent[2], ['input', 'y']);
});

test('a normal edit ends a dropped composition the browser never closed', () => {
  const h = harness();
  composeOverB(h);
  h.input.invalidate();
  // The browser left the old text in the field and never ended it.
  h.field.value = S + 'a漢' + S;
  h.field.setSelectionRange(S.length + 2, S.length + 2);
  h.type('z');
  assert.deepEqual(h.sent, [['input', 'a'], ['input', 'b'], ['input', 'z']]);
  assert.equal(h.input.cancelled, false);
});
