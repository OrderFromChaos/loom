"""Materialize recurring templates into real tasks, by anchor days or by interval.

Two cadence modes, exactly one per template: anchored ('on Mon and Thu') or interval
('every 4 days'). A third choice cuts across both, wait_for_completion:

    waits      at most one copy is ever open. An interval counts from the later of the
               last completion and the last spawn (shaving: you only shave once)
    stacks     every occurrence owes a copy, open copies or not, caught up at most 31
               days back. An interval keeps a fixed rhythm (dishes: a pile of copies
               shows how far behind you are)

Each copy is a root task under the template's goal, with deadline = its occurrence date
plus the configured offset, and the template's step tree comes along as nested subtasks.

Three manual corrections cover facts outside the data. Pull ahead spawns a copy now and
restarts the clock from now (the trash went out early). Delay to now merges every open
copy into one dated today and restarts the clock from now (the shave was put off for a
few days). An interval template's next date can also be set by hand (next_due). The
rhythm then realigns, and nothing spawns until that date.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from loom import config, graph, store
from loom.config import CONFIG
from loom.vocabulary import (
    DAY_NAMES,
    Area,
    RecurringId,
    RecurringStep,
    RecurringTask,
    Status,
    StepId,
    StepIndex,
    Task,
    TaskId,
    TemplateNotFoundError,
)


_CATCHUP_DAYS = 31

LOG = logging.getLogger('loom')


def spawnDueCopies(area: Area, now: datetime) -> int:
    """Create the copies owed across this area, and return how many spawned.

    Raises:
        StoreError: tasks.json or recurring.json is unreadable or malformed.
    """
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    if not templates:
        return 0

    tasks = area_store.loadTasks()
    steps = area_store.loadSteps()
    today = config.computeLocalDate(now)
    done_dates = collectLastDoneDates(tasks)
    open_counts = countOpenCopies(tasks)

    updated = dict(templates)
    spawned = 0
    for template in templates.values():
        occurrences = listOwedOccurrences(
            template,
            today,
            last_done=done_dates.get(template.id),
            open_copy=open_counts[template.id] > 0,
        )
        if not occurrences:
            continue
        for occurrence in occurrences:
            tasks.update((task.id, task) for task in buildCopy(template, steps, now, occurrence))
            spawned += 1
        updated[template.id] = replace(
            template,
            last_added=_computeClockAfter(template, occurrences[-1], now),
            next_due=None,
        )

    if spawned:
        area_store.saveTasks(tasks)
        area_store.saveTemplates(updated)
        LOG.info('recurring.spawned', extra={'area': area, 'count': spawned})

    return spawned


def collectLastDoneDates(tasks: Mapping[TaskId, Task]) -> dict[RecurringId, date]:
    """Collect the latest local completion date of each template's copies."""
    done: dict[RecurringId, date] = {}
    for task in tasks.values():
        if task.origin is None or task.completed is None:
            continue
        when = config.computeLocalDate(task.completed)
        if task.origin not in done or when > done[task.origin]:
            done[task.origin] = when

    return done


def countOpenCopies(tasks: Mapping[TaskId, Task]) -> Counter[RecurringId]:
    """Count the open spawned copies of each template. An absent template has none."""
    return Counter(task.origin for task in tasks.values() if task.origin is not None and graph.stillOpen(task))


def listOwedOccurrences(
    template: RecurringTask,
    today: date,
    *,
    last_done: date | None,
    open_copy: bool,
) -> tuple[date, ...]:
    """List the occurrence dates that still owe a copy.

    A template that waits for completion owes nothing while a copy is open, and at most
    one date otherwise: for an interval the first date due, for anchor days the latest
    one passed. One that does not wait owes every occurrence since its clock, at most
    31 days back, open copies or not. An interval template that never spawned is due
    today. An anchored one counts from the day before its creation. A hand-set next date
    on an interval template replaces the clock: it is the first occurrence owed.
    """
    start = _computeClockStart(template, last_done)
    if template.every_days is not None and template.next_due is not None:
        # a hand-set next date is itself the first occurrence: count from one interval before it
        first_due = template.next_due - timedelta(days=template.every_days)
        owed = _listIntervalOccurrences(template.every_days, first_due, today)
    elif template.every_days is not None:
        owed = _listIntervalOccurrences(template.every_days, start, today)
    else:
        owed = _listAnchoredOccurrences(template, start, today)

    if template.wait_for_completion:
        if open_copy:
            return ()
        return owed[:1] if template.every_days is not None else owed[-1:]

    floor = today - timedelta(days=_CATCHUP_DAYS)
    return tuple(day for day in owed if day > floor)


def _computeClockStart(template: RecurringTask, last_done: date | None) -> date | None:
    """Compute the local date the cadence counts from, None before any spawn or completion.

    The date is the last spawn, or for a template that waits for completion, the later of
    the last spawn and the last completion.
    """
    known = []
    if template.last_added is not None:
        known.append(config.computeLocalDate(template.last_added))
    if template.wait_for_completion and last_done is not None:
        known.append(last_done)

    return max(known, default=None)


def _listIntervalOccurrences(every_days: int, start: date | None, today: date) -> tuple[date, ...]:
    """List every interval date after the clock start up to today. Before any spawn, the list is today alone."""
    if start is None:
        return (today,)

    owed = []
    day = start + timedelta(days=every_days)
    while day <= today:
        owed.append(day)
        day += timedelta(days=every_days)

    return tuple(owed)


def _listAnchoredOccurrences(template: RecurringTask, start: date | None, today: date) -> tuple[date, ...]:
    """List the anchor days after the clock start, up to today."""
    if start is None:
        start = config.computeLocalDate(template.created) - timedelta(days=1)

    owed = []
    day = start + timedelta(days=1)
    while day <= today:
        if day.weekday() in template.days:
            owed.append(day)
        day += timedelta(days=1)

    return tuple(owed)


