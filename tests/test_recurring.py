"""Occurrence math for recurring templates, anchored and interval modes.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_recurring.py
"""

from __future__ import annotations

import contextlib
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from loom import graph, recurring, store, triage
from loom.config import CONFIG
from loom.store import Store
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


def _buildTemplate(
    days: frozenset[int],
    created: datetime,
    last_added: datetime | None,
    every_days: int | None = None,
) -> RecurringTask:
    """Build a template with fixed incidentals. The cadence fields are the test's inputs."""
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
    # 2026-09-07 is a Monday. At noon local, 19:00 UTC, the local date is unambiguous
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset({_MONDAY}), created, None)

    assert recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == (
        date(2026, 9, 7),
    )


def testMissedAnchorsEachOweACopy() -> None:
    last_added = datetime(2026, 8, 17, 19, 0, tzinfo=UTC)  # Monday
    template = _buildTemplate(frozenset({_MONDAY}), last_added, last_added)

    owed = recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False)

    assert owed == (date(2026, 8, 24), date(2026, 8, 31), date(2026, 9, 7))


def testAnchorsStackEvenWithAnOpenCopy() -> None:
    last_added = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)  # Monday
    template = _buildTemplate(frozenset({_MONDAY, _THURSDAY}), last_added, last_added)

    owed = recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=True)

    assert owed == (date(2026, 9, 3), date(2026, 9, 7))


def testCatchUpNeverLooksBackMoreThanAMonth() -> None:
    last_added = datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset({_MONDAY}), last_added, last_added)

    owed = recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False)

    assert len(owed) == 5  # only the Mondays within the 31-day window
    assert owed[-1] == date(2026, 9, 7)
    assert (date(2026, 9, 7) - owed[0]).days <= 31


def testNothingOwedRightAfterASpawn() -> None:
    last_added = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)  # Monday, already processed
    template = _buildTemplate(frozenset({_MONDAY}), last_added, last_added)

    assert recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == ()


def testNewIntervalTemplateIsDueImmediately() -> None:
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset(), created, None, every_days=14)

    assert recurring.listOwedOccurrences(template, date(2026, 9, 7), last_done=None, open_copy=False) == (
        date(2026, 9, 7),
    )


def testIntervalCountsFromTheLastCompletion() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset(), last_added, last_added, every_days=14)
    shaved_on = date(2026, 8, 30)

    not_yet = recurring.listOwedOccurrences(template, date(2026, 9, 12), last_done=shaved_on, open_copy=False)
    due = recurring.listOwedOccurrences(template, date(2026, 9, 13), last_done=shaved_on, open_copy=False)

    assert not_yet == ()
    assert due == (date(2026, 9, 13),)


def testIntervalNeverStacksWhileACopyIsOpen() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset(), last_added, last_added, every_days=14)

    assert recurring.listOwedOccurrences(template, date(2026, 9, 13), last_done=None, open_copy=True) == ()


def testIntervalFallsBackToTheLastSpawn() -> None:
    last_added = datetime(2026, 8, 30, 19, 0, tzinfo=UTC)  # copy dropped, never completed
    template = _buildTemplate(frozenset(), last_added, last_added, every_days=14)

    assert recurring.listOwedOccurrences(template, date(2026, 9, 12), last_done=None, open_copy=False) == ()
    assert recurring.listOwedOccurrences(template, date(2026, 9, 13), last_done=None, open_copy=False) == (
        date(2026, 9, 13),
    )


def testNextDueLandsOnAnAnchorWithinAWeek() -> None:
    created = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset({_MONDAY, _THURSDAY}), created, created)

    due = recurring.computeNextDue(template, None, open_copy=False)

    assert due is not None
    assert due.weekday() in template.days
    assert 1 <= (due - datetime.now(UTC).astimezone(CONFIG.timezone).date()).days <= 7


def testNextDueForAnIntervalIsCompletionPlusInterval() -> None:
    last_added = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    template = _buildTemplate(frozenset(), last_added, last_added, every_days=14)

    assert recurring.computeNextDue(template, date(2026, 8, 30), open_copy=False) == date(2026, 9, 13)


