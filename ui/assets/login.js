'use strict';
/* The sign-in page sits outside the polling layer on purpose — it must work
   when nothing else does. What it should not do is forget where you came
   from: landing back on Home after being bounced out of Notifications is how
   you lose your place. `?next=` carries the way home. */
try { window.sessionStorage.removeItem('ct.advisor.session.v1'); } catch (_) {}

const COPY = {
  working: 'Signing in…',
  failed: 'Sign-in did not complete (HTTP {status}).',
};
const copy = (key, vars = {}) => Object.entries(vars).reduce(
  (text, [name, value]) => text.replaceAll('{' + name + '}', value), COPY[key],
);

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

const form = document.getElementById('login-form');
const message = document.getElementById('login-result');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('login-submit');
  button.disabled = true;
  message.textContent = copy('working');
  try {
    const response = await fetch('/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CT-Request': '1' },
      body: JSON.stringify({
        username: form.elements.username.value,
        password: form.elements.password.value,
      }),
    });
    /* A proxy in front of the API answers a gateway error with HTML, and parsing that as JSON
       showed the reader a SyntaxError instead of the fact that the service did not answer. */
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(typeof body.detail === 'string' ? body.detail : copy('failed', { status: response.status }));
    }
    form.elements.password.value = '';
    window.location.assign(next || 'index.html');
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
