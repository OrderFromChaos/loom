"""Invariant tests for the scheduler core: placement, time expansion, staleness.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_scheduler.py
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from loom.config import CONFIG
from loom.scheduler import placeInOrder, placeTasks, selectCandidates, standardADHDOrdering
from loom.timemap import freeSlots
from loom.triage import staleTaskIds
from loom.vocabulary import (
    Area,
    EventId,
    EventKind,
    EventSource,
    LoggedEvent,
    Plan,
    PlanBlock,
    Priority,
    Slot,
    Status,
    Task,
    TaskId,
    TaskOrder,
)


_BASE = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)


def _taskIds() -> st.SearchStrategy[TaskId]:
    return st.from_regex(r't_[a-z2-7]{8}', fullmatch=True).map(TaskId)


def _candidates() -> st.SearchStrategy[Task]:
    """Generate schedulable tasks: flagged, todo, estimated, no deadline, maybe a not-before."""

    def build(task_id: TaskId, estimate: int, priority: Priority) -> Task:
        return Task(
            id=task_id,
            title='candidate',
            status=Status.TODO,
            parent=None,
            goal=None,
            blocked_by=(),
            notes='',
            estimate_minutes=estimate,
            deadline=None,
            auto_schedule=True,
            priority=priority,
            created=_BASE,
            completed=None,
        )

    tasks = st.builds(
        build,
        task_id=_taskIds(),
        estimate=st.integers(min_value=1, max_value=600),
        priority=st.sampled_from(Priority),
    )
    floors = st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=20000).map(lambda minutes: _BASE + timedelta(minutes=minutes)),
    )
    return st.tuples(tasks, floors).map(lambda pair: replace(pair[0], not_before=pair[1]))


def _slotLists() -> st.SearchStrategy[list[Slot]]:
    def build(spans: list[tuple[int, int]]) -> list[Slot]:
        slots = []
        cursor = _BASE
        for gap_minutes, length_minutes in sorted(spans):
            start = max(cursor, _BASE + timedelta(minutes=gap_minutes))
            end = start + timedelta(minutes=length_minutes)
            slots.append(Slot(start=start, end=end))
            cursor = end
        return slots

    spans = st.tuples(st.integers(min_value=0, max_value=20000), st.integers(min_value=10, max_value=600))
    return st.lists(spans, max_size=6).map(build)


@given(
    task_list=st.lists(_candidates(), max_size=6, unique_by=lambda task: task.id),
    slots=_slotLists(),
)
def testPlacementInvariantsHold(task_list: list[Task], slots: list[Slot]) -> None:
    """Blocks never overlap, stay inside slots, respect not-before, and every task is placed or reported."""
    placement = placeTasks(tuple(task_list), tuple(slots))
    blocks, unplaced = placement.blocks, placement.unplaced

    ordered = sorted(blocks, key=lambda block: (block.start, block.end))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.end <= later.start

    for block in blocks:
        assert any(slot.start <= block.start and block.end <= slot.end for slot in slots)
        chunk_minutes = int((block.end - block.start).total_seconds() // 60)
        assert chunk_minutes <= CONFIG.scheduler.max_chunk_minutes

    floors = {task.id: task.not_before for task in task_list}
    for block in blocks:
        floor = floors[block.task]
        assert floor is None or block.start >= floor

    placed_ids = {block.task for block in blocks}
    unplaced_ids = {entry.task for entry in unplaced}
    assert not placed_ids & unplaced_ids
    for task in task_list:
        assert task.id in placed_ids or task.id in unplaced_ids
        if task.id in placed_ids:
            total = sum(
                int((block.end - block.start).total_seconds() // 60) for block in blocks if block.task == task.id
            )
            assert total == task.estimate_minutes


def testNotBeforeSplitsAStraddlingSlot() -> None:
    """A deferred task takes the slot from its not-before on; the time before stays free for others."""
    slot = Slot(start=_BASE, end=_BASE + timedelta(hours=4))
    deferred = replace(_fixedCandidate('t_deferred'), estimate_minutes=60, not_before=_BASE + timedelta(hours=1))
    eager = replace(_fixedCandidate('t_eagertsk'), estimate_minutes=60)

    placement = placeTasks((deferred, eager), (slot,))

    starts = {block.task: block.start for block in placement.blocks}
    assert starts[TaskId('t_deferred')] == _BASE + timedelta(hours=1)
    assert starts[TaskId('t_eagertsk')] == _BASE
    assert not placement.unplaced


def testShortestFirstTakesTheEarliestTime() -> None:
    """Under shortest-first the quickest task is placed first; under deadline order age decides."""
    older_long = replace(_fixedCandidate('t_longtask'), estimate_minutes=90)
    newer_quick = replace(_fixedCandidate('t_quicktsk'), estimate_minutes=15, created=_BASE + timedelta(minutes=1))
    tasks = {task.id: task for task in (older_long, newer_quick)}
    slot = Slot(start=_BASE, end=_BASE + timedelta(hours=4))

    for order, first in ((TaskOrder.SHORTEST, 't_quicktsk'), (TaskOrder.DEADLINE, 't_longtask')):
        selection = selectCandidates(tasks, order)
        assert selection.candidates[0].id == TaskId(first)
        placement = placeTasks(selection.candidates, (slot,))
        assert min(placement.blocks, key=lambda block: block.start).task == TaskId(first)


def testStandardADHDOrderingBuckets() -> None:
    """Each task lands in the first bucket that claims it; at-risk beats not-yet."""
    later = _BASE + timedelta(days=1)
    tasks = (
        replace(_fixedCandidate('t_unestmtd'), estimate_minutes=None),
        replace(_fixedCandidate('t_riskdefr'), estimate_minutes=60, not_before=later),
        replace(_fixedCandidate('t_defrdtsk'), estimate_minutes=5, not_before=later),
        replace(_fixedCandidate('t_longtask'), estimate_minutes=90),
        replace(_fixedCandidate('t_quicktsk'), estimate_minutes=10),
    )

    buckets = standardADHDOrdering(tasks, frozenset({TaskId('t_riskdefr')}), _BASE)

    assert [task.id for task in buckets.at_risk] == ['t_riskdefr']
    assert [task.id for task in buckets.quickest] == ['t_quicktsk', 't_longtask']
    assert [task.id for task in buckets.not_yet] == ['t_defrdtsk']
    assert [task.id for task in buckets.unestimated] == ['t_unestmtd']
    assert [task.id for task in buckets.placementQueue()] == ['t_riskdefr', 't_quicktsk', 't_longtask', 't_defrdtsk']


def testAtRiskTaskJumpsTheShortestQueue() -> None:
    """A long deadlined task that shortest-first would starve is moved ahead and fits."""
    due_today = _BASE.astimezone(CONFIG.timezone).date()
    long_due = replace(_fixedCandidate('t_longdue1'), estimate_minutes=120, deadline=due_today)
    quick = replace(_fixedCandidate('t_quicktsk'), estimate_minutes=30)
    slot = Slot(start=_BASE, end=_BASE + timedelta(hours=2))

    ordered = placeInOrder((long_due, quick), (slot,), order=TaskOrder.SHORTEST, now=_BASE)

    assert ordered.at_risk == frozenset({TaskId('t_longdue1')})
    assert {block.task for block in ordered.placement.blocks} == {TaskId('t_longdue1')}
    assert [entry.task for entry in ordered.placement.unplaced] == [TaskId('t_quicktsk')]


def _fixedCandidate(task_id: str) -> Task:
    return Task(
        id=TaskId(task_id),
        title='candidate',
        status=Status.TODO,
        parent=None,
        goal=None,
        blocked_by=(),
        notes='',
        estimate_minutes=30,
        deadline=None,
        auto_schedule=True,
        priority=Priority.MEDIUM,
        created=_BASE,
        completed=None,
    )


@given(
    task_list=st.lists(_candidates(), max_size=6, unique_by=lambda task: task.id),
    slots=_slotLists(),
)
def testPlacementIsDeterministic(task_list: list[Task], slots: list[Slot]) -> None:
    assert placeTasks(tuple(task_list), tuple(slots)) == placeTasks(tuple(task_list), tuple(slots))


@given(
    now=st.datetimes(
        min_value=datetime(2026, 3, 5, 0, 0),
        max_value=datetime(2026, 3, 10, 0, 0),
        timezones=st.just(UTC),
    ),
)
def testFreeSlotsKeepWallClockAcrossSpringForward(now: datetime) -> None:
    """Across the 2026-03-08 US DST jump, every slot still lands inside a configured window."""
    slots = freeSlots(Area('work'), (), now, 7)
    zone = CONFIG.timezone
    windows = CONFIG.areas[Area('work')].windows

    ordered = sorted(slots, key=lambda slot: slot.start)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.end <= later.start

    for slot in slots:
        assert slot.start >= now
        local_start = slot.start.astimezone(zone)
        local_end = slot.end.astimezone(zone)
        assert local_start.date() == local_end.date()
        assert any(
            local_end.time() == window.end
            and local_end.date().weekday() in window.days
            and window.start <= local_start.time() < window.end
            for window in windows
        )


def _bareTask(task_id: TaskId, status: Status = Status.TODO, completed: datetime | None = None) -> Task:
    return Task(
        id=task_id,
        title='bare',
        status=status,
        parent=None,
        goal=None,
        blocked_by=(),
        notes='',
        estimate_minutes=30,
        deadline=None,
        auto_schedule=True,
        priority=Priority.MEDIUM,
        created=_BASE,
        completed=completed,
    )


def _blockEndingAt(task_id: TaskId, end: datetime) -> PlanBlock:
    return PlanBlock(task=task_id, start=end - timedelta(minutes=30), end=end, chunk=1, chunks=1)


def _ackAt(task_id: TaskId, at: datetime) -> LoggedEvent:
    return LoggedEvent(
        id=EventId('e_abcdefgh'),
        kind=EventKind.ACK,
        at=at,
        start=None,
        end=None,
        note='',
        task=task_id,
        source=EventSource.MANUAL,
    )


def testPassedBlockWithNoTouchIsStale() -> None:
    task = _bareTask(TaskId('t_stale111'))
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    block_end = _BASE
    now = block_end + grace + timedelta(minutes=1)
    plan = Plan(generated=_BASE, blocks=(_blockEndingAt(task.id, block_end),), unplaced=())

    assert staleTaskIds({task.id: task}, plan, (), (), now) == {task.id}


def testAckAfterTheBlockClearsStaleness() -> None:
    task = _bareTask(TaskId('t_acked111'))
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    block_end = _BASE
    now = block_end + grace + timedelta(minutes=10)
    plan = Plan(generated=_BASE, blocks=(_blockEndingAt(task.id, block_end),), unplaced=())
    ack = _ackAt(task.id, block_end + timedelta(minutes=5))

    assert staleTaskIds({task.id: task}, plan, (), (ack,), now) == frozenset()


def testANewerUnmetBlockMakesAnAckedTaskStaleAgain() -> None:
    task = _bareTask(TaskId('t_again111'))
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    first_end = _BASE
    second_end = _BASE + timedelta(days=1)
    now = second_end + grace + timedelta(minutes=1)
    history = (_blockEndingAt(task.id, first_end), _blockEndingAt(task.id, second_end))
    ack = _ackAt(task.id, first_end + timedelta(minutes=5))

    assert staleTaskIds({task.id: task}, None, history, (ack,), now) == {task.id}


def testRetiredHistoryAloneStillMarksStale() -> None:
    task = _bareTask(TaskId('t_hist1111'))
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    block_end = _BASE
    now = block_end + grace + timedelta(minutes=1)

    assert staleTaskIds({task.id: task}, None, (_blockEndingAt(task.id, block_end),), (), now) == {task.id}


def testDoneTaskIsNeverStale() -> None:
    task = _bareTask(TaskId('t_done1111'), status=Status.DONE, completed=_BASE + timedelta(hours=2))
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    now = _BASE + grace + timedelta(hours=3)

    assert staleTaskIds({task.id: task}, None, (_blockEndingAt(task.id, _BASE),), (), now) == frozenset()
