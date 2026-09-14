// Two drag interactions, nothing more: drag on empty day space to prefill the log form,
// and drag a block's bottom handle to change its duration. Blocks never move sideways.

(function () {
  'use strict';

  const week = document.querySelector('.week');
  const form = document.getElementById('log-form');
  if (!week || !form) return;

  const dayStart = parseInt(week.dataset.dayStart, 10);
  const dayEnd = parseInt(week.dataset.dayEnd, 10);
  const SNAP = 5;

  // land the initial view at the configured hour; earlier hours stay a scroll away
  const scrollBox = document.querySelector('.week-scroll');
  const scrollStart = parseInt(week.dataset.scrollStart, 10);
  const firstBody = document.querySelector('.day-body');
  if (scrollBox && firstBody && !Number.isNaN(scrollStart)) {
    const ratio = (scrollStart - dayStart) / (dayEnd - dayStart);
    scrollBox.scrollTop = ratio * firstBody.getBoundingClientRect().height;
  }

  let drag = null;   // { body, date, startY, ghost }
  let resize = null; // { block, startY, origHeight, origStyleHeight, minutesPerPixel }

  function minutesAt(body, clientY) {
    const rect = body.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
    const raw = dayStart + ratio * (dayEnd - dayStart);
    return Math.round(raw / SNAP) * SNAP;
  }

  function clock(minutes) {
    const clamped = Math.min(minutes, 23 * 60 + 55);
    const h = String(Math.floor(clamped / 60)).padStart(2, '0');
    const m = String(clamped % 60).padStart(2, '0');
    return `${h}:${m}`;
  }

  function shiftDate(dateText, days) {
    const shifted = new Date(`${dateText}T12:00:00`);
    shifted.setDate(shifted.getDate() + days);
    return shifted.toISOString().slice(0, 10);
  }

  function parseClock(text) {
    const [h, m] = text.split(':');
    return parseInt(h, 10) * 60 + parseInt(m, 10);
  }

  // ── drag on empty space: prefill the log form ─────────────────────────────

  function endDrag(commit, clientY) {
    if (!drag) return;
    const { body, date, startY, ghost } = drag;
    ghost.remove();
    document.body.style.userSelect = '';
    let from = minutesAt(body, Math.min(startY, clientY));
    let to = minutesAt(body, Math.max(startY, clientY));
    drag = null;
    if (!commit || to - from < SNAP) return;

    // a selection in the past-midnight strip belongs to the next calendar day
    let day = date;
    if (from >= 24 * 60) {
      day = shiftDate(date, 1);
      from -= 24 * 60;
      to -= 24 * 60;
    }
    form.querySelector('[name="day"]').value = day;
    form.querySelector('[name="start"]').value = clock(from);
    form.querySelector('[name="end"]').value = clock(to);
    form.scrollIntoView({ behavior: 'smooth', block: 'center' });
    document.getElementById('log-note').focus();
  }

  week.addEventListener('mousedown', (event) => {
    const body = event.target.closest('.day-body');
    if (!body || event.target.closest('.block') || event.button !== 0) return;
    const day = body.closest('.day');
    if (!day) return;

    const ghost = document.createElement('div');
    ghost.className = 'drag-ghost';
    body.appendChild(ghost);
    drag = { body, date: day.dataset.date, startY: event.clientY, ghost };
    document.body.style.userSelect = 'none';
    event.preventDefault();
  });

  // ── drag a bottom handle: resize the block ────────────────────────────────

  week.addEventListener('mousedown', (event) => {
    const handle = event.target.closest('.resize-handle');
    if (!handle || event.button !== 0) return;
    const block = handle.closest('.block');
    const body = block.closest('.day-body');
    const minutesPerPixel = (dayEnd - dayStart) / body.getBoundingClientRect().height;
    resize = {
      block,
      startY: event.clientY,
      origHeight: block.getBoundingClientRect().height,
      origStyleHeight: block.style.height,
      minutesPerPixel,
    };
    block.classList.add('resizing');
    document.body.style.userSelect = 'none';
    event.preventDefault();
  });

  function endResize(commit, clientY) {
    if (!resize) return;
    const { block, startY, origStyleHeight, minutesPerPixel } = resize;
    resize = null;
    document.body.style.userSelect = '';
    block.classList.remove('resizing');
    const deltaMinutes = Math.round(((clientY - startY) * minutesPerPixel) / SNAP) * SNAP;
    if (!commit || !deltaMinutes) {
      block.style.height = origStyleHeight;
      return;
    }

    const body = new URLSearchParams({ return_to: window.location.pathname + window.location.search });
    if (block.dataset.kind === 'planned') {
      const estimate = parseInt(block.dataset.estimate, 10);
      body.set('estimate_minutes', String(Math.max(SNAP, estimate + deltaMinutes)));
    } else {
      const start = parseClock(block.dataset.start);
      const end = Math.max(start + SNAP, parseClock(block.dataset.end) + deltaMinutes);
      body.set('day', block.dataset.day);
      body.set('start', block.dataset.start);
      body.set('end', clock(end));
      body.set('note', block.dataset.note);
      body.set('task', block.dataset.taskRef);
    }
    fetch(block.dataset.endpoint, { method: 'POST', body }).then(() => window.location.reload());
  }

  // ── shared pointer plumbing ───────────────────────────────────────────────

  window.addEventListener('mousemove', (event) => {
    if (drag) {
      const rect = drag.body.getBoundingClientRect();
      const top = Math.min(drag.startY, event.clientY) - rect.top;
      const bottom = Math.max(drag.startY, event.clientY) - rect.top;
      drag.ghost.style.top = `${Math.max(0, top)}px`;
      drag.ghost.style.height = `${Math.max(2, bottom - Math.max(0, top))}px`;
    }
    if (resize) {
      const grown = Math.max(8, resize.origHeight + (event.clientY - resize.startY));
      resize.block.style.height = `${grown}px`;
    }
  });

  window.addEventListener('mouseup', (event) => {
    endDrag(true, event.clientY);
    endResize(true, event.clientY);
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      endDrag(false, 0);
      endResize(false, 0);
    }
  });
})();
