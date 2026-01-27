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
    document.getElementById('more-language-value').textContent = window.I18N.lang === 'zh' ? '中文' : 'English';

    const signedIn = CT_API.authenticated;
    const session = document.getElementById('more-session');
    session.href = signedIn ? 'system.html#devices' : 'login.html';
    document.getElementById('more-session-label').textContent = C.t(signedIn ? 'more.account' : 'more.signIn');
    document.getElementById('more-session-hint').textContent = C.t(signedIn ? 'more.accountHint' : 'more.sessionHint');
    document.getElementById('more-logout').hidden = !signedIn;
  }

  document.getElementById('more-language').addEventListener('click', () => {
    document.getElementById('lang-switch').click();
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
