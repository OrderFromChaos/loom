"""HTTP plumbing: routes, form parsing, dispatch, and the single-threaded server.

Zero cache: every GET re-reads the data files through the store's parse gate. An edit
made by hand or by an LLM is on the next refresh, and a malformed file gets an error
page with the file's path. Mutations are plain form POSTs answered with 303 redirects,
and every task-mutating route re-plans its area before redirecting. The server is
single-threaded: requests run one at a time, so a read-modify-write never interleaves
with another request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time as clock
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from http import HTTPMethod, HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit

from loom import config, graph, live_config, recurring, scheduler, store, triage
from loom.config import CONFIG
from loom.vocabulary import (
    DAY_NAMES,
    Area,
    ConfigError,
    EventKind,
    EventSource,
    Goal,
    GoalId,
    LoggedEvent,
    Priority,
    RecurringId,
    RecurringStep,
    RecurringTask,
    Status,
    StepId,
    StoreConflictError,
    StoreError,
    Task,
    TaskId,
    TaskOrder,
    TemplateNotFoundError,
)
from loom.web import (
    achievements_page,
    calendar_page,
    deadlines_page,
    page_shell,
    recurring_page,
    settings_page,
    tasks_page,
)
from loom.web.web_vocabulary import FormError, MoveDirection


_LISTEN_HOST = '127.0.0.1'
_HOUSEKEEPING_SECONDS = 300
_STATIC_DIR = Path(__file__).resolve().parent / 'static'
_STATIC_FILES = {
    'style.css': 'text/css; charset=utf-8',
    'calendar.js': 'text/javascript; charset=utf-8',
    'tree.js': 'text/javascript; charset=utf-8',
    'pickers.js': 'text/javascript; charset=utf-8',
    'areas.js': 'text/javascript; charset=utf-8',
    'restore.js': 'text/javascript; charset=utf-8',
}
_HTML_CONTENT_TYPE = 'text/html; charset=utf-8'

LOG = logging.getLogger('loom')

_HOME_AREA = Area('work')  # where GET / redirects, set by serveForever from the CLI
_PORT = 0  # the listening port, set by serveForever. A settings restart reuses it


def serveForever(home: Area, port: int) -> int:
    """Serve requests and, between them, keep recurring copies spawned in every area.

    Housekeeping runs at startup and then every five minutes in this same thread, so a
    copy appears on its day without anyone opening the page, and requests and
    housekeeping still never interleave.
    """
    global _HOME_AREA, _PORT
    _HOME_AREA = home
    _PORT = port
    server = HTTPServer((_LISTEN_HOST, port), _Handler)
    LOG.info('server.listening', extra={'url': f'http://{_LISTEN_HOST}:{port}/', 'home': home})
    try:
        next_housekeeping = 0.0
        while True:
            if clock.monotonic() >= next_housekeeping:
                _housekeep(datetime.now(UTC))
                next_housekeeping = clock.monotonic() + _HOUSEKEEPING_SECONDS
            server.timeout = max(0.0, next_housekeeping - clock.monotonic())
            server.handle_request()
    except KeyboardInterrupt:
        LOG.info('server.stopped')
    finally:
        server.server_close()

    return 0


def _housekeep(now: datetime) -> None:
    """Spawn owed recurring copies in every open area. A broken area is logged and skipped."""
    for area in config.listOpenAreas():
        try:
            _refreshArea(area, now)
        except StoreError as exc:
            LOG.warning('housekeeping.skipped', extra={'area': area, 'error': str(exc)})


def _refreshArea(area: Area, now: datetime) -> None:
    """Spawn due recurring copies, and re-plan when any appeared."""
    if recurring.spawnDueCopies(area, now):
        scheduler.runPlan(area, now)


def _dispatch(handler: _Handler, method: HTTPMethod) -> None:
    """Match the path against the route table, run the handler, translate errors to pages."""
    split = urlsplit(handler.path)
    query = dict(parse_qsl(split.query))
    form: dict[str, str] = {}
    if method is HTTPMethod.POST:
        length = int(handler.headers.get('Content-Length', '0') or '0')
        form = dict(parse_qsl(handler.rfile.read(length).decode('utf-8')))

    for route_method, pattern, route_handler in _ROUTES:
        matched = pattern.match(split.path)
        if route_method is not method or matched is None:
            continue
        try:
            response = route_handler(_Request(match=matched, query=query, form=form))
        except FormError as exc:
            response = _buildErrorPage(HTTPStatus.BAD_REQUEST, 'That form did not parse', str(exc))
        except ConfigError as exc:
            # a POST that edits loom.toml raises ConfigError for a bad value, and the file is unchanged
            if method is HTTPMethod.POST:
                response = _buildErrorPage(HTTPStatus.BAD_REQUEST, 'That form did not parse', str(exc))
            else:
                response = _buildErrorPage(HTTPStatus.INTERNAL_SERVER_ERROR, 'loom.toml did not parse', str(exc))
        except StoreConflictError as exc:
            message = f'{exc}. Go back, refresh, and retry.'
            response = _buildErrorPage(HTTPStatus.CONFLICT, 'The file changed under us', message)
        except StoreError as exc:
            response = _buildErrorPage(HTTPStatus.INTERNAL_SERVER_ERROR, 'A data file did not parse', str(exc))
        _send(handler, response)
        if response.restart:
            # exec replaces this process in place, so the new CONFIG loads and the
            # listening socket reopens on the same port a moment later
            handler.wfile.flush()
            LOG.info('server.restarting', extra={'reason': 'settings saved'})
            os.execv(
                sys.executable,
                [sys.executable, '-m', 'loom', 'serve', '--area', _HOME_AREA, '--port', str(_PORT)],
            )
        return

    _send(handler, _buildErrorPage(HTTPStatus.NOT_FOUND, 'Not found', f'no route for {method.value} {split.path}'))


def _send(handler: _Handler, response: _Response) -> None:
    payload = response.body.encode('utf-8') if isinstance(response.body, str) else response.body
    handler.send_response(response.status)
    if response.location is not None:
        handler.send_header('Location', response.location)
    handler.send_header('Content-Type', response.content_type)
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _handleHome(request: _Request) -> _Response:
    return _buildRedirect(f'/{_HOME_AREA}/calendar')


def _handleStatic(request: _Request) -> _Response:
    file_name = request.match.group('name')
    content_type = _STATIC_FILES.get(file_name)
    if content_type is None:
        return _buildErrorPage(HTTPStatus.NOT_FOUND, 'Not found', f'{file_name} is not a static file here')

    body = (_STATIC_DIR / file_name).read_bytes()
    return _Response(status=HTTPStatus.OK, body=body, content_type=content_type, location=None)


def _handleCalendarPage(request: _Request) -> _Response:
    """Render the week view from freshly loaded files.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = _readArea(request)
    now = datetime.now(UTC)
    _refreshArea(area, now)
    area_store = store.openStore(area)
    events = area_store.loadEvents()

    edit_id = request.query.get('edit')
    body = calendar_page.renderCalendarPage(
        area,
        _computeWeekMonday(request.query.get('week'), now),
        tasks=area_store.loadTasks(),
        plan=area_store.loadPlan(),
        fixed=area_store.loadFixed(),
        events=events,
        now=now,
        edit_event=next((event for event in events if event.id == edit_id), None),
    )
    return _buildHtmlResponse(body)


