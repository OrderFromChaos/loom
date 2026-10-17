// Keep the viewport where it was across form-post reloads (the PRG round trip).
// On any form submit the scroll position is stashed; when the follow-up page load
// lands on the same path, it is restored — overriding anchor jumps and the calendar's
// default scroll, which only apply to fresh navigations.
//
// This is the last deferred script, so it also lifts the paint hold set in the page
// head: by now tree.js has reapplied folds and calendar.js its initial scroll, and the
// page first appears in its final state instead of visibly snapping into it.

(function () {
  'use strict';

  const KEY = 'loom-scroll';
  const here = window.location.pathname + window.location.search;

  document.addEventListener('submit', () => {
    const scroller = document.querySelector('.week-scroll');
    sessionStorage.setItem(KEY, JSON.stringify({
      path: here,
      y: window.scrollY,
      week: scroller ? scroller.scrollTop : null,
    }));
  }, true);

  try {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { saved = null; }
    sessionStorage.removeItem(KEY);
    if (!saved || saved.path !== here) return;

    window.scrollTo(0, saved.y);
    if (saved.week !== null) {
      const scroller = document.querySelector('.week-scroll');
      if (scroller) scroller.scrollTop = saved.week;
    }
  } finally {
    document.documentElement.classList.remove('restoring');
  }
})();
