/* Subscribing this device to push, and saying plainly when it cannot.
 *
 * iOS only delivers web push to a page opened from its home-screen icon, and only after an
 * explicit tap grants permission. Both of those fail silently if you let them, leaving someone
 * waiting for notifications that were never possible — so every refusal here names its reason.
 */
window.CT_PUSH = (function () {
  'use strict';

  const supported = () => 'serviceWorker' in navigator && 'PushManager' in window &&
    'Notification' in window;

  /* iOS reports this only for a page launched from the home screen. Safari on desktop has no
     such notion, so its absence is not treated as a failure there. */
  const standalone = () => window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;

  const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  function why() {
    if (!supported()) {
      return isIOS()
        ? 'This iPhone needs iOS 16.4 or newer for web notifications.'
        : 'This browser cannot receive web notifications.';
    }
    if (isIOS() && !standalone()) {
      return 'On iPhone, notifications only work from the home-screen icon. Tap Share, then ' +
             '"Add to Home Screen", and open the console from there.';
    }
    if (Notification.permission === 'denied') {
      return 'Notifications are blocked for this app in iOS Settings. Allow them there first.';
    }
    return null;
  }

  const decode = (base64) => {
    const padded = (base64 + '='.repeat((4 - base64.length % 4) % 4))
      .replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };

  async function registration() {
    return navigator.serviceWorker.register('sw.js', { scope: './' });
  }

  async function current() {
    if (!supported()) return null;
    const ready = await navigator.serviceWorker.getRegistration('./');
    return ready ? ready.pushManager.getSubscription() : null;
  }

  /* Must be called from a tap: iOS refuses a permission prompt raised any other way. */
  async function enable() {
    const blocked = why();
    if (blocked) throw new Error(blocked);

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notifications were not allowed.');

    const { public_key: key } = await CT_API.fetchJSON('/notifications/key');
    const reg = await registration();
    await navigator.serviceWorker.ready;
    const subscription = await reg.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: decode(key),
    });
    /* The endpoint is a capability: whoever holds it can push to this device. It goes to our
       own API over the same origin and nowhere else. */
    return CT_API.post('/notifications/subscribe', {
      subscription: subscription.toJSON(),
      label: isIOS() ? 'iPhone' : 'browser',
    });
  }

  async function disable() {
    const subscription = await current();
    let deviceId = null;
    if (subscription && window.crypto && crypto.subtle) {
      const bytes = new TextEncoder().encode(subscription.endpoint);
      const digest = await crypto.subtle.digest('SHA-256', bytes);
      deviceId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0'))
        .join('').slice(0, 16);
    }
    if (subscription) await subscription.unsubscribe();
    return { unsubscribed: Boolean(subscription), deviceId };
  }

  return { supported, standalone, isIOS, why, enable, disable, current };
})();
