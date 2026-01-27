'use strict';
/* The debate table on the sign-in screen.
 *
 * This is the product illustrating itself, not a live readout — nobody is
 * signed in, so there is no run to report. Nothing here reads a desk's real
 * state: three seats are filled from the roster's own names and left in one
 * neutral state, and the other nine are marked disabled so the room draws
 * empty around them. The CSS crops to what is left.
 *
 * No one walks. A journey routes through the shared aisle, which is outside
 * the crop, so a figure would slide out of frame and back for no reason.
 *
 * pixel-office.js owns the drawing and is loaded first, so its canvas is
 * already bound by the time this runs.
 */
(() => {
  const scene = window.labPixelScene;
  const frame = document.getElementById('login-office');
  if (!scene || !frame) return;

  /* Seat order belongs to the drawn room and matches the lab page exactly.
     Seats 4, 5 and 6 are the debate table; everything else stays empty. */
  const SEATS = ['market', 'news', 'social', 'fundamentals', 'bull', 'bear',
                 'manager', 'trader', 'portfolio', 'aggressive', 'neutral', 'conservative'];
  const AT_THE_TABLE = new Set(['bull', 'bear', 'manager']);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const t = (key, fallback) => {
    const value = window.I18N ? window.I18N.t(key) : key;
    return value === key ? fallback : value;
  };

  function paint() {
    scene.sync({
      roles: SEATS.map((key) => ({
        id: key,
        name: t('lab.role.' + key, key),
        /* `disabled` is the one state pixel-office.js declines to draw. */
        state: AT_THE_TABLE.has(key) ? 'waiting' : 'disabled',
      })),
      selected: '',
      active: !reduced.matches,
    });
  }

  paint();
  document.getElementById('scene-caption').textContent =
    t('login.office', 'The research floor you are signing in to.');
  reduced.addEventListener('change', paint);
})();
