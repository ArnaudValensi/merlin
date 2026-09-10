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
    icon: data.icon || '/static/icons/icon-192.png',
    data: { url: data.url || '/terminal', sid: data.sid || '', state: data.state || '' },
  };
  if (data.tag) options.tag = data.tag;
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || '/terminal';
  var target = new URL(url, self.location.origin).href;
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (new URL(c.url).origin !== self.location.origin) continue;
        var focused = c.focus ? c.focus() : Promise.resolve(c);
        return focused.then(function (w) {
          var win = w || c;
          return win.navigate ? win.navigate(target) : win;
        });
      }
      return self.clients.openWindow(target);
    })
  );
});
