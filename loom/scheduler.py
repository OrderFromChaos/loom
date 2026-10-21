"""Choose what to schedule and place it into free time.

Placement is greedy and deterministic. Candidates sort by _computeSchedulingKey for the
area's order, each fills the earliest free slot, and a long task splits into chunks
between the configured minimum and maximum. A rerun on the same inputs writes a
byte-identical plan.

runPlan retires past blocks to plan_history.jsonl, computes a new plan for the future
from `now`, and writes plan.json. The past is never re-planned.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from loom import graph, store, timemap
from loom.config import CONFIG
from loom.vocabulary import (
    Area,
    OrderedPlacement,
    Placement,
    Plan,
    PlanBlock,
    Reason,
    Selection,
    Slot,
    Status,
    Task,
    TaskId,
    TaskOrder,
    TasksByBucket,
    Unplaced,
)


LOG = logging.getLogger('loom')


def runPlan(area: Area, now: datetime) -> Plan:
    """Retire past blocks, re-plan the future, persist and return the new plan.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    fixed = area_store.loadFixed()

    previous = area_store.loadPlan()
    if previous is not None:
        area_store.retireBlocks([block for block in previous.blocks if block.end <= now], now)

    order = area_store.loadPreferences().order
    selection = selectCandidates(tasks, order)
    slots = timemap.computeFreeSlots(area, fixed, now, CONFIG.horizon_days)
    # a task deferred past the horizon has nothing to place yet and nothing to report
    horizon_end = now + timedelta(days=CONFIG.horizon_days)
    in_reach = tuple(task for task in selection.candidates if task.not_before is None or task.not_before < horizon_end)
    ordered = placeInOrder(in_reach, tuple(slots), order=order, now=now)
    placement = ordered.placement

    plan = Plan(
        generated=now,
        blocks=placement.blocks,
        unplaced=selection.rejected + placement.unplaced,
        at_risk=tuple(sorted(ordered.at_risk)),
    )
    area_store.savePlan(plan)
    LOG.info(
        'plan.generated',
        extra={'area': area, 'blocks': len(plan.blocks), 'unplaced': len(plan.unplaced)},
    )
    return plan


def selectCandidates(tasks: Mapping[TaskId, Task], order: TaskOrder) -> Selection:
    """Split the auto-scheduled open tasks into placeable candidates and rejections.

    Candidates sort by _computeSchedulingKey for `order`.
    """
    blocked = graph.collectDependencyBlockedIds(tasks)

    candidates = []
    rejected = []
    for task in sorted(tasks.values(), key=lambda task: _computeSchedulingKey(task, order)):
        if not task.auto_schedule or task.status not in (Status.TODO, Status.IN_PROGRESS):
            continue
        if task.id in blocked:
            rejected.append(Unplaced(task=task.id, reason=Reason.DEPENDENCY_BLOCKED))
        elif task.estimate_minutes is None:
            rejected.append(Unplaced(task=task.id, reason=Reason.NO_ESTIMATE))
        else:
            candidates.append(task)

    return Selection(candidates=tuple(candidates), rejected=tuple(rejected))


def _computeSchedulingKey(task: Task, order: TaskOrder) -> _SchedulingKey:
    """Compute the placement order key. The task with the smaller key is placed first, in the earlier free time.

    DEADLINE: deadlined tasks by deadline, then priority, age, id. SHORTEST: fewest
    estimated minutes first (unestimated last), ties broken as DEADLINE.
    """
    by_deadline = (
        task.deadline is None,
        task.deadline or date.max,
        task.priority.computeSortRank(),
        task.created,
        task.id,
    )
    match order:
        case TaskOrder.DEADLINE:
            return (0, *by_deadline)
        case TaskOrder.SHORTEST:
            minutes = task.estimate_minutes if task.estimate_minutes is not None else sys.maxsize
            return (minutes, *by_deadline)


def placeInOrder(
    candidates: tuple[Task, ...],
    slots: tuple[Slot, ...],
    *,
    order: TaskOrder,
    now: datetime,
) -> OrderedPlacement:
    """Place the candidates in the area's order. Shortest-first goes through the ADHD buckets.

    Shortest-first alone can starve a long task whose deadline is near. So each pass is
    one placement of the buckets of sortIntoBuckets, in bucket order. A task left unplaced
    with MISSES_DEADLINE joins the at-risk set, and the next pass puts it in the first
    bucket. The passes stop when one adds no task to the set. The set only grows, so the
    loop ends within one pass per candidate. A task unplaced even from the first bucket,
    because its deadline is already past, stays at risk.

    Raises:
        Nothing: a task that did not fit is in the returned placement's unplaced list.
    """
    match order:
        case TaskOrder.DEADLINE:
            return OrderedPlacement(placement=placeTasks(candidates, slots), at_risk=frozenset())
        case TaskOrder.SHORTEST:
            return _placeByBuckets(candidates, slots, now)