def _handleTasksPage(request: _Request) -> _Response:
    """Render the goal tree and Auto group, with the saved plan's at-risk set.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = _readArea(request)
    _refreshArea(area, datetime.now(UTC))
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()

    plan = area_store.loadPlan()
    root = request.query.get('root')
    focus = request.query.get('focus')
    body = tasks_page.renderTasksPage(
        area,
        tasks=tasks,
        goals=area_store.loadGoals(),
        root=TaskId(root) if root else None,
        focus=TaskId(focus) if focus else None,
        order=area_store.loadPreferences().order,
        at_risk=frozenset(plan.at_risk) if plan is not None else frozenset(),
    )
    return _buildHtmlResponse(body)


def _handleDeadlinesPage(request: _Request) -> _Response:
    area = _readArea(request)
    now = datetime.now(UTC)
    _refreshArea(area, now)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    plan = area_store.loadPlan()

    entries = triage.listDeadlineEntries(tasks, area_store.loadGoals(), plan, now)
    return _buildHtmlResponse(deadlines_page.renderDeadlinesPage(area, entries=entries, plan=plan, tasks=tasks))


def _handleAchievementsPage(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)

    body = achievements_page.renderAchievementsPage(area, tasks=area_store.loadTasks(), goals=area_store.loadGoals())
    return _buildHtmlResponse(body)


def _handleRecurringPage(request: _Request) -> _Response:
    area = _readArea(request)
    _refreshArea(area, datetime.now(UTC))
    area_store = store.openStore(area)

    body = recurring_page.renderRecurringPage(
        area,
        templates=area_store.loadTemplates(),
        steps=area_store.loadSteps(),
        goals=area_store.loadGoals(),
        tasks=area_store.loadTasks(),
    )
    return _buildHtmlResponse(body)


def _handleAddTemplate(request: _Request) -> _Response:
    """Create a recurring template from the add form.

    Raises:
        FormError: the title or cadence is missing or malformed.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()

    blank = RecurringTask(
        id=store.generateRecurringId(),
        title='',
        notes='',
        estimate_minutes=None,
        auto_schedule=False,
        priority=Priority.MEDIUM,
        goal=None,
        days=frozenset(),
        every_days=None,
        deadline_after_days=None,
        wait_for_completion=False,
        created=datetime.now(UTC),
        last_added=None,
    )
    template = _applyTemplateForm(request.form, blank)
    templates[template.id] = template
    area_store.saveTemplates(templates)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleEditTemplate(request: _Request) -> _Response:
    """Apply the edit form to one template.

    Raises:
        FormError: the template does not exist, or a field is malformed.
    """
    return _handleTemplateChange(request, lambda template: _applyTemplateForm(request.form, template))


