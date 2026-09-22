/* Merlin service worker. Two duties and nothing else:
   push: show the notification carried by the payload.
   notificationclick: focus an open Merlin window and send it to the deep
   link, or open one.
   No fetch handler, no cache, no precache: a page proxied through Merlin
   Cloud must never be served stale by a worker. The registration URL carries
   the Merlin version as a query string, so a release replaces this file. */

self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', function (e) {
  var data = {};
  try { data = e.data ? e.data.json() : {}; }
  catch (x) { data = { body: e.data ? e.data.text() : '' }; }
  var title = data.title || 'Merlin';
  var options = {
    body: data.body || '',
    icon: data.icon || '/static/icons/green/icon-192.png',
    data: {
      url: data.url || '/terminal',
      target: data.target || '',
      sid: data.sid || '',
      state: data.state || '',
    },
  };
  if (data.tag) options.tag = data.tag;
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var data = e.notification.data || {};
  var abs = new URL(data.url || '/terminal', self.location.origin).href;
  var target = data.target || '';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (new URL(c.url).origin !== self.location.origin) continue;
        var focused = c.focus ? c.focus() : Promise.resolve(c);
        return focused.then(function (w) {
          var win = w || c;
          // Send the open page straight to the target by message. navigate()
          // only acts on a client this worker controls, which is not
          // guaranteed: a tab loaded before the worker claimed it, or a
          // browser that keeps it uncontrolled (Brave), fails the navigation
          // silently, leaving the window focused but never switched, so the
          // done pill never clears. postMessage reaches controlled and
          // uncontrolled clients alike; the page's switchSession runs tmux's
          // select-window, whose hook clears the pill on arrival.
          if (target && win.postMessage) {
            try { win.postMessage({ type: 'deep-link', target: target }); return win; }
            catch (x) { /* fall through to navigate */ }
          }
          if (win.navigate) { try { return win.navigate(abs); } catch (x) { /* focus only */ } }
          return win;
        });
      }
      // No open window to message: open one on the deep-link URL, whose
      // ?target= is read and switched to once the socket confirms.
      return self.clients.openWindow(abs);
    })
  );
});
