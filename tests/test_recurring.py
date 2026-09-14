"""Occurrence math for recurring templates, anchored and interval modes.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_recurring.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from loom.config import CONFIG
from loom.recurring import nextDue, owedOccurrences
from loom.vocabulary import Priority, RecurringId, RecurringTask


_MONDAY = 0
_THURSDAY = 3


def _template(
    days: frozenset[int],
    created: datetime,
    last_added: datetime | None,
    every_days: int | None = None,
) -> RecurringTask:
    """A template with fixed incidentals; the cadence fields are the test's inputs."""
    return RecurringTask(
        id=RecurringId('r_testtmpl'),
        title='Take out the trash',
        notes='',
        estimate_minutes=10,
        auto_schedule=True,
        priority=Priority.MEDIUM,
        goal=None,
        days=days,
        every_days=every_days,
        deadline_after_days=2,
        created=created,
        last_added=last_added,
    )


def testCreationOnAnchorDayOwesThatDay() -> None:
    # 2026-09-07 is a Monday; noon local (19:00 UTC) keeps the local date unambiguous
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _template(frozenset({_MONDAY}), created, None)

    assert owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == (date(2026, 9, 7),)


def testMissedAnchorsEachOweACopy() -> None:
    last_added = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)  # Monday
    template = _template(frozenset({_MONDAY}), last_added, last_added)

    owed = owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False)

    assert owed == (date(2026, 8, 24), date(2026, 8, 31), date(2026, 9, 7))


def testAnchorsStackEvenWithAnOpenCopy() -> None:
    last_added = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)  # Monday
    template = _template(frozenset({_MONDAY, _THURSDAY}), last_added, last_added)

    owed = owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=True)

    assert owed == (date(2026, 9, 3), date(2026, 9, 7))


def testCatchUpNeverLooksBackMoreThanAMonth() -> None:
    last_added = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    template = _template(frozenset({_MONDAY}), last_added, last_added)

    owed = owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False)

    assert len(owed) == 5  # only the Mondays within the 31-day window
    assert owed[-1] == date(2026, 9, 7)
    assert (date(2026, 9, 7) - owed[0]).days <= 31


def testNothingOwedRightAfterASpawn() -> None:
    last_added = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)  # Monday, already processed
    template = _template(frozenset({_MONDAY}), last_added, last_added)

    assert owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == ()


def testNewIntervalTemplateIsDueImmediately() -> None:
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _template(frozenset(), created, None, every_days=14)

    assert owedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == (date(2026, 9, 7),)


def testIntervalCountsFromTheLastCompletion() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _template(frozenset(), last_added, last_added, every_days=14)
    shaved_on = date(2026, 8, 30)

    not_yet = owedOccurrences(template, date(2026, 9, 12), last_done=shaved_on, open_copy=False)
    due = owedOccurrences(template, date(2026, 9, 13), last_done=shaved_on, open_copy=False)

    assert not_yet == ()
    assert due == (date(2026, 9, 13),)


def testIntervalNeverStacksWhileACopyIsOpen() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _template(frozenset(), last_added, last_added, every_days=14)

    assert owedOccurrences(template, date(2026, 9, 13), last_done=None, open_copy=True) == ()


def testIntervalFallsBackToTheLastSpawn() -> None:
    last_added = datetime(2026, 8, 30, 19, 0, tzinfo=UTC)  # copy dropped, never completed
    template = _template(frozenset(), last_added, last_added, every_days=14)

    assert owedOccurrences(template, date(2026, 9, 12), last_done=None, open_copy=False) == ()
    assert owedOccurrences(template, date(2026, 9, 13), last_done=None, open_copy=False) == (date(2026, 9, 13),)


def testNextDueLandsOnAnAnchorWithinAWeek() -> None:
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _template(frozenset({_MONDAY, _THURSDAY}), created, created)

    due = nextDue(template, None)

    assert due is not None
    assert due.weekday() in template.days
    assert 1 <= (due - datetime.now(UTC).astimezone(CONFIG.timezone).date()).days <= 7


def testNextDueForAnIntervalIsCompletionPlusInterval() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _template(frozenset(), last_added, last_added, every_days=14)

    assert nextDue(template, date(2026, 8, 30)) == date(2026, 9, 13)
