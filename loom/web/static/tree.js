// Persist which containers are open, per area, in localStorage: task nodes, goal
// sections, the auto group, the all-goals line, and the finished wrappers. When the URL carries
// ?focus=, stored "closed" states are ignored so the server-opened chain stays visible.

(function () {
  'use strict';

  const area = document.body.dataset.area;
  const focused = new URLSearchParams(window.location.search).has('focus');

  function persist(nodes, storageKey, keyOf, purge) {
    if (!nodes.length) return;
    let stored = {};
    try { stored = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch { stored = {}; }

    const present = new Set();
    nodes.forEach((node) => {
      const key = keyOf(node);
      present.add(key);
      // a purged node (e.g. a finished task) never restores its remembered state
      if (purge && purge(node)) { delete stored[key]; return; }
      if (key in stored && (stored[key] || !focused)) node.open = stored[key];
    });

    // drop keys that no longer exist so the store cannot grow stale forever
    Object.keys(stored).forEach((key) => { if (!present.has(key)) delete stored[key]; });
    localStorage.setItem(storageKey, JSON.stringify(stored));

    nodes.forEach((node) => {
      node.addEventListener('toggle', () => {
        stored[keyOf(node)] = node.open;
        localStorage.setItem(storageKey, JSON.stringify(stored));
      });
    });
  }

  // An inert tick box sits inside a step's <summary>; a click on it must not fold the step.
  document.addEventListener('click', (event) => {
    if (event.target instanceof Element && event.target.closest('.tick.inert')) event.preventDefault();
  });

  // An edit/add panel lives inside its task's <details> body, so revealing one on a
  // collapsed task must also unfold the task, or the panel stays invisible.
  document.addEventListener('change', (event) => {
    const box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.classList.contains('panel-state') || !box.checked) return;
    const holder = box.closest('details');
    if (holder) holder.open = true;
  });

  persist(
    [...document.querySelectorAll('details.task')],
    `loom-open-${area}`,
    (node) => node.dataset.task,
    (node) => node.dataset.done === '1',
  );
  persist(
    [...document.querySelectorAll('details[data-fold]')],
    `loom-folds-${area}`,
    (node) => node.dataset.fold,
    null,
  );
})();
