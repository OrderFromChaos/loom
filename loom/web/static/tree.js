// Persist which task nodes are open, per area, in localStorage. When the URL carries
// ?focus=, stored "closed" states are ignored so the server-opened chain stays visible.

(function () {
  'use strict';

  const nodes = document.querySelectorAll('details.task');
  if (!nodes.length) return;

  const key = `loom-open-${document.body.dataset.area}`;
  const focused = new URLSearchParams(window.location.search).has('focus');
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(key) || '{}'); } catch { stored = {}; }

  const present = new Set();
  nodes.forEach((node) => {
    const id = node.dataset.task;
    present.add(id);
    // a finished task stays collapsed regardless of remembered state
    if (node.dataset.done === '1') { delete stored[id]; return; }
    if (id in stored && (stored[id] || !focused)) node.open = stored[id];
  });

  // drop ids that no longer exist so the store cannot grow stale forever
  Object.keys(stored).forEach((id) => { if (!present.has(id)) delete stored[id]; });
  localStorage.setItem(key, JSON.stringify(stored));

  nodes.forEach((node) => {
    node.addEventListener('toggle', () => {
      stored[node.dataset.task] = node.open;
      localStorage.setItem(key, JSON.stringify(stored));
    });
  });
})();
