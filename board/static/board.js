/* Sessions board — the content of the terminal's Sessions panel.
   Vanilla JS, IIFE. window.SessionsBoard.init({container, onAttention, onJump,
   onClose}) builds its own shell into `container`, polls /api/board, and renders
   a flat, dense, nav-like list of agent instances. Filter (fuse.js), drag-to-
   reorder (SortableJS, by the grip), rename and close per row. Never reorders by
   state — the server owns order; the current window is marked with a caret. */
window.SessionsBoard = (function () {
  'use strict';

  var DOT = { idle: '○', busy: '◐', done: '●' };
  var POLL_MS = 4000;

  var S = { root: null, list: null, status: null, filter: null,
            rows: [], counts: { total: 0, working: 0, waiting: 0 },
            query: '', lastSig: null, paused: false, sortable: null,
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
  var IC_KILL = svg('<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>');
  var IC_DISMISS = svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');
  var IC_COLLAPSE = svg('<path d="m9 18 6-6-6-6"/>');
  var IC_GRIP = svg('<circle cx="9" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="18" r="1"/>', { fill: 'currentColor', stroke: 'none' });

  function sigOf(v) {
    return JSON.stringify({ s: v.sessions, c: v.counts });
  }

  // --- status line -------------------------------------------------------
  function renderStatus() {
    var c = S.counts;
    var st = S.status;
    st.textContent = '';
    st.appendChild(document.createTextNode('sessions · '));
    st.appendChild(el('span', 'st-total', String(c.total)));
    if (c.working) {
      st.appendChild(document.createTextNode(' · '));
      st.appendChild(el('span', 'st-working', c.working + ' working'));
    }
    if (c.waiting) {
      st.appendChild(document.createTextNode(' · '));
      st.appendChild(el('span', 'st-waiting', c.waiting + ' waiting'));
    }
  }

  // --- one row -----------------------------------------------------------
  function renameRow(node, rowEl) {
    var nameEl = rowEl.querySelector('.srow-name');
    if (!nameEl || rowEl.querySelector('.srow-name-input')) return;
    S.paused = true;
    var input = el('input', 'srow-name-input');
    input.value = node.custom_name || '';
    input.placeholder = node.auto_name;
    nameEl.replaceWith(input);
    input.focus();
    input.select();
    var done = false;
    function commit(save) {
      if (done) return;
      done = true;
      S.paused = false;
      if (save) api('/name', { sid: node.sid, name: input.value }).then(load);
      else load();
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') commit(true);
      else if (e.key === 'Escape') commit(false);
    });
    input.addEventListener('blur', function () { commit(true); });
    input.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  function makeRow(node) {
    var row = el('div', 'srow st-' + node.state);
    row.setAttribute('data-sid', node.sid);
    row.setAttribute('data-depth', String(Math.min(node.depth, 3)));
    if (node.active) row.classList.add('current');
    if (node.tombstone) row.classList.add('tombstone');

    var grip = el('span', 'srow-grip');
    grip.innerHTML = IC_GRIP;
    row.appendChild(grip);

    row.appendChild(el('span', 'srow-caret', '▸'));
    row.appendChild(Object.assign(el('span', 'srow-dot'), {
      textContent: node.tombstone ? '✕' : (DOT[node.state] || DOT.idle),
    }));

    var label = el('div', 'srow-label');
    label.appendChild(el('span', 'srow-name', node.name));
    var metaBits = [node.project || '~'];
    var meta = el('span', 'srow-meta');
    meta.textContent = metaBits[0];
    if (node.relation === 'child') {
      meta.appendChild(document.createTextNode(' · '));
      meta.appendChild(el('span', 'srow-rel', 'child'));
    }
    if (node.tombstone) {
      meta.appendChild(document.createTextNode(' · died'));
    }
    label.appendChild(meta);
    row.appendChild(label);

    var actions = el('div', 'srow-actions');
    if (node.tombstone) {
      var dismiss = el('button', 'srow-btn', null);
      dismiss.innerHTML = IC_DISMISS;
      dismiss.title = 'Dismiss';
      dismiss.addEventListener('click', function (e) {
        e.stopPropagation();
        api('/dismiss', { sid: node.sid }).then(load);
      });
      actions.appendChild(dismiss);
    } else {
      var edit = el('button', 'srow-btn', null);
      edit.innerHTML = IC_EDIT;
      edit.title = 'Rename';
      edit.addEventListener('click', function (e) { e.stopPropagation(); renameRow(node, row); });
      actions.appendChild(edit);
      var kill = el('button', 'srow-btn srow-btn-danger', null);
      kill.innerHTML = IC_KILL;
      kill.title = 'Close session';
      kill.addEventListener('click', function (e) {
        e.stopPropagation();
        var label2 = node.custom_name || node.auto_name;
        if (window.confirm('Close session "' + label2 + '"? Its agent window will be killed.')) {
          api('/kill', { sid: node.sid }).then(load);
        }
      });
      actions.appendChild(kill);
    }
    row.appendChild(actions);

    if (!node.tombstone) {
      row.addEventListener('click', function () {
        api('/focus', { sid: node.sid }).then(function (r) { if (r) S.onJump(); });
      });
    }
    return row;
  }

  // --- list (with filter + drag) -----------------------------------------
  function filtered() {
    var q = S.query.trim();
    if (!q || typeof Fuse === 'undefined') {
      if (!q) return S.rows;
    }
    if (typeof Fuse === 'undefined') {
      return S.rows.filter(function (n) {
        return (n.name + ' ' + (n.project || '')).toLowerCase().indexOf(q.toLowerCase()) >= 0;
      });
    }
    var fuse = new Fuse(S.rows, { keys: ['name', 'project'], threshold: 0.4, ignoreLocation: true });
    return fuse.search(q).map(function (r) { return r.item; });
  }

  function renderList() {
    var list = S.list;
    if (S.sortable) { S.sortable.destroy(); S.sortable = null; }
    list.textContent = '';
    var rows = filtered();
    if (!rows.length) {
      list.appendChild(el('div', 'board-empty',
        S.query ? 'no match' : '$ no agents running'));
      return;
    }
    rows.forEach(function (n) { list.appendChild(makeRow(n)); });

    // Drag-to-reorder by the grip. Disabled while filtering (a filtered subset
    // has no meaningful global order). Persists the full new order on drop.
    if (!S.query && typeof Sortable !== 'undefined') {
      S.sortable = Sortable.create(list, {
        handle: '.srow-grip',
        animation: 120,
        ghostClass: 'srow-ghost',
        onStart: function () { S.paused = true; list.classList.add('sorting'); },
        onEnd: function () {
          list.classList.remove('sorting');
          var order = Array.prototype.map.call(
            list.querySelectorAll('.srow'), function (r) { return r.getAttribute('data-sid'); });
          api('/order', { sids: order }).then(function () { S.paused = false; load(); });
        },
      });
    }
  }

  function render(v) {
    S.rows = v.sessions || [];
    S.counts = v.counts || { total: 0, working: 0, waiting: 0 };
    S.lastSig = sigOf(v);
    renderStatus();
    renderList();
    S.onAttention(v.attention || 0);
  }

  function load() {
    if (S.paused) return Promise.resolve();
    return api('').then(function (v) {
      if (v && sigOf(v) !== S.lastSig) render(v);
    });
  }

  // --- shell -------------------------------------------------------------
  function buildShell(root) {
    var head = el('div', 'board-head');
    S.status = el('div', 'board-status');
    head.appendChild(S.status);
    var collapse = el('button', 'board-head-btn', null);
    collapse.innerHTML = IC_COLLAPSE;
    collapse.title = 'Hide panel';
    collapse.addEventListener('click', function () { if (S.onClose) S.onClose(); });
    head.appendChild(collapse);
    root.appendChild(head);

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

  function init(opts) {
    S.root = opts.container;
    S.onAttention = opts.onAttention || function () {};
    S.onJump = opts.onJump || function () {};
    S.onClose = opts.onClose || null;
    buildShell(S.root);
    load();
    setInterval(load, POLL_MS);
  }

  return { init: init, refresh: load };
})();
