/* Press feedback, on every page, the sign-in page included. The stylesheets never style :active;
   they style .is-pressed, which only this file sets.

   :active cannot be used on a phone. Safari on iOS paints it only where a touch listener covers
   the element, which was nowhere but the tab bar, so a tapped row or button showed nothing until
   the page had already changed. Once a listener does cover it, Safari keeps it painted for as
   long as the finger stays down, including while that finger scrolls the list it landed on.

   So a press shows at once for a mouse, a key and the fixed bars. Anywhere that scrolls it shows
   once the finger has rested a moment, so a scroll that starts on a row does not light the row
   up. It goes as soon as the finger moves or the page scrolls. A tap is shown for a moment even
   when it is quicker than that: the press appears as the finger lifts, if it had not yet, and
   stays long enough to be seen. */
(function () {
  'use strict';
  const PRESSABLE = 'a[href], button, summary, label, select, [role="button"], [role="tab"], ' +
    '[role="switch"], [role="option"], [role="menuitem"]';
  const IMMEDIATE = '.tabbar, .topnav';
  const SETTLE_MS = 80;   // how long a finger rests before a press shows where the page scrolls
  const TAP_MS = 110;     // how long a tap's press is shown, at least
  const SLOP_PX = 10;     // how far a finger drifts and is still pressing
  const MOVING_MS = 120;  // a touch this soon after a scroll stops the scroll, it presses nothing
  let press = null, flash = null, scrolledAt = -Infinity;

  const clear = (el) => el.classList.remove('is-pressed');
  function drop() {
    if (!press) return;
    clearTimeout(press.timer);
    clear(press.el);
    press = null;
  }
  function unflash() {
    if (!flash) return;
    clearTimeout(flash.timer);
    clear(flash.el);
    flash = null;
  }
  function pressable(target) {
    const el = target && target.closest ? target.closest(PRESSABLE) : null;
    if (!el || el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true' || el.closest('[inert]')) return null;
    return el;
  }
  function begin(el, id, x, y, now) {
    drop(); unflash();
    press = { el, id, x, y, timer: 0, shownAt: null };
    const show = () => { if (press && press.el === el) { press.shownAt = performance.now(); el.classList.add('is-pressed'); } };
    if (now) show();
    else press.timer = setTimeout(show, SETTLE_MS);
  }

  const quiet = { capture: true, passive: true };
  document.addEventListener('pointerdown', (event) => {
    drop();
    if (!event.isPrimary || event.button !== 0) return;
    const el = pressable(event.target);
    if (!el || (event.pointerType !== 'mouse' && performance.now() - scrolledAt < MOVING_MS)) return;
    begin(el, event.pointerId, event.clientX, event.clientY,
      event.pointerType === 'mouse' || !!el.closest(IMMEDIATE));
  }, quiet);
  document.addEventListener('pointermove', (event) => {
    if (press && event.pointerId === press.id &&
        Math.hypot(event.clientX - press.x, event.clientY - press.y) > SLOP_PX) drop();
  }, quiet);
  document.addEventListener('pointerup', (event) => {
    if (!press || event.pointerId !== press.id) return;
    const { el, shownAt } = press;
    drop();
    const shown = shownAt === null ? 0 : performance.now() - shownAt;
    if (event.pointerType === 'mouse' || shown >= TAP_MS) return;
    el.classList.add('is-pressed');
    flash = { el, timer: setTimeout(unflash, TAP_MS - shown) };
  }, quiet);
  document.addEventListener('pointercancel', (event) => {
    if (press && event.pointerId === press.id) drop();
  }, quiet);
  document.addEventListener('contextmenu', drop, quiet);
  document.addEventListener('scroll', () => { scrolledAt = performance.now(); drop(); }, quiet);
  document.addEventListener('visibilitychange', () => { drop(); unflash(); });
  window.addEventListener('blur', () => { drop(); unflash(); });

  /* Keys press what they activate: Enter a link or a control, Space a control. */
  document.addEventListener('keydown', (event) => {
    if (event.repeat || (event.key !== 'Enter' && event.key !== ' ')) return;
    const el = pressable(event.target);
    if (!el || el !== event.target || (event.key === ' ' && el.matches('a[href]'))) return;
    begin(el, 'key', 0, 0, true);
  }, true);
  document.addEventListener('keyup', () => { if (press && press.id === 'key') drop(); }, true);
})();
