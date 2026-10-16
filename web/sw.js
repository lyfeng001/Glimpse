// Glimpse service worker: only handles push notifications (no offline caching: the app needs the hub anyway)
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'Glimpse';
  const opts = {
    body: [data.subtitle, data.body].filter(Boolean).join('\n'),
    tag: data.tag || 'glimpse',          // same tag per session: a new notification replaces the old one
    renotify: true,
    data: { url: data.url || '/' },
    icon: '/static/icon-192.png?v=4',
    badge: '/static/icon-192.png?v=4',
  };
  event.waitUntil((async () => {
    await self.registration.showNotification(title, opts);
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of cs) c.postMessage({ type: 'push', data });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of cs) {
      if ('focus' in c) {
        c.postMessage({ type: 'open', url });
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});
