"""Choose what to schedule and place it into free time.

Placement is greedy and deterministic: candidates sort by (deadline, priority, created,
id), each fills the earliest free slot, long tasks split into chunks between the
configured minimum and maximum. The same inputs always give a byte-identical plan.

runPlan is the coordinator: it retires past blocks to plan_history.jsonl, re-plans the
future from `now`, and writes plan.json. The past is never re-planned.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from loom import graph, timemap
from loom.config import CONFIG
from loom.store import storeFor
from loom.vocabulary import Area, Placement, Plan, PlanBlock, Reason, Selection, Slot, Status, Task, TaskId, Unplaced


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

    selection = selectCandidates(tasks)
    slots = timemap.freeSlots(area, fixed, now, CONFIG.horizon_days)
    placement = placeTasks(selection.candidates, slots)

    plan = Plan(generated=now, blocks=placement.blocks, unplaced=selection.rejected + placement.unplaced)
    area_store.savePlan(plan)
    LOG.info(
        'plan.generated',
        extra={'area': area.value, 'blocks': len(plan.blocks), 'unplaced': len(plan.unplaced)},
    )
    return plan


def selectCandidates(tasks: Mapping[TaskId, Task]) -> Selection:
    """Split flagged open tasks into placeable candidates and reported rejections.

    Candidates come back in scheduling order: deadline first, then priority, created, id.
    """
    blocked = graph.dependencyBlocked(tasks)

    candidates = []
    rejected = []
    for task in sorted(tasks.values(), key=_schedulingKey):
        if not task.auto_schedule or task.status not in (Status.TODO, Status.IN_PROGRESS):
            continue
        if task.id in blocked:
            rejected.append(Unplaced(task=task.id, reason=Reason.DEPENDENCY_BLOCKED))
        elif task.estimate_minutes is None:
            rejected.append(Unplaced(task=task.id, reason=Reason.NO_ESTIMATE))
        else:
            candidates.append(task)

    return Selection(candidates=tuple(candidates), rejected=tuple(rejected))


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
    """
    remaining = task.estimate_minutes
    if remaining is None:
        return _Fit(blocks=None, free_after=free)

    cutoff = _deadlineCutoff(task.deadline) if task.deadline is not None else None
    min_chunk = CONFIG.scheduler.min_chunk_minutes
    max_chunk = CONFIG.scheduler.max_chunk_minutes

    working = list(free)
    pieces: list[Slot] = []
    index = 0
    while remaining > 0 and index < len(working):
        slot = working[index]
        if cutoff is not None and slot.start >= cutoff:
            break

        usable_end = min(slot.end, cutoff) if cutoff is not None else slot.end
        available = int((usable_end - slot.start).total_seconds() // 60)
        chunk = min(remaining, available, max_chunk)
        if chunk <= 0 or (chunk < min_chunk and chunk < remaining):
            index += 1
            continue

        chunk_end = slot.start + timedelta(minutes=chunk)
        pieces.append(Slot(start=slot.start, end=chunk_end))
        remaining -= chunk
        if chunk_end < slot.end:
            working[index] = Slot(start=chunk_end, end=slot.end)
        else:
            del working[index]

    if remaining > 0:
        return _Fit(blocks=None, free_after=free)

    blocks = [
        PlanBlock(task=task.id, start=piece.start, end=piece.end, chunk=number, chunks=len(pieces))
        for number, piece in enumerate(pieces, start=1)
    ]
    return _Fit(blocks=blocks, free_after=working)


def _schedulingKey(task: Task) -> _SchedulingKey:
    return (task.deadline is None, task.deadline or date.max, task.priority.rank(), task.created, task.id)


def _deadlineCutoff(deadline: date) -> datetime:
    day_after = datetime.combine(deadline + timedelta(days=1), time(0), tzinfo=CONFIG.timezone)
    return day_after.astimezone(UTC)


### vocabulary #########################################################################


# a sort key stays a tuple: dataclasses do not order, and ordering is the whole point
_SchedulingKey = tuple[bool, date, int, datetime, TaskId]


@dataclass(frozen=True)
class _Fit:
    blocks: list[PlanBlock] | None  # None: the task did not fit and consumed nothing
    free_after: list[Slot]