def _handleDeleteTemplate(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = _findRecord(templates, RecurringId(request.match.group('id')), 'recurring template')

    del templates[template.id]
    area_store.saveTemplates(templates)
    steps = area_store.loadSteps()
    if any(step.template == template.id for step in steps.values()):
        area_store.saveSteps({step_id: step for step_id, step in steps.items() if step.template != template.id})
    return _buildRedirect(_readReturnTarget(request, area))


def _handlePullAhead(request: _Request) -> _Response:
    """Spawn a copy now and restart the template's clock from now, then re-plan.

    Raises:
        FormError: the template does not exist, or it waits for completion and a copy
            is already open. Nothing is written.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    template = _findRecord(area_store.loadTemplates(), RecurringId(request.match.group('id')), 'recurring template')
    if template.wait_for_completion and recurring.countOpenCopies(area_store.loadTasks())[template.id]:
        raise FormError(f'{template.title!r} already has an open copy. Finish it, or delay it to now')

    now = datetime.now(UTC)
    recurring.pullAhead(area, template.id, now)
    scheduler.runPlan(area, now)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleDelayToNow(request: _Request) -> _Response:
    """Merge the template's open copies into one dated today, restart its clock, re-plan.

    Raises:
        FormError: the template does not exist. Nothing is written.
    """
    area = _readArea(request)
    template_id = RecurringId(request.match.group('id'))
    now = datetime.now(UTC)
    try:
        recurring.delayToNow(area, template_id, now)
    except TemplateNotFoundError as exc:
        raise FormError(f'recurring template {template_id} does not exist') from exc

    scheduler.runPlan(area, now)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleSetNextDue(request: _Request) -> _Response:
    """Hand-set an interval template's next occurrence. The rhythm continues from that day.

    Raises:
        FormError: the template does not exist, runs on anchor days, or the date does not
            parse. Nothing is written.
    """

    def setNextDue(template: RecurringTask) -> RecurringTask:
        if template.every_days is None:
            raise FormError(f'{template.title!r} runs on anchor days. Change its days to realign it')
        return replace(template, next_due=_readFormDate(request.form, 'next_due'))

    return _handleTemplateChange(request, setNextDue)


def _handleAddStep(request: _Request) -> _Response:
    """Add a step under a template, or under one of that template's steps.

    Raises:
        FormError: the template or parent step does not exist, or a field is malformed.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    template = _findRecord(area_store.loadTemplates(), RecurringId(request.match.group('id')), 'recurring template')
    steps = area_store.loadSteps()

    raw_parent = request.form.get('parent', '').strip()
    parent = StepId(raw_parent) if raw_parent else None
    if parent is not None and (parent not in steps or steps[parent].template != template.id):
        raise FormError(f'step {parent} is not part of {template.title!r}')

    step = RecurringStep(
        id=store.generateStepId(),
        template=template.id,
        parent=parent,
        title=_readFormText(request.form, 'title'),
        estimate_minutes=_readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
        created=datetime.now(UTC),
    )
    steps[step.id] = step
    area_store.saveSteps(steps)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleEditStep(request: _Request) -> _Response:
    """Apply a step's edit form. Copies already spawned are left as they are.

    Raises:
        FormError: the step does not exist, or a field is malformed.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    steps = area_store.loadSteps()
    step = _findRecord(steps, StepId(request.match.group('id')), 'recurring step')

    steps[step.id] = replace(
        step,
        title=_readFormText(request.form, 'title'),
        estimate_minutes=_readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
    )
    area_store.saveSteps(steps)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleDeleteStep(request: _Request) -> _Response:
    """Delete a step and every step beneath it.

    Raises:
        FormError: the step does not exist.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    steps = area_store.loadSteps()
    step = _findRecord(steps, StepId(request.match.group('id')), 'recurring step')
    doomed = recurring.collectStepSubtreeIds(step.id, steps)
    area_store.saveSteps({step_id: kept for step_id, kept in steps.items() if step_id not in doomed})
    return _buildRedirect(_readReturnTarget(request, area))


def _handleSettingsPage(request: _Request) -> _Response:
    return _buildHtmlResponse(settings_page.renderSettingsPage(_readArea(request), saved='saved' in request.query))


def _handleSaveWindows(request: _Request) -> _Response:
    """Write this area's windows into loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: a window line does not parse. Nothing is written.
    """
    area = _readArea(request)
    lines = [line.strip() for line in _readFormText(request.form, 'windows').splitlines() if line.strip()]
    live_config.saveAreaWindows(area, lines)
    return _buildRestartPage(_readReturnTarget(request, area))


def _handleSaveAreaColor(request: _Request) -> _Response:
    """Write this area's calendar color into loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: the color is not "#rrggbb". Nothing is written.
    """
    area = _readArea(request)
    live_config.saveAreaColor(area, _readFormText(request.form, 'color'))
    return _buildRestartPage(_readReturnTarget(request, area))


def _handleSavePalette(request: _Request) -> _Response:
    """Write the [palette] presets into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "Name #rrggbb". Nothing is written.
        ConfigError: a preset is malformed or still in use. Nothing is written.
    """
    entries = _parseLinePairs(request.form.get('palette', ''), separator=' ', shape='Name #rrggbb')
    live_config.savePalette(entries)
    return _buildRestartPage(_readReturnTarget(request, _readArea(request)))


def _handleSaveCategories(request: _Request) -> _Response:
    """Write the [categories] rule table into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "pattern -> Category". Nothing is written.
        ConfigError: the regex does not compile, or the category is not a palette name.
            Nothing is written.
    """
    entries = _parseLinePairs(request.form.get('categories', ''), separator=' -> ', shape='pattern -> Category')
    live_config.saveCategoryRules(entries)
    return _buildRestartPage(_readReturnTarget(request, _readArea(request)))


def _handleAddArea(request: _Request) -> _Response:
    """Append a new [areas.<name>] table to loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: the name is malformed, reserved, or taken, or the color is not
            "#rrggbb". Nothing is written.
    """
    area_name = _readFormText(request.form, 'name').strip().lower()
    live_config.saveNewArea(area_name, _readFormText(request.form, 'color').strip())
    # land on the new area's settings page, where its hours can be tightened
    return _buildRestartPage(f'/{area_name}/settings?saved=1')


def _handleSaveAreaOrder(request: _Request) -> _Response:
    """Save the switcher order from a drag and drop, then return to the page. There is no restart.

    Raises:
        ConfigError: a name is not an area, is hidden, or repeats. Nothing is written.
    """
    areas = [Area(area_name) for area_name in _readFormText(request.form, 'order').split(',') if area_name]
    live_config.saveAreaOrder(areas)
    return _buildRedirect(_readReturnTarget(request, _readArea(request)))


def _handleFinishArea(request: _Request) -> _Response:
    """Mark this area finished in loom.toml, then restart on the home area, or another open one.

    Raises:
        ConfigError: this is the last open area. Nothing is written.
    """
    area = _readArea(request)
    live_config.saveAreaFinished(area, config.computeLocalDate(datetime.now(UTC)))

    # land on home, else a switcher area before a hidden one
    still_open = sorted(
        (other for other in config.listOpenAreas() if other != area),
        key=lambda other: CONFIG.areas[other].hidden,
    )
    landing = _HOME_AREA if _HOME_AREA in still_open else still_open[0]
    return _buildRestartPage(f'/{landing}/tasks')


def _handleReopenArea(request: _Request) -> _Response:
    """Clear this area's finished date in loom.toml, then restart and land on it.

    Raises:
        ConfigError: the area vanished from loom.toml. Nothing is written.
    """
    area = _readArea(request)
    live_config.saveAreaFinished(area, None)
    return _buildRestartPage(f'/{area}/tasks')


def _handleAddGoal(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()

    goal = Goal(
        id=store.generateGoalId(),
        title=_readFormText(request.form, 'title'),
        notes='',
        created=datetime.now(UTC),
        completed=None,
    )
    goals[goal.id] = goal
    area_store.saveGoals(goals)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleEditGoal(request: _Request) -> _Response:
    def editGoal(goal: Goal) -> Goal:
        return replace(goal, title=_readFormText(request.form, 'title'), notes=request.form.get('notes', '').strip())

    return _handleGoalChange(request, editGoal)


def _handleMoveGoal(request: _Request) -> _Response:
    """Swap the goal one step up or down in the display order, renumbering every goal.

    Raises:
        FormError: the goal does not exist, is finished, or the direction is malformed.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()
    goal = _findRecord(goals, GoalId(request.match.group('id')), 'goal')

    direction = _readFormEnum(request.form, 'direction', MoveDirection)
    ordered = sorted(
        (candidate for candidate in goals.values() if candidate.completed is None),
        key=graph.computeGoalSortKey,
    )
    index = next((position for position, candidate in enumerate(ordered) if candidate.id == goal.id), None)
    if index is None:
        raise FormError('a finished goal cannot be reordered')

    target = index + direction.computeOffset()
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
    for position, moved in enumerate(ordered, start=1):
        goals[moved.id] = replace(moved, position=position)
    area_store.saveGoals(goals)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleFinishGoal(request: _Request) -> _Response:
    return _handleGoalChange(request, lambda goal: replace(goal, completed=datetime.now(UTC)))


def _handleReopenGoal(request: _Request) -> _Response:
    return _handleGoalChange(request, lambda goal: replace(goal, completed=None))


def _handleAddTask(request: _Request) -> _Response:
    """Create a task under an optional parent and goal, then re-plan the area.

    Raises:
        FormError: the title is missing, or a named parent does not exist.
    """
    area = _readArea(request)
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
        title=_readFormText(request.form, 'title'),
        status=Status.TODO,
        parent=parent,
        goal=GoalId(raw_goal) if raw_goal else None,
        blocked_by=(),
        notes='',
        estimate_minutes=_readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        deadline=_readFormOptionalDate(request.form, 'deadline'),
        auto_schedule='auto_schedule' in request.form,
        priority=Priority.MEDIUM,
        created=now,
        completed=None,
    )
    tasks[task.id] = task
    area_store.saveTasks(tasks)
    scheduler.runPlan(area, now)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleToggleOrder(request: _Request) -> _Response:
    """Flip the area between deadline-first and shortest-first, then re-plan in that order."""
    area = _readArea(request)
    area_store = store.openStore(area)
    preferences = area_store.loadPreferences()
    match preferences.order:
        case TaskOrder.DEADLINE:
            flipped = TaskOrder.SHORTEST
        case TaskOrder.SHORTEST:
            flipped = TaskOrder.DEADLINE

    area_store.savePreferences(replace(preferences, order=flipped))
    scheduler.runPlan(area, datetime.now(UTC))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleEditTask(request: _Request) -> _Response:
    """Apply the edit form to one task, then re-plan the area."""
    return _handleTaskChange(request, lambda task: _applyTaskEditForm(request.form, task))


def _handleTickTask(request: _Request) -> _Response:
    return _handleTaskChange(request, lambda task: replace(task, status=Status.DONE, completed=datetime.now(UTC)))


def _handleReopenTask(request: _Request) -> _Response:
    return _handleTaskChange(request, lambda task: replace(task, status=Status.TODO, completed=None))


def _handleToggleAuto(request: _Request) -> _Response:
    return _handleTaskChange(request, lambda task: replace(task, auto_schedule=not task.auto_schedule))


def _handleSetEstimate(request: _Request) -> _Response:
    """Change one task's estimate, then re-plan. An empty value clears the estimate.

    Raises:
        FormError: the task does not exist or the minutes are not a positive number.
    """

    def setEstimate(task: Task) -> Task:
        return replace(task, estimate_minutes=_readFormOptionalPositiveInt(request.form, 'estimate_minutes'))

    return _handleTaskChange(request, setEstimate)


def _handleDeleteTask(request: _Request) -> _Response:
    """Delete a leaf task and strip every dependency edge pointing at it."""
    area = _readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = _findRecord(tasks, TaskId(request.match.group('id')), 'task')

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
    return _buildRedirect(_readReturnTarget(request, area))


def _handleAddDependency(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = _findRecord(tasks, TaskId(request.match.group('id')), 'task')

    dependency = TaskId(_readFormText(request.form, 'blocked_by'))
    if dependency not in tasks:
        raise FormError(f'{dependency} does not exist (pick a task from the list)')
    if dependency == task.id:
        raise FormError('a task cannot block itself')

    if dependency not in task.blocked_by:
        tasks[task.id] = replace(task, blocked_by=(*task.blocked_by, dependency))
        area_store.saveTasks(tasks)
        scheduler.runPlan(area, datetime.now(UTC))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleRemoveDependency(request: _Request) -> _Response:
    def removeDependency(task: Task) -> Task:
        dependency = _readFormText(request.form, 'blocked_by')
        return replace(task, blocked_by=tuple(kept for kept in task.blocked_by if kept != dependency))

    return _handleTaskChange(request, removeDependency)


def _handleAckTask(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    task = _findRecord(area_store.loadTasks(), TaskId(request.match.group('id')), 'task')

    area_store.appendEvent(
        LoggedEvent(
            id=store.generateEventId(),
            kind=EventKind.ACK,
            at=datetime.now(UTC),
            start=None,
            end=None,
            note='',
            task=task.id,
            source=EventSource.MANUAL,
        ),
    )
    return _buildRedirect(_readReturnTarget(request, area))


def _handleLogEvent(request: _Request) -> _Response:
    """Append a 'did' event from local day + times, optionally attributed to a task.

    Raises:
        FormError: a time field is malformed, or the named task does not exist.
    """
    area = _readArea(request)
    area_store = store.openStore(area)

    start, end = _readFormInterval(request.form)
    raw_task = request.form.get('task', '')
    task = TaskId(raw_task) if raw_task else None
    if task is not None and task not in area_store.loadTasks():
        raise FormError(f'task {task} does not exist')

    area_store.appendEvent(
        LoggedEvent(
            id=store.generateEventId(),
            kind=EventKind.DID,
            at=datetime.now(UTC),
            start=start,
            end=end,
            note=_readFormText(request.form, 'note'),
            task=task,
            source=EventSource.MANUAL,
        ),
    )
    return _buildRedirect(_readReturnTarget(request, area))


def _handleEditLoggedEvent(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    events = area_store.loadEvents()
    event = _findLoggedEvent(request, events)

    start, end = _readFormInterval(request.form)
    raw_task = request.form.get('task', '')
    edited = replace(
        event,
        start=start,
        end=end,
        note=_readFormText(request.form, 'note'),
        task=TaskId(raw_task) if raw_task else None,
    )
    area_store.rewriteEvents(tuple(edited if kept.id == event.id else kept for kept in events))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleDeleteLoggedEvent(request: _Request) -> _Response:
    area = _readArea(request)
    area_store = store.openStore(area)
    events = area_store.loadEvents()
    event = _findLoggedEvent(request, events)

    area_store.rewriteEvents(tuple(kept for kept in events if kept.id != event.id))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleReschedule(request: _Request) -> _Response:
    area = _readArea(request)
    scheduler.runPlan(area, datetime.now(UTC))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleTaskChange(request: _Request, change: Callable[[Task], Task]) -> _Response:
    """Replace the route's task with change(task), save, re-plan the area, and redirect.

    Raises:
        FormError: the task does not exist, or `change` raised it. Nothing is written.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    task = _findRecord(tasks, TaskId(request.match.group('id')), 'task')

    tasks[task.id] = change(task)
    area_store.saveTasks(tasks)
    scheduler.runPlan(area, datetime.now(UTC))
    return _buildRedirect(_readReturnTarget(request, area))


def _handleGoalChange(request: _Request, change: Callable[[Goal], Goal]) -> _Response:
    """Replace the route's goal with change(goal), save, and redirect.

    Raises:
        FormError: the goal does not exist, or `change` raised it. Nothing is written.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()
    goal = _findRecord(goals, GoalId(request.match.group('id')), 'goal')

    goals[goal.id] = change(goal)
    area_store.saveGoals(goals)
    return _buildRedirect(_readReturnTarget(request, area))


def _handleTemplateChange(request: _Request, change: Callable[[RecurringTask], RecurringTask]) -> _Response:
    """Replace the route's template with change(template), save, and redirect.

    Raises:
        FormError: the template does not exist, or `change` raised it. Nothing is written.
    """
    area = _readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = _findRecord(templates, RecurringId(request.match.group('id')), 'recurring template')

    templates[template.id] = change(template)
    area_store.saveTemplates(templates)
    return _buildRedirect(_readReturnTarget(request, area))


def _applyTemplateForm(form: Mapping[str, str], template: RecurringTask) -> RecurringTask:
    """Apply the add or edit form to a template. A form with no priority leaves the template's as it is.

    Raises:
        FormError: the cadence, title or another field is missing or malformed.
    """
    days, every = _readFormCadence(form)
    return replace(
        template,
        title=_readFormText(form, 'title'),
        notes=form.get('notes', '').strip(),
        estimate_minutes=_readFormOptionalPositiveInt(form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in form,
        priority=_readFormEnum(form, 'priority', Priority) if 'priority' in form else template.priority,
        goal=_readFormOptionalGoal(form),
        days=days,
        every_days=every,
        deadline_after_days=_readFormOptionalDayCount(form, 'deadline_after_days'),
        wait_for_completion='wait_for_completion' in form,
    )


def _applyTaskEditForm(form: Mapping[str, str], task: Task) -> Task:
    """Apply the task edit form. Moving into DONE stamps completed, and leaving DONE clears it.

    Raises:
        FormError: a field is absent or malformed.
    """
    status = _readFormEnum(form, 'status', Status)
    completed = task.completed
    if status is Status.DONE and completed is None:
        completed = datetime.now(UTC)
    if status is not Status.DONE:
        completed = None

    return replace(
        task,
        title=_readFormText(form, 'title'),
        notes=form.get('notes', '').strip(),
        estimate_minutes=_readFormOptionalPositiveInt(form, 'estimate_minutes'),
        deadline=_readFormOptionalDate(form, 'deadline'),
        status=status,
        priority=_readFormEnum(form, 'priority', Priority),
        auto_schedule='auto_schedule' in form,
        completed=completed,
        color=_readFormColor(form, task),
        not_before=_readFormNotBefore(form),
    )


def _parseLinePairs(raw_lines: str, *, separator: str, shape: str) -> list[tuple[str, str]]:
    """Split a textarea into (left, right) pairs at the last `separator` of each line.

    Raises:
        FormError: a line has no separator, or an empty side. The message has `shape`.
    """
    entries = []
    for line in raw_lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        left, found, right = stripped.rpartition(separator)
        if not (found and left.strip() and right.strip()):
            raise FormError(f'{stripped!r} is not "{shape}"')
        entries.append((left.strip(), right.strip()))

    return entries


def _buildRestartPage(target_path: str) -> _Response:
    """Build the page shown while the server re-executes itself. It loads the target once the server is back.

    The server goes down right after sending the page, so a linked stylesheet would fail
    to load and leave a bare white page. The style and script are inline. The script
    polls until the server answers, and a blind refresh would land on the browser's white
    error page. The meta refresh is in <noscript>, so it covers only a browser without
    scripts.
    """
    RESTART_STYLE = (
        'html,body{margin:0;background:#0d1117;color:#e6edf3;'
        "font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif}"
        'main{padding:0.75rem 1rem}a{color:#58a6ff}'
        '.banner{border:1px solid #30363d;border-radius:8px;background:#161b22;padding:0.6rem 0.9rem;margin:0.6rem 0}'
    )
    RESTART_POLL = (
        '(function () {\n'
        '  const target = TARGET;\n'
        '  function poll() {\n'
        "    fetch('/static/style.css', { cache: 'no-store' })\n"
        '      .then((response) => { if (response.ok) window.location.replace(target); else setTimeout(poll, 300); })\n'
        '      .catch(() => setTimeout(poll, 300));\n'
        '  }\n'
        '  setTimeout(poll, 300);\n'
        '})();'
    )
    target_attribute = page_shell.escapeHtml(target_path)
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: restarting</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<noscript><meta http-equiv="refresh" content="3;url={target_attribute}"></noscript>'
        f'<style>{RESTART_STYLE}</style></head><body><main>'
        f'<div class="banner"><strong>Saved.</strong> Restarting the server with the new config&hellip; '
        f'<a href="{target_attribute}">continue</a></div></main>'
        f'<script>{RESTART_POLL.replace("TARGET", json.dumps(target_path))}</script></body></html>'
    )
    return _Response(status=HTTPStatus.OK, body=body, content_type=_HTML_CONTENT_TYPE, location=None, restart=True)


def _readArea(request: _Request) -> Area:
    return Area(request.match.group('area'))


def _findRecord(records: Mapping[_K, _V], record_id: _K, noun: str) -> _V:
    record = records.get(record_id)
    if record is None:
        raise FormError(f'{noun} {record_id} does not exist')

    return record


def _findLoggedEvent(request: _Request, events: tuple[LoggedEvent, ...]) -> LoggedEvent:
    event_id = request.match.group('id')
    event = next((event for event in events if event.id == event_id), None)
    if event is None:
        raise FormError(f'logged event {event_id} does not exist')

    return event


def _readReturnTarget(request: _Request, area: Area) -> str:
    target = request.form.get('return_to', '')
    # a target that starts with '//' is another host. Accept only a path on this one
    if target.startswith('/') and not target.startswith('//'):
        return target

    return f'/{area}/calendar'


def _computeWeekMonday(raw_week: str | None, now: datetime) -> date:
    if raw_week:
        try:
            anchor = date.fromisoformat(raw_week)
        except ValueError as exc:
            raise FormError(f'{raw_week!r} is not a YYYY-MM-DD date') from exc
    else:
        anchor = config.computeLocalDate(now)

    return anchor - timedelta(days=anchor.weekday())


def _readFormOptionalGoal(form: Mapping[str, str]) -> GoalId | None:
    raw_goal = form.get('goal', '').strip()
    return GoalId(raw_goal) if raw_goal else None


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


def _readFormCadence(form: Mapping[str, str]) -> tuple[frozenset[int], int | None]:
    """Read the template's cadence: anchored weekdays, or an every-N-days interval.

    Raises:
        FormError: both modes given, or neither.
    """
    days = frozenset(index for index, name in enumerate(DAY_NAMES) if f'day_{name.lower()}' in form)
    every = _readFormOptionalPositiveInt(form, 'every_days')
    if bool(days) == (every is not None):
        raise FormError('pick weekdays or an every-N-days interval, one or the other')

    return days, every


def _readFormOptionalDayCount(form: Mapping[str, str], key: str) -> int | None:
    """Read a day count. A blank field is None, and zero days is valid (due the same day)."""
    raw_count = form.get(key, '').strip()
    if not raw_count:
        return None
    # isdigit() admits non-ASCII digits such as '²'. Check isascii() too
    if not (raw_count.isascii() and raw_count.isdigit()):
        raise FormError(f'{key} must be a whole number of days')

    return int(raw_count)


def _readFormInterval(form: Mapping[str, str]) -> tuple[datetime, datetime]:
    """Read the form's local day and HH:MM pair as aware UTC (start, end).

    Raises:
        FormError: a field is absent or malformed, or the end is not after the start.
    """
    day = _readFormDate(form, 'day')
    start_clock = _readFormClock(form, 'start')
    end_clock = _readFormClock(form, 'end')
    if end_clock <= start_clock:
        raise FormError('the end time must be after the start time')

    timezone = CONFIG.timezone
    start = datetime.combine(day, start_clock, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(day, end_clock, tzinfo=timezone).astimezone(UTC)
    return start, end


def _readFormText(form: Mapping[str, str], key: str) -> str:
    value = form.get(key, '').strip()
    if not value:
        raise FormError(f'{key} is required')

    return value


def _readFormOptionalPositiveInt(form: Mapping[str, str], key: str) -> int | None:
    raw_number = form.get(key, '').strip()
    if not raw_number:
        return None
    # isdigit() admits non-ASCII digits such as '²'. Check isascii() too
    if not (raw_number.isascii() and raw_number.isdigit()) or int(raw_number) <= 0:
        raise FormError(f'{key} must be a positive whole number')

    return int(raw_number)


def _readFormDate(form: Mapping[str, str], key: str) -> date:
    raw_date = _readFormText(form, key)
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_date!r} is not a YYYY-MM-DD date') from exc


def _readFormOptionalDate(form: Mapping[str, str], key: str) -> date | None:
    if not form.get(key, '').strip():
        return None

    return _readFormDate(form, key)


def _readFormNotBefore(form: Mapping[str, str]) -> datetime | None:
    """Read the edit form's not-before day and optional time as local wall-clock.

    An empty day clears it, and an empty time means the start of that day.

    Raises:
        FormError: the day or the time does not parse.
    """
    day = _readFormOptionalDate(form, 'not_before_day')
    if day is None:
        return None

    time_of_day = _readFormClock(form, 'not_before_time') if form.get('not_before_time', '').strip() else time(0)
    return datetime.combine(day, time_of_day, tzinfo=CONFIG.timezone).astimezone(UTC)


def _readFormClock(form: Mapping[str, str], key: str) -> time:
    raw_time = _readFormText(form, key)
    try:
        return time.fromisoformat(raw_time)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_time!r} is not a HH:MM time') from exc


def _readFormEnum(form: Mapping[str, str], key: str, options: type[_E]) -> _E:
    raw_value = _readFormText(form, key)
    try:
        return options(raw_value)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_value!r} is not one of {[option.value for option in options]}') from exc


def _buildHtmlResponse(body: str) -> _Response:
    return _Response(status=HTTPStatus.OK, body=body, content_type=_HTML_CONTENT_TYPE, location=None)


def _buildRedirect(location: str) -> _Response:
    return _Response(status=HTTPStatus.SEE_OTHER, body='', content_type=_HTML_CONTENT_TYPE, location=location)


def _buildErrorPage(status: HTTPStatus, heading: str, message: str) -> _Response:
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: {status.value}</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<link rel="stylesheet" href="/static/style.css"></head><body><main>'
        f'<div class="banner warn"><strong>{page_shell.escapeHtml(heading)}</strong>'
        f'<p>{page_shell.escapeHtml(message)}</p>'
        f'<p><a href="/">back to the calendar</a></p></div></main></body></html>'
    )
    return _Response(status=status, body=body, content_type=_HTML_CONTENT_TYPE, location=None)


_AREA = f'(?P<area>{"|".join(CONFIG.areas)})'
_ID = '(?P<id>[a-z0-9_]+)'
_ROUTES: tuple[_Route, ...] = (
    (HTTPMethod.GET, re.compile(r'^/$'), _handleHome),
    (HTTPMethod.GET, re.compile(r'^/static/(?P<name>[a-z_.]+)$'), _handleStatic),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/calendar$'), _handleCalendarPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/tasks$'), _handleTasksPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/deadlines$'), _handleDeadlinesPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/achievements$'), _handleAchievementsPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/recurring$'), _handleRecurringPage),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/add$'), _handleAddTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/edit$'), _handleEditTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/delete$'), _handleDeleteTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/pull-ahead$'), _handlePullAhead),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/delay$'), _handleDelayToNow),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/next$'), _handleSetNextDue),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/steps/add$'), _handleAddStep),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/steps/{_ID}/edit$'), _handleEditStep),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/steps/{_ID}/delete$'), _handleDeleteStep),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/settings$'), _handleSettingsPage),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/windows$'), _handleSaveWindows),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/color$'), _handleSaveAreaColor),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/palette$'), _handleSavePalette),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/categories$'), _handleSaveCategories),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/areas/add$'), _handleAddArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/areas/order$'), _handleSaveAreaOrder),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/area/finish$'), _handleFinishArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/area/reopen$'), _handleReopenArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/add$'), _handleAddGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/edit$'), _handleEditGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/move$'), _handleMoveGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/finish$'), _handleFinishGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/reopen$'), _handleReopenGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/add$'), _handleAddTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/order$'), _handleToggleOrder),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/edit$'), _handleEditTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/tick$'), _handleTickTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/reopen$'), _handleReopenTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/auto$'), _handleToggleAuto),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/estimate$'), _handleSetEstimate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/delete$'), _handleDeleteTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/deps/add$'), _handleAddDependency),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/deps/remove$'), _handleRemoveDependency),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/ack/{_ID}$'), _handleAckTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log$'), _handleLogEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log/{_ID}/edit$'), _handleEditLoggedEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log/{_ID}/delete$'), _handleDeleteLoggedEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/reschedule$'), _handleReschedule),
)


### vocabulary #########################################################################


_E = TypeVar('_E', bound=Enum)
_K = TypeVar('_K')
_V = TypeVar('_V')


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, HTTPMethod.GET)

    def do_POST(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, HTTPMethod.POST)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        LOG.debug('http.request', extra={'line': format % args})


@dataclass(frozen=True)
class _Request:
    match: re.Match[str]
    query: Mapping[str, str]
    form: Mapping[str, str]


@dataclass(frozen=True)
class _Response:
    status: HTTPStatus
    body: str | bytes
    content_type: str
    location: str | None
    restart: bool = False  # after sending: exec a fresh server so CONFIG reloads


_RouteHandler = Callable[[_Request], _Response]
_Route = tuple[HTTPMethod, re.Pattern[str], _RouteHandler]
