// The native terminal page (/terminal/native): everything around the engine
// that the classic page (terminal/templates/terminal.html) does around
// xterm.js, through the same seams. window.MerlinTerminal for the Sessions
// panel (board.js), the notifications bell, the voice console and the Apps
// panel; the shared status bar and panels (terminal-status-bar.html,
// terminal-panels.html); the clipboard ladder (clipboard-core.js) and the
// session identity (session-identity.js).
//
// What differs from the classic page, and why:
// - The terminal shows tmux's grid only, so a message the classic page writes
//   into its screen (a voice or upload error) is a notice over it.
// - Selection and copy are the platform's: a right-click with a selection
//   opens the browser's menu (Copy), without one it pastes as in the classic
//   page; a phone's long press selects.
(function () {
  'use strict';
  const Choice = window.MerlinTerminalChoice;
  if (Choice && Choice.leaving) return;
  const Core = window.MerlinNativeCore;

  const $ = (id) => document.getElementById(id);
  let config = {};
  try { config = JSON.parse(($('nt-config') || {}).textContent || '{}'); } catch (e) { config = {}; }

  const container = $('nt-container');
  const jump = $('nt-jump');
  const field = $('nt-input');
  const statusDot = $('status-dot');
  const statusText = $('status-text');
  const toolbarEl = $('nt-toolbar');
  const toolbarToggle = $('toolbar-toggle');
  const noticeEl = $('nt-notice');
  const copyToast = $('copy-toast');
  const coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  const ACTIVE_EVERY_MS = 1000;
  const isTouchDevice = ('ontouchstart' in window || navigator.maxTouchPoints > 0);

  // Menu button opens the sidebar (the mobile top bar is hidden here).
  const menuBtn = $('status-menu');
  if (menuBtn) {
    menuBtn.addEventListener('click', () => {
      const sidebar = document.querySelector('.sidebar');
      const overlay = document.querySelector('.sidebar-overlay');
      if (sidebar) sidebar.classList.toggle('open');
      if (overlay) overlay.classList.toggle('open');
    });
  }

  // -- the status line and the notices ---------------------------------------------

  let base = { text: '', cls: '' };
  let flashTimer = null;
  function setStatus(state, text) {
    statusDot.className = state;
    base = { text, cls: '' };
    if (!flashTimer) { statusText.textContent = text; statusText.className = ''; }
  }
  // A short message in the status line, then the status again.
  function flash(text, cls) {
    statusText.textContent = text;
    statusText.className = cls || '';
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashTimer = null;
      statusText.textContent = base.text;
      statusText.className = base.cls;
    }, 1500);
  }

  let noticeTimer = null;
  // One line over the terminal, for a while or until tapped.
  function notice(message, level) {
    noticeEl.textContent = message;
    noticeEl.className = 'nt-notice' + (level === 'error' ? ' error' : '');
    noticeEl.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 8000);
  }
  noticeEl.addEventListener('click', () => { noticeEl.hidden = true; });
  function showVoiceRefused(message) { notice(message || 'Voice input unavailable', 'warn'); }

  // -- the session --------------------------------------------------------------------

  let current = null;
  let currentSession = '';
  let currentWindowId = '';
  let attempted = null;
  let confirmed = false;
  let stopped = false;

  // Deep link: ?target=<session>:<window_id> switches this client to that
  // window once its tmux client is confirmed (the first session frame), then
  // leaves the URL (as the classic page does).
  let pendingTarget = '';
  try {
    const t = new URLSearchParams(location.search).get('target');
    if (t && /^[^:]+:@\d+$/.test(t)) pendingTarget = t;
  } catch (e) { /* no URL API, no deep link */ }
  function dropTargetParam() {
    try {
      const params = new URLSearchParams(location.search);
      if (!params.has('target')) return;
      params.delete('target');
      const q = params.toString();
      history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);
    } catch (e) { /* ignore */ }
  }

  function onSession(msg) {
    if (MerlinSessionIdentity.confirm(msg)) confirmed = true;
    // Attached: whatever sent this tab back to the classic page before (an
    // older tmux) no longer holds.
    if (Choice && Choice.fallback()) Choice.clearFallback();
    current = msg;
    currentSession = msg.name || '';
    currentWindowId = msg.window_id || '';
    if (pendingTarget) {
      const target = pendingTarget;
      pendingTarget = '';
      // Always, even to the current window: a deep link is a visit, and the
      // switch runs select-window, whose hook clears the done pill.
      window.MerlinTerminal.switchSession(target);
      dropTargetParam();
    }
    if (window.MerlinPageTitle) MerlinPageTitle.set('term', MerlinPageTitle.tmuxContext(msg.name, msg.window));
    else document.title = (msg.name || 'term') + (msg.window ? ': ' + msg.window : '');
    setStatus('connected', (msg.name || '') + (msg.window ? ' : ' + msg.window : ''));
    if (window.SessionsBoard && window.SessionsBoard.setCurrentSession) window.SessionsBoard.setCurrentSession(currentSession);
    document.dispatchEvent(new CustomEvent('merlin:terminal-window', {
      detail: { session: currentSession, windowId: currentWindowId, window: msg.window || '' },
    }));
  }

  function refreshBoard() {
    if (window.SessionsBoard && window.SessionsBoard.refresh) window.SessionsBoard.refresh();
  }

  // -- the engine ---------------------------------------------------------------------

  // The last text a program copied (merlin-clip, OSC 52, tmux's own copy):
  // what Ctrl+Shift+C copies when nothing is selected, and what a paste
  // falls back to when the browser's clipboard gives nothing.
  let lastClip = null;
  let input = null;

  const engine = new MerlinNativeTerminal({
    container,
    wsUrl: () => {
      attempted = MerlinSessionIdentity.read();
      confirmed = false;
      return MerlinSessionIdentity.websocketUrl(location, attempted).replace('/ws/terminal', '/ws/terminal2');
    },
    onOpen: () => {
      reconnectDelay = 1000;
      setStatus('connected', 'Connected');
      sendVisibility();
      // A (re)connected page may have missed frames: voice mode asks for its
      // session's state again.
      document.dispatchEvent(new CustomEvent('merlin:terminal-socket'));
    },
    onControl: (msg) => {
      switch (msg.type) {
        case 'pong': onPong(); break;
        case 'session': onSession(msg); break;
        case 'sessions-changed': refreshBoard(); break;
        // A background transcription (the 202 path) was lost after the page
        // was told it was accepted.
        case 'voice_error': showVoiceRefused(msg.message); break;
        case 'voice': if (window.MerlinVoice) window.MerlinVoice.onFrame(msg); break;
        case 'paste_refused': flash(msg.reason || 'Paste refused', ''); break;
        case 'unsupported': {
          stopped = true;
          const why = 'The new terminal needs tmux ' + msg.minimum + ' or newer (this server runs '
            + (msg.version || 'an unknown version') + '): this is the classic terminal.';
          if (Choice) Choice.fallBack(why);
          else setStatus('disconnected', why);
          break;
        }
        default: break;
      }
    },
    onClipboard: (text) => programCopied(text),
    onStuck: (stuck) => { jump.hidden = stuck; },
    // Another pane (of this window or another): what the field holds was
    // typed for the old one.
    onPane: () => { if (input) input.invalidate(); },
    onClose: (e) => {
      cancelProbe();
      if (e.code === 4401) { stopped = true; setStatus('disconnected', 'Unauthorized: reload the page'); return; }
      MerlinSessionIdentity.clearAfterClose(attempted, confirmed, e.code);
      if (stopped) return;
      setStatus('disconnected', 'Disconnected');
      scheduleReconnect();
    },
  });

  // -- input ----------------------------------------------------------------------------

  function syncClipboard(text) {
    if (text) engine.send({ type: 'clipboard_sync', text });
  }

  // What the input field and the toolbar send. A paste also becomes what
  // `merlin-clip paste` reads in the container.
  const sink = {
    input: (data) => engine.input(data),
    key: (name) => engine.key(name),
    paste: (text) => {
      if (!text) return;
      engine.paste(text);
      syncClipboard(text);
      flash('Pasted!', 'copied');
    },
  };

  const modifiers = new MerlinNativeInput.Modifiers();
  function hasSelection() {
    const sel = getSelection();
    return !!(sel && !sel.isCollapsed && sel.rangeCount && sel.getRangeAt(0).intersectsNode(container));
  }
  function writeClipboard(text, ok, failed) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, failed);
    } else {
      failed();
    }
  }
  input = new MerlinNativeInput(field, sink, {
    modifiers,
    hasSelection,
    cursorRect: () => {
      const c = container.querySelector('.nt-cursor');
      return c && c.isConnected ? c.getBoundingClientRect() : null;
    },
    // Ctrl+Shift+C: the selection, else the last text a program copied.
    copy: () => {
      const text = engine.selectionText() || lastClip;
      if (!text) return;
      syncClipboard(text);
      writeClipboard(text, () => flash('Copied!', 'copied'), () => flash('Copied (server only)', 'copied'));
    },
    onPasteFiles: (files) => uploadAndInjectFiles(files),
  });
  // Text the page types for the user (a path, a cd, a transcription): the same
  // boundary as a key, so the field never keeps text the terminal moved past.
  const typed = input.keys();
  function typeText(text) { if (text) typed.input(text); }
  function pasteText(text) { input.sendKey({ paste: text }); }

  const toolbar = new MerlinNativeToolbar(toolbarEl, input.keys(), {
    modifiers,
    focus: () => input.focus(),
    // The Sessions panel's create-and-switch path, else the F2 binding.
    newWindow: (btn) => {
      input.invalidate();
      const board = window.SessionsBoard;
      if (board && board.openNewWindow && currentSession) {
        btn.disabled = true;
        Promise.resolve(board.openNewWindow(currentSession)).then((result) => {
          if (!result || !result.window_id) flash('Could not open window', '');
        }, () => flash('Could not open window', '')).finally(() => { btn.disabled = false; input.focus(); });
      } else {
        engine.key('F2');
      }
    },
    onWindowKey: () => setTimeout(refreshBoard, 300),
  });

  // ?suggestions=1 (or 0) turns the keyboard's suggestions on (or off) for
  // this browser; so does the terminal menu.
  const wanted = new URLSearchParams(location.search).get('suggestions');
  if (wanted === '1' || wanted === '0') input.setSuggestions(wanted === '1');
  const suggestionsItem = $('term-menu-suggestions');
  if (suggestionsItem) {
    const paintSuggestions = () => suggestionsItem.setAttribute('aria-checked', String(!!input.suggestions));
    paintSuggestions();
    suggestionsItem.addEventListener('click', () => { input.setSuggestions(!input.suggestions); paintSuggestions(); });
  }

  // The keyboard toolbar, shown by default, toggled from the status bar.
  toolbarToggle.classList.add('active');
  toolbarToggle.addEventListener('click', () => {
    toolbarEl.classList.toggle('visible');
    toolbarToggle.classList.toggle('active', toolbarEl.classList.contains('visible'));
  });

  // The keyboard stays as it was: the button takes no focus, and on a touch
  // screen the field gets it back only if it had it.
  let jumpFromField = false;
  jump.addEventListener('touchstart', () => { jumpFromField = document.activeElement === field; }, { passive: true });
  jump.addEventListener('mousedown', (e) => e.preventDefault());
  jump.addEventListener('click', () => { engine.toBottom(); if (!coarse || jumpFromField) input.focus(); });

  // tmux sizes a window after its latest active client, and what this page
  // sends does not count as activity for a control client (only keys typed at
  // a terminal do): the page says so itself, on any gesture of the user.
  let lastActive = 0;
  function reportActive() {
    const now = Date.now();
    if (now - lastActive < ACTIVE_EVERY_MS) return;
    lastActive = now;
    engine.send({ type: 'active' });
  }
  for (const name of ['keydown', 'beforeinput', 'pointerdown', 'touchstart', 'wheel']) {
    document.addEventListener(name, reportActive, { capture: true, passive: true });
  }
  // A tap or a click focuses the input field, unless it ends a selection or
  // hits a link.
  container.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const sel = getSelection();
    if (sel && !sel.isCollapsed) return;
    input.focus();
  });
  field.addEventListener('focus', () => container.classList.add('nt-focused'));
  field.addEventListener('blur', () => container.classList.remove('nt-focused'));

  // -- copy ----------------------------------------------------------------------------

  // A program copied text (merlin-clip copy, OSC 52, tmux's own copy). On a
  // touch device the clipboard needs a gesture, so the Copy pill offers one
  // and `merlin-clip paste` gets it at once; on a desktop the recent key or
  // click covers the write (docs/merlin-cli/clipboard.md).
  let toastTimer = null;
  // Set while the pill offers the selection made under a held finger, kept
  // here because a tap on the pill may clear the selection before its click.
  let offeredSelection = null;
  function hideCopyToast() { copyToast.hidden = true; clearTimeout(toastTimer); offeredSelection = null; }
  function programCopied(text) {
    if (!text) return;
    lastClip = text;
    offeredSelection = null;
    if (isTouchDevice) {
      syncClipboard(text);
      copyToast.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(hideCopyToast, 3000);
      return;
    }
    // On success no sync: NeoVim checks its clipboard cache with a paste
    // right after a yank, and a sync would race it.
    writeClipboard(text, () => flash('Copied!', 'copied'), () => {
      syncClipboard(text);
      flash('Copied: Ctrl+Shift+C for the system clipboard', 'copied');
    });
  }
  copyToast.addEventListener('mousedown', (e) => e.preventDefault());
  copyToast.addEventListener('click', (e) => {
    e.stopPropagation();
    const selection = offeredSelection;
    const text = selection || lastClip;
    if (text) {
      syncClipboard(text);
      writeClipboard(text, () => flash('Copied!', 'copied'), () => {});
    }
    if (selection) getSelection().removeAllRanges();
    hideCopyToast();
    input.focus();
  });

  // -- paste ---------------------------------------------------------------------------

  // Why the last paste went the way it did, for a phone with no console.
  let lastPasteResult = null;

  // The paste button and a right-click: the clipboard ladder
  // (clipboard-core.js). Keyboard paste needs none of it: the field gets the
  // browser's own paste event (input.js).
  async function handlePaste() {
    const result = await MerlinClipboard.readClipboard();
    lastPasteResult = result;
    if (result.kind === 'image') {
      const blob = result.blob;
      const file = blob.name ? blob : new File([blob], 'clipboard.png', { type: blob.type });
      await uploadAndInjectFiles([file]);
    } else if (result.kind === 'text') {
      pasteText(result.text);
    } else if (lastClip) {
      // Nothing from the browser: the last text a program copied, usually
      // what the user copied a moment ago anyway.
      pasteText(lastClip);
    } else {
      console.warn('[merlin] paste failed:', result.reason, '-', result.detail);
      flash(MerlinClipboard.statusFor(result.reason), '');
    }
    input.focus();
  }
  $('paste-btn').addEventListener('click', (e) => { e.preventDefault(); handlePaste(); });
  // On a touch screen the field lies over the button (the template): a tap
  // lands on the field. A long press raises the system's menu and no click.
  field.addEventListener('click', () => {
    if (coarse && toolbarEl.classList.contains('visible')) handlePaste();
  });

  // A right-click with a selection is the browser's menu (Copy); without one
  // it pastes, as in the classic terminal. Not on a touch screen, where a
  // long press is the platform's selection.
  container.addEventListener('contextmenu', (e) => {
    if (coarse || hasSelection()) return;
    e.preventDefault();
    handlePaste();
  });

  // -- uploads -------------------------------------------------------------------------

  const uploadBtn = $('upload-btn');
  const uploadFileInput = $('upload-file-input');

  async function uploadFile(blob, filename) {
    const form = new FormData();
    form.append('file', blob, filename || 'file');
    const resp = await fetch('/api/terminal/upload-file', { method: 'POST', body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `Upload failed: ${resp.status}`);
    }
    const { path } = await resp.json();
    return path;
  }

  // Upload files in parallel and type the paths that made it, in order,
  // separated by spaces (server paths are shell-safe, no quoting).
  async function uploadAndInjectFiles(files) {
    if (!files.length) return;
    uploadBtn.classList.add('uploading');
    flash(files.length > 1 ? `Uploading ${files.length} files...` : 'Uploading...', 'uploading');
    try {
      const results = await Promise.allSettled(files.map((f) => uploadFile(f, f.name || 'clipboard.png')));
      const paths = results.filter((r) => r.status === 'fulfilled').map((r) => r.value);
      const errors = results.filter((r) => r.status === 'rejected').map((r) => r.reason);
      if (paths.length) typeText(paths.join(' '));
      if (errors.length) {
        flash(paths.length ? `${errors.length} of ${files.length} uploads failed` : 'Upload failed', '');
        notice('File upload error: ' + errors.map((err) => err.message).join('; '), 'error');
      } else {
        flash(paths.length > 1 ? `${paths.length} files ready` : 'File ready', 'copied');
      }
    } finally {
      uploadBtn.classList.remove('uploading');
      input.focus();
    }
  }

  container.addEventListener('dragover', (e) => { e.preventDefault(); container.classList.add('drag-over'); });
  container.addEventListener('dragleave', (e) => {
    if (!container.contains(e.relatedTarget)) container.classList.remove('drag-over');
  });
  container.addEventListener('drop', (e) => {
    e.preventDefault();
    container.classList.remove('drag-over');
    uploadAndInjectFiles(Array.from((e.dataTransfer && e.dataTransfer.files) || []));
  });
  uploadBtn.addEventListener('click', () => uploadFileInput.click());
  uploadFileInput.addEventListener('change', () => {
    const files = Array.from(uploadFileInput.files);
    uploadFileInput.value = '';
    uploadAndInjectFiles(files);
  });

  // -- cd and commits ----------------------------------------------------------------

  function shellQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

  $('commits-btn').addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      const resp = await fetch('/api/terminal/cwd');
      if (!resp.ok) throw new Error('Failed to get CWD');
      const cwd = await resp.json();
      if (cwd.is_git_repo && cwd.repo_root) {
        window.location.href = '/commits?repo=' + encodeURIComponent(cwd.repo_root);
        return;
      }
      flash('Not a git repo', '');
    } catch (err) {
      flash('Not a git repo', '');
    }
    input.focus();
  });

  const cdBtn = $('cd-btn');
  if (cdBtn && window.FolderPicker) {
    FolderPicker.init({
      homeDir: config.homeDir || '',
      selectMode: 'folder',
      onSelect: (path) => {
        typeText('cd ' + shellQuote(path) + '\r');
        input.focus();
      },
    });
    cdBtn.addEventListener('click', async (e) => {
      e.preventDefault();
      let start;
      try {
        const resp = await fetch('/api/terminal/cwd');
        if (resp.ok) start = (await resp.json()).cwd;
      } catch (err) { /* homeDir then */ }
      FolderPicker.open(start);
    });
  }

  // -- the seam ------------------------------------------------------------------------

  function target() { return (currentSession && currentWindowId) ? currentSession + ':' + currentWindowId : ''; }

  window.MerlinTerminal = {
    // Per client: the switch rides the socket, the bridge answers with a
    // session frame.
    switchSession: (t) => {
      engine.send({ type: 'switch', target: t });
      input.focus();
    },
    currentSession: () => currentSession,
    currentWindow: () => currentWindowId,
    lastPasteResult: () => lastPasteResult,
    notice: showVoiceRefused,
    focus: () => input.focus(),
    currentTarget: target,
    probeStats: () => Object.assign({}, probeStats),
  };
  window.MerlinNativeTerminalPage = { engine, input, toolbar, modifiers, current: () => current, notice };

  // A tapped push notification reaches an open page as a message from the
  // service worker (notifications/static/sw.js).
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (e) => {
      const d = e.data || {};
      if (d.type !== 'deep-link') return;
      if (d.target && /^[^:]+:@\d+$/.test(d.target)) window.MerlinTerminal.switchSession(d.target);
    });
  }

  // -- voice input (microphone, transcription, the terminal) --------------------------

  const micBtn = $('mic-btn');
  const micLang = $('mic-lang');
  const autoEnterBtn = $('auto-enter-btn');
  let mediaRecorder = null;
  let audioChunks = [];
  let voiceRetryPending = false;
  const voiceAvailable = !!config.voiceAvailable;
  let voiceWarnShown = false;
  if (!voiceAvailable) micBtn.classList.add('voice-unavailable');

  const MAX_AUDIO_SIZE = 25 * 1024 * 1024;
  const RETRY_BACKOFF = [1000, 2000];
  const VOICE_DB_KEY = 'pending_voice';

  function stored(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* not kept */ } }
  const savedLang = stored('mic-lang');
  if (savedLang) micLang.value = savedLang;
  micLang.addEventListener('change', () => store('mic-lang', micLang.value));
  if (stored('auto-enter') === 'true') autoEnterBtn.classList.add('active');
  autoEnterBtn.addEventListener('click', () => {
    autoEnterBtn.classList.toggle('active');
    store('auto-enter', autoEnterBtn.classList.contains('active') ? 'true' : 'false');
  });

  function showUploadDone() {
    micBtn.classList.add('upload-done');
    setTimeout(() => micBtn.classList.remove('upload-done'), 500);
  }

  // IndexedDB: the recording survives a network failure or a reload (the
  // classic page's store, so a recording left by either page is retried).
  function openVoiceDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open('merlin-voice', 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('audio')) db.createObjectStore('audio');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function saveVoiceToDB(blob, language, autoEnter, t) {
    const db = await openVoiceDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('audio', 'readwrite');
      tx.objectStore('audio').put({ blob, language, autoEnter, target: t, timestamp: Date.now() }, VOICE_DB_KEY);
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }
  async function voiceDB(mode, op) {
    try {
      const db = await openVoiceDB();
      return await new Promise((resolve) => {
        const tx = db.transaction('audio', mode);
        const req = op(tx.objectStore('audio'));
        tx.oncomplete = () => { db.close(); resolve(req ? req.result : undefined); };
        tx.onerror = () => { db.close(); resolve(null); };
      });
    } catch (e) { return null; }
  }
  const loadVoiceFromDB = async () => (await voiceDB('readonly', (s) => s.get(VOICE_DB_KEY))) || null;
  const deleteVoiceFromDB = () => voiceDB('readwrite', (s) => { s.delete(VOICE_DB_KEY); return null; });
  const hasVoiceInDB = async () => ((await voiceDB('readonly', (s) => s.count(VOICE_DB_KEY))) || 0) > 0;

  const micUploadHooks = {
    progress: (fraction) => micBtn.style.setProperty('--upload-progress', fraction.toString()),
    done: () => micBtn.style.removeProperty('--upload-progress'),
    refused: (message) => showVoiceRefused(message),
  };

  // 202: the server types the text into the target window. 200: the text
  // comes back and is typed here. 402 / 422: a final refusal.
  function uploadVoice(blob, language, autoEnter, t, hooks) {
    return new Promise((resolve, reject) => {
      const ext = blob.type.includes('mp4') ? '.mp4' : blob.type.includes('wav') ? '.wav' : '.webm';
      const formData = new FormData();
      formData.append('file', blob, 'recording' + ext);
      formData.append('language', language);
      formData.append('auto_enter', autoEnter ? 'true' : 'false');
      if (t) formData.append('target', t);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/terminal/transcribe');
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) hooks.progress(e.loaded / e.total); };
      xhr.onload = () => {
        hooks.done();
        if (xhr.status === 202) {
          resolve('accepted');
        } else if (xhr.status === 200) {
          try {
            const data = JSON.parse(xhr.responseText);
            if (data.text) {
              // Enter as a separate key after a short gap: Claude Code reads
              // a CR sent with the text as a newline inside the paste.
              typeText(data.text);
              if (autoEnter) setTimeout(() => typeText('\r'), 150);
            } else if (data.error) {
              notice('Voice error: ' + data.error, 'error');
            }
          } catch (e) {
            notice('Voice error: bad response', 'error');
          }
          resolve('fallback');
        } else if (xhr.status === 402 || xhr.status === 422) {
          let message = '';
          try { message = JSON.parse(xhr.responseText).error; } catch (e) { /* keep default */ }
          hooks.refused(message);
          resolve('refused');
        } else {
          reject(new Error(`server ${xhr.status}`));
        }
      };
      xhr.onerror = () => { hooks.done(); reject(new Error('network error')); };
      xhr.send(formData);
    });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function sendVoiceWithRetry(blob, language, autoEnter, t) {
    micUploadHooks.progress(0);
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await uploadVoice(blob, language, autoEnter, t, micUploadHooks);
      } catch (err) {
        if (attempt < 2) { micUploadHooks.progress(0); await sleep(RETRY_BACKOFF[attempt]); }
      }
    }
    micUploadHooks.done();
    return 'failed';
  }
  async function uploadWithRetry(blob, language, autoEnter, t) {
    micBtn.classList.add('uploading');
    const outcome = await sendVoiceWithRetry(blob, language, autoEnter, t);
    micBtn.classList.remove('uploading');
    if (outcome === 'failed') { enterRetryState(); input.focus(); return; }
    await deleteVoiceFromDB();
    if (outcome !== 'refused') showUploadDone();
    input.focus();
  }

  function enterRetryState() {
    voiceRetryPending = true;
    micBtn.classList.add('retry-pending');
    micBtn.title = 'Voice upload failed: tap to retry';
  }
  function exitRetryState() {
    voiceRetryPending = false;
    micBtn.classList.remove('retry-pending');
    micBtn.title = 'Voice input (tap to record)';
  }
  async function retryFromDB() {
    const pending = await loadVoiceFromDB();
    exitRetryState();
    if (!pending) return;
    // The stored target, never the window current at retry time.
    await uploadWithRetry(pending.blob, pending.language, pending.autoEnter, pending.target || '');
  }
  (async () => { if (await hasVoiceInDB()) enterRetryState(); })();

  micBtn.addEventListener('click', async () => {
    if (!voiceAvailable) {
      if (!voiceWarnShown) {
        notice('Voice unavailable: set OPENAI_API_KEY or MERLIN_SAAS_TOKEN in .env', 'warn');
        voiceWarnShown = true;
      }
      micBtn.classList.remove('voice-unavailable');
      input.focus();
      return;
    }
    if (micBtn.classList.contains('uploading')) return;
    if (voiceRetryPending) { retryFromDB(); return; }
    if (mediaRecorder && mediaRecorder.state === 'recording') { mediaRecorder.stop(); return; }
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        notice('Voice requires HTTPS: the mic API is unavailable over HTTP', 'error');
        return;
      }
      // Safari refuses the mic while the page's audio session says
      // "playback": give it back to the default first.
      if (navigator.audioSession && navigator.audioSession.type === 'playback') {
        try { navigator.audioSession.type = 'auto'; } catch (e) { /* not settable */ }
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunks = [];
      const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/wav']
        .find((t) => MediaRecorder.isTypeSupported(t)) || '';
      mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        micBtn.classList.remove('recording');
        // The window the recording stopped in, now, before any await.
        const t = target();
        const blob = new Blob(audioChunks, { type: mimeType || 'audio/webm' });
        if (blob.size > MAX_AUDIO_SIZE) { notice('Voice error: recording too large (>25 MB)', 'error'); input.focus(); return; }
        if (blob.size === 0) { showVoiceRefused('Voice recording was empty, try again'); input.focus(); return; }
        const language = micLang.value;
        const autoEnter = autoEnterBtn.classList.contains('active');
        if (voiceRetryPending) exitRetryState();
        try { await saveVoiceToDB(blob, language, autoEnter, t); } catch (e) { /* no persistence */ }
        await uploadWithRetry(blob, language, autoEnter, t);
      };
      mediaRecorder.start();
      micBtn.classList.add('recording');
    } catch (err) {
      if (err.name === 'NotAllowedError') notice('Microphone permission denied', 'error');
      else notice('Mic error: ' + err.message, 'error');
    }
  });

  // -- the connection: reconnect, and after a suspension -------------------------------

  let reconnectDelay = 1000;
  const MAX_RECONNECT_DELAY = 30000;
  let reconnectTimer = null;
  function scheduleReconnect() {
    if (reconnectTimer) return;
    setStatus('connecting', `Reconnecting in ${Math.round(reconnectDelay / 1000)}s...`);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }
  function connect() {
    setStatus('connecting', 'Connecting...');
    engine.connect();
  }

  // A phone suspends an installed app and kills its sockets without telling
  // the page. Back after a short absence, a ping probes the socket (replaced
  // when no pong comes back); after a long one, it is replaced at once.
  const SHORT_ABSENCE_MS = 2000;
  const LONG_ABSENCE_MS = 60000;
  const PONG_TIMEOUT_MS = 3000;
  let hiddenAt = 0;
  let pongTimer = null;
  const probeStats = { probes: 0, pongs: 0, replaced: 0 };
  const open = () => engine.ws && engine.ws.readyState === WebSocket.OPEN;
  function cancelProbe() { if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; } }
  function replaceSocket(why) {
    probeStats.replaced++;
    cancelProbe();
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    reconnectDelay = 1000;
    console.info('[merlin] replacing terminal socket: ' + why);
    connect();
  }
  function probeSocket() {
    if (!open()) return;
    const probed = engine.ws;
    probeStats.probes++;
    cancelProbe();
    pongTimer = setTimeout(() => {
      pongTimer = null;
      if (engine.ws !== probed) return;
      replaceSocket('no pong');
    }, PONG_TIMEOUT_MS);
    try { probed.send(JSON.stringify({ type: 'ping' })); } catch (e) { if (engine.ws === probed) replaceSocket('send failed'); }
  }
  function onPong() { probeStats.pongs++; cancelProbe(); }
  // Visible, focused and displaying a window: the page is looking at it, and
  // the server silences its notifications.
  function sendVisibility() {
    if (open()) engine.send({ type: 'visibility', visible: !document.hidden, focused: document.hasFocus() });
  }
  document.addEventListener('visibilitychange', sendVisibility);
  window.addEventListener('focus', sendVisibility);
  window.addEventListener('blur', sendVisibility);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (!hiddenAt) return;
    const away = Date.now() - hiddenAt;
    hiddenAt = 0;
    if (!open()) return;
    if (away >= LONG_ABSENCE_MS) replaceSocket('away ' + Math.round(away / 1000) + 's');
    else if (away >= SHORT_ABSENCE_MS) probeSocket();
  });
  window.addEventListener('pageshow', (e) => { if (e.persisted) replaceSocket('bfcache'); });

  // Follow the software keyboard: the page column is sized to the visual
  // viewport whenever the keyboard leaves less than the layout viewport, and
  // the scroll is pinned at the top (as on the classic page).
  (function followKeyboard() {
    const vv = window.visualViewport;
    const main = document.querySelector('.main');
    if (!vv || !main || !coarse) return;
    // A finger held still on the terminal while the keyboard is up. iOS only
    // selects inside the focused field, and does not follow a finger already
    // down when the field lets go: the page selects itself, the word under
    // the finger, extended by dragging, with the Copy pill when it lifts. The
    // keyboard leaves, and the layout waits for the selection to be over, so
    // the text stays under the finger.
    const HOLD_MS = 320;
    const HOLD_SLOP = 10;
    const PILL_GRACE_MS = 400;
    let held = false;
    let fingerDown = false;
    let holdTimer = null;
    let selecting = false;
    function caretAt(x, y) {
      let node = null, offset = 0;
      if (document.caretRangeFromPoint) {
        const r = document.caretRangeFromPoint(x, y);
        if (r) { node = r.startContainer; offset = r.startOffset; }
      } else if (document.caretPositionFromPoint) {
        const p = document.caretPositionFromPoint(x, y);
        if (p) { node = p.offsetNode; offset = p.offset; }
      }
      return node && container.contains(node) ? { node, offset } : null;
    }
    function selectWordAt(x, y) {
      const at = caretAt(x, y);
      if (!at) return;
      const sel = getSelection();
      sel.collapse(at.node, at.offset);
      // One character in first: a caret on the word's first letter would
      // otherwise go back to the word before.
      sel.modify('move', 'forward', 'character');
      sel.modify('move', 'backward', 'word');
      sel.modify('extend', 'forward', 'word');
      if (!sel.isCollapsed) return;
      // No word here (a blank): the row.
      const el = at.node.nodeType === 1 ? at.node : at.node.parentElement;
      const row = el && el.closest('.r');
      if (row) sel.selectAllChildren(row);
    }
    function releaseLayout() {
      if (!held || fingerDown || hasSelection()) return;
      held = false;
      apply();
    }
    container.addEventListener('touchstart', (e) => {
      clearTimeout(holdTimer);
      holdTimer = null;
      if (document.activeElement !== field || e.touches.length !== 1 || e.target.closest('a')) return;
      fingerDown = true;
      const { clientX: x, clientY: y } = e.touches[0];
      holdTimer = setTimeout(() => {
        holdTimer = null;
        held = true;
        selecting = true;
        field.blur();
        selectWordAt(x, y);
        // The drag selects: the view must not start scrolling.
        engine.scroller.style.overflow = 'hidden';
      }, HOLD_MS);
      // On the node itself: output may redraw it out of the document.
      const moved = (m) => {
        const t = m.touches[0];
        if (!t) return;
        if (selecting) {
          const at = caretAt(t.clientX, t.clientY);
          if (at) getSelection().extend(at.node, at.offset);
        } else if (Math.hypot(t.clientX - x, t.clientY - y) > HOLD_SLOP) {
          clearTimeout(holdTimer);
          holdTimer = null;
        }
      };
      const ended = () => {
        clearTimeout(holdTimer);
        holdTimer = null;
        fingerDown = false;
        e.target.removeEventListener('touchmove', moved);
        e.target.removeEventListener('touchend', ended);
        e.target.removeEventListener('touchcancel', ended);
        if (selecting) {
          selecting = false;
          engine.scroller.style.overflow = '';
          const text = hasSelection() ? engine.selectionText() : null;
          if (text) {
            offeredSelection = text;
            clearTimeout(toastTimer);
            copyToast.hidden = false;
          }
        }
        releaseLayout();
      };
      e.target.addEventListener('touchmove', moved, { passive: true });
      e.target.addEventListener('touchend', ended, { passive: true });
      e.target.addEventListener('touchcancel', ended, { passive: true });
    }, { passive: true });
    document.addEventListener('selectionchange', () => {
      if (offeredSelection !== null) {
        if (hasSelection()) offeredSelection = engine.selectionText() || offeredSelection;
        else setTimeout(() => { if (!hasSelection()) hideCopyToast(); }, PILL_GRACE_MS);
      }
      releaseLayout();
    });
    function apply() {
      if (held || Math.abs(vv.scale - 1) > 0.01) return;
      const free = Math.round(vv.height);
      const opened = free < window.innerHeight - 1;
      engine.keyboard = opened;
      main.style.height = opened ? free + 'px' : '';
      document.documentElement.classList.toggle('keyboard-open', opened);
      if (window.scrollY || vv.offsetTop) window.scrollTo(0, 0);
    }
    vv.addEventListener('resize', apply);
    vv.addEventListener('scroll', apply);
    // A backgrounded app loses its keyboard while suspended: leaving drops
    // the focus, and returning measures again.
    function remeasure() { apply(); requestAnimationFrame(apply); setTimeout(apply, 300); }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) { remeasure(); return; }
      const el = document.activeElement;
      if (el && el !== document.body && el.blur) el.blur();
    });
    window.addEventListener('pageshow', remeasure);
    window.addEventListener('focus', remeasure);
    apply();
  })();

  // -- the clipboard permission icon (the paste button's read) -------------------------

  const clipboardPermBtn = $('clipboard-perm');
  const clipboardPermBadge = $('clipboard-perm-badge');
  function updateClipboardIcon(state) {
    if (state === 'granted') {
      clipboardPermBtn.classList.remove('visible', 'denied');
    } else if (state === 'denied') {
      clipboardPermBtn.classList.add('visible', 'denied');
      clipboardPermBtn.title = 'Clipboard blocked: enable it in the browser settings';
      clipboardPermBadge.textContent = '✕';
    } else {
      clipboardPermBtn.classList.add('visible');
      clipboardPermBtn.classList.remove('denied');
      clipboardPermBtn.title = 'Click to enable clipboard';
      clipboardPermBadge.textContent = '?';
    }
  }
  (async () => {
    try {
      const status = await navigator.permissions.query({ name: 'clipboard-read' });
      updateClipboardIcon(status.state);
      status.addEventListener('change', () => updateClipboardIcon(status.state));
    } catch (e) {
      // Safari and Firefox have no clipboard-read permission: every read
      // needs a gesture, which the paste button gives. No icon.
    }
  })();
  clipboardPermBtn.addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      await navigator.clipboard.readText();
      updateClipboardIcon('granted');
      flash('Clipboard enabled!', 'copied');
    } catch (err) {
      updateClipboardIcon('denied');
      flash('Clipboard blocked', '');
    }
    input.focus();
  });

  connect();
  if (!coarse) input.focus();
})();
