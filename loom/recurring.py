"""Materialize recurring templates into real tasks, by anchor days or by interval.

Two cadence modes, exactly one per template: anchored ('on Mon and Thu') or interval
('every 4 days'). A third choice cuts across both, wait_for_completion:

    waits      at most one copy is ever open; an interval counts from the later of the
               last completion and the last spawn (shaving: you only shave once)
    stacks     every occurrence owes its own copy, open copies or not, caught up at most
               31 days back; an interval keeps a fixed rhythm (dishes: a pile of copies
               shows how far behind you are)

Each copy is a root task under the template's goal, with deadline = its occurrence date
plus the configured offset, and the template's step tree comes along as nested subtasks.

Three manual corrections cover what the app cannot know. Pull ahead spawns a copy now
and restarts the clock from now (the trash went out early). Delay to now merges every
open copy into one dated today and restarts the clock from now (shaving waited a few
days on purpose). An interval template's next date can also be set by hand (next_due),
which realigns the rhythm without spawning anything until that date.

spawnDue runs when an area is viewed and in the server's periodic housekeeping.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from loom import graph
from loom.config import CONFIG
from loom.store import newTaskId, storeFor
from loom.vocabulary import Area, RecurringId, RecurringStep, RecurringTask, Status, StepId, StepIndex, Task, TaskId


_CATCHUP_DAYS = 31

LOG = logging.getLogger('loom')


def spawnDue(area: Area, now: datetime) -> int:
    """Create a task per owed occurrence across this area; returns how many spawned.

    Raises:
        StoreError: tasks.json or recurring.json is unreadable or malformed.
    """
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    if not templates:
        return 0

    tasks = area_store.loadTasks()
    steps = area_store.loadSteps()
    today = now.astimezone(CONFIG.timezone).date()
    done_dates = lastDoneDates(tasks)
    open_counts = openCopies(tasks)

    updated = dict(templates)
    spawned = 0
    for template in templates.values():
        occurrences = owedOccurrences(
            template,
            today,
            last_done=done_dates.get(template.id),
            open_copy=open_counts[template.id] > 0,
        )
        if not occurrences:
            continue
        for occurrence in occurrences:
            tasks.update((task.id, task) for task in spawnCopy(template, steps, now, occurrence))
            spawned += 1
        updated[template.id] = replace(
            template,
            last_added=_clockAfter(template, occurrences[-1], now),
            next_due=None,
        )

    if spawned:
        area_store.saveTasks(tasks)
        area_store.saveRecurring(updated)
        LOG.info('recurring.spawned', extra={'area': area, 'count': spawned})

    return spawned


def pullAhead(area: Area, recurring_id: RecurringId, now: datetime) -> TaskId:
    """Spawn one copy dated today and restart the template's clock from now.

    Raises:
        KeyError: the template does not exist.
        StoreError: the data files are unreadable or malformed.
    """
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    template = templates.get(recurring_id)
    if template is None:
        raise KeyError(recurring_id)

    tasks = area_store.loadTasks()
    today = now.astimezone(CONFIG.timezone).date()
    copy = spawnCopy(template, area_store.loadSteps(), now, today)
    tasks.update((task.id, task) for task in copy)
    templates[template.id] = replace(template, last_added=now, next_due=None)
    area_store.saveTasks(tasks)
    area_store.saveRecurring(templates)

    return copy[0].id


def delayToNow(area: Area, recurring_id: RecurringId, now: datetime) -> None:
    """Merge the template's open copies into one dated today, and restart its clock from now.

    Raises:
        KeyError: the template does not exist.
        StoreError: the data files are unreadable or malformed.
    """
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    template = templates.get(recurring_id)
    if template is None:
        raise KeyError(recurring_id)

    tasks = area_store.loadTasks()
    area_store.saveTasks(mergeOpenCopies(template, tasks, now))
    templates[template.id] = replace(template, last_added=now, next_due=None)
    area_store.saveRecurring(templates)


def mergeOpenCopies(template: RecurringTask, tasks: Mapping[TaskId, Task], now: datetime) -> dict[TaskId, Task]:
    """Collapse a template's open copies into one, as if it had spawned today.

    The newest open copy survives with its subtasks, its deadline moved to today plus
    the template's offset. Every older open copy is deleted with everything beneath it,
    and other tasks' dependencies on deleted tasks are dropped. With no open copy the
    tasks come back unchanged.
    """
    copies = sorted(
        (task for task in tasks.values() if task.origin == template.id and graph.stillOpen(task)),
        key=lambda task: (task.created, task.id),
    )
    if not copies:
        return dict(tasks)

    doomed: set[TaskId] = set()
    for older in copies[:-1]:
        doomed |= graph.subtreeIds(older.id, tasks)

    merged = {}
    for task_id, task in tasks.items():
        if task_id in doomed:
            continue
        if any(dependency in doomed for dependency in task.blocked_by):
            task = replace(task, blocked_by=tuple(dep for dep in task.blocked_by if dep not in doomed))
        merged[task_id] = task

    survivor = copies[-1]
    today = now.astimezone(CONFIG.timezone).date()
    deadline = None
    if template.deadline_after_days is not None:
        deadline = today + timedelta(days=template.deadline_after_days)
    merged[survivor.id] = replace(merged[survivor.id], deadline=deadline)
    return merged


def owedOccurrences(
    template: RecurringTask,
    today: date,
    *,
    last_done: date | None,
    open_copy: bool,
) -> tuple[date, ...]:
    """The occurrence dates that still owe a copy.

    A template that waits for completion owes nothing while a copy is open, and at most
    one date otherwise: for an interval the first date due, for anchor days the latest
    one passed. One that does not wait owes every occurrence since its clock, at most
    31 days back, open copies or not. An interval template that never spawned is due
    today; an anchored one counts from the day before its creation. A hand-set next date
    on an interval template replaces the clock: it is the first occurrence owed.
    """
    start = _clockStart(template, last_done)
    if template.every_days is not None and template.next_due is not None:
        # a hand-set next date is itself the first occurrence: count from one interval before it
        owed = _intervalOccurrences(template.every_days, template.next_due - timedelta(days=template.every_days), today)
    elif template.every_days is not None:
        owed = _intervalOccurrences(template.every_days, start, today)
    else:
        owed = _anchoredOccurrences(template, start, today)

    if template.wait_for_completion:
        if open_copy:
            return ()
        return owed[:1] if template.every_days is not None else owed[-1:]

    floor = today - timedelta(days=_CATCHUP_DAYS)
    return tuple(day for day in owed if day > floor)


def _clockStart(template: RecurringTask, last_done: date | None) -> date | None:
    """The local date the cadence counts from: the last spawn, or for a template that
    waits for completion the later of that and the last completion; None before either."""
    known = []
    if template.last_added is not None:
        known.append(template.last_added.astimezone(CONFIG.timezone).date())
    if template.wait_for_completion and last_done is not None:
        known.append(last_done)

    return max(known, default=None)


def _clockAfter(template: RecurringTask, last_occurrence: date, now: datetime) -> datetime:
    """Where the clock stands after spawning through `last_occurrence`.

    A fixed rhythm (an interval that does not wait) counts from the occurrence's own
    date, so a copy spawned late does not push every later copy back. Every other
    template counts from the spawn moment.
    """
    if template.every_days is not None and not template.wait_for_completion:
        return datetime.combine(last_occurrence, time(0), tzinfo=CONFIG.timezone).astimezone(UTC)

    return now


def _intervalOccurrences(every_days: int, start: date | None, today: date) -> tuple[date, ...]:
    """Every interval date after the clock start up to today; today alone before any spawn."""
    if start is None:
        return (today,)

    owed = []
    day = start + timedelta(days=every_days)
    while day <= today:
        owed.append(day)
        day += timedelta(days=every_days)

    return tuple(owed)


def _anchoredOccurrences(template: RecurringTask, start: date | None, today: date) -> tuple[date, ...]:
    """Anchor days after the clock start, up to today."""
    if start is None:
        start = template.created.astimezone(CONFIG.timezone).date() - timedelta(days=1)

    owed = []
    day = start + timedelta(days=1)
    while day <= today:
        if day.weekday() in template.days:
            owed.append(day)
        day += timedelta(days=1)

    return tuple(owed)


def openCopies(tasks: Mapping[TaskId, Task]) -> Counter[RecurringId]:
    """How many open spawned copies each template has in the tree; absent means none."""
    return Counter(task.origin for task in tasks.values() if task.origin is not None and graph.stillOpen(task))


def spawnCopy(
    template: RecurringTask,
    steps: Mapping[StepId, RecurringStep],
    now: datetime,
    occurrence: date,
) -> tuple[Task, ...]:
    """The tasks one occurrence stamps out: the root copy first, then its steps as subtasks.

    The walk is iterative, so a step tree of any depth copies. A step whose parent chain
    dangles or loops is unreachable from the template and is not copied. Each task is
    stamped one microsecond after the one before: siblings sort by created, so this keeps
    the template's step order in the tree.

    Raises:
        Nothing: steps come from the parse gate already well-formed.
    """
    root = spawnTask(template, now, occurrence)
    children = stepsByParent(steps, template.id)
    spawned = [root]
    stack = [(step, root.id) for step in reversed(children.get(None, ()))]
    seen: set[StepId] = set()
    while stack:
        step, parent_task = stack.pop()
        if step.id in seen:
            continue
        seen.add(step.id)
        task = Task(
            id=newTaskId(),
            title=step.title,
            status=Status.TODO,
            parent=parent_task,
            goal=None,
            blocked_by=(),
            notes='',
            estimate_minutes=step.estimate_minutes,
            deadline=None,
            auto_schedule=step.auto_schedule,
            priority=template.priority,
            created=now + timedelta(microseconds=len(spawned)),
            completed=None,
        )
        spawned.append(task)
        stack.extend((child, task.id) for child in reversed(children.get(step.id, ())))

    return tuple(spawned)


def stepsByParent(
    steps: Mapping[StepId, RecurringStep],
    template_id: RecurringId,
) -> StepIndex:
    """One template's steps grouped under their parent step (None: the top level), oldest first."""
    grouped: dict[StepId | None, list[RecurringStep]] = {}
    for step in steps.values():
        if step.template == template_id:
            grouped.setdefault(step.parent, []).append(step)

    return {parent: tuple(sorted(group, key=lambda step: (step.created, step.id))) for parent, group in grouped.items()}


