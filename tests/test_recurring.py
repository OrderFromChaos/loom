"""Occurrence math for recurring templates, anchored and interval modes.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_recurring.py
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from loom import graph, recurring
from loom.config import CONFIG
from loom.recurring import nextDue, owedOccurrences, spawnCopy, stepSubtree
from loom.store import Store
from loom.triage import overdueTaskIds
from loom.vocabulary import (
    Area,
    Priority,
    RecurringId,
    RecurringStep,
    RecurringTask,
    Status,
    StepId,
    StoreError,
    TaskId,
)


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
        wait_for_completion=every_days is not None,
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

    due = nextDue(template, None, open_copy=False)

    assert due is not None
    assert due.weekday() in template.days
    assert 1 <= (due - datetime.now(UTC).astimezone(CONFIG.timezone).date()).days <= 7


def testNextDueForAnIntervalIsCompletionPlusInterval() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _template(frozenset(), last_added, last_added, every_days=14)

    assert nextDue(template, date(2026, 8, 30), open_copy=False) == date(2026, 9, 13)


def _step(step_id: str, parent: str | None, title: str) -> RecurringStep:
    return RecurringStep(
        id=StepId(step_id),
        template=RecurringId('r_testtmpl'),
        parent=StepId(parent) if parent is not None else None,
        title=title,
        estimate_minutes=None,
        auto_schedule=False,
        created=datetime(2026, 9, 1, tzinfo=UTC),
    )


def testCopyCarriesTheStepTreeInOrder() -> None:
    """Every step becomes a subtask under its parent's copy, siblings in template order."""
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _template(frozenset({_MONDAY}), created, None)
    steps = {
        step.id: step
        for step in (
            _step('s_zmoistur', None, 'Moisturizer'),
            _step('s_adrinkwa', None, 'Drink water'),
            _step('s_mfacecrm', 's_zmoistur', 'Face cream'),
            _step('s_orphaned', 's_missings', 'Unreachable'),
        )
    }
    # created sets sibling order: Moisturizer before Drink water, despite the ids
    steps = {
        step_id: replace(step, created=step.created.replace(minute=index))
        for index, (step_id, step) in enumerate(steps.items())
    }

    copy = spawnCopy(template, steps, created, date(2026, 9, 7))

    root, *rest = copy
    by_title = {task.title: task for task in rest}
    assert root.parent is None and root.origin == template.id
    assert set(by_title) == {'Moisturizer', 'Drink water', 'Face cream'}
    assert by_title['Moisturizer'].parent == root.id
    assert by_title['Face cream'].parent == by_title['Moisturizer'].id
    assert all(task.origin is None and task.goal is None for task in rest)

    children = graph.childrenByParent({task.id: task for task in copy})
    assert [task.title for task in children[root.id]] == ['Moisturizer', 'Drink water']


def testStepSubtreeSurvivesALoop() -> None:
    steps = {step.id: step for step in (_step('s_aaaaaaaa', 's_bbbbbbbb', 'A'), _step('s_bbbbbbbb', 's_aaaaaaaa', 'B'))}

    assert stepSubtree(StepId('s_aaaaaaaa'), steps) == frozenset({StepId('s_aaaaaaaa'), StepId('s_bbbbbbbb')})


def _noonLocal(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=CONFIG.timezone).astimezone(UTC)


@pytest.fixture(name='scratch_store')
def scratchStore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    """A Store in a temp folder that every recurring entry point uses instead of a real area."""
    area_store = Store(tmp_path)
    monkeypatch.setattr(recurring, 'storeFor', lambda area: area_store)
    return area_store


def _openCopyDates(area_store: Store, template_id: RecurringId) -> list[date]:
    copies = [task for task in area_store.loadTasks().values() if task.origin == template_id and graph.stillOpen(task)]
    return sorted(task.deadline for task in copies if task.deadline is not None)


