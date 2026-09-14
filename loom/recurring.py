"""Materialize recurring templates into real tasks, by anchor days or by interval.

Two cadence modes, exactly one per template. Anchored ('on Mon and Thu'): each anchor
day owes one copy, catch-up copies spawn for missed days (at most 31 days back), and a
copy spawns even while an earlier one is open — an anchored occurrence is its own
obligation. Interval ('every 14 days'): the clock counts from the last completed copy
(falling back to the last spawn), and nothing spawns while a copy is open — you do not
owe two shaves. The copy is a root task under the template's goal, with deadline = its
occurrence date plus the configured offset.

spawnDue runs whenever an area is viewed or re-planned, so copies appear the next time
the app is looked at, not on a background schedule.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from loom import graph
from loom.config import CONFIG
from loom.store import newTaskId, storeFor
from loom.vocabulary import Area, RecurringId, RecurringTask, Status, Task, TaskId


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
    today = now.astimezone(CONFIG.timezone).date()
    done_dates = lastDoneDates(tasks)
    open_origins = {task.origin for task in tasks.values() if task.origin is not None and graph.stillOpen(task)}

    updated = dict(templates)
    spawned = 0
    for template in templates.values():
        occurrences = owedOccurrences(
            template,
            today,
            last_done=done_dates.get(template.id),
            open_copy=template.id in open_origins,
        )
        if not occurrences:
            continue
        for occurrence in occurrences:
            task = spawnTask(template, now, occurrence)
            tasks[task.id] = task
            spawned += 1
        updated[template.id] = replace(template, last_added=now)

    if spawned:
        area_store.saveTasks(tasks)
        area_store.saveRecurring(updated)
        LOG.info('recurring.spawned', extra={'area': area.value, 'count': spawned})

    return spawned


def spawnNow(area: Area, recurring_id: RecurringId, now: datetime) -> TaskId:
    """Force one copy dated today, regardless of the cadence.

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
    task = spawnTask(template, now, today)
    tasks[task.id] = task
    templates[template.id] = replace(template, last_added=now)
    area_store.saveTasks(tasks)
    area_store.saveRecurring(templates)

    return task.id


def owedOccurrences(
    template: RecurringTask,
    today: date,
    *,
    last_done: date | None,
    open_copy: bool,
) -> tuple[date, ...]:
    """The occurrence dates that still owe a copy; at most one for an interval template.

    Anchored: every anchor day after the spawn cursor up to today, looking back at most
    31 days; an open copy does not suppress these. Interval: last completion (or last
    spawn) plus the interval, suppressed entirely while a copy is open; a template that
    never spawned is due today.
    """
    if template.every_days is not None:
        return _intervalOccurrence(template, today, last_done, open_copy)

    return _anchoredOccurrences(template, today)


def _intervalOccurrence(
    template: RecurringTask,
    today: date,
    last_done: date | None,
    open_copy: bool,
) -> tuple[date, ...]:
    """At most one owed date: interval past the newest of completion and spawn."""
    if open_copy or template.every_days is None:
        return ()

    references = [last_done]
    if template.last_added is not None:
        references.append(template.last_added.astimezone(CONFIG.timezone).date())
    known = [reference for reference in references if reference is not None]
    if not known:
        return (today,)

    due = max(known) + timedelta(days=template.every_days)
    return (due,) if due <= today else ()


def _anchoredOccurrences(template: RecurringTask, today: date) -> tuple[date, ...]:
    """Anchor days after the cursor, up to today; the cursor starts the day before creation."""
    zone = CONFIG.timezone
    if template.last_added is not None:
        cursor = template.last_added.astimezone(zone).date()
    else:
        cursor = template.created.astimezone(zone).date() - timedelta(days=1)
    cursor = max(cursor, today - timedelta(days=_CATCHUP_DAYS))

    owed = []
    day = cursor + timedelta(days=1)
    while day <= today:
        if day.weekday() in template.days:
            owed.append(day)
        day += timedelta(days=1)

    return tuple(owed)


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


def nextDue(template: RecurringTask, last_done: date | None) -> date | None:
    """The date the template next spawns, None when that is unknowable (never spawned)."""
    if template.every_days is not None:
        references = [last_done]
        if template.last_added is not None:
            references.append(template.last_added.astimezone(CONFIG.timezone).date())
        known = [reference for reference in references if reference is not None]
        if not known:
            return None
        return max(known) + timedelta(days=template.every_days)

    if not template.days:
        return None
    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()
    for offset in range(1, 8):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in template.days:
            return candidate

    return None
