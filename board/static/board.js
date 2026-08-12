/* Session switcher — the content of the terminal's Sessions panel.
   Vanilla JS, IIFE. window.SessionsBoard.init({container, onAttention, onJump,
   onClose}) builds its own shell into `container`, polls /api/board, and renders
   tmux's session -> window tree with an agent-activity overlay. Tapping a session
   switches this client to it; tapping a window switches + jumps to it. Rename and
   close per session and per window; create a session from a directory. Switching
   is per-client and rides the terminal WebSocket (window.MerlinTerminal), so one
   browser tab never moves another. The server owns order (tmux's own); we never
   reorder by state. */
window.SessionsBoard = (function () {
  'use strict';

  var DOT = { idle: '○', busy: '◐', done: '●' };
  var POLL_MS = 2000;

  var S = { root: null, list: null, status: null, filter: null, newBtn: null,
            sessions: [], counts: { sessions: 0, waiting: 0, working: 0 },
            current: '', query: '', lastSig: null, paused: false,
            onAttention: function () {}, onJump: function () {}, onClose: null };

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
  var IC_COLLAPSE = svg('<path d="m9 18 6-6-6-6"/>');
  var IC_PLUS = svg('<path d="M12 5v14"/><path d="M5 12h14"/>');
  // Layers glyph: marks the session tier (windows carry a state dot instead).
  var IC_SESSION = svg('<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>');

  function isDesktop() { return window.matchMedia('(min-width: 769px)').matches; }

  // Inline arm-then-confirm for destructive actions (no native dialog). First
  // tap arms (turns red, becomes a check); a second tap within the window acts;
  // anything else (timeout / another arm) disarms.
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
    return JSON.stringify({ s: v.sessions, c: v.counts, cur: v.current_session });
  }

  // --- status line -------------------------------------------------------
  function renderStatus() {
    var c = S.counts;
    var st = S.status;
    st.textContent = '';
    st.appendChild(document.createTextNode('sessions · '));
    st.appendChild(el('span', 'st-total', String(c.sessions || 0)));
    if (c.working) {
      st.appendChild(document.createTextNode(' · '));
      st.appendChild(el('span', 'st-working', c.working + ' working'));
    }
    if (c.waiting) {
      st.appendChild(document.createTextNode(' · '));
      st.appendChild(el('span', 'st-waiting', c.waiting + ' waiting'));
    }
  }

  // --- inline rename -----------------------------------------------------
  // Swap a label element for an input; commit(save) writes via `onSave(value)`.
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
      S.lastSig = null;  // force a re-render even if a no-op rename left the sig unchanged
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

  // --- a window row ------------------------------------------------------
  function makeWindow(sessionName, w) {
    var row = el('div', 'wrow st-' + (w.state || 'plain'));
    row.setAttribute('data-depth', String(Math.min(w.depth, 3)));
    if (w.active) row.classList.add('active');

    var dot = el('span', 'wrow-dot');
    dot.textContent = w.is_agent ? (DOT[w.state] || DOT.idle) : '·';
    row.appendChild(dot);

    var label = el('div', 'wrow-label');
    var nameEl = el('span', 'wrow-name', w.name || 'window');
    label.appendChild(nameEl);
    if (w.relation === 'child') {
      var rel = el('span', 'wrow-rel', 'child');
      label.appendChild(rel);
    }
    row.appendChild(label);

    var actions = el('div', 'srow-actions');
    var edit = iconBtn(IC_EDIT, 'Rename window');
    edit.addEventListener('click', function (e) {
      e.stopPropagation();
      editInline(nameEl, w.name, w.name, function (val) {
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

    row.addEventListener('click', function () {
      // session:window jumps across sessions in one per-client switch.
      switchTo(sessionName + ':' + w.window_id);
    });
    return row;
  }

  // --- a session group ---------------------------------------------------
  function makeSession(sess) {
    var group = el('div', 'sgroup');
    if (sess.current) group.classList.add('current');

    var head = el('div', 'sgroup-head');
    var icon = el('span', 'sgroup-icon');
    icon.innerHTML = IC_SESSION;
    head.appendChild(icon);
    var nameEl = el('span', 'sgroup-name', sess.name);
    head.appendChild(nameEl);

    var meta = el('span', 'sgroup-meta');
    var bits = [sess.counts.total + (sess.counts.total === 1 ? ' win' : ' wins')];
    if (sess.counts.waiting) bits.push(sess.counts.waiting + ' waiting');
    else if (sess.counts.working) bits.push(sess.counts.working + ' working');
    meta.textContent = bits.join(' · ');
    head.appendChild(meta);

    var actions = el('div', 'srow-actions');
    var edit = iconBtn(IC_EDIT, 'Rename session');
    edit.addEventListener('click', function (e) {
      e.stopPropagation();
      editInline(nameEl, sess.name, sess.name, function (val) {
        return api('/session/rename', { name: sess.name, new: val }).then(function (r) {
          // Renaming the session you're on: keep the "current" highlight by
          // adopting the new name (the WS still knows the old one until a switch).
          if (r && r.name && sess.current) S.current = r.name;
          return r;
        });
      });
    });
    actions.appendChild(edit);
    var kill = iconBtn(IC_KILL, 'Close session', 'srow-btn-danger');
    kill.addEventListener('click', function (e) {
      e.stopPropagation();
      armConfirm(kill, function () {
        api('/session/kill', { name: sess.name }).then(load);
      });
    });
    actions.appendChild(kill);
    head.appendChild(actions);

    head.addEventListener('click', function () { switchTo(sess.name); });
    group.appendChild(head);

    var wins = el('div', 'sgroup-wins');
    sess.windows.forEach(function (w) { wins.appendChild(makeWindow(sess.name, w)); });
    group.appendChild(wins);
    return group;
  }

  function switchTo(target) {
    if (window.MerlinTerminal && window.MerlinTerminal.switchSession) {
      window.MerlinTerminal.switchSession(target);
      S.onJump();
      // The server confirms via a session control frame; also poll twice so the
      // visit-cleared done pill (tmux clears it on window-change) syncs promptly.
      setTimeout(load, 250);
      setTimeout(load, 800);
    }
  }

  // --- filtering (substring over session + window names / projects) ------
  function matchWindow(w, q) {
    return ((w.name || '') + ' ' + (w.project || '')).toLowerCase().indexOf(q) >= 0;
  }
  function filteredSessions() {
    var q = S.query.trim().toLowerCase();
    if (!q) return S.sessions;
    return S.sessions.map(function (s) {
      if (s.name.toLowerCase().indexOf(q) >= 0) return s;  // whole session matches
      var wins = s.windows.filter(function (w) { return matchWindow(w, q); });
      return wins.length ? Object.assign({}, s, { windows: wins }) : null;
    }).filter(Boolean);
  }

  function renderList() {
    var list = S.list;
    list.textContent = '';
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
    renderStatus();
    renderList();
    S.onAttention(v.attention || 0);
  }

  function load() {
    if (S.paused) return Promise.resolve();
    return api('?current=' + encodeURIComponent(S.current)).then(function (v) {
      if (v && sigOf(v) !== S.lastSig) render(v);
    });
  }

  // --- new session from a directory --------------------------------------
  function openNewSession() {
    if (S.list.querySelector('.board-new')) return;
    S.paused = true;
    var wrap = el('div', 'board-new');
    var input = el('input', 'board-new-input');
    input.type = 'text';
    input.placeholder = 'session name…';
    input.setAttribute('autocomplete', 'off');
    wrap.appendChild(input);
    S.list.insertBefore(wrap, S.list.firstChild);
    input.focus();
    var done = false;
    function finish(create) {
      if (done) return;
      done = true;
      S.paused = false;
      var name = input.value.trim();
      if (create && name) {
        // Name only: the server roots the new session at a sensible default dir
        // (a directory path is a power-user detail, not required to make one).
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
  function attachSwipeDown(elm) {
    var y0 = null;
    elm.addEventListener('pointerdown', function (e) { if (!isDesktop()) y0 = e.clientY; });
    elm.addEventListener('pointermove', function (e) {
      if (y0 != null && e.clientY - y0 > 60) { y0 = null; if (S.onClose) S.onClose(); }
    });
    elm.addEventListener('pointerup', function () { y0 = null; });
    elm.addEventListener('pointercancel', function () { y0 = null; });
  }

  function buildShell(root) {
    var head = el('div', 'board-head');
    S.status = el('div', 'board-status');
    head.appendChild(S.status);
    S.newBtn = el('button', 'board-head-btn', null);
    S.newBtn.innerHTML = IC_PLUS;
    S.newBtn.title = 'New session';
    S.newBtn.addEventListener('click', openNewSession);
    head.appendChild(S.newBtn);
    var collapse = el('button', 'board-head-btn board-collapse', null);
    collapse.innerHTML = IC_COLLAPSE;
    collapse.title = 'Hide panel';
    collapse.addEventListener('click', function () { if (S.onClose) S.onClose(); });
    head.appendChild(collapse);
    root.appendChild(head);
    attachSwipeDown(head);

    var fwrap = el('div', 'board-filter-wrap');
    S.filter = el('input', 'board-filter');
    S.filter.type = 'text';
    S.filter.placeholder = 'filter…';
    S.filter.setAttribute('autocomplete', 'off');
    S.filter.addEventListener('input', function () { S.query = S.filter.value; renderList(); });
    S.filter.addEventListener('focus', function () { S.paused = true; });
    S.filter.addEventListener('blur', function () { S.paused = false; });
    fwrap.appendChild(S.filter);
    root.appendChild(fwrap);

    S.list = el('div', 'board-list');
    root.appendChild(S.list);
  }

  function setCurrentSession(name) {
    S.current = name || '';
    S.lastSig = null;  // current changed -> re-render highlight
    load();
  }

  function init(opts) {
    S.root = opts.container;
    S.onAttention = opts.onAttention || function () {};
    S.onJump = opts.onJump || function () {};
    S.onClose = opts.onClose || null;
    buildShell(S.root);
    // If the terminal already learned its session, adopt it before the first poll.
    if (window.MerlinTerminal && window.MerlinTerminal.currentSession) {
      S.current = window.MerlinTerminal.currentSession() || '';
    }
    load();
    setInterval(load, POLL_MS);
  }

  return { init: init, refresh: load, setCurrentSession: setCurrentSession };
})();
