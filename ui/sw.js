/* Service worker: push notifications only.
 *
 * There is deliberately no fetch handler here, so nothing this console shows is ever served
 * from a cache. Two reasons, and the second is the important one.
 *
 * A cached page would present old prices, an old drawdown level and an old proposal as if they
 * were current, which is the exact failure this console is built to avoid everywhere else.
 *
 * And an offline-capable page invites offline actions. An approval queued while disconnected
 * and replayed later would authorise an idea against a world that has moved on — the freshness
 * clock, the revalidation and the Hard Risk re-check all exist precisely to stop that. So the
 * approval path must never leave the network: no background sync, no request replay, nothing
 * that could submit an order the person is no longer standing behind.
 */

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: 'Conservative Trading', detail: 'A notification arrived unreadable.' };
  }
  const title = payload.title || 'Conservative Trading';
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.detail || '',
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    /* One notification per event key: a standing condition replaces its own banner
       instead of stacking a new one every quiet period. */
    tag: payload.key || 'conservative-trading',
    renotify: Boolean(payload.urgent),
    requireInteraction: Boolean(payload.urgent),
    data: { link: payload.link || 'index.html' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const link = (event.notification.data && event.notification.data.link) || 'index.html';
  const target = new URL(link, self.registration.scope).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      /* Reuse the open app rather than stacking another window on top of it. */
      if (client.url.startsWith(self.registration.scope) && 'focus' in client) {
        await client.navigate(target).catch(() => {});
        return client.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});

/* A subscription can be rotated by the push service. Without this the device goes quiet and
   nothing says why, so the new endpoint is handed straight back to the console's API. */
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const subscription = await self.registration.pushManager.subscribe(
      event.oldSubscription ? event.oldSubscription.options : { userVisibleOnly: true });
    await fetch('../notifications/subscribe', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CT-Request': '1' },
      body: JSON.stringify({ subscription: subscription.toJSON(), renewed: true }),
    }).catch(() => {});
  })());
});
