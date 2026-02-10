(function () {
  'use strict';
  const C = window.Console;
  C.mount('more');

  document.querySelectorAll('[data-icon]').forEach((el) => {
    const name = el.dataset.icon;
    el.innerHTML = name === 'chevron' ? C.CHEVRON : (C.ICON[name] || '');
  });

  function render() {
    const status = C.systemStatus();
    document.getElementById('more-status-dot').className = 'dot ' +
      (status === 'READY' ? 'dot-ok' : (status === 'PAUSED' ? 'dot-warn' : 'dot-bad'));
    document.getElementById('more-status-copy').textContent = C.code(status) + ' · ' + C.sysCopy(status);
    document.getElementById('more-language-value').textContent = nameOf(window.I18N.lang);

    const signedIn = CT_API.authenticated;
    const session = document.getElementById('more-session');
    session.hidden = !window.CT_DEMO && !signedIn && (!CT_API.state.authChecked || CT_API.state.authPending);
    session.href = signedIn ? 'notifications.html' : 'login.html?next=more.html';
    document.getElementById('more-session-label').textContent = C.t(signedIn ? 'more.account' : 'more.signIn');
    document.getElementById('more-session-hint').textContent = C.t(signedIn ? 'more.accountHint' : 'more.sessionHint');
    document.getElementById('more-logout').hidden = !signedIn;
  }

  /* ─── language ────────────────────────────────────────────────────
     Native names, never translated: someone looking for their own language
     needs to recognise it in the list they cannot currently read. */
  const NATIVE = { en: 'English', zh: '中文' };
  const nameOf = (key) => NATIVE[key] || window.I18N.LANGS[key] || key;
  let pending = window.I18N.lang;

  function renderLanguageOptions() {
    document.getElementById('language-options').innerHTML =
      Object.keys(window.I18N.LANGS).map((key) =>
        '<button class="option-row" type="button" role="radio" data-lang="' + key + '"' +
        ' aria-checked="' + (key === pending ? 'true' : 'false') + '">' +
          '<span class="option-name">' + C.escapeHtml(nameOf(key)) + '</span>' +
          '<span class="option-mark" aria-hidden="true">' + C.ICON.check + '</span>' +
        '</button>').join('');
  }

  C.setHTML(document.getElementById('language-close'), C.ICON.cross);

  document.getElementById('more-language').addEventListener('click', () => {
    pending = window.I18N.lang;          /* always open on the current choice */
    renderLanguageOptions();
    C.openSheet('language-sheet');
  });
  document.getElementById('language-options').addEventListener('click', (event) => {
    const row = event.target.closest('[data-lang]');
    if (!row) return;
    pending = row.dataset.lang;          /* marks the choice only */
    renderLanguageOptions();
  });
  document.getElementById('language-confirm').addEventListener('click', () => {
    C.closeSheet('language-sheet');
    window.I18N.set(pending);            /* a no-op when nothing changed */
  });
  document.getElementById('more-logout').addEventListener('click', async () => {
    const button = document.getElementById('more-logout');
    if (button.disabled) return;
    button.disabled = true;
    try {
      await CT_API.post('/auth/logout', {});
      window.location.assign('login.html');
    } catch (error) {
      button.disabled = false;
      document.getElementById('more-session-hint').textContent = error.message;
    }
  });
  document.addEventListener('ct:auth', render);
  C.live([], render);
})();