def stepSubtree(root: StepId, steps: Mapping[StepId, RecurringStep]) -> frozenset[StepId]:
    """A step and every step beneath it; a looping parent chain cannot trap the walk."""
    children: dict[StepId, list[StepId]] = {}
    for step in steps.values():
        if step.parent is not None:
            children.setdefault(step.parent, []).append(step.id)

    found: set[StepId] = set()
    stack = [root]
    while stack:
        step_id = stack.pop()
        if step_id in found:
            continue
        found.add(step_id)
        stack.extend(children.get(step_id, ()))

    return frozenset(found)


def spawnTask(template: RecurringTask, now: datetime, occurrence: date) -> Task:
    """The task one occurrence stamps out: a root task under the template's goal.

    The deadline offsets from the occurrence date, not the spawn moment, so a caught-up
    copy of a missed occurrence is already due (or overdue) exactly as the cadence implies.
    """
    deadline = None
    if template.deadline_after_days is not None:
        deadline = occurrence + timedelta(days=template.deadline_after_days)

    return Task(
        id=newTaskId(),
        title=template.title,
        status=Status.TODO,
        parent=None,
        goal=template.goal,
        blocked_by=(),
        notes=template.notes,
        estimate_minutes=template.estimate_minutes,
        deadline=deadline,
        auto_schedule=template.auto_schedule,
        priority=template.priority,
        created=now,
        completed=None,
        origin=template.id,
    )


def lastDoneDates(tasks: Mapping[TaskId, Task]) -> dict[RecurringId, date]:
    """Latest completion local date per template, read off the copies it spawned."""
    zone = CONFIG.timezone
    done: dict[RecurringId, date] = {}
    for task in tasks.values():
        if task.origin is None or task.completed is None:
            continue
        when = task.completed.astimezone(zone).date()
        if task.origin not in done or when > done[task.origin]:
            done[task.origin] = when

    return done


def nextDue(template: RecurringTask, last_done: date | None, *, open_copy: bool) -> date | None:
    """The date the template next spawns, None when that is unknowable (never spawned).

    A template that waits for completion restarts its clock when the open copy is done,
    so while one is open this projects finishing it today.
    """
    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()
    if template.next_due is not None:
        return template.next_due
    if template.every_days is not None:
        start = _clockStart(template, last_done)
        if template.wait_for_completion and open_copy:
            start = max(start, today) if start is not None else today
        return start + timedelta(days=template.every_days) if start is not None else None

    if not template.days:
        return None
    for offset in range(1, 8):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in template.days:
            return candidate

    return None
