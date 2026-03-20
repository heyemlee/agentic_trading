window.Console.page('more', function (document) {
  'use strict';
  const C = window.Console;
  C.mount('more');

  /* The rows' beads and the call to action carry the console's own line icons. */
  document.querySelectorAll('[data-icon]').forEach((el) => {
    el.innerHTML = C.ICON[el.dataset.icon] || '';
  });

  function render() {
    /* What needs you is Home's, above the accounts; here only a decision on a proposal keeps a
       row, because Approvals is otherwise reached from nowhere on this tab. */
    const attention = C.attentionState();
    const approvals = attention.items.filter(x => x.requires_person && ['proposal_waiting', 'proposal_stale', 'exit_incomplete'].includes(x.kind));
    document.getElementById('more-rows').hidden = !approvals.length;
    document.getElementById('more-approvals').hidden = !approvals.length;
    document.getElementById('more-approvals').href = C.taskLink(approvals[0]?.link) || 'approvals.html';
    document.getElementById('more-approvals-copy').textContent = attention.known ? C.t('at.approvals', { n: approvals.length }) : C.t('at.unknown');
    /* The state is the card's figure; its dot is the one colour on the page, and means it. */
    const status = C.systemStatus();
    document.getElementById('more-status-dot').className = 'me-dot ' +
      (status === 'READY' ? 'is-ok' : (status === 'PAUSED' ? 'is-warn' : 'is-bad'));
    document.getElementById('more-status-word').textContent = C.code(status);
    document.getElementById('more-status-copy').textContent = C.sysCopy(status);
    /* Notices are counted here, beside the state, and never dealt into Home's to-dos: System
       holds their text, and a notice that reads the same for weeks is not a to-do. */
    const notices = (C.D.health.warnings || []).length;
    document.getElementById('more-notices').hidden = !notices;
    document.getElementById('more-notices').textContent = notices ? C.t('more.notices', { n: notices }) : '';
    document.getElementById('more-language-value').textContent = nameOf(window.I18N.lang);

    /* Signed in, devices and sign-in sessions are the "Devices & notifications" row above; a
       second row to the same page read as two different settings. Only a signed-out visitor
       needs a way in. */
    const signedIn = CT_API.authenticated;
    document.getElementById('more-session').hidden =
      signedIn || (!window.CT_DEMO && (!CT_API.state.authChecked || CT_API.state.authPending));
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
      document.getElementById('more-logout-hint').textContent = error.message;
    }
  });
  document.addEventListener('ct:auth', render);
  C.live([], render, ['attention']);
});
