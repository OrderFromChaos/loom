"""Expand configured weekly windows into concrete free slots for the scheduler.

Windows are wall-clock in the configured timezone, so a 09:00 start stays 09:00 through
a DST change and a day at a transition is 23 or 25 hours long. Slots come back in UTC,
sorted, starting no earlier than `now` rounded up to five minutes, with every fixed
event subtracted.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from loom.config import CONFIG
from loom.vocabulary import Area, FixedEvent, Slot


def computeFreeSlots(
    area: Area,
    fixed: Iterable[FixedEvent],
    now: datetime,
    horizon_days: int,
) -> tuple[Slot, ...]:
    """Compute the free UTC intervals of one area over [now, now + horizon_days].

    Returns:
        Slots sorted by start, never overlapping, never before `now` rounded up.
    """
    timezone = CONFIG.timezone
    first_day = now.astimezone(timezone).date()
    leading_edge = _ceilToStep(now)

    expanded = []
    for day_offset in range(horizon_days + 1):
        day = first_day + timedelta(days=day_offset)
        for window in CONFIG.areas[area].windows:
            if day.weekday() not in window.days:
                continue
            start = datetime.combine(day, window.start, tzinfo=timezone).astimezone(UTC)
            end = datetime.combine(day, window.end, tzinfo=timezone).astimezone(UTC)
            if end <= leading_edge:
                continue
            expanded.append(Slot(start=max(start, leading_edge), end=end))

    expanded.sort(key=lambda slot: slot.start)
    free = _subtractFixedEvents(expanded, fixed)
    return tuple(free)


def _ceilToStep(moment: datetime) -> datetime:
    STEP_MINUTES = 5
    floored = moment.replace(minute=moment.minute - moment.minute % STEP_MINUTES, second=0, microsecond=0)
    if floored < moment:
        floored += timedelta(minutes=STEP_MINUTES)

    return floored


def _subtractFixedEvents(slots: Iterable[Slot], fixed: Iterable[FixedEvent]) -> list[Slot]:
    busy = sorted((event.start, event.end) for event in fixed)

    free = []
    for slot in slots:
        pieces = [slot]
        for busy_start, busy_end in busy:
            remaining: list[Slot] = []
            for piece in pieces:
                remaining.extend(_subtractInterval(piece, busy_start, busy_end))
            pieces = remaining
        free.extend(pieces)

    return free


def _subtractInterval(piece: Slot, busy_start: datetime, busy_end: datetime) -> tuple[Slot, ...]:
    if busy_end <= piece.start or busy_start >= piece.end:
        return (piece,)

    remaining = []
    if busy_start > piece.start:
        remaining.append(Slot(start=piece.start, end=busy_start))
    if busy_end < piece.end:
        remaining.append(Slot(start=busy_end, end=piece.end))

    return tuple(remaining)
