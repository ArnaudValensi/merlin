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

  var S = { bell: null, dot: null, pop: null, body: null, toggle: null, toggleRow: null,
            status: null, notice: null, pushSlot: null, pushRow: null, pushToggle: null,
            devices: null, testBtn: null,
            pushSubscribed: false, subscription: null, open: false, attention: 0,
            busy: false, deviceList: [],
            tmux: null };   // null: unknown, false: no tmux server at the last sweep

  function api(path, method, body) {
    var opts = { method: method || 'GET', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch('/api/notifications' + path, opts).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; },
                           function () { return { ok: r.ok, status: r.status, body: null }; });
    });
  }

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

  // The title and the body come composed from the instance (the event
  // carries them, the push payload carries the same): shown verbatim, never
  // composed here.
  function deepLink(ev) { return '/terminal?target=' + encodeURIComponent(ev.target); }

  function show(ev) {
    var n;
    try {
      n = new Notification(ev.title || 'Merlin', {
        body: ev.body || '', tag: ev.sid || ev.target, icon: ICON, data: { target: ev.target },
      });
    } catch (e) {
      // Some browsers only show notifications from a service worker (Android
      // Chrome). The worker's click handler then lands on the deep link.
      showViaWorker(ev);
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

  function showViaWorker(ev) {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.getRegistration('/').then(function (reg) {
      if (!reg) return;
      return reg.showNotification(ev.title || 'Merlin', {
        body: ev.body || '', tag: ev.sid || ev.target, icon: ICON,
        data: { url: deepLink(ev), sid: ev.sid, state: ev.state },
      });
    }).catch(function () { /* no worker, no notification */ });
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

  // --- Web Push (decision 9 and 13) ----------------------------------------
  function pushSupported() {
    return secure() && 'serviceWorker' in navigator && 'PushManager' in window;
  }
  function urlBase64ToUint8Array(base64) {
    var padding = '='.repeat((4 - (base64.length % 4)) % 4);
    var raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function registration() {
    return navigator.serviceWorker.getRegistration('/').then(function (reg) {
      if (reg) return reg;
      return navigator.serviceWorker.ready;
    });
  }
  // What this browser holds, read when the popover opens.
  function refreshSubscription() {
    if (!pushSupported()) { S.subscription = null; reconcile(); return Promise.resolve(); }
    return registration().then(function (reg) { return reg.pushManager.getSubscription(); })
      .then(function (sub) { S.subscription = sub || null; reconcile(); })
      .catch(function () { S.subscription = null; reconcile(); });
  }
  function refreshDevices() {
    return api('/devices').then(function (r) {
      S.deviceList = (r.ok && r.body && r.body.devices) || [];
      reconcile();
    }).catch(function () { S.deviceList = []; reconcile(); });
  }
  // Push is on for this device only when the browser holds a subscription AND
  // the instance has it on file: either side alone cannot deliver.
  function reconcile() {
    var sub = S.subscription;
    S.pushSubscribed = !!sub && S.deviceList.some(function (d) { return d.endpoint === sub.endpoint; });
  }
  function labelForThisDevice() {
    var d = navigator.userAgentData;
    if (d && d.platform) {
      var brand = (d.brands || []).map(function (b) { return b.brand; })
        .filter(function (b) { return !/Not|Chromium/.test(b); })[0];
      return d.platform + (brand ? ' · ' + brand : '');
    }
    return '';   // the server derives one from the user agent
  }
  function subscribePush() {
    S.busy = true; S.pending = true; S.lastError = ''; S.lastInfo = ''; render();
    return api('/public-key').then(function (r) {
      if (!r.ok || !r.body || !r.body.key) throw new Error('no key');
      return registration().then(function (reg) {
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(r.body.key),
        });
      });
    }).then(function (sub) {
      S.subscription = sub;
      return api('/subscribe', 'POST', { subscription: sub.toJSON(), label: labelForThisDevice() })
        .then(function (r) {
          if (r.ok) return refreshDevices();
          // The instance did not record it: a browser-only subscription would
          // read as "on" and never deliver. Roll it back so both sides agree.
          return sub.unsubscribe().catch(function () {}).then(function () {
            S.subscription = null;
            throw new Error('subscribe failed');
          });
        });
    }).catch(function (e) {
      S.lastError = (e && e.name === 'NotAllowedError') ? 'Notifications were not allowed, so push stays off.' : 'Could not subscribe this device to push. Try again.';
      reconcile();
    }).then(function () { S.busy = false; render(); });
  }
  function unsubscribePush() {
    S.busy = true; S.pending = false; S.lastError = ''; render();
    var sub = S.subscription;
    if (!sub) { S.busy = false; reconcile(); render(); return Promise.resolve(); }
    var endpoint = sub.endpoint;
    // Browser first. If it keeps the subscription, nothing is cleared: the
    // device stays on both sides and the toggle stays on with the reason.
    return sub.unsubscribe().then(function () {
      S.subscription = null;
      return api('/subscribe', 'DELETE', { endpoint: endpoint }).then(function (r) {
        if (!r.ok) S.lastError = 'The browser dropped the subscription but the instance still lists this device. Remove it from the list.';
      }, function () {
        S.lastError = 'The browser dropped the subscription but the instance still lists this device. Remove it from the list.';
      });
    }, function () {
      S.lastError = 'The browser kept the subscription, so push stays on. Try again.';
    }).then(refreshDevices).then(function () { S.busy = false; render(); });
  }
  function removeDevice(endpoint) {
    if (S.subscription && S.subscription.endpoint === endpoint) return unsubscribePush();
    return api('/subscribe', 'DELETE', { endpoint: endpoint }).then(refreshDevices).then(render);
  }
  function sendTest() {
    S.busy = true; S.lastError = ''; render();
    var body = S.subscription ? { endpoint: S.subscription.endpoint } : {};
    return api('/test', 'POST', body).then(function (r) {
      if (r.ok && r.body && r.body.ok) S.lastInfo = 'Test sent' + (S.subscription ? ' to this device.' : ' to every device.');
      else if (r.status === 409) S.lastError = 'No device is subscribed yet.';
      else S.lastError = 'The test push failed' + (r.body && r.body.removed ? ' and a dead subscription was removed.' : '.');
      return refreshDevices();
    }).catch(function () { S.lastError = 'The test push failed.'; })
      .then(function () { S.busy = false; render(); });
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

  // iOS delivers Web Push only to an installed web app (16.4 and later).
  function isIos() {
    var ua = navigator.userAgent || '';
    return /iPhone|iPad|iPod/.test(ua) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }
  function isStandalone() {
    if (navigator.standalone === true) return true;
    try { return window.matchMedia('(display-mode: standalone)').matches; } catch (e) { return false; }
  }

  function render() {
    if (!S.pop) return;
    // The whole popover has one message when the last sweep found no tmux
    // server: nothing can be watched (Merlin runs outside tmux, its terminal
    // clients live in one).
    var noTmux = S.tmux === false;
    S.notice.hidden = !noTmux;
    S.body.hidden = noTmux;
    if (noTmux) {
      S.notice.textContent = 'Notifications need a running tmux server. Open the terminal to start one, then come back here.';
      if (S.dot) S.dot.hidden = true;
      return;
    }
    renderPush();
    var r = reason();
    var on = enabled();
    S.toggle.disabled = !r.ok;
    S.toggle.checked = on;
    S.toggleRow.classList.toggle('disabled', !r.ok);
    S.toggleRow.classList.toggle('on', on);
    if (S.lastError) S.status.textContent = S.lastError;
    else if (S.lastInfo) S.status.textContent = S.lastInfo;
    else if (!r.ok) S.status.textContent = r.text;
    else if (on && S.pushSubscribed) S.status.textContent = 'On here and pushed to this device, even with the tab closed.';
    else if (on) S.status.textContent = 'On. You get a notification when an agent finishes or needs an answer, except for the window you are looking at.';
    else if (S.pushSubscribed) S.status.textContent = 'Push is on for this device. Turn on the browser toggle for notifications while a tab is open.';
    else if (permission() === 'granted') S.status.textContent = 'Off. Turn on to be notified when an agent finishes or needs an answer.';
    else S.status.textContent = 'Turn on to be notified when an agent finishes or needs an answer. The browser will ask once.';
    S.status.classList.toggle('error', !!S.lastError);
    // The dot is a hint, never an alert: this page may notify, but nothing
    // reaches this device with the tab closed until push is on.
    if (S.dot) S.dot.hidden = !(permission() === 'granted' && !S.pushSubscribed);
  }

  // The push slot: the toggle, or the iPhone sentence, or why push is off.
  function renderPush() {
    S.pushSlot.textContent = '';
    if (isIos() && !isStandalone()) {
      S.pushSlot.appendChild(el('div', 'notif-sentence', 'On iPhone, add Merlin to the Home Screen first, then enable push from there.'));
    } else {
      var row = el('label', 'notif-row');
      row.id = 'notif-push-row';
      row.appendChild(el('span', 'notif-label', 'Push to this device'));
      var sw = el('span', 'notif-switch');
      var t = el('input', null, null);
      t.type = 'checkbox';
      t.id = 'notif-push-toggle';
      // While a request runs, show the state the tap asked for, not the old one.
      t.checked = S.busy ? !!S.pending : S.pushSubscribed;
      var ok = pushSupported() && permission() !== 'denied';
      t.disabled = !ok || S.busy;
      row.classList.toggle('disabled', !ok);
      row.classList.toggle('on', S.pushSubscribed);
      t.addEventListener('change', function () { t.checked ? subscribePush() : unsubscribePush(); });
      sw.appendChild(t);
      sw.appendChild(el('span', 'notif-knob'));
      row.appendChild(sw);
      S.pushSlot.appendChild(row);
      if (!pushSupported()) {
        S.pushSlot.appendChild(el('div', 'notif-sentence', secure()
          ? 'This browser has no Web Push support.'
          : 'Push needs HTTPS. See the notifications doc for the one Caddy block that adds it.'));
      }
    }
    // Devices: every subscription the instance holds, this one marked.
    var mine = S.subscription ? S.subscription.endpoint : '';
    if (S.deviceList.length) {
      var list = el('div', 'notif-devices');
      list.id = 'notif-devices';
      S.deviceList.forEach(function (d) {
        var row = el('div', 'notif-device');
        row.appendChild(el('span', 'notif-device-name', d.label || 'Device'));
        if (d.endpoint === mine) row.appendChild(el('span', 'notif-device-me', 'this device'));
        var when = d.last_success ? 'pushed ' + timeAgoShort(d.last_success) : 'added ' + timeAgoShort(d.created);
        row.appendChild(el('span', 'notif-device-when', when));
        var rm = el('button', 'notif-device-remove', null);
        rm.type = 'button';
        rm.title = 'Remove this device';
        rm.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
        rm.addEventListener('click', function (e) { e.preventDefault(); removeDevice(d.endpoint); });
        row.appendChild(rm);
        list.appendChild(row);
      });
      S.pushSlot.appendChild(list);
    }
    var test = el('button', 'notif-test', 'Send a test');
    test.type = 'button';
    test.id = 'notif-test-btn';
    test.disabled = S.busy || !S.deviceList.length;
    test.addEventListener('click', function (e) { e.preventDefault(); sendTest(); });
    S.pushSlot.appendChild(test);
  }

  function timeAgoShort(iso) {
    var t = Date.parse(iso);
    if (!t) return '';
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
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

    S.notice = el('div', 'notif-notice');
    S.notice.id = 'notif-notice';
    S.notice.hidden = true;
    pop.appendChild(S.notice);

    var body = el('div', 'notif-body');
    S.body = body;
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
    S.pushSlot = el('div', 'notif-push-slot');
    S.pushSlot.id = 'notif-push-slot';
    body.appendChild(S.pushSlot);
    S.status = el('div', 'notif-status');
    S.status.id = 'notif-status';
    body.appendChild(S.status);
    pop.appendChild(body);
  }

  // What the watcher sees, read when the popover opens. Not polled, with one
  // exception: while the popover shows the "no tmux server" notice it checks
  // again every two seconds, because opening the terminal starts the server
  // and the next sweep clears the notice.
  var statusTimer = null;
  function refreshStatus() {
    clearTimeout(statusTimer); statusTimer = null;
    return fetch('/api/notifications/status', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (st) {
        if (!st) return;
        S.tmux = st.swept ? st.tmux : null;
        render();
        if (S.open && S.tmux === false) statusTimer = setTimeout(refreshStatus, 2000);
      })
      .catch(function () { /* keep the last known state */ });
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
    S.lastError = ''; S.lastInfo = '';
    render();
    refreshStatus();
    Promise.all([refreshSubscription(), refreshDevices()]).then(render);
    S.pop.hidden = false;
    S.pop.classList.add('open');
    place();
    S.bell.classList.add('active');
    setTimeout(function () { document.addEventListener('click', onDocClick); }, 0);
  }
  function closePop() {
    if (!S.open) return;
    S.open = false;
    clearTimeout(statusTimer); statusTimer = null;
    S.pop.hidden = true;
    S.pop.classList.remove('open');
    S.bell.classList.remove('active');
    document.removeEventListener('click', onDocClick);
  }
  function togglePop() { S.open ? closePop() : openPop(); }
  function onDocClick(e) {
    // A click inside the popover can re-render it before bubbling here, and
    // its target is then detached: that is not a click outside.
    if (!document.contains(e.target)) return;
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
    // Both sides before the first real render: push is on only when the
    // browser subscription is also on file, so the bell's hint needs the list.
    Promise.all([refreshSubscription(), refreshDevices()]).then(render);
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
    refreshStatus: refreshStatus,
  };
})();
