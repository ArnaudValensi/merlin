/* Session switcher — the content of the terminal's Sessions panel.
   Vanilla JS, IIFE. window.SessionsBoard.init({container, onAttention, onJump,
   onClose}) builds its own shell into `container`, polls /api/board, and renders
   tmux's session -> window tree with an agent-activity overlay.

   Interaction (Tree-Style-Tab-like): a session is a collapsible group. Click a
   session header to FOLD it; click a window to switch this client to it
   (session:window, per-client, over the terminal WebSocket). A "+" row per
   session opens a new window there; a "+ session" row at the top makes a new
   session. When the panel is dragged narrow it collapses to a rail of dots with
   hover tooltips. The server owns order (tmux's own); we never reorder by
   state. */
window.SessionsBoard = (function () {
  'use strict';

  var DOT = { idle: '○', busy: '◐', ask: '?', done: '●' };
  var POLL_MS = 2000;
  var RAIL_W = 130;           // below this width: rail of dots; above: full view
  var FOLD_KEY = 'board-folded';

  var S = { root: null, list: null, filter: null, fwrap: null,
            sessions: [], counts: { sessions: 0, waiting: 0, working: 0, asking: 0 },
            current: '', query: '', lastSig: null, paused: false,
            folded: loadFolded(),
            onAttention: function () {}, onJump: function () {}, onClose: null };

  function loadFolded() {
    try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || '[]')); }
    catch (e) { return new Set(); }
  }
  function saveFolded() {
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(Array.prototype.slice.call(S.folded))); }
    catch (e) { /* private mode: fold is session-only, fine */ }
  }

  function api(path, body) {
    var opts = { headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch('/api/board' + path, opts).then(function (r) {
      if (r.status === 401) { location.reload(); return null; }
      return r.ok ? r.json() : null;
    }).catch(function () { return null; });
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function svg(paths, opts) {
    opts = opts || {};
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="' +
      (opts.fill || 'none') + '" stroke="' + (opts.stroke || 'currentColor') +
      '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      paths + '</svg>';
  }
  var IC_EDIT = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>');
  var IC_KILL = svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');
  var IC_CHECK = svg('<path d="M20 6 9 17l-5-5"/>');
  var IC_PLUS = svg('<path d="M12 5v14"/><path d="M5 12h14"/>');
  var IC_CHEVRON = svg('<path d="m9 18 6-6-6-6"/>');  // rotates to ▾ when expanded
  var IC_SESSION = svg('<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>');
  var IC_GRIP = svg('<circle cx="9" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="18" r="1"/>', { fill: 'currentColor', stroke: 'none' });

  // Inline arm-then-confirm for destructive actions. First tap arms (red check);
  // a second tap acts; anything else disarms.
  var armed = null;
  function disarm() {
    if (!armed) return;
    if (armed._t) clearTimeout(armed._t);
    armed.classList.remove('armed');
    armed.innerHTML = IC_KILL;
    armed.title = armed._title || 'Close';
    armed = null;
    S.paused = false;
  }
  function armConfirm(btn, act) {
    if (btn === armed) {
      if (btn._t) clearTimeout(btn._t);
      armed = null; S.paused = false; act();
      return;
    }
    disarm();
    btn.classList.add('armed');
    btn.innerHTML = IC_CHECK;
    btn.title = 'Tap again to confirm';
    armed = btn;
    S.paused = true;
    btn._t = setTimeout(disarm, 3000);
  }

  function sigOf(v) {
    return JSON.stringify({ s: v.sessions, cur: v.current_session });
  }

  function badge(n, asking) {
    return el('span', asking ? 'sbadge asking' : 'sbadge', String(n));
  }

  // Rail fly-out: the hover popover to the LEFT of a dot. A body-level element
  // (so the panel's scroll can't clip it), it shows the item's name and — for a
  // session or window — its rename/close actions. This is the rail's management
  // surface: the actions live in the fly-out, never over the dot, so the dot's
  // tap stays a clean switch. Rail is a desktop-narrow mode, so hover is the
  // trigger (a tap also opens it, for a touchscreen desktop).
  var fly = null, flyItem = null, flyHideT = null, flyEditing = false;
  function ensureFly() {
    if (fly) return fly;
    fly = el('div', 'board-flyout');
    fly.addEventListener('mouseenter', function () { clearTimeout(flyHideT); });
    fly.addEventListener('mouseleave', scheduleHideFly);
    document.body.appendChild(fly);
    return fly;
  }
  function showFly(item) {
    if (item === flyItem && fly && fly.style.display !== 'none') { clearTimeout(flyHideT); return; }
    clearTimeout(flyHideT);
    flyItem = item;
    S.paused = true;  // hold the poll so a re-render can't yank the fly-out mid-hover
    var f = ensureFly();
    f.textContent = '';
    var lbl = el('span', 'board-flyout-label', item.getAttribute('data-tooltip') || '');
    f.appendChild(lbl);
    var kind = item.getAttribute('data-fly-kind');
    if (kind === 'session' || kind === 'window') {
      var t = {
        kind: kind,
        session: item.getAttribute('data-fly-session') || '',
        window: item.getAttribute('data-fly-window') || '',
        name: item.getAttribute('data-fly-name') || '',
      };
      var edit = iconBtn(IC_EDIT, 'Rename');
      edit.addEventListener('click', function (e) { e.stopPropagation(); renameInFly(lbl, t); });
      f.appendChild(edit);
      var kill = iconBtn(IC_KILL, 'Close', 'srow-btn-danger');
      kill.addEventListener('click', function (e) { e.stopPropagation(); armConfirm(kill, function () { killFly(t); }); });
      f.appendChild(kill);
    }
    f.style.display = 'flex';
    var r = item.getBoundingClientRect();
    var fr = f.getBoundingClientRect();
    f.style.top = Math.max(4, r.top + r.height / 2 - fr.height / 2) + 'px';
    f.style.left = (r.left - fr.width - 8) + 'px';
  }
  // While renaming, the mouse leaving the fly-out must NOT dismiss it (you'd lose
  // the edit). Only committing, Escape, or a click away (which blurs the input)
  // closes it then.
  function scheduleHideFly() {
    if (flyEditing) return;
    clearTimeout(flyHideT);
    flyHideT = setTimeout(hideFly, 220);
  }
  function hideFly() {
    if (fly) fly.style.display = 'none';
    flyItem = null;
    flyEditing = false;
    disarm();
    S.paused = false;
  }
  function renameInFly(lblEl, t) {
    clearTimeout(flyHideT);
    flyEditing = true;  // pin the fly-out open while the input has focus
    var input = el('input', 'srow-name-input');
    input.value = t.name || '';
    input.placeholder = t.name || '';
    lblEl.replaceWith(input);
    input.focus();
    input.select();
    // The input is wider than the label, so re-anchor the fly-out to the left of
    // the dot again; its right edge stays 8px off the dot so the buttons never
    // get pushed off-screen.
    if (fly && flyItem) {
      var r = flyItem.getBoundingClientRect();
      fly.style.left = (r.left - fly.getBoundingClientRect().width - 8) + 'px';
    }
    var done = false;
    function commit(save) {
      if (done) return;
      done = true;
      var val = input.value.trim();
      var after = function () { hideFly(); load(); };
      if (save && val && t.kind === 'session') {
        api('/session/rename', { name: t.session, new: val }).then(function (r) {
          if (r && r.name && t.session === S.current) S.current = r.name;
          after();
        });
      } else if (save && val) {
        api('/window/rename', { session: t.session, window_id: t.window, name: val }).then(after);
      } else { after(); }
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') commit(true);
      else if (e.key === 'Escape') commit(false);
    });
    input.addEventListener('blur', function () { commit(true); });
    input.addEventListener('click', function (e) { e.stopPropagation(); });
  }
  function killFly(t) {
    var p = t.kind === 'session'
      ? api('/session/kill', { name: t.session })
      : api('/window/kill', { session: t.session, window_id: t.window });
    p.then(function () { hideFly(); load(); });
  }

  // Swap a label for an input; commit(save) writes via onSave(value).
  function editInline(labelEl, initial, placeholder, onSave) {
    if (labelEl.parentNode.querySelector('.srow-name-input')) return;
    S.paused = true;
    var input = el('input', 'srow-name-input');
    input.value = initial || '';
    input.placeholder = placeholder || '';
    labelEl.replaceWith(input);
    input.focus();
    input.select();
    var done = false;
    function commit(save) {
      if (done) return;
      done = true;
      S.paused = false;
      S.lastSig = null;
      if (save && input.value.trim()) onSave(input.value.trim()).then(load);
      else load();
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') commit(true);
      else if (e.key === 'Escape') commit(false);
    });
    input.addEventListener('blur', function () { commit(true); });
    input.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  function iconBtn(icon, title, cls) {
    var b = el('button', 'srow-btn' + (cls ? ' ' + cls : ''), null);
    b.innerHTML = icon;
    b.title = title;
    b._title = title;
    return b;
  }

  function switchTo(target) {
    var idx = target.indexOf(':');
    var sname = idx >= 0 ? target.slice(0, idx) : target;
    var wid = idx >= 0 ? target.slice(idx + 1) : '';
    // Optimistic: reflect the selection in the UI immediately, before tmux
    // confirms over the WebSocket, so a tap feels instant. load() reconciles.
    S.current = sname;
    if (wid) {
      S.sessions.forEach(function (s) {
        if (s.name === sname) {
          s.windows.forEach(function (w) { w.active = w.window_id === wid; });
        }
      });
    }
    S.lastSig = null;
    renderList();
    if (window.MerlinTerminal && window.MerlinTerminal.switchSession) {
      window.MerlinTerminal.switchSession(target);
      S.onJump();
      setTimeout(load, 250);
      setTimeout(load, 800);
    }
  }

  function toggleFold(name) {
    if (S.folded.has(name)) S.folded.delete(name);
    else S.folded.add(name);
    saveFolded();
    renderList();
  }

  // --- a window row ------------------------------------------------------
  function makeWindow(sessionName, w, canReorder) {
    var row = el('div', 'wrow st-' + (w.state || 'plain'));
    row.setAttribute('data-depth', String(Math.min(w.depth, 3)));
    if (w.active) row.classList.add('active');
    row.setAttribute('data-window', w.window_id);  // for drag-reorder
    // Rail fly-out: label + (kind/session/window) so it can offer rename/close.
    row.setAttribute('data-tooltip', sessionName + ' · ' + (w.name || 'window'));
    row.setAttribute('data-fly-kind', 'window');
    row.setAttribute('data-fly-session', sessionName);
    row.setAttribute('data-fly-window', w.window_id);
    row.setAttribute('data-fly-name', w.name || 'window');

    var dot = el('span', 'wrow-dot');
    dot.textContent = w.is_agent ? (DOT[w.state] || DOT.idle) : '·';
    row.appendChild(dot);

    var label = el('div', 'wrow-label');
    label.appendChild(el('span', 'wrow-name', w.name || 'window'));
    if (w.relation === 'child') label.appendChild(el('span', 'wrow-rel', 'child'));
    row.appendChild(label);

    var actions = el('div', 'srow-actions');
    var edit = iconBtn(IC_EDIT, 'Rename window');
    edit.addEventListener('click', function (e) {
      e.stopPropagation();
      editInline(row.querySelector('.wrow-name'), w.name, w.name, function (val) {
        return api('/window/rename', { session: sessionName, window_id: w.window_id, name: val });
      });
    });
    actions.appendChild(edit);
    var kill = iconBtn(IC_KILL, 'Close window', 'srow-btn-danger');
    kill.addEventListener('click', function (e) {
      e.stopPropagation();
      armConfirm(kill, function () {
        api('/window/kill', { session: sessionName, window_id: w.window_id }).then(load);
      });
    });
    actions.appendChild(kill);
    row.appendChild(actions);
    // Drag handle: always visible on the right (full view), rename/close overlay
    // to its left on hover. Only when there's more than one window to reorder. A
    // click on it must not switch. The rail hides it and drags the whole dot.
    if (canReorder) {
      row.classList.add('has-grip');
      var grip = el('span', 'srow-grip');
      grip.innerHTML = IC_GRIP;
      grip.title = 'Drag to reorder';
      grip.addEventListener('click', function (e) { e.stopPropagation(); });
      row.appendChild(grip);
    }
    row.addEventListener('click', function () { switchTo(sessionName + ':' + w.window_id); });
    return row;
  }

  // A "+ add" button: the shared markup/style for both "new session" (top of
  // the list) and "new window" (foot of a session), so they match exactly.
  function makeAdd(label, extraCls, onClick) {
    var row = el('div', 'board-add' + (extraCls ? ' ' + extraCls : ''));
    row.setAttribute('data-tooltip', label);
    var icon = el('span', 'board-add-icon');
    icon.innerHTML = IC_PLUS;
    row.appendChild(icon);
    row.appendChild(el('span', 'board-add-label', label));
    row.addEventListener('click', onClick);
    return row;
  }

  // The "+ new window" row at the foot of a session (Tree-Style-Tab "New Tab").
  function openNewWindow(sessionName) {
    if (!sessionName) return Promise.resolve(null);
    return api('/window/new', { name: sessionName }).then(function (r) {
      if (r && r.window_id) switchTo(sessionName + ':' + r.window_id);
      else load();
      return r;
    });
  }

  function makeNewWindow(sessionName) {
    return makeAdd('new window', 'board-add-sub', function () {
      openNewWindow(sessionName);
    });
  }

  // --- a session group ---------------------------------------------------
  function makeSession(sess) {
    var group = el('div', 'sgroup');
    // Use the client's own current session (optimistic) for the highlight, so a
    // switch lands instantly rather than waiting for the server's echo.
    if (sess.name === S.current) group.classList.add('current');
    var folded = S.folded.has(sess.name);
    if (folded) group.classList.add('folded');

    var head = el('div', 'sgroup-head');
    head.setAttribute('data-tooltip', sess.name);
    head.setAttribute('data-fly-kind', 'session');
    head.setAttribute('data-fly-session', sess.name);
    head.setAttribute('data-fly-name', sess.name);
    var caret = el('span', 'sgroup-caret');
    caret.innerHTML = IC_CHEVRON;
    head.appendChild(caret);
    var icon = el('span', 'sgroup-icon');
    icon.innerHTML = IC_SESSION;
    head.appendChild(icon);
    var nameEl = el('span', 'sgroup-name', sess.name);
    head.appendChild(nameEl);
    // Both states want you: 'asking' blocks a live turn, 'waiting' is unread.
    var wants = (sess.counts.waiting || 0) + (sess.counts.asking || 0);
    if (wants) head.appendChild(badge(wants, sess.counts.asking > 0));

    var actions = el('div', 'srow-actions');
    var edit = iconBtn(IC_EDIT, 'Rename session');
    edit.addEventListener('click', function (e) {
      e.stopPropagation();
      editInline(head.querySelector('.sgroup-name'), sess.name, sess.name, function (val) {
        return api('/session/rename', { name: sess.name, new: val }).then(function (r) {
          if (r && r.name && sess.current) S.current = r.name;
          return r;
        });
      });
    });
    actions.appendChild(edit);
    var kill = iconBtn(IC_KILL, 'Close session', 'srow-btn-danger');
    kill.addEventListener('click', function (e) {
      e.stopPropagation();
      armConfirm(kill, function () { api('/session/kill', { name: sess.name }).then(load); });
    });
    actions.appendChild(kill);
    head.appendChild(actions);

    // Click the header to fold/unfold; switching happens at the window level.
    head.addEventListener('click', function () { toggleFold(sess.name); });
    group.appendChild(head);

    if (!folded) {
      var wins = el('div', 'sgroup-wins');
      wins.setAttribute('data-session', sess.name);  // drag-reorder target
      var canReorder = sess.windows.length > 1;
      sess.windows.forEach(function (w) { wins.appendChild(makeWindow(sess.name, w, canReorder)); });
      wins.appendChild(makeNewWindow(sess.name));
      group.appendChild(wins);
    }
    return group;
  }

  // The "+ new session" row at the top of the list.
  function makeNewSession() {
    return makeAdd('new session', '', function (e) { openNewSession(e.currentTarget); });
  }

  // --- filtering (substring over session + window names / projects) ------
  function matchWindow(w, q) {
    return ((w.name || '') + ' ' + (w.project || '')).toLowerCase().indexOf(q) >= 0;
  }
  function filteredSessions() {
    var q = S.query.trim().toLowerCase();
    if (!q) return S.sessions;
    return S.sessions.map(function (s) {
      if (s.name.toLowerCase().indexOf(q) >= 0) return s;
      var wins = s.windows.filter(function (w) { return matchWindow(w, q); });
      return wins.length ? Object.assign({}, s, { windows: wins }) : null;
    }).filter(Boolean);
  }

  function renderList() {
    var list = S.list;
    list.textContent = '';
    list.appendChild(makeNewSession());
    var sessions = filteredSessions();
    if (!sessions.length) {
      list.appendChild(el('div', 'board-empty', S.query ? 'no match' : '$ no sessions'));
      setupSortables();
      return;
    }
    sessions.forEach(function (s) { list.appendChild(makeSession(s)); });
    setupSortables();
  }

  // Drag-to-reorder windows within a session, applied to the real tmux order
  // (POST /window/reorder -> swap-window). One Sortable per session's window
  // list. Full view drags by the grip handle; the rail has no room for a grip,
  // so the whole dot drags (a small delay keeps a tap-to-switch from starting a
  // drag). Disabled while filtering (a filtered subset has no real order).
  function setupSortables() {
    (S.sortables || []).forEach(function (s) { try { s.destroy(); } catch (e) {} });
    S.sortables = [];
    if (typeof Sortable === 'undefined' || S.query.trim()) return;
    var rail = S.root.classList.contains('rail');
    Array.prototype.forEach.call(S.list.querySelectorAll('.sgroup-wins'), function (wins) {
      var opts = {
        draggable: '.wrow',           // window rows only; the +new-window is a .board-add
        animation: 120,
        ghostClass: 'wrow-ghost',
        onStart: function () { S.paused = true; hideFly(); },
        onEnd: function () {
          var session = wins.getAttribute('data-session');
          var order = Array.prototype.map.call(
            wins.querySelectorAll('.wrow'), function (r) { return r.getAttribute('data-window'); });
          api('/window/reorder', { session: session, order: order }).then(function () {
            S.paused = false;
            S.lastSig = null;
            load();
          });
        },
      };
      if (rail) { opts.delay = 130; opts.delayOnTouchOnly = false; }
      else { opts.handle = '.srow-grip'; }
      S.sortables.push(Sortable.create(wins, opts));
    });
  }

  function render(v) {
    S.sessions = v.sessions || [];
    S.counts = v.counts || { sessions: 0, waiting: 0, working: 0, asking: 0 };
    if (v.current_session) S.current = v.current_session;
    S.lastSig = sigOf(v);
    renderList();
    // Second arg: how many of those are blocked on a question. The button is
    // the only signal visible with the panel closed, so it needs to tell an
    // unanswered dialog apart from a merely unread finish.
    S.onAttention(v.attention || 0, S.counts.asking || 0);
  }

  function load() {
    if (S.paused) return Promise.resolve();
    return api('?current=' + encodeURIComponent(S.current)).then(function (v) {
      if (v && sigOf(v) !== S.lastSig) render(v);
    });
  }

  // --- new session from a name -------------------------------------------
  function openNewSession(afterRow) {
    if (S.list.querySelector('.board-new')) return;
    S.paused = true;
    var wrap = el('div', 'board-new');
    var input = el('input', 'board-new-input');
    input.type = 'text';
    input.placeholder = 'session name…';
    input.setAttribute('autocomplete', 'off');
    wrap.appendChild(input);
    if (afterRow && afterRow.nextSibling) S.list.insertBefore(wrap, afterRow.nextSibling);
    else S.list.appendChild(wrap);
    input.focus();
    var done = false;
    function finish(create) {
      if (done) return;
      done = true;
      S.paused = false;
      var name = input.value.trim();
      if (create && name) {
        api('/session/new', { name: name }).then(function (r) {
          if (r && r.name) switchTo(r.name);
          else load();
        });
      } else { load(); }
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') finish(true);
      else if (e.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', function () { finish(true); });
  }

  // --- shell -------------------------------------------------------------
  function isDesktop() { return window.matchMedia('(min-width: 769px)').matches; }

  function attachSwipeDown(elm) {
    var y0 = null;
    elm.addEventListener('pointerdown', function (e) { if (!isDesktop()) y0 = e.clientY; });
    elm.addEventListener('pointermove', function (e) {
      if (y0 != null && e.clientY - y0 > 60) { y0 = null; if (S.onClose) S.onClose(); }
    });
    elm.addEventListener('pointerup', function () { y0 = null; });
    elm.addEventListener('pointercancel', function () { y0 = null; });
  }

  // Two tiers: the full view (dense on desktop), and — below RAIL_W — a rail of
  // dots with instant hover tooltips.
  function applyMode() {
    var w = S.root.clientWidth;
    if (!w) return;
    var rail = w < RAIL_W;
    if (rail !== S.root.classList.contains('rail')) {
      S.root.classList.toggle('rail', rail);
      setupSortables();  // full drags by the grip; the rail drags the whole dot
    }
  }

  function buildShell(root) {
    S.fwrap = el('div', 'board-filter-wrap');
    S.filter = el('input', 'board-filter');
    S.filter.type = 'text';
    S.filter.placeholder = 'filter…';
    S.filter.setAttribute('autocomplete', 'off');
    S.filter.addEventListener('input', function () { S.query = S.filter.value; renderList(); });
    S.filter.addEventListener('focus', function () { S.paused = true; });
    S.filter.addEventListener('blur', function () { S.paused = false; });
    S.fwrap.appendChild(S.filter);
    root.appendChild(S.fwrap);
    attachSwipeDown(S.fwrap);

    S.list = el('div', 'board-list');
    root.appendChild(S.list);

    // Rail fly-out (delegated). Only in rail mode; wider modes show names inline.
    S.list.addEventListener('mouseover', function (e) {
      if (!S.root.classList.contains('rail')) return;
      var item = e.target.closest('[data-tooltip]');
      if (item) showFly(item);
    });
    S.list.addEventListener('mouseleave', scheduleHideFly);
    S.list.addEventListener('click', function (e) {
      if (!S.root.classList.contains('rail')) return;
      var item = e.target.closest('[data-tooltip]');
      if (item) showFly(item);  // touchscreen desktop: a tap opens the fly-out too
    });
    S.list.addEventListener('scroll', hideFly);
    // A click anywhere else dismisses an open fly-out (e.g. after a touch tap).
    document.addEventListener('click', function (e) {
      if (fly && fly.style.display !== 'none' &&
          !fly.contains(e.target) && !e.target.closest('[data-tooltip]')) hideFly();
    });

    if (window.ResizeObserver) {
      new ResizeObserver(function () { hideFly(); applyMode(); }).observe(root);
    } else {
      window.addEventListener('resize', applyMode);
    }
  }

  function setCurrentSession(name) {
    S.current = name || '';
    S.lastSig = null;
    load();
  }

  function init(opts) {
    S.root = opts.container;
    S.onAttention = opts.onAttention || function () {};
    S.onJump = opts.onJump || function () {};
    S.onClose = opts.onClose || null;
    buildShell(S.root);
    applyMode();
    if (window.MerlinTerminal && window.MerlinTerminal.currentSession) {
      S.current = window.MerlinTerminal.currentSession() || '';
    }
    load();
    setInterval(load, POLL_MS);
  }

  return {
    init: init,
    refresh: load,
    setCurrentSession: setCurrentSession,
    openNewWindow: openNewWindow,
  };
})();