def buildCopy(
    template: RecurringTask,
    steps: Mapping[StepId, RecurringStep],
    now: datetime,
    occurrence: date,
) -> tuple[Task, ...]:
    """Build the tasks of one occurrence: the root copy first, then its steps as subtasks.

    The walk is iterative, so a step tree of any depth copies. A step whose parent chain
    dangles or loops is unreachable from the template and is not copied. Siblings sort by
    created, so each task is created one microsecond after the one before, in the
    template's step order.

    Raises:
        Nothing: steps are well-formed once parsed.
    """
    root = _buildRootCopy(template, now, occurrence)
    children = groupStepsByParent(steps, template.id)
    spawned = [root]
    stack = [(step, root.id) for step in reversed(children.get(None, ()))]
    seen: set[StepId] = set()
    while stack:
        step, parent_task = stack.pop()
        if step.id in seen:
            continue
        seen.add(step.id)
        task = Task(
            id=store.generateTaskId(),
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


def _buildRootCopy(template: RecurringTask, now: datetime, occurrence: date) -> Task:
    """Build the root task of one occurrence, under the template's goal.

    The deadline counts from the occurrence date, so a caught-up copy of a missed
    occurrence is already due, or overdue, as the cadence implies.
    """
    deadline = None
    if template.deadline_after_days is not None:
        deadline = occurrence + timedelta(days=template.deadline_after_days)

    return Task(
        id=store.generateTaskId(),
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


def groupStepsByParent(
    steps: Mapping[StepId, RecurringStep],
    template_id: RecurringId,
) -> StepIndex:
    """Group one template's steps under their parent step, oldest first. The None key groups the top level."""
    grouped: dict[StepId | None, list[RecurringStep]] = {}
    for step in steps.values():
        if step.template == template_id:
            grouped.setdefault(step.parent, []).append(step)

    return {parent: tuple(sorted(group, key=lambda step: (step.created, step.id))) for parent, group in grouped.items()}


def _computeClockAfter(template: RecurringTask, last_occurrence: date, now: datetime) -> datetime:
    """Compute where the clock stands after spawning through `last_occurrence`.

    A fixed rhythm, an interval that does not wait, counts from the date of the
    occurrence, so a late spawn leaves the dates of later copies unchanged. Every other
    template counts from the spawn moment.
    """
    if template.every_days is not None and not template.wait_for_completion:
        return datetime.combine(last_occurrence, time(0), tzinfo=CONFIG.timezone).astimezone(UTC)

    return now


def pullAhead(area: Area, template_id: RecurringId, now: datetime) -> TaskId:
    """Spawn one copy dated today and restart the template's clock from now.

    Raises:
        TemplateNotFoundError: the template does not exist.
        StoreError: the data files are unreadable or malformed.
    """
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = templates.get(template_id)
    if template is None:
        raise TemplateNotFoundError(template_id)

    tasks = area_store.loadTasks()
    copy = buildCopy(template, area_store.loadSteps(), now, config.computeLocalDate(now))
    tasks.update((task.id, task) for task in copy)
    templates[template.id] = replace(template, last_added=now, next_due=None)
    area_store.saveTasks(tasks)
    area_store.saveTemplates(templates)

    return copy[0].id


def delayToNow(area: Area, template_id: RecurringId, now: datetime) -> None:
    """Merge the template's open copies into one dated today, and restart its clock from now.

    Raises:
        TemplateNotFoundError: the template does not exist.
        StoreError: the data files are unreadable or malformed.
    """
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = templates.get(template_id)
    if template is None:
        raise TemplateNotFoundError(template_id)

    tasks = area_store.loadTasks()
    area_store.saveTasks(_mergeOpenCopies(template, tasks, now))
    templates[template.id] = replace(template, last_added=now, next_due=None)
    area_store.saveTemplates(templates)


def _mergeOpenCopies(template: RecurringTask, tasks: Mapping[TaskId, Task], now: datetime) -> dict[TaskId, Task]:
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
        doomed |= graph.collectSubtreeIds(older.id, tasks)

    merged = {}
    for task_id, task in tasks.items():
        if task_id in doomed:
            continue
        if any(dependency in doomed for dependency in task.blocked_by):
            kept = tuple(dependency for dependency in task.blocked_by if dependency not in doomed)
            task = replace(task, blocked_by=kept)
        merged[task_id] = task

    survivor = copies[-1]
    deadline = None
    if template.deadline_after_days is not None:
        deadline = config.computeLocalDate(now) + timedelta(days=template.deadline_after_days)
    merged[survivor.id] = replace(merged[survivor.id], deadline=deadline)
    return merged


def collectStepSubtreeIds(root: StepId, steps: Mapping[StepId, RecurringStep]) -> frozenset[StepId]:
    """Collect a step and every step beneath it, even through a looping parent chain."""
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


def computeNextDue(template: RecurringTask, last_done: date | None, *, open_copy: bool) -> date | None:
    """Compute the date the template next spawns, None for an interval template with no clock yet.

    A template that waits for completion restarts its clock when the open copy is done.
    While a copy is open, the date is computed as if the copy were finished today.
    """
    today = config.computeLocalDate(datetime.now(UTC))
    if template.next_due is not None:
        return template.next_due
    if template.every_days is not None:
        start = _computeClockStart(template, last_done)
        if template.wait_for_completion and open_copy:
            start = max(start, today) if start is not None else today
        return start + timedelta(days=template.every_days) if start is not None else None

    if not template.days:
        return None
    for offset in range(1, len(DAY_NAMES) + 1):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in template.days:
            return candidate

    return None
