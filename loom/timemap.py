"""Expand configured weekly windows into concrete free slots for the scheduler.

Windows are wall-clock in the configured timezone, so a 09:00 start stays 09:00 through
a DST change and a day at a transition is genuinely 23 or 25 hours long. Slots come back
in UTC, sorted, starting no earlier than `now` rounded up to five minutes, with every
fixed event subtracted.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from loom.config import CONFIG
from loom.vocabulary import Area, FixedEvent, Slot


def freeSlots(
    area: Area,
    fixed: Iterable[FixedEvent],
    now: datetime,
    horizon_days: int,
) -> tuple[Slot, ...]:
    """Free UTC intervals for one area over [now, now + horizon_days].

    Returns:
        Slots sorted by start; never overlapping, never before `now` rounded up.
    """
    zone = CONFIG.timezone
    first_day = now.astimezone(zone).date()
    leading_edge = _ceilToStep(now)

    expanded = []
    for day_offset in range(horizon_days + 1):
        day = first_day + timedelta(days=day_offset)
        for window in CONFIG.areas[area].windows:
            if day.weekday() not in window.days:
                continue
            start = datetime.combine(day, window.start, tzinfo=zone).astimezone(UTC)
            end = datetime.combine(day, window.end, tzinfo=zone).astimezone(UTC)
            if end <= leading_edge:
                continue
            expanded.append(Slot(start=max(start, leading_edge), end=end))

    expanded.sort(key=lambda slot: slot.start)
    free = _subtractBusy(expanded, fixed)
    return tuple(free)


def _ceilToStep(moment: datetime) -> datetime:
    STEP_MINUTES = 5
    floored = moment.replace(minute=moment.minute - moment.minute % STEP_MINUTES, second=0, microsecond=0)
    if floored < moment:
        floored += timedelta(minutes=STEP_MINUTES)

    return floored


def _subtractBusy(slots: Iterable[Slot], fixed: Iterable[FixedEvent]) -> list[Slot]:
    busy = sorted((event.start, event.end) for event in fixed)

    free = []
    for slot in slots:
        pieces = [slot]
        for busy_start, busy_end in busy:
            remaining = []
            for piece in pieces:
                if busy_end <= piece.start or busy_start >= piece.end:
                    remaining.append(piece)
                    continue
                if busy_start > piece.start:
                    remaining.append(Slot(start=piece.start, end=busy_start))
                if busy_end < piece.end:
                    remaining.append(Slot(start=busy_end, end=piece.end))
            pieces = remaining
        free.extend(pieces)

    return free