def testAFixedRhythmStacksAndDoesNotDriftWhenLate(scratch_store: Store) -> None:
    """Every 4 days, not waiting: a late spawn keeps the rhythm, and open copies pile up."""
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_template(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveRecurring({template.id: template})

    assert recurring.spawnDue(Area('demo'), _noonLocal(date(2026, 9, 26))) == 1
    assert recurring.spawnDue(Area('demo'), _noonLocal(date(2026, 9, 28))) == 1

    # occurrences 9/24 and 9/28, deadline +2 days each; the late 9/26 view did not shift 9/28
    assert _openCopyDates(scratch_store, template.id) == [date(2026, 9, 26), date(2026, 9, 30)]


def testWaitingIntervalOwesNothingWhileOpenAndCountsFromCompletion() -> None:
    created = _noonLocal(date(2026, 9, 1))
    template = _template(frozenset(), created, _noonLocal(date(2026, 9, 10)), every_days=4)
    assert template.wait_for_completion

    assert owedOccurrences(template, date(2026, 9, 20), last_done=None, open_copy=True) == ()
    # first due date only, not every multiple since
    assert owedOccurrences(template, date(2026, 9, 20), last_done=None, open_copy=False) == (date(2026, 9, 14),)
    assert owedOccurrences(template, date(2026, 9, 20), last_done=date(2026, 9, 18), open_copy=False) == ()
    assert nextDue(template, date(2026, 9, 18), open_copy=False) == date(2026, 9, 22)


def testWaitingAnchoredOwesOnlyTheLatestAnchorDay() -> None:
    last = _noonLocal(date(2026, 8, 31))  # Monday
    template = replace(_template(frozenset({_MONDAY}), last, last), wait_for_completion=True)

    owed = owedOccurrences(template, date(2026, 9, 16), last_done=None, open_copy=False)

    assert owed == (date(2026, 9, 14),)
    assert owedOccurrences(template, date(2026, 9, 16), last_done=None, open_copy=True) == ()


def testDelayToNowMergesOpenCopiesIntoOneDatedToday(scratch_store: Store) -> None:
    """Two stacked copies, one with a subtask, become one copy due today + offset."""
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_template(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveRecurring({template.id: template})
    scratch_store.saveSteps({StepId('s_moistrzr'): _step('s_moistrzr', None, 'Moisturizer')})
    recurring.spawnDue(Area('demo'), _noonLocal(date(2026, 9, 28)))
    assert len(_openCopyDates(scratch_store, template.id)) == 2

    now = _noonLocal(date(2026, 10, 1))
    recurring.delayToNow(Area('demo'), template.id, now)

    tasks = scratch_store.loadTasks()
    copies = [task for task in tasks.values() if task.origin == template.id]
    assert [copy.deadline for copy in copies] == [date(2026, 10, 3)]
    assert [task.title for task in tasks.values() if task.parent == copies[0].id] == ['Moisturizer']
    assert len(tasks) == 2  # the older copy and its subtask are gone
    assert nextDue(scratch_store.loadRecurring()[template.id], None, open_copy=False) == date(2026, 10, 5)


def testPullAheadSpawnsNowAndRestartsTheClock(scratch_store: Store) -> None:
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_template(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveRecurring({template.id: template})

    recurring.pullAhead(Area('demo'), template.id, _noonLocal(date(2026, 9, 22)))

    assert _openCopyDates(scratch_store, template.id) == [date(2026, 9, 24)]
    assert nextDue(scratch_store.loadRecurring()[template.id], None, open_copy=False) == date(2026, 9, 26)


def testStaleMeansOpenAndPastItsDeadline() -> None:
    now = _noonLocal(date(2026, 10, 2))
    base = spawnCopy(_template(frozenset({_MONDAY}), now, None), {}, now, date(2026, 10, 2))[0]
    tasks = {
        TaskId('t_overdue1'): replace(base, id=TaskId('t_overdue1'), deadline=date(2026, 10, 1)),
        TaskId('t_duetoday'): replace(base, id=TaskId('t_duetoday'), deadline=date(2026, 10, 2)),
        TaskId('t_overdone'): replace(base, id=TaskId('t_overdone'), deadline=date(2026, 9, 1), status=Status.DONE),
    }

    assert overdueTaskIds(tasks, now) == frozenset({TaskId('t_overdue1')})


def testHandSetNextDateRealignsTheRhythm(scratch_store: Store) -> None:
    """Shower every 2 days, a day out of step: set next to tomorrow, then every 2 from there."""
    last = datetime.combine(date(2026, 10, 15), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    shower = replace(_template(frozenset(), last, last, every_days=2), wait_for_completion=False)
    assert nextDue(shower, None, open_copy=False) == date(2026, 10, 17)
    scratch_store.saveRecurring({shower.id: replace(shower, next_due=date(2026, 10, 18))})

    assert recurring.spawnDue(Area('demo'), _noonLocal(date(2026, 10, 17))) == 0
    assert recurring.spawnDue(Area('demo'), _noonLocal(date(2026, 10, 18))) == 1

    realigned = scratch_store.loadRecurring()[shower.id]
    assert realigned.next_due is None
    assert nextDue(realigned, None, open_copy=False) == date(2026, 10, 20)


def testHandSetNextDateBeatsARecentCompletion() -> None:
    """A waiting template would count from its completion; the hand-set date wins."""
    last = _noonLocal(date(2026, 10, 10))
    template = replace(_template(frozenset(), last, last, every_days=4), next_due=date(2026, 10, 13))
    assert template.wait_for_completion

    owed = owedOccurrences(template, date(2026, 10, 13), last_done=date(2026, 10, 12), open_copy=False)

    assert owed == (date(2026, 10, 13),)


def testNextDueOnAnAnchoredTemplateIsRejected(tmp_path: Path) -> None:
    area_store = Store(tmp_path)
    template = _template(frozenset({_MONDAY}), _noonLocal(date(2026, 10, 1)), None)
    area_store.saveRecurring({template.id: replace(template, next_due=date(2026, 10, 20))})

    with pytest.raises(StoreError, match='next_due'):
        Store(tmp_path).loadRecurring()


def testWaitingWithACopyOpenProjectsFinishingToday() -> None:
    """Shower waits and a copy is open: next shows today + interval, and a hand-set date wins."""
    last = _noonLocal(date(2026, 9, 1))
    shower = _template(frozenset(), last, last, every_days=2)
    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()

    assert nextDue(shower, None, open_copy=True) == today + timedelta(days=2)
    assert nextDue(replace(shower, next_due=date(2030, 1, 1)), None, open_copy=True) == date(2030, 1, 1)
