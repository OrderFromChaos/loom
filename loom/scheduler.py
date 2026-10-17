"""Choose what to schedule and place it into free time.

Placement is greedy and deterministic: candidates sort by the area's chosen order (see
schedulingKey), each fills the earliest free slot, long tasks split into chunks between
the configured minimum and maximum. The same inputs always give a byte-identical plan.

runPlan is the coordinator: it retires past blocks to plan_history.jsonl, re-plans the
future from `now`, and writes plan.json. The past is never re-planned.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from loom import graph, timemap
from loom.config import CONFIG
from loom.store import storeFor
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
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    fixed = area_store.loadFixed()

    previous = area_store.loadPlan()
    if previous is not None:
        area_store.retireBlocks([block for block in previous.blocks if block.end <= now], now)

    order = area_store.loadPreferences().order
    selection = selectCandidates(tasks, order)
    slots = timemap.freeSlots(area, fixed, now, CONFIG.horizon_days)
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
    """Split flagged open tasks into placeable candidates and reported rejections.

    Candidates come back in scheduling order for `order` (see schedulingKey).
    """
    blocked = graph.dependencyBlocked(tasks)

    candidates = []
    rejected = []
    for task in sorted(tasks.values(), key=lambda task: schedulingKey(task, order)):
        if not task.auto_schedule or task.status not in (Status.TODO, Status.IN_PROGRESS):
            continue
        if task.id in blocked:
            rejected.append(Unplaced(task=task.id, reason=Reason.DEPENDENCY_BLOCKED))
        elif task.estimate_minutes is None:
            rejected.append(Unplaced(task=task.id, reason=Reason.NO_ESTIMATE))
        else:
            candidates.append(task)

    return Selection(candidates=tuple(candidates), rejected=tuple(rejected))


def placeInOrder(
    candidates: tuple[Task, ...],
    slots: tuple[Slot, ...],
    *,
    order: TaskOrder,
    now: datetime,
) -> OrderedPlacement:
    """Place the candidates in the area's order; shortest-first goes through the ADHD buckets.

    Shortest-first alone can starve a long task whose deadline is near. So each pass
    places the standard ADHD ordering's buckets in turn; a deadlined task that misses
    joins the at-risk set, which puts it in the first bucket next pass, until a pass adds
    nobody new. The set only grows, so this ends within one pass per candidate. A task
    that misses even when first (its deadline is already past) stays at risk.

    Raises:
        Nothing: placement reports what did not fit instead of failing.
    """
    match order:
        case TaskOrder.DEADLINE:
            return OrderedPlacement(placement=placeTasks(candidates, slots), at_risk=frozenset())
        case TaskOrder.SHORTEST:
            return _placeByBuckets(candidates, slots, now)


def _placeByBuckets(candidates: tuple[Task, ...], slots: tuple[Slot, ...], now: datetime) -> OrderedPlacement:
    at_risk: frozenset[TaskId] = frozenset()
    while True:
        queue = standardADHDOrdering(candidates, at_risk, now).placementQueue()
        placement = placeTasks(queue, slots)
        missed = {entry.task for entry in placement.unplaced if entry.reason is Reason.MISSES_DEADLINE}
        if missed <= at_risk:
            return OrderedPlacement(placement=placement, at_risk=at_risk)
        at_risk |= missed


def standardADHDOrdering(tasks: Iterable[Task], at_risk: frozenset[TaskId], now: datetime) -> TasksByBucket:
    """Sort tasks into the four shortest-first buckets; the one ordering both planner and page use.

    A task lands in the first bucket that claims it: unestimated, then deadline at risk
    (which beats not-yet: a deferred task can still miss its deadline), then not-yet,
    then quickest. `at_risk` comes from the planner's promotion passes, or from the saved
    plan when the tasks page lists the Auto group.
    """
    risky, quickest, not_yet, unestimated = [], [], [], []
    for task in tasks:
        if task.estimate_minutes is None:
            unestimated.append(task)
        elif task.id in at_risk:
            risky.append(task)
        elif graph.notYet(task, now):
            not_yet.append(task)
        else:
            quickest.append(task)

    return TasksByBucket(
        at_risk=tuple(sorted(risky, key=lambda task: schedulingKey(task, TaskOrder.DEADLINE))),
        quickest=tuple(sorted(quickest, key=lambda task: schedulingKey(task, TaskOrder.SHORTEST))),
        not_yet=tuple(sorted(not_yet, key=lambda task: (task.not_before or now, task.id))),
        unestimated=tuple(unestimated),
    )


def placeTasks(candidates: Iterable[Task], slots: Iterable[Slot]) -> Placement:
    """Place each candidate into the earliest free time, in the given order.

    A task that cannot be fully placed consumes nothing and is reported: a deadlined task
    as MISSES_DEADLINE (half-scheduling a deadline would read as handled when it is not),
    the rest as NO_ROOM.
    """
    free = sorted(slots, key=lambda slot: slot.start)

    placed: list[PlanBlock] = []
    unplaced: list[Unplaced] = []
    for task in candidates:
        fit = _placeOne(task, free)
        if fit.blocks is None:
            reason = Reason.MISSES_DEADLINE if task.deadline is not None else Reason.NO_ROOM
            unplaced.append(Unplaced(task=task.id, reason=reason))
        else:
            placed.extend(fit.blocks)
            free = fit.free_after

    placed.sort(key=lambda block: (block.start, block.task))
    return Placement(blocks=tuple(placed), unplaced=tuple(unplaced))


def _placeOne(task: Task, free: list[Slot]) -> _Fit:
    """Fit one task, returning its blocks and the free time left, or no blocks and `free` untouched.

    A chunk shorter than min_chunk_minutes is refused unless it is the task's own tail.
    Time before the task's not_before stays free for other tasks: a slot straddling it is
    split, and only the part from not_before on is used.
    """
    remaining = task.estimate_minutes
    if remaining is None:
        return _Fit(blocks=None, free_after=free)

    cutoff = _deadlineCutoff(task.deadline) if task.deadline is not None else None
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
        available = int((usable_end - start).total_seconds() // 60)
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
            index += 1  # the piece before not_before is not this task's to use

    if remaining > 0:
        return _Fit(blocks=None, free_after=free)

    blocks = [
        PlanBlock(task=task.id, start=piece.start, end=piece.end, chunk=number, chunks=len(pieces))
        for number, piece in enumerate(pieces, start=1)
    ]
    return _Fit(blocks=blocks, free_after=working)


def schedulingKey(task: Task, order: TaskOrder) -> _SchedulingKey:
    """Placement order: a smaller key places first and takes the earlier free time.

    DEADLINE: deadlined tasks by deadline, then priority, age, id. SHORTEST: fewest
    estimated minutes first (unestimated last), ties broken as DEADLINE.
    """
    by_deadline = (task.deadline is None, task.deadline or date.max, task.priority.rank(), task.created, task.id)
    match order:
        case TaskOrder.DEADLINE:
            return (0, *by_deadline)
        case TaskOrder.SHORTEST:
            minutes = task.estimate_minutes if task.estimate_minutes is not None else sys.maxsize
            return (minutes, *by_deadline)


def _deadlineCutoff(deadline: date) -> datetime:
    day_after = datetime.combine(deadline + timedelta(days=1), time(0), tzinfo=CONFIG.timezone)
    return day_after.astimezone(UTC)


### vocabulary #########################################################################


# a sort key stays a tuple: dataclasses do not order, and ordering is the whole point
_SchedulingKey = tuple[int, bool, date, int, datetime, TaskId]


@dataclass(frozen=True)
class _Fit:
    blocks: list[PlanBlock] | None  # None: the task did not fit and consumed nothing
    free_after: list[Slot]
