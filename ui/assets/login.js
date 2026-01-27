'use strict';
/* The sign-in page sits outside the polling layer on purpose — it must work
   when nothing else does. What it should not do is forget where you came
   from: landing back on Home after being bounced out of Notifications is how
   you lose your place. `?next=` carries the way home. */
try { window.sessionStorage.removeItem('ct.advisor.session.v1'); } catch (_) {}

const t = (key, vars) => window.I18N.t(key, vars);

/* Only ever return to a page of this app. A `next` that points anywhere else
   — another origin, a protocol handler — is discarded, not followed. */
function safeNext() {
  const raw = new URLSearchParams(window.location.search).get('next');
  if (!raw) return null;
  try {
    const url = new URL(raw, window.location.href);
    if (url.origin !== window.location.origin) return null;
    if (!/\.html$/.test(url.pathname)) return null;
    if (url.pathname.endsWith('/login.html')) return null;
    return url.pathname + url.search;
  } catch (e) { return null; }
}

const next = safeNext();
const back = document.getElementById('login-back');
back.href = next || 'index.html';
back.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15.5 19.5l-7-7.5 7-7.5"/></svg>' +
  '<span>' + t('common.cancel') + '</span>';

window.I18N.applyStatic();
document.getElementById('login-setup').innerHTML =
  window.I18N.t('login.setup', { cmd: '<code>uv run ct-auth</code>' });

const form = document.getElementById('login-form');
const message = document.getElementById('login-result');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('login-submit');
  button.disabled = true;
  message.textContent = t('login.working');
  try {
    const response = await fetch('/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CT-Request': '1' },
      body: JSON.stringify({
        username: form.elements.username.value,
        password: form.elements.password.value,
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Login failed');
    form.elements.password.value = '';
    window.location.assign(next || 'index.html');
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
