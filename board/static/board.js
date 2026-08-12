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

  var DOT = { idle: '○', busy: '◐', done: '●' };
  var POLL_MS = 2000;
  var RAIL_W = 130;           // below this width: rail of dots; above: full view
  var FOLD_KEY = 'board-folded';

  var S = { root: null, list: null, filter: null, fwrap: null,
            sessions: [], counts: { sessions: 0, waiting: 0, working: 0 },
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

  function badge(n) { return el('span', 'sbadge', String(n)); }

  // Instant hover tooltip for the rail. A single body-level element (so the
  // panel's scroll container can't clip it), positioned to the left of the
  // hovered item — the panel hugs the right screen edge.
  var tip = null;
  function showTip(target) {
    var text = target.getAttribute('data-tooltip');
    if (!text) return;
    if (!tip) { tip = el('div', 'board-tip'); document.body.appendChild(tip); }
    tip.textContent = text;
    tip.style.display = 'block';
    var r = target.getBoundingClientRect();
    var tr = tip.getBoundingClientRect();
    tip.style.top = (r.top + r.height / 2 - tr.height / 2) + 'px';
    tip.style.left = (r.left - tr.width - 8) + 'px';
  }
  function hideTip() { if (tip) tip.style.display = 'none'; }

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
  function makeWindow(sessionName, w) {
    var row = el('div', 'wrow st-' + (w.state || 'plain'));
    row.setAttribute('data-depth', String(Math.min(w.depth, 3)));
    if (w.active) row.classList.add('active');
    // Instant hover tooltip (sidebar-style ::after) for the rail/compact modes.
    row.setAttribute('data-tooltip', sessionName + ' · ' + (w.name || 'window'));

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

    row.addEventListener('click', function () { switchTo(sessionName + ':' + w.window_id); });
    return row;
  }

  // The "+ new window" row at the foot of a session (Tree-Style-Tab "New Tab").
  function makeNewWindow(sessionName) {
    var row = el('div', 'wrow wrow-add');
    row.setAttribute('data-tooltip', 'New window in ' + sessionName);
    var dot = el('span', 'wrow-dot');
    dot.innerHTML = IC_PLUS;
    row.appendChild(dot);
    row.appendChild(el('span', 'wrow-name wrow-add-label', 'new window'));
    row.addEventListener('click', function () {
      api('/window/new', { name: sessionName }).then(function (r) {
        if (r && r.window_id) switchTo(sessionName + ':' + r.window_id);
        else load();
      });
    });
    return row;
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
    var caret = el('span', 'sgroup-caret');
    caret.innerHTML = IC_CHEVRON;
    head.appendChild(caret);
    var icon = el('span', 'sgroup-icon');
    icon.innerHTML = IC_SESSION;
    head.appendChild(icon);
    var nameEl = el('span', 'sgroup-name', sess.name);
    head.appendChild(nameEl);
    if (sess.counts.waiting) head.appendChild(badge(sess.counts.waiting));

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
      sess.windows.forEach(function (w) { wins.appendChild(makeWindow(sess.name, w)); });
      wins.appendChild(makeNewWindow(sess.name));
      group.appendChild(wins);
    }
    return group;
  }

  // The "+ new session" row at the top of the list.
  function makeNewSession() {
    var row = el('div', 'board-add');
    row.setAttribute('data-tooltip', 'New session');
    var icon = el('span', 'board-add-icon');
    icon.innerHTML = IC_PLUS;
    row.appendChild(icon);
    row.appendChild(el('span', 'board-add-label', 'new session'));
    row.addEventListener('click', function () { openNewSession(row); });
    return row;
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
      return;
    }
    sessions.forEach(function (s) { list.appendChild(makeSession(s)); });
  }

  function render(v) {
    S.sessions = v.sessions || [];
    S.counts = v.counts || { sessions: 0, waiting: 0, working: 0 };
    if (v.current_session) S.current = v.current_session;
    S.lastSig = sigOf(v);
    renderList();
    S.onAttention(v.attention || 0);
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
    S.root.classList.toggle('rail', w < RAIL_W);
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

    // Rail hover tooltips (delegated). Only in rail mode; wider modes show names.
    S.list.addEventListener('mouseover', function (e) {
      if (!S.root.classList.contains('rail')) return;
      var t = e.target.closest('[data-tooltip]');
      if (t) showTip(t);
    });
    S.list.addEventListener('mouseout', function (e) {
      if (e.target.closest('[data-tooltip]')) hideTip();
    });
    S.list.addEventListener('scroll', hideTip);

    if (window.ResizeObserver) {
      new ResizeObserver(function () { hideTip(); applyMode(); }).observe(root);
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

  return { init: init, refresh: load, setCurrentSession: setCurrentSession };
})();
