// Drag an area in the switcher to reorder it. Items move live while dragging; on drop
// the new order posts as a plain form, so it round-trips like every other edit and is
// written to loom.toml. A hidden or finished area shown leftmost has no handle.

(function () {
  'use strict';

  const nav = document.querySelector('nav.areas');
  const form = document.querySelector('form.area-order-form');
  if (!nav || !form) return;

  const names = () => [...nav.querySelectorAll('[data-area-name]')].map((item) => item.dataset.areaName).join(',');
  let dragged = null;
  let before = '';

  nav.addEventListener('dragstart', (event) => {
    const item = event.target instanceof Element ? event.target.closest('[data-area-name]') : null;
    if (!item) return;
    dragged = item;
    before = names();
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', item.dataset.areaName);
    item.classList.add('dragging');
  });

  nav.addEventListener('dragover', (event) => {
    if (!dragged) return;
    event.preventDefault();
    const over = event.target instanceof Element ? event.target.closest('[data-area-name]') : null;
    if (!over || over === dragged) return;
    const box = over.getBoundingClientRect();
    const after = event.clientX > box.left + box.width / 2;
    over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
  });

  nav.addEventListener('drop', (event) => {
    if (dragged) event.preventDefault();
  });

  nav.addEventListener('dragend', () => {
    if (!dragged) return;
    dragged.classList.remove('dragging');
    dragged = null;
    const order = names();
    if (order === before) return;
    form.elements.order.value = order;
    form.elements.return_to.value = window.location.pathname + window.location.search;
    form.requestSubmit();
  });
})();
