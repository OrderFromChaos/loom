// A click anywhere on a date field opens the browser's calendar picker, instead of
// dropping a cursor into one segment of the typed MM/DD/YYYY text. Typing still works.

(function () {
  'use strict';

  document.addEventListener('click', (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'date') return;
    if (typeof input.showPicker !== 'function') return;
    // showPicker throws when the picker is already open; the field still works as typed
    try { input.showPicker(); } catch { /* nothing to do */ }
  });
})();
