/* Notifications: the terminal page's client for attention events.
   Vanilla JS, IIFE. window.MerlinNotifications.init({bell, dot, popover})
   owns the bell button in the terminal status bar, the popover it opens, the
   in-tab Notification rule, the app badge and the title count.

   Events come from the sessions panel's poll (board.js hands them to
   handleEvents). Nothing here asks for a permission on its own: the only
   Notification.requestPermission() call is inside the toggle's change handler,
   which is a tap. */
window.MerlinNotifications = (function () {
  'use strict';

  var PREF_KEY = 'notify-in-browser';   // per-browser preference, decision 13
  var ICON = '/static/favicon.svg';

  var S = { bell: null, dot: null, pop: null, toggle: null, toggleRow: null,
            status: null, pushSubscribed: false, open: false, attention: 0 };

  // --- capability -------------------------------------------------------
  function hasApi() { return typeof Notification !== 'undefined' && !!Notification; }
  function secure() { return window.isSecureContext === true; }
  function permission() { return hasApi() ? Notification.permission : 'unsupported'; }
  function prefOn() {
    try { return localStorage.getItem(PREF_KEY) === '1'; } catch (e) { return false; }
  }
  function setPref(on) {
    try { localStorage.setItem(PREF_KEY, on ? '1' : '0'); } catch (e) { /* private mode */ }
  }
  function enabled() { return prefOn() && secure() && permission() === 'granted'; }

  // --- the in-tab rule (decision 5) --------------------------------------
  function currentTarget() {
    var t = window.MerlinTerminal;
    if (!t || !t.currentSession || !t.currentWindow) return '';
    var s = t.currentSession(), w = t.currentWindow();
    return s && w ? s + ':' + w : '';
  }

  function titleOf(ev) {
    return (ev.project || ev.session || '') + ' · ' + (ev.window_name || 'window');
  }
  function bodyOf(ev) { return ev.state === 'ask' ? 'Needs an answer' : 'Finished'; }

  function show(ev) {
    var n;
    try {
      n = new Notification(titleOf(ev), {
        body: bodyOf(ev), tag: ev.sid || ev.target, icon: ICON, data: { target: ev.target },
      });
    } catch (e) {
      // Some browsers only show notifications from a service worker (Android
      // Chrome). Nothing to do here without one.
      return;
    }
    n.onclick = function () {
      try { window.focus(); } catch (e) { /* not allowed, fine */ }
      if (window.MerlinTerminal && window.MerlinTerminal.switchSession) {
        window.MerlinTerminal.switchSession(ev.target);
      }
      try { n.close(); } catch (e) { /* already gone */ }
    };
  }

  // Called by board.js with the events of one poll. Skips an event for the
  // window this client is looking at while the page is visible.
  function handleEvents(events) {
    if (!enabled()) return;
    var visible = document.visibilityState === 'visible';
    var here = currentTarget();
    (events || []).forEach(function (ev) {
      if (!ev || !ev.target) return;
      if (visible && here && ev.target === here) return;
      show(ev);
    });
  }

  // --- badge and title (decision 6) ---------------------------------------
  function setAttention(n) {
    n = Number(n) || 0;
    S.attention = n;
    if (window.MerlinPageTitle && window.MerlinPageTitle.setCount) window.MerlinPageTitle.setCount(n);
    // The Badging API is capability-detected: absent on Firefox, present on
    // Chromium and Safari 17. Its promise rejects without permission on some
    // browsers, which is not an error worth surfacing.
    try {
      if (n > 0 && navigator.setAppBadge) navigator.setAppBadge(n).catch(function () {});
      else if (n === 0 && navigator.clearAppBadge) navigator.clearAppBadge().catch(function () {});
    } catch (e) { /* ignore */ }
  }

  // --- the popover (decision 13) ------------------------------------------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function reason() {
    if (!hasApi()) return { ok: false, text: 'This browser has no notification support.' };
    if (!secure()) return { ok: false, text: 'Browser notifications need HTTPS, or http://localhost. This page is on plain HTTP, so only the title count and the pills work here.' };
    if (permission() === 'denied') return { ok: false, text: 'Notifications are blocked for this site. Allow them in the browser’s site settings, then reload.' };
    return { ok: true, text: '' };
  }

  function render() {
    if (!S.pop) return;
    var r = reason();
    var on = enabled();
    S.toggle.disabled = !r.ok;
    S.toggle.checked = on;
    S.toggleRow.classList.toggle('disabled', !r.ok);
    S.toggleRow.classList.toggle('on', on);
    if (!r.ok) S.status.textContent = r.text;
    else if (on) S.status.textContent = 'On. You get a notification when an agent finishes or needs an answer, except for the window you are looking at.';
    else if (permission() === 'granted') S.status.textContent = 'Off. Turn on to be notified when an agent finishes or needs an answer.';
    else S.status.textContent = 'Turn on to be notified when an agent finishes or needs an answer. The browser will ask once.';
    // The dot is a hint, never an alert: this page may notify, but nothing
    // reaches this device with the tab closed until push is on.
    if (S.dot) S.dot.hidden = !(permission() === 'granted' && !S.pushSubscribed);
  }

  function onToggle() {
    if (!S.toggle.checked) { setPref(false); render(); return; }
    if (!hasApi()) { S.toggle.checked = false; render(); return; }
    // The one permission prompt, from a tap.
    var p;
    try { p = Notification.requestPermission(); } catch (e) { p = null; }
    if (!p || typeof p.then !== 'function') { p = Promise.resolve(permission()); }
    p.then(function (result) {
      setPref(result === 'granted');
      render();
    }, function () { setPref(false); render(); });
  }

  function build() {
    var pop = S.pop;
    pop.textContent = '';
    var head = el('div', 'notif-head');
    head.appendChild(el('span', 'notif-title', 'Notifications'));
    var close = el('button', 'notif-close', null);
    close.type = 'button';
    close.title = 'Close';
    close.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
    close.addEventListener('click', closePop);
    head.appendChild(close);
    pop.appendChild(head);

    var body = el('div', 'notif-body');
    S.toggleRow = el('label', 'notif-row');
    S.toggleRow.appendChild(el('span', 'notif-label', 'Notify in this browser'));
    var sw = el('span', 'notif-switch');
    S.toggle = el('input', null, null);
    S.toggle.type = 'checkbox';
    S.toggle.id = 'notif-browser-toggle';
    S.toggle.addEventListener('change', onToggle);
    sw.appendChild(S.toggle);
    sw.appendChild(el('span', 'notif-knob'));
    S.toggleRow.appendChild(sw);
    body.appendChild(S.toggleRow);
    S.status = el('div', 'notif-status');
    S.status.id = 'notif-status';
    body.appendChild(S.status);
    pop.appendChild(body);
  }

  function isDesktop() { return window.matchMedia('(min-width: 769px)').matches; }

  function place() {
    var pop = S.pop;
    var bar = document.getElementById('terminal-status');
    var barH = bar ? bar.offsetHeight : 0;
    if (isDesktop()) {
      var r = S.bell.getBoundingClientRect();
      pop.style.left = '';
      pop.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
      pop.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    } else {
      pop.style.left = '0';
      pop.style.right = '0';
      pop.style.bottom = barH + 'px';
    }
  }

  function openPop() {
    if (S.open) return;
    S.open = true;
    render();
    S.pop.hidden = false;
    S.pop.classList.add('open');
    place();
    S.bell.classList.add('active');
    setTimeout(function () { document.addEventListener('click', onDocClick); }, 0);
  }
  function closePop() {
    if (!S.open) return;
    S.open = false;
    S.pop.hidden = true;
    S.pop.classList.remove('open');
    S.bell.classList.remove('active');
    document.removeEventListener('click', onDocClick);
  }
  function togglePop() { S.open ? closePop() : openPop(); }
  function onDocClick(e) {
    if (S.pop.contains(e.target) || S.bell.contains(e.target)) return;
    closePop();
  }

  function init(opts) {
    S.bell = opts.bell;
    S.dot = opts.dot || null;
    S.pop = opts.popover;
    if (!S.bell || !S.pop) return;
    build();
    S.pop.hidden = true;
    S.bell.addEventListener('click', function (e) { e.preventDefault(); togglePop(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePop(); });
    window.addEventListener('resize', function () { if (S.open) place(); });
    render();
    // A permission can change under us (site settings): reflect it on return.
    document.addEventListener('visibilitychange', function () { if (!document.hidden) render(); });
  }

  return {
    init: init,
    handleEvents: handleEvents,
    setAttention: setAttention,
    render: render,
    enabled: enabled,
    open: openPop,
    close: closePop,
    setPushSubscribed: function (v) { S.pushSubscribed = !!v; render(); },
  };
})();
