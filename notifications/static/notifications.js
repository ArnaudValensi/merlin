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
            busy: false, deviceList: [], pushError: '', removedElsewhere: false,
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

  // The page's own notifications, by tag (the sid), so a window that stops
  // waiting can have its notification closed here.
  var shown = {};

  function show(ev) {
    var n;
    try {
      n = new Notification(ev.title || 'Merlin', {
        body: ev.body || '', tag: ev.sid || ev.target, icon: ICON, data: { target: ev.target },
      });
      shown[ev.sid || ev.target] = n;
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

  // Called by board.js with the events of one poll. Every event is shown,
  // with two exceptions. A browser that holds a push subscription gets the
  // event as a push, which reaches it even with the tab closed, so the page
  // shows nothing itself: one notification per browser. And a quiet event,
  // one the instance produced while a page (this one or any other) was
  // looking at its window, is shown nowhere: the decision is the instance's,
  // made once, the same for the push and for every page.
  function handleEvents(events) {
    if (!enabled()) return;
    if (S.pushSubscribed) return;
    (events || []).forEach(function (ev) {
      if (!ev || !ev.target || ev.quiet) return;
      show(ev);
    });
  }

  // Called by board.js on every successful poll with the sids of the windows
  // still waiting (done or ask). A notification whose window is no longer
  // waiting is closed: the same state that clears the green pill when the
  // window is visited or left, or answered, clears the notification here,
  // and the worker's pushed ones too. So a window handled on one device
  // disappears from the others the next time they poll.
  function syncWaiting(sids) {
    var keep = {};
    (sids || []).forEach(function (s) { keep[s] = true; });
    Object.keys(shown).forEach(function (tag) {
      if (keep[tag]) return;
      try { shown[tag].close(); } catch (e) { /* already gone */ }
      delete shown[tag];
    });
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.getRegistration('/').then(function (reg) {
      if (!reg || !reg.getNotifications) return;
      return reg.getNotifications().then(function (list) {
        list.forEach(function (n) {
          if (n.tag && n.tag !== 'test' && !keep[n.tag]) n.close();
        });
      });
    }).catch(function () { /* no worker, nothing pushed to close */ });
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
  // A subscription this browser holds that the instance no longer lists was
  // removed from another device (or dropped by the instance after the push
  // service declared it dead). Either way this device is no longer told when
  // Merlin is closed, and "remove the device" means "turn it off there": the
  // page drops its preference and the browser's subscription and reads off,
  // saying so once. Turning it on again is one tap. Only checked between
  // flows, never while one is running.
  function applyRemoteRemoval() {
    var sub = S.subscription;
    if (S.busy || !sub || S.pushSubscribed) return Promise.resolve();
    if (!S.deviceList.some(function (d) { return d.endpoint === sub.endpoint; })) {
      setPref(false);
      S.subscription = null;
      S.removedElsewhere = true;   // shown until the toggle is used again
      return sub.unsubscribe().catch(function () {}).then(function () { reconcile(); render(); });
    }
    return Promise.resolve();
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
    }).then(function () { S.pushError = ''; }, function (e) {
      if (e && e.name === 'NotAllowedError') S.pushError = 'The browser did not allow push.';
      else if (navigator.brave) S.pushError = 'Brave blocks push until "Use Google services for push messaging" is on, in brave://settings/privacy.';
      else S.pushError = 'Push could not be set up. Turn off and on to retry.';
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
      S.subscription = null; S.pushError = '';
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
  // The test goes through this device's own channel: a push when it is
  // subscribed, otherwise the notification the open tab would show.
  function sendTest() {
    S.busy = true; S.lastError = ''; S.lastInfo = ''; render();
    if (!S.pushSubscribed) {
      if (enabled()) {
        show({ title: 'Merlin', body: 'This is a test. You are told here while Merlin is open.', sid: 'test', target: '' });
        S.lastInfo = 'Test shown.';
      }
      S.busy = false; render();
      return Promise.resolve();
    }
    return api('/test', 'POST', { endpoint: S.subscription.endpoint }).then(function (r) {
      if (r.ok && r.body && r.body.ok) S.lastInfo = 'Test sent to this device.';
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
    // HTTPS first: without a secure context nothing works, whatever the
    // browser, and on an iPhone the missing API is that same cause.
    if (!secure()) return { ok: false, text: 'Notifications need HTTPS, or http://localhost. This page is on plain HTTP, so only the title count and the pills work here. The notifications doc shows how to put HTTPS in front of Merlin with Caddy, a few lines.' };
    if (!hasApi()) return { ok: false, text: 'This browser has no notification support.' };
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

  // Push is possible here: a secure context with the APIs, and not an iPhone
  // browser tab (iOS delivers push to the installed app only).
  function pushPossible() { return pushSupported() && !(isIos() && !isStandalone()); }

  // Why this device is not told when Merlin is closed, in one sentence.
  function pushWhy() {
    if (isIos() && !isStandalone()) return 'Add Merlin to the Home Screen to be told when it is closed.';
    if (!secure()) return 'Push needs HTTPS to reach this device when Merlin is closed. See the notifications doc.';
    if (!pushSupported()) return 'This browser has no Web Push, so nothing reaches it when Merlin is closed.';
    if (S.pushError) return S.pushError;
    return 'Turn off and on again to also be told when Merlin is closed.';
  }

  function isOn() { return enabled() || S.pushSubscribed; }

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
    renderSlot();
    var r = reason();
    var on = isOn();
    S.toggle.disabled = !r.ok || S.busy;
    // While a request runs, show the state the tap asked for, not the old one.
    S.toggle.checked = S.busy ? !!S.pending : on;
    S.toggleRow.classList.toggle('disabled', !r.ok);
    S.toggleRow.classList.toggle('on', on);
    S.testBtn.disabled = S.busy || !on;
    var text;
    if (S.lastError) text = S.lastError;
    else if (S.lastInfo) text = S.lastInfo;
    else if (!r.ok) text = r.text;
    else if (on && S.pushSubscribed) text = 'On. You will be told here, and on this device even when Merlin is closed.';
    else if (on) text = 'On while Merlin is open in this browser. ' + pushWhy();
    else if (S.removedElsewhere) text = 'Turned off from another device. Turn on to be told here again.';
    else if (permission() === 'granted') text = 'Off. Turn on to be told when an agent finishes or needs an answer.';
    else text = 'Turn on to be told when an agent finishes or needs an answer. The browser will ask once.';
    S.status.textContent = text;
    S.status.classList.toggle('error', !!S.lastError);
    // The dot is a hint, never an alert: on here, but nothing reaches this
    // device when Merlin is closed.
    if (S.dot) S.dot.hidden = !(on && !S.pushSubscribed && pushPossible());
  }

  // The slot under the toggle: the iPhone sentence, the devices that get a
  // push, and the test button.
  function renderSlot() {
    S.pushSlot.textContent = '';
    // The Home Screen sentence only where installing would help: over plain
    // HTTP an installed app gets no push either.
    if (isIos() && !isStandalone() && secure()) {
      S.pushSlot.appendChild(el('div', 'notif-sentence', 'On iPhone, add Merlin to the Home Screen to be told when Merlin is closed.'));
    }
    // Devices: every subscription the instance holds, this one marked.
    var mine = S.subscription ? S.subscription.endpoint : '';
    if (S.deviceList.length) {
      var list = el('div', 'notif-devices');
      list.id = 'notif-devices';
      list.appendChild(el('div', 'notif-devices-title', 'Devices told when Merlin is closed'));
      S.deviceList.forEach(function (d) {
        var row = el('div', 'notif-device');
        row.appendChild(el('span', 'notif-device-name', d.label || 'Device'));
        if (d.endpoint === mine) row.appendChild(el('span', 'notif-device-me', 'this device'));
        var when = d.last_success ? 'pushed ' + timeAgoShort(d.last_success) : 'added ' + timeAgoShort(d.created);
        row.appendChild(el('span', 'notif-device-when', when));
        var rm = el('button', 'notif-device-remove', null);
        rm.type = 'button';
        rm.title = 'Remove this device';
        rm.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
        rm.addEventListener('click', function (e) { e.preventDefault(); removeDevice(d.endpoint); });
        row.appendChild(rm);
        list.appendChild(row);
      });
      S.pushSlot.appendChild(list);
    }
    S.pushSlot.appendChild(S.testBtn);
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

  // One toggle: "notify me on this device". Off drops the preference and the
  // push subscription. On asks the permission (the one prompt, from this
  // tap), keeps the preference, then subscribes to push where the platform
  // allows it. A push that cannot be set up leaves the toggle on: the open
  // tab still notifies, and the sentence says what is missing.
  function onToggle() {
    S.lastError = ''; S.lastInfo = ''; S.removedElsewhere = false;
    if (!S.toggle.checked) {
      setPref(false);
      if (S.pushSubscribed) { S.pending = false; unsubscribePush(); } else render();
      return;
    }
    if (!hasApi()) { render(); return; }
    var p;
    try { p = Notification.requestPermission(); } catch (e) { p = null; }
    if (!p || typeof p.then !== 'function') { p = Promise.resolve(permission()); }
    S.busy = true; S.pending = true; render();
    p.then(function (result) {
      var granted = result === 'granted';
      setPref(granted);
      S.busy = false;
      if (granted && pushPossible() && !S.pushSubscribed) return subscribePush();
      render();
    }, function () { setPref(false); S.busy = false; render(); });
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
    S.toggleRow.appendChild(el('span', 'notif-label', 'Notify me on this device'));
    var sw = el('span', 'notif-switch');
    S.toggle = el('input', null, null);
    S.toggle.type = 'checkbox';
    S.toggle.id = 'notif-toggle';
    S.toggle.addEventListener('change', onToggle);
    sw.appendChild(S.toggle);
    sw.appendChild(el('span', 'notif-knob'));
    S.toggleRow.appendChild(sw);
    body.appendChild(S.toggleRow);
    S.pushSlot = el('div', 'notif-push-slot');
    S.pushSlot.id = 'notif-push-slot';
    body.appendChild(S.pushSlot);
    S.testBtn = el('button', 'notif-test', 'Test it');
    S.testBtn.type = 'button';
    S.testBtn.id = 'notif-test-btn';
    S.testBtn.addEventListener('click', function (e) { e.preventDefault(); sendTest(); });
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
    Promise.all([refreshSubscription(), refreshDevices()]).then(applyRemoteRemoval).then(render);
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
    Promise.all([refreshSubscription(), refreshDevices()]).then(applyRemoteRemoval).then(render);
    // A permission can change under us (site settings): reflect it on return.
    document.addEventListener('visibilitychange', function () { if (!document.hidden) render(); });
  }

  return {
    init: init,
    handleEvents: handleEvents,
    syncWaiting: syncWaiting,
    setAttention: setAttention,
    render: render,
    enabled: enabled,
    open: openPop,
    close: closePop,
    setPushSubscribed: function (v) { S.pushSubscribed = !!v; render(); },
    refreshStatus: refreshStatus,
  };
})();
