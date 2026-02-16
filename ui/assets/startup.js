/* One curtain per document startup; never recreated by route changes or polling. */
(function () {
  'use strict';
  const panel = document.getElementById('startup');
  if (!panel) return;
  const message = document.getElementById('startup-message');
  const actions = document.getElementById('startup-actions');
  const retry = document.getElementById('startup-retry');
  let finished = false, timer, retrying = false, pageStarted = false;
  // CSS visibility keeps the background out of the focus/accessibility tree
  // without touching the router's own inert state while page scripts initialize.
  panel.focus({ preventScroll: true });
  function copy(key) {
    for (const text of message.querySelectorAll('[data-i18n]')) {
      text.hidden = text.dataset.i18n !== key;
      if (window.I18N) text.textContent = I18N.t(text.dataset.i18n);
    }
  }
  function waiting(key) {
    if (finished) return;
    clearTimeout(timer);
    panel.classList.add('is-waiting');
    copy(key);
    actions.hidden = false;
  }
  function arm() {
    clearTimeout(timer);
    timer = setTimeout(() => waiting('startup.slow'), 10000);
  }
  function finish() {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    const focused = panel.contains(document.activeElement);
    document.documentElement.removeAttribute('data-ct-starting');
    panel.classList.add('is-leaving');
    panel.inert = true;
    if (focused) {
      const target = document.querySelector('#content:not([hidden]), .conn-signin, .tabitem');
      if (target) {
        if (target.id === 'content') target.setAttribute('tabindex', '-1');
        target.focus({ preventScroll: true });
      }
    }
    panel.setAttribute('aria-hidden', 'true');
    setTimeout(() => panel.remove(), window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180);
  }
  document.addEventListener('ct:page-status', event => {
    if (finished) return;
    pageStarted = true;
    const status = event.detail.status;
    if (status === 'signedOut' || status === 'ready' && (window.CT_DEMO || window.CT_API?.authenticated)) finish();
    else if (status === 'error') waiting('startup.failed');
  });
  document.getElementById('startup-continue').addEventListener('click', finish);
  retry.addEventListener('click', async () => {
    if (finished || retrying) return;
    if (!window.CT_API || !pageStarted) { window.location.reload(); return; }
    retrying = true; retry.disabled = true;
    panel.classList.remove('is-waiting');
    copy('startup.preparing');
    arm();
    try { await CT_API.refresh(); }
    catch (_) { waiting('startup.failed'); }
    finally { retrying = false; retry.disabled = false; }
  });
  arm();
})();
