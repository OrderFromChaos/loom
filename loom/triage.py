"""Time-derived readings over tasks, plans and events: staleness and deadline pressure.

Nothing here is stored; both readings are recomputed from the plan record and the event
log on every call. Re-planning does not clear staleness, because the record of what was
promised lives in plan_history.jsonl; logging work on the task or acknowledging it does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta

from loom import graph
from loom.config import CONFIG
from loom.vocabulary import (
    DeadlineEntry,
    EventKind,
    Goal,
    GoalId,
    LoggedEvent,
    Plan,
    PlanBlock,
    Task,
    TaskId,
)


def staleTaskIds(
    tasks: Mapping[TaskId, Task],
    plan: Plan | None,
    history: Iterable[PlanBlock],
    events: Iterable[LoggedEvent],
    now: datetime,
) -> frozenset[TaskId]:
    """Open tasks whose latest passed block (plus grace) has no touch at or after it.

    A touch is a 'did' event for the task (at its end time), an 'ack' for it, or the
    task's completion timestamp.
    """
    grace = timedelta(minutes=CONFIG.scheduler.stale_grace_minutes)
    plan_blocks = plan.blocks if plan is not None else ()

    promised: dict[TaskId, datetime] = {}
    for block in (*plan_blocks, *history):
        if block.end + grace <= now:
            recorded = promised.get(block.task)
            if recorded is None or block.end > recorded:
                promised[block.task] = block.end

    touches: dict[TaskId, datetime] = {}
    for event in events:
        if event.task is None:
            continue
        touched_at = event.end if event.kind is EventKind.DID and event.end is not None else event.at
        recorded = touches.get(event.task)
        if recorded is None or touched_at > recorded:
            touches[event.task] = touched_at

    stale = set()
    for task_id, promised_end in promised.items():
        task = tasks.get(task_id)
        if task is None or not graph.stillOpen(task):
            continue
        touch = touches.get(task_id)
        if task.completed is not None and (touch is None or task.completed > touch):
            touch = task.completed
        if touch is None or touch < promised_end:
            stale.add(task_id)

    return frozenset(stale)


def deadlineTasks(
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
    plan: Plan | None,
    now: datetime,
) -> tuple[DeadlineEntry, ...]:
    """Every open task carrying a deadline, soonest first."""
    today = now.astimezone(CONFIG.timezone).date()
    blocked = graph.dependencyBlocked(tasks)

    minutes_planned: dict[TaskId, int] = {}
    for block in plan.blocks if plan is not None else ():
        block_minutes = int((block.end - block.start).total_seconds() // 60)
        minutes_planned[block.task] = minutes_planned.get(block.task, 0) + block_minutes

    entries = []
    for task in tasks.values():
        if task.deadline is None or not graph.stillOpen(task):
            continue
        goal_id = graph.effectiveGoal(task, tasks)
        entries.append(
            DeadlineEntry(
                task=task,
                goal=goals.get(goal_id) if goal_id is not None else None,
                days_remaining=(task.deadline - today).days,
                dependency_blocked=task.id in blocked,
                minutes_planned=minutes_planned.get(task.id, 0),
            ),
        )

    entries.sort(key=lambda entry: (entry.task.deadline or date.max, entry.task.priority.rank(), entry.task.id))
    return tuple(entries)
