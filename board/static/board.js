/* Sessions board — client. Vanilla JS, IIFE (design-system convention).
   Fetches /api/board, renders stable-position cards, and drives the rename /
   reorder / focus / dismiss mutations. Never reorders cards by state: the server
   owns order and returns it stable, and attention is shown as an in-place glow. */
(function () {
  'use strict';

  var GLYPH = { idle: '○', busy: '◐', done: '●' }; // ○ ◐ ●
  var POLL_MS = 4000;
  var view = null;      // last rendered view model
  var reordering = false;
  var paused = false;   // true while a rename input is open (don't clobber it)

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
    });
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

  // Preorder walk of the tree to a flat list of sids (display order) — used to
  // send a full manual-order list to the server after a reorder.
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

  function renameCard(node, cardEl) {
    var nameEl = cardEl.querySelector('.session-name');
    if (!nameEl || cardEl.querySelector('.session-name-input')) return;
    paused = true;
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
      paused = false;
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

  // Move a top-level (root) session within its project, then persist the whole
  // display order. Children keep first-seen order (reorder is root-level).
  function moveRoot(projectIndex, sid, dir) {
    var arr = view.projects[projectIndex].sessions;
    var i = arr.findIndex(function (n) { return n.sid === sid; });
    var j = i + dir;
    if (i < 0 || j < 0 || j >= arr.length) return;
    var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    render(view); // optimistic
    api('/order', { sids: flatten(view) }).then(load);
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

    var metaBits = [];
    if (node.tombstone) metaBits.push('died while working');
    if (node.relation) metaBits.push(node.relation);
    var meta = el('div', 'session-meta');
    meta.textContent = node.project || '~';
    metaBits.forEach(function (b) {
      meta.appendChild(document.createTextNode(' · '));
      var s = el('span', b === node.relation ? 'session-relation' : null, b);
      meta.appendChild(s);
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
        if (reordering) return;
        api('/focus', { sid: node.sid });
      });
    }
    return card;
  }

  function render(v) {
    view = v;
    var body = document.getElementById('board-body');
    var empty = document.getElementById('board-empty');
    // Clear everything except the empty-state placeholder.
    Array.prototype.slice.call(body.children).forEach(function (c) {
      if (c !== empty) body.removeChild(c);
    });

    var hasSessions = (v.projects || []).some(function (p) { return p.sessions.length; });
    empty.hidden = hasSessions;

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

    // Attention badge + jump-to-next (in place; never reorders the cards).
    var att = document.getElementById('board-attention');
    var next = document.getElementById('board-next');
    if (v.attention > 0) {
      att.textContent = v.attention + ' waiting on you';
      att.hidden = false;
      next.hidden = false;
    } else {
      att.hidden = true;
      next.hidden = true;
    }
  }

  function load() {
    if (paused) return Promise.resolve();
    return api('').then(function (v) { if (v) render(v); });
  }

  function firstWaitingSid(v) {
    var found = null;
    (v.projects || []).forEach(function (p) {
      (p.sessions || []).forEach(function walk(n) {
        if (!found && n.waiting) found = n.sid;
        (n.children || []).forEach(walk);
      });
    });
    return found;
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('board-reorder').addEventListener('click', function () {
      reordering = !reordering;
      this.classList.toggle('active', reordering);
      document.getElementById('board-body').classList.toggle('reordering', reordering);
    });
    document.getElementById('board-next').addEventListener('click', function () {
      if (!view) return;
      var sid = firstWaitingSid(view);
      if (sid) api('/focus', { sid: sid });
    });
    load();
    setInterval(load, POLL_MS);
  });
})();