def _buildStep(step_id: str, parent: str | None, title: str) -> RecurringStep:
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
    template = _buildTemplate(frozenset({_MONDAY}), created, None)
    steps = {
        step.id: step
        for step in (
            _buildStep('s_zmoistur', None, 'Moisturizer'),
            _buildStep('s_adrinkwa', None, 'Drink water'),
            _buildStep('s_mfacecrm', 's_zmoistur', 'Face cream'),
            _buildStep('s_orphaned', 's_missings', 'Unreachable'),
        )
    }
    # created sets sibling order: Moisturizer before Drink water, despite the ids
    steps = {
        step_id: replace(step, created=step.created.replace(minute=index))
        for index, (step_id, step) in enumerate(steps.items())
    }

    copy = recurring.buildCopy(template, steps, created, date(2026, 9, 7))

    root, *rest = copy
    by_title = {task.title: task for task in rest}
    assert root.parent is None and root.origin == template.id
    assert set(by_title) == {'Moisturizer', 'Drink water', 'Face cream'}
    assert by_title['Moisturizer'].parent == root.id
    assert by_title['Face cream'].parent == by_title['Moisturizer'].id
    assert all(task.origin is None and task.goal is None for task in rest)

    children = graph.groupChildrenByParent({task.id: task for task in copy})
    assert [task.title for task in children[root.id]] == ['Moisturizer', 'Drink water']


def testStepSubtreeSurvivesALoop() -> None:
    steps = {
        step.id: step
        for step in (_buildStep('s_aaaaaaaa', 's_bbbbbbbb', 'A'), _buildStep('s_bbbbbbbb', 's_aaaaaaaa', 'B'))
    }

    assert recurring.collectStepSubtreeIds(StepId('s_aaaaaaaa'), steps) == frozenset(
        {StepId('s_aaaaaaaa'), StepId('s_bbbbbbbb')},
    )


def _computeLocalNoon(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=CONFIG.timezone).astimezone(UTC)


@pytest.fixture(name='scratch_store')
def openScratchStore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The demo area's Store, opened under a temp working directory."""
    # data_root resolves against the working directory. An absolute root would reach real data
    assert not CONFIG.data_root.is_absolute()
    monkeypatch.chdir(tmp_path)
    return store.openStore(Area('demo'))


def _listOpenCopyDeadlines(area_store: Store, template_id: RecurringId) -> list[date]:
    copies = [task for task in area_store.loadTasks().values() if task.origin == template_id and graph.stillOpen(task)]
    return sorted(task.deadline for task in copies if task.deadline is not None)