def placeTasks(candidates: Iterable[Task], slots: Iterable[Slot]) -> Placement:
    """Place each candidate into the earliest free time, in the given order.

    A task that cannot be fully placed takes no time and goes to unplaced: a deadlined
    task as MISSES_DEADLINE, the rest as NO_ROOM. A deadline scheduled by half would
    look handled.
    """
    free = tuple(sorted(slots, key=lambda slot: slot.start))

    placed: list[PlanBlock] = []
    unplaced: list[Unplaced] = []
    for task in candidates:
        fit = _fitTask(task, free)
        if fit.blocks is None:
            reason = Reason.MISSES_DEADLINE if task.deadline is not None else Reason.NO_ROOM
            unplaced.append(Unplaced(task=task.id, reason=reason))
        else:
            placed.extend(fit.blocks)
            free = fit.free_after

    placed.sort(key=lambda block: (block.start, block.task))
    return Placement(blocks=tuple(placed), unplaced=tuple(unplaced))


def _fitTask(task: Task, free: Sequence[Slot]) -> _Fit:
    """Fit one task, returning its blocks and the free time left, or no blocks and `free` untouched.

    A chunk shorter than min_chunk_minutes is refused unless it is the last chunk of the
    task. Time before the task's not_before stays free for other tasks: a slot across it
    is split, and only the part from not_before on is used.
    """
    remaining = task.estimate_minutes
    if remaining is None:
        return _Fit(blocks=None, free_after=tuple(free))

    cutoff = _computeDeadlineCutoff(task.deadline) if task.deadline is not None else None
    floor = task.not_before
    min_chunk = CONFIG.scheduler.min_chunk_minutes
    max_chunk = CONFIG.scheduler.max_chunk_minutes

    working = list(free)
    pieces: list[Slot] = []
    index = 0
    while remaining > 0 and index < len(working):
        slot = working[index]
        start = max(slot.start, floor) if floor is not None else slot.start
        if cutoff is not None and start >= cutoff:
            break

        usable_end = min(slot.end, cutoff) if cutoff is not None else slot.end
        available = (usable_end - start) // timedelta(minutes=1)
        chunk = min(remaining, available, max_chunk)
        if chunk <= 0 or (chunk < min_chunk and chunk < remaining):
            index += 1
            continue

        chunk_end = start + timedelta(minutes=chunk)
        pieces.append(Slot(start=start, end=chunk_end))
        remaining -= chunk
        leftover = []
        if slot.start < start:
            leftover.append(Slot(start=slot.start, end=start))
        if chunk_end < slot.end:
            leftover.append(Slot(start=chunk_end, end=slot.end))
        working[index : index + 1] = leftover
        if slot.start < start:
            index += 1  # the piece before not_before stays free for other tasks

    if remaining > 0:
        return _Fit(blocks=None, free_after=tuple(free))

    blocks = tuple(
        PlanBlock(task=task.id, start=piece.start, end=piece.end, chunk=number, chunks=len(pieces))
        for number, piece in enumerate(pieces, start=1)
    )
    return _Fit(blocks=blocks, free_after=tuple(working))


def _computeDeadlineCutoff(deadline: date) -> datetime:
    day_after = datetime.combine(deadline + timedelta(days=1), time(0), tzinfo=CONFIG.timezone)
    return day_after.astimezone(UTC)


def _placeByBuckets(candidates: tuple[Task, ...], slots: tuple[Slot, ...], now: datetime) -> OrderedPlacement:
    at_risk: frozenset[TaskId] = frozenset()
    while True:
        queue = sortIntoBuckets(candidates, at_risk, now).listPlacementQueue()
        placement = placeTasks(queue, slots)
        missed = {entry.task for entry in placement.unplaced if entry.reason is Reason.MISSES_DEADLINE}
        if missed <= at_risk:
            return OrderedPlacement(placement=placement, at_risk=at_risk)
        at_risk |= missed


def sortIntoBuckets(tasks: Iterable[Task], at_risk: frozenset[TaskId], now: datetime) -> TasksByBucket:
    """Sort tasks into the four shortest-first buckets.

    A task goes to the first bucket that matches, in this order: unestimated, deadline at
    risk, not yet doable, quickest. At risk comes before not yet doable, because the
    deadline of a deferred task can still be missed.
    """
    risky, quickest, not_yet, unestimated = [], [], [], []
    for task in tasks:
        if task.estimate_minutes is None:
            unestimated.append(task)
        elif task.id in at_risk:
            risky.append(task)
        elif graph.notYetDoable(task, now):
            not_yet.append(task)
        else:
            quickest.append(task)

    return TasksByBucket(
        at_risk=tuple(sorted(risky, key=lambda task: _computeSchedulingKey(task, TaskOrder.DEADLINE))),
        quickest=tuple(sorted(quickest, key=lambda task: _computeSchedulingKey(task, TaskOrder.SHORTEST))),
        not_yet=tuple(sorted(not_yet, key=lambda task: (task.not_before or now, task.id))),
        unestimated=tuple(unestimated),
    )


### vocabulary #########################################################################


# dataclasses do not compare with <. The sort key stays a tuple
_SchedulingKey = tuple[int, bool, date, int, datetime, TaskId]


@dataclass(frozen=True)
class _Fit:
    blocks: tuple[PlanBlock, ...] | None  # None: the task did not fit and consumed nothing
    free_after: tuple[Slot, ...]
