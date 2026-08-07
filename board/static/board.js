/* Sessions board — the content of the terminal's Sessions drawer.
   Vanilla JS, IIFE. Exposes window.SessionsBoard.init({container, onAttention,
   onJump}): it builds its own shell inside `container`, polls /api/board, renders
   stable-position cards, and drives rename / reorder / focus / dismiss. Never
   reorders cards by state — the server owns order; attention is reported to the
   host (the toolbar badge) and shown as an in-place glow, never movement. */
window.SessionsBoard = (function () {
  'use strict';

  var GLYPH = { idle: '○', busy: '◐', done: '●' };
  var POLL_MS = 4000;

  var S = { root: null, body: null, att: null, next: null, reorderBtn: null,
            view: null, reordering: false, paused: false,
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
  function icon(paths) {
    return '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round">' + paths + '</svg>';
  }
  var IC_EDIT = icon('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>');
  var IC_UP = icon('<path d="m18 15-6-6-6 6"/>');
  var IC_DOWN = icon('<path d="m6 9 6 6 6-6"/>');
  var IC_X = icon('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');

  function flatten(v) {
    var out = [];
    (v.projects || []).forEach(function (p) {
      (p.sessions || []).forEach(function walk(n) {
        out.push(n.sid);
        (n.children || []).forEach(walk);
      });
    });
    return out;
  }
  function firstWaiting(v) {
    var found = null;
    (v.projects || []).forEach(function (p) {
      (p.sessions || []).forEach(function walk(n) {
        if (!found && n.waiting) found = n.sid;
        (n.children || []).forEach(walk);
      });
    });
    return found;
  }

  function renameCard(node, cardEl) {
    var nameEl = cardEl.querySelector('.session-name');
    if (!nameEl || cardEl.querySelector('.session-name-input')) return;
    S.paused = true;
    var input = el('input', 'session-name-input');
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

  function moveRoot(projectIndex, sid, dir) {
    var arr = S.view.projects[projectIndex].sessions;
    var i = arr.findIndex(function (n) { return n.sid === sid; });
    var j = i + dir;
    if (i < 0 || j < 0 || j >= arr.length) return;
    var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    render(S.view);
    api('/order', { sids: flatten(S.view) }).then(load);
  }

  function makeCard(node, projectIndex) {
    var card = el('div', 'session-card state-' + node.state);
    card.setAttribute('data-depth', String(Math.min(node.depth, 3)));
    if (node.waiting) card.classList.add('waiting');
    if (node.active) card.classList.add('active');
    if (node.tombstone) card.classList.add('tombstone');

    card.appendChild(Object.assign(el('span', 'session-glyph'), {
      textContent: node.tombstone ? '✕' : (GLYPH[node.state] || GLYPH.idle),
    }));

    var body = el('div', 'session-body');
    var nameRow = el('div', 'session-name-row');
    nameRow.appendChild(el('span', 'session-name', node.name));
    nameRow.appendChild(el('span', 'session-short-id', node.short_id));
    body.appendChild(nameRow);

    var meta = el('div', 'session-meta');
    meta.textContent = node.project || '~';
    var bits = [];
    if (node.tombstone) bits.push(['died while working', false]);
    if (node.relation) bits.push([node.relation, true]);
    bits.forEach(function (b) {
      meta.appendChild(document.createTextNode(' · '));
      meta.appendChild(el('span', b[1] ? 'session-relation' : null, b[0]));
    });
    body.appendChild(meta);
    card.appendChild(body);

    var actions = el('div', 'session-actions');
    if (node.tombstone) {
      var dismiss = el('button', 'card-btn', null);
      dismiss.innerHTML = IC_X;
      dismiss.title = 'Dismiss';
      dismiss.addEventListener('click', function (e) {
        e.stopPropagation();
        api('/dismiss', { sid: node.sid }).then(load);
      });
      actions.appendChild(dismiss);
    } else {
      var edit = el('button', 'card-btn', null);
      edit.innerHTML = IC_EDIT;
      edit.title = 'Rename';
      edit.addEventListener('click', function (e) { e.stopPropagation(); renameCard(node, card); });
      actions.appendChild(edit);
      if (node.depth === 0) {
        var up = el('button', 'card-btn reorder-btn', null);
        up.innerHTML = IC_UP; up.title = 'Move up';
        up.addEventListener('click', function (e) { e.stopPropagation(); moveRoot(projectIndex, node.sid, -1); });
        var down = el('button', 'card-btn reorder-btn', null);
        down.innerHTML = IC_DOWN; down.title = 'Move down';
        down.addEventListener('click', function (e) { e.stopPropagation(); moveRoot(projectIndex, node.sid, 1); });
        actions.appendChild(up);
        actions.appendChild(down);
      }
    }
    card.appendChild(actions);

    if (!node.tombstone) {
      card.addEventListener('click', function () {
        if (S.reordering) return;
        api('/focus', { sid: node.sid }).then(function (r) { if (r) S.onJump(); });
      });
    }
    return card;
  }

  function render(v) {
    S.view = v;
    var body = S.body;
    body.textContent = '';

    var hasSessions = (v.projects || []).some(function (p) { return p.sessions.length; });
    if (!hasSessions) {
      var empty = el('div', 'empty-state');
      empty.appendChild(el('p', null, 'No agent sessions yet.'));
      empty.appendChild(el('p', 'empty-hint',
        'Run claude in a terminal window and it appears here.'));
      body.appendChild(empty);
    }

    v.projects.forEach(function (p, pi) {
      var group = el('div', 'board-project');
      var head = el('div', 'board-project-head');
      head.appendChild(el('span', 'board-project-name', p.project || '~'));
      if (p.cwd) head.appendChild(el('span', 'board-project-path', p.cwd));
      group.appendChild(head);
      p.sessions.forEach(function (n) {
        (function place(node) {
          group.appendChild(makeCard(node, pi));
          (node.children || []).forEach(place);
        })(n);
      });
      body.appendChild(group);
    });

    if (v.other_windows && v.other_windows.length) {
      var det = el('details', 'board-other');
      det.appendChild(el('summary', null, 'Other windows (' + v.other_windows.length + ')'));
      v.other_windows.forEach(function (w) {
        var row = el('div', 'other-window' + (w.active ? ' active' : ''));
        row.appendChild(el('span', null, w.name || '(shell)'));
        row.appendChild(el('span', 'ow-session', w.session));
        det.appendChild(row);
      });
      body.appendChild(det);
    }

    if (v.attention > 0) {
      S.att.textContent = v.attention + ' waiting';
      S.att.hidden = false;
      S.next.hidden = false;
    } else {
      S.att.hidden = true;
      S.next.hidden = true;
    }
    S.onAttention(v.attention || 0);
  }

  function load() {
    if (S.paused) return Promise.resolve();
    return api('').then(function (v) { if (v) render(v); });
  }

  function buildShell(root) {
    root.classList.add('board-body-wrap');
    var header = el('div', 'board-header');
    var title = el('div', 'board-title');
    title.appendChild(el('h2', null, 'Sessions'));
    S.att = el('span', 'board-attention');
    S.att.hidden = true;
    title.appendChild(S.att);
    header.appendChild(title);

    var toolbar = el('div', 'board-toolbar');
    S.next = el('button', 'btn-icon', null);
    S.next.innerHTML = icon('<path d="m6 9 6 6 6-6"/>');
    S.next.title = 'Jump to next waiting';
    S.next.hidden = true;
    S.next.addEventListener('click', function () {
      if (!S.view) return;
      var sid = firstWaiting(S.view);
      if (sid) api('/focus', { sid: sid }).then(function (r) { if (r) S.onJump(); });
    });
    S.reorderBtn = el('button', 'btn-icon board-reorder-toggle', null);
    S.reorderBtn.innerHTML = icon('<path d="m18 8-4-4-4 4"/><path d="M14 4v8"/><path d="m6 16 4 4 4-4"/><path d="M10 20v-8"/>');
    S.reorderBtn.title = 'Reorder sessions';
    S.reorderBtn.addEventListener('click', function () {
      S.reordering = !S.reordering;
      S.reorderBtn.classList.toggle('active', S.reordering);
      S.body.classList.toggle('reordering', S.reordering);
    });
    toolbar.appendChild(S.next);
    toolbar.appendChild(S.reorderBtn);
    if (S.onClose) {
      var close = el('button', 'btn-icon', null);
      close.innerHTML = icon('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');
      close.title = 'Close';
      close.addEventListener('click', function () { S.onClose(); });
      toolbar.appendChild(close);
    }
    header.appendChild(toolbar);

    S.body = el('div', 'board-body');
    root.appendChild(header);
    root.appendChild(S.body);
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