def testAFixedRhythmStacksAndDoesNotDriftWhenLate(scratch_store: Store) -> None:
    """Every 4 days, not waiting: a late spawn keeps the rhythm, and open copies pile up."""
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_buildTemplate(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveTemplates({template.id: template})

    assert recurring.spawnDueCopies(Area('demo'), _computeLocalNoon(date(2026, 9, 26))) == 1
    assert recurring.spawnDueCopies(Area('demo'), _computeLocalNoon(date(2026, 9, 28))) == 1

    # occurrences 9/24 and 9/28, deadline +2 days each. The late 9/26 view did not shift 9/28
    assert _listOpenCopyDeadlines(scratch_store, template.id) == [date(2026, 9, 26), date(2026, 9, 30)]


def testWaitingIntervalOwesNothingWhileOpenAndCountsFromCompletion() -> None:
    created = _computeLocalNoon(date(2026, 9, 1))
    template = _buildTemplate(frozenset(), created, _computeLocalNoon(date(2026, 9, 10)), every_days=4)
    assert template.wait_for_completion

    assert recurring.listOwedOccurrences(template, date(2026, 9, 20), last_done=None, open_copy=True) == ()
    # first due date only, not every multiple since
    assert recurring.listOwedOccurrences(template, date(2026, 9, 20), last_done=None, open_copy=False) == (
        date(2026, 9, 14),
    )
    assert (
        recurring.listOwedOccurrences(template, date(2026, 9, 20), last_done=date(2026, 9, 18), open_copy=False) == ()
    )
    assert recurring.computeNextDue(template, date(2026, 9, 18), open_copy=False) == date(2026, 9, 22)


def testWaitingAnchoredOwesOnlyTheLatestAnchorDay() -> None:
    last = _computeLocalNoon(date(2026, 8, 31))  # Monday
    template = replace(_buildTemplate(frozenset({_MONDAY}), last, last), wait_for_completion=True)

    owed = recurring.listOwedOccurrences(template, date(2026, 9, 16), last_done=None, open_copy=False)

    assert owed == (date(2026, 9, 14),)
    assert recurring.listOwedOccurrences(template, date(2026, 9, 16), last_done=None, open_copy=True) == ()


def testDelayToNowMergesOpenCopiesIntoOneDatedToday(scratch_store: Store) -> None:
    """Two stacked copies, one with a subtask, become one copy due today + offset."""
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_buildTemplate(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveTemplates({template.id: template})
    scratch_store.saveSteps({StepId('s_moistrzr'): _buildStep('s_moistrzr', None, 'Moisturizer')})
    recurring.spawnDueCopies(Area('demo'), _computeLocalNoon(date(2026, 9, 28)))
    assert len(_listOpenCopyDeadlines(scratch_store, template.id)) == 2

    now = _computeLocalNoon(date(2026, 10, 1))
    recurring.delayToNow(Area('demo'), template.id, now)

    tasks = scratch_store.loadTasks()
    copies = [task for task in tasks.values() if task.origin == template.id]
    assert [copy.deadline for copy in copies] == [date(2026, 10, 3)]
    assert [task.title for task in tasks.values() if task.parent == copies[0].id] == ['Moisturizer']
    assert len(tasks) == 2  # the older copy and its subtask are gone
    assert recurring.computeNextDue(scratch_store.loadTemplates()[template.id], None, open_copy=False) == date(
        2026,
        10,
        5,
    )


def testPullAheadSpawnsNowAndRestartsTheClock(scratch_store: Store) -> None:
    last = datetime.combine(date(2026, 9, 20), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    template = replace(_buildTemplate(frozenset(), last, last, every_days=4), wait_for_completion=False)
    scratch_store.saveTemplates({template.id: template})

    recurring.pullAhead(Area('demo'), template.id, _computeLocalNoon(date(2026, 9, 22)))

    assert _listOpenCopyDeadlines(scratch_store, template.id) == [date(2026, 9, 24)]
    assert recurring.computeNextDue(scratch_store.loadTemplates()[template.id], None, open_copy=False) == date(
        2026,
        9,
        26,
    )


def testStaleMeansOpenAndPastItsDeadline() -> None:
    now = _computeLocalNoon(date(2026, 10, 2))
    base = recurring.buildCopy(_buildTemplate(frozenset({_MONDAY}), now, None), {}, now, date(2026, 10, 2))[0]
    tasks = {
        TaskId('t_overdue1'): replace(base, id=TaskId('t_overdue1'), deadline=date(2026, 10, 1)),
        TaskId('t_duetoday'): replace(base, id=TaskId('t_duetoday'), deadline=date(2026, 10, 2)),
        TaskId('t_overdone'): replace(base, id=TaskId('t_overdone'), deadline=date(2026, 9, 1), status=Status.DONE),
    }

    assert triage.collectOverdueTaskIds(tasks, now) == frozenset({TaskId('t_overdue1')})


def testHandSetNextDateRealignsTheRhythm(scratch_store: Store) -> None:
    """Shower every 2 days, a day out of step: set next to tomorrow, then every 2 from there."""
    last = datetime.combine(date(2026, 10, 15), time(0), tzinfo=CONFIG.timezone).astimezone(UTC)
    shower = replace(_buildTemplate(frozenset(), last, last, every_days=2), wait_for_completion=False)
    assert recurring.computeNextDue(shower, None, open_copy=False) == date(2026, 10, 17)
    scratch_store.saveTemplates({shower.id: replace(shower, next_due=date(2026, 10, 18))})

    assert recurring.spawnDueCopies(Area('demo'), _computeLocalNoon(date(2026, 10, 17))) == 0
    assert recurring.spawnDueCopies(Area('demo'), _computeLocalNoon(date(2026, 10, 18))) == 1

    realigned = scratch_store.loadTemplates()[shower.id]
    assert realigned.next_due is None
    assert recurring.computeNextDue(realigned, None, open_copy=False) == date(2026, 10, 20)


def testHandSetNextDateBeatsARecentCompletion() -> None:
    """A waiting template would count from its completion. The hand-set date wins."""
    last = _computeLocalNoon(date(2026, 10, 10))
    template = replace(_buildTemplate(frozenset(), last, last, every_days=4), next_due=date(2026, 10, 13))
    assert template.wait_for_completion

    owed = recurring.listOwedOccurrences(template, date(2026, 10, 13), last_done=date(2026, 10, 12), open_copy=False)

    assert owed == (date(2026, 10, 13),)


def testNextDueOnAnAnchoredTemplateIsRejected(tmp_path: Path) -> None:
    area_store = Store(tmp_path)
    template = _buildTemplate(frozenset({_MONDAY}), _computeLocalNoon(date(2026, 10, 1)), None)
    area_store.saveTemplates({template.id: replace(template, next_due=date(2026, 10, 20))})

    with pytest.raises(StoreError, match='next_due'):
        Store(tmp_path).loadTemplates()


def testWaitingWithACopyOpenProjectsFinishingToday() -> None:
    """Shower waits and a copy is open: the next date is today + interval, and a hand-set date wins."""
    last = _computeLocalNoon(date(2026, 9, 1))
    shower = _buildTemplate(frozenset(), last, last, every_days=2)
    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()

    assert recurring.computeNextDue(shower, None, open_copy=True) == today + timedelta(days=2)
    assert recurring.computeNextDue(replace(shower, next_due=date(2030, 1, 1)), None, open_copy=True) == date(
        2030,
        1,
        1,
    )


@st.composite
def _buildOwedScenarioStrategy(draw: st.DrawFn) -> _OwedScenario:
    """A template, a today, a last completion and an open-copy flag, in any consistent combination."""
    created_day = draw(st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 6, 30)))
    today = created_day + timedelta(days=draw(st.integers(0, 150)))
    span = (today - created_day).days
    added_offset = draw(st.none() | st.integers(0, span))
    every_days = draw(st.none() | st.integers(1, 30))
    days = frozenset() if every_days is not None else draw(st.frozensets(st.integers(0, 6), min_size=1))
    last_added = None if added_offset is None else _computeLocalNoon(created_day + timedelta(days=added_offset))
    template = replace(
        _buildTemplate(days, _computeLocalNoon(created_day), last_added, every_days=every_days),
        wait_for_completion=draw(st.booleans()),
    )
    if every_days is not None and draw(st.booleans()):
        template = replace(template, next_due=today + timedelta(days=draw(st.integers(-20, 20))))

    done_offset = draw(st.none() | st.integers(0, span))
    last_done = None if done_offset is None else created_day + timedelta(days=done_offset)
    return _OwedScenario(template=template, today=today, last_done=last_done, open_copy=draw(st.booleans()))


@given(scenario=_buildOwedScenarioStrategy())
def testOwedOccurrencesKeepTheirInvariants(scenario: _OwedScenario) -> None:
    template, today = scenario.template, scenario.today

    owed = recurring.listOwedOccurrences(template, today, last_done=scenario.last_done, open_copy=scenario.open_copy)

    assert list(owed) == sorted(set(owed))
    assert all(day <= today for day in owed)
    if template.wait_for_completion:
        assert len(owed) <= 1
        if scenario.open_copy:
            assert owed == ()
    else:
        assert all((today - day).days < 31 for day in owed)
    if template.every_days is None:
        assert all(day.weekday() in template.days for day in owed)
    elif not template.wait_for_completion and template.next_due is None:
        gaps = [(later - earlier).days for earlier, later in zip(owed, owed[1:], strict=False)]
        assert all(gap == template.every_days for gap in gaps)
    if template.last_added is not None and template.next_due is None:
        assert all(day > template.last_added.astimezone(CONFIG.timezone).date() for day in owed)


@settings(max_examples=50, deadline=None)
@given(scenario=_buildOwedScenarioStrategy())
def testASecondSpawnTheSameDaySpawnsNothing(scenario: _OwedScenario) -> None:
    template = scenario.template
    now = _computeLocalNoon(scenario.today)
    # a function-scoped tmp_path would be shared by every example. Each example gets a fresh directory
    with tempfile.TemporaryDirectory() as scratch_directory, contextlib.chdir(scratch_directory):
        area_store = store.openStore(Area('demo'))
        area_store.saveTemplates({template.id: template})

        spawned = recurring.spawnDueCopies(Area('demo'), now)

        assert recurring.spawnDueCopies(Area('demo'), now) == 0
        roots = [task for task in area_store.loadTasks().values() if task.origin == template.id]
        assert len(roots) == spawned


### vocabulary #########################################################################


@dataclass(frozen=True)
class _OwedScenario:
    template: RecurringTask
    today: date
    last_done: date | None
    open_copy: bool
