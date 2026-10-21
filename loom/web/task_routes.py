"""The POST routes that change tasks. The area is re-planned before each redirect."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, time

from loom import scheduler, store
from loom.config import CONFIG
from loom.vocabulary import GoalId, Priority, Status, Task, TaskId, TaskOrder
from loom.web import request_fields, responses
from loom.web.web_vocabulary import FormError, Request, Response


def handleAddTask(request: Request) -> Response:
    """Create a task under an optional parent and goal, then re-plan the area.

    Raises:
        FormError: the title is missing, or a named parent does not exist.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()

    raw_parent = request.form.get('parent', '')
    parent = TaskId(raw_parent) if raw_parent else None
    if parent is not None and parent not in tasks:
        raise FormError(f'parent {parent} does not exist')
    raw_goal = request.form.get('goal', '')

    now = datetime.now(UTC)
    task = Task(
        id=store.generateTaskId(),
        title=request_fields.readFormText(request.form, 'title'),
        status=Status.TODO,
        parent=parent,
        goal=GoalId(raw_goal) if raw_goal else None,
        blocked_by=(),
        notes='',
        estimate_minutes=request_fields.readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        deadline=request_fields.readFormOptionalDate(request.form, 'deadline'),
        auto_schedule='auto_schedule' in request.form,
        priority=Priority.MEDIUM,
        created=now,
        completed=None,
    )
    tasks[task.id] = task
    area_store.saveTasks(tasks)
    scheduler.runPlan(area, now)
    return responses.buildReturnRedirect(request, area)


def handleToggleOrder(request: Request) -> Response:
    """Flip the area between deadline-first and shortest-first, then re-plan in that order."""
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    preferences = area_store.loadPreferences()
    match preferences.order:
        case TaskOrder.DEADLINE:
            flipped = TaskOrder.SHORTEST
        case TaskOrder.SHORTEST:
            flipped = TaskOrder.DEADLINE

    area_store.savePreferences(replace(preferences, order=flipped))
    scheduler.runPlan(area, datetime.now(UTC))
    return responses.buildReturnRedirect(request, area)


def handleEditTask(request: Request) -> Response:
    """Apply the edit form to one task, then re-plan the area."""
    return _handleTaskChange(request, lambda task: _applyTaskEditForm(request.form, task))


def _handleTaskChange(request: Request, change: Callable[[Task], Task]) -> Response:
    """Replace the route's task with change(task), save, re-plan the area, and redirect.

    Raises:
        FormError: the task does not exist, or `change` raised it. Nothing is written.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = request_fields.findRecord(tasks, TaskId(request.match.group('id')), 'task')

    tasks[task.id] = change(task)
    area_store.saveTasks(tasks)
    scheduler.runPlan(area, datetime.now(UTC))
    return responses.buildReturnRedirect(request, area)


def _applyTaskEditForm(form: Mapping[str, str], task: Task) -> Task:
    """Apply the task edit form. Moving into DONE stamps completed, and leaving DONE clears it.

    Raises:
        FormError: a field is absent or malformed.
    """
    status = request_fields.readFormEnum(form, 'status', Status)
    completed = task.completed
    if status is Status.DONE and completed is None:
        completed = datetime.now(UTC)
    if status is not Status.DONE:
        completed = None

    return replace(
        task,
        title=request_fields.readFormText(form, 'title'),
        notes=form.get('notes', '').strip(),
        estimate_minutes=request_fields.readFormOptionalPositiveInt(form, 'estimate_minutes'),
        deadline=request_fields.readFormOptionalDate(form, 'deadline'),
        status=status,
        priority=request_fields.readFormEnum(form, 'priority', Priority),
        auto_schedule='auto_schedule' in form,
        completed=completed,
        color=_readFormColor(form, task),
        not_before=_readFormNotBefore(form),
    )


def _readFormColor(form: Mapping[str, str], task: Task) -> str | None:
    """Read the chosen palette preset name. The task's current name is valid even when retired.

    Raises:
        FormError: the name is not in the palette and not the task's current color.
    """
    raw_color = form.get('color', '').strip()
    if not raw_color:
        return None
    if raw_color not in CONFIG.palette and raw_color != task.color:
        raise FormError(f'{raw_color!r} is not a palette preset (add it on the Settings page)')

    return raw_color


def _readFormNotBefore(form: Mapping[str, str]) -> datetime | None:
    """Read the edit form's not-before day and optional time as local wall-clock.

    An empty day clears it, and an empty time means the start of that day.

    Raises:
        FormError: the day or the time does not parse.
    """
    day = request_fields.readFormOptionalDate(form, 'not_before_day')
    if day is None:
        return None

    time_of_day = (
        request_fields.readFormClock(form, 'not_before_time') if form.get('not_before_time', '').strip() else time(0)
    )
    return datetime.combine(day, time_of_day, tzinfo=CONFIG.timezone).astimezone(UTC)


def handleTickTask(request: Request) -> Response:
    return _handleTaskChange(request, lambda task: replace(task, status=Status.DONE, completed=datetime.now(UTC)))


def handleReopenTask(request: Request) -> Response:
    return _handleTaskChange(request, lambda task: replace(task, status=Status.TODO, completed=None))


def handleToggleAuto(request: Request) -> Response:
    return _handleTaskChange(request, lambda task: replace(task, auto_schedule=not task.auto_schedule))


def handleSetEstimate(request: Request) -> Response:
    """Change one task's estimate, then re-plan. An empty value clears the estimate.

    Raises:
        FormError: the task does not exist or the minutes are not a positive number.
    """

    def setEstimate(task: Task) -> Task:
        return replace(
            task,
            estimate_minutes=request_fields.readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        )

    return _handleTaskChange(request, setEstimate)


def handleDeleteTask(request: Request) -> Response:
    """Delete a leaf task and strip every dependency edge pointing at it."""
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = request_fields.findRecord(tasks, TaskId(request.match.group('id')), 'task')

    if any(other.parent == task.id for other in tasks.values()):
        raise FormError(f'{task.title!r} still has children. Delete or move them first')

    del tasks[task.id]
    for other in list(tasks.values()):
        if task.id in other.blocked_by:
            tasks[other.id] = replace(
                other,
                blocked_by=tuple(dependency for dependency in other.blocked_by if dependency != task.id),
            )
    area_store.saveTasks(tasks)
    scheduler.runPlan(area, datetime.now(UTC))
    return responses.buildReturnRedirect(request, area)


def handleAddDependency(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = request_fields.findRecord(tasks, TaskId(request.match.group('id')), 'task')

    dependency = TaskId(request_fields.readFormText(request.form, 'blocked_by'))
    if dependency not in tasks:
        raise FormError(f'{dependency} does not exist (pick a task from the list)')
    if dependency == task.id:
        raise FormError('a task cannot block itself')

    if dependency not in task.blocked_by:
        tasks[task.id] = replace(task, blocked_by=(*task.blocked_by, dependency))
        area_store.saveTasks(tasks)
        scheduler.runPlan(area, datetime.now(UTC))
    return responses.buildReturnRedirect(request, area)


def handleRemoveDependency(request: Request) -> Response:
    def removeDependency(task: Task) -> Task:
        dependency = request_fields.readFormText(request.form, 'blocked_by')
        return replace(task, blocked_by=tuple(kept for kept in task.blocked_by if kept != dependency))

    return _handleTaskChange(request, removeDependency)


def handleReschedule(request: Request) -> Response:
    area = request_fields.readArea(request)
    scheduler.runPlan(area, datetime.now(UTC))
    return responses.buildReturnRedirect(request, area)
