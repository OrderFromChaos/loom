"""HTTP plumbing: routes, form parsing, dispatch, and the single-threaded server.

Zero cache: every GET re-reads the data files through the store's parse gate, so an
edit made by hand or by an LLM shows on the next refresh, and a malformed file becomes
an error page naming the file, never a crash. Mutations are plain form POSTs answered
with 303 redirects, and every task-mutating route re-plans its area before redirecting.
The server is deliberately single-threaded: requests serialize, so a read-modify-write
can never interleave with another request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit

from loom import recurring
from loom.config import (
    CONFIG,
    openAreas,
    saveAreaColor,
    saveAreaFinished,
    saveAreaOrder,
    saveAreaWindows,
    saveCategoryRules,
    saveNewArea,
    savePalette,
)
from loom.graph import goalSortKey
from loom.scheduler import runPlan
from loom.store import newEventId, newGoalId, newRecurringId, newStepId, newTaskId, storeFor
from loom.triage import deadlineTasks, overdueTaskIds
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
)
from loom.web.achievements_page import renderAchievementsPage
from loom.web.calendar_page import renderCalendarPage
from loom.web.deadlines_page import renderDeadlinesPage
from loom.web.page_shell import esc
from loom.web.recurring_page import renderRecurringPage
from loom.web.settings_page import renderSettingsPage
from loom.web.tasks_page import renderTasksPage
from loom.web.web_vocabulary import FormError


_STATIC_DIR = Path(__file__).resolve().parent / 'static'
_STATIC_FILES = {
    'style.css': 'text/css; charset=utf-8',
    'calendar.js': 'text/javascript; charset=utf-8',
    'tree.js': 'text/javascript; charset=utf-8',
    'pickers.js': 'text/javascript; charset=utf-8',
    'areas.js': 'text/javascript; charset=utf-8',
    'restore.js': 'text/javascript; charset=utf-8',
}
_HTML = 'text/html; charset=utf-8'

# the restart page carries its own look: the stylesheet cannot load while the server is down
_RESTART_STYLE = (
    'html,body{margin:0;background:#0d1117;color:#e6edf3;'
    "font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif}"
    'main{padding:0.75rem 1rem}a{color:#58a6ff}'
    '.banner{border:1px solid #30363d;border-radius:8px;background:#161b22;padding:0.6rem 0.9rem;margin:0.6rem 0}'
)
_RESTART_POLL = """(function () {
  const target = TARGET;
  function poll() {
    fetch('/static/style.css', { cache: 'no-store' })
      .then((response) => { if (response.ok) window.location.replace(target); else setTimeout(poll, 300); })
      .catch(() => setTimeout(poll, 300));
  }
  setTimeout(poll, 300);
})();"""

LOG = logging.getLogger('loom')

HOME_AREA = Area('work')  # where GET / lands; serveForever sets it from the CLI
PORT = 0  # the listening port; serveForever sets it, and a settings restart reuses it

_HOUSEKEEPING_SECONDS = 300


def serveForever(home: Area, port: int) -> int:
    """Serve requests and, between them, keep recurring copies spawned in every area.

    Housekeeping runs at startup and then every five minutes in this same thread, so a
    copy appears on its day without anyone opening the page, and requests and
    housekeeping still never interleave.
    """
    global HOME_AREA, PORT
    HOME_AREA = home
    PORT = port
    server = HTTPServer(('127.0.0.1', port), _Handler)
    print(f'loom serving http://127.0.0.1:{port}/ (home: {home})', flush=True)
    try:
        next_housekeeping = 0.0
        while True:
            if clock.monotonic() >= next_housekeeping:
                _housekeep(datetime.now(UTC))
                next_housekeeping = clock.monotonic() + _HOUSEKEEPING_SECONDS
            server.timeout = max(0.0, next_housekeeping - clock.monotonic())
            server.handle_request()
    except KeyboardInterrupt:
        print('stopped')
    finally:
        server.server_close()

    return 0


def _housekeep(now: datetime) -> None:
    """Spawn owed recurring copies in every open area; a broken area is logged and skipped."""
    for area in openAreas():
        try:
            _freshenArea(area, now)
        except StoreError as exc:
            LOG.warning('housekeeping.skipped', extra={'area': area, 'error': str(exc)})


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, 'GET')

    def do_POST(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, 'POST')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        LOG.debug('http.request', extra={'line': format % args})


def _dispatch(handler: _Handler, method: str) -> None:
    """Match the path against the route table, run the handler, translate errors to pages."""
    split = urlsplit(handler.path)
    query = dict(parse_qsl(split.query))
    form: dict[str, str] = {}
    if method == 'POST':
        length = int(handler.headers.get('Content-Length', '0') or '0')
        form = dict(parse_qsl(handler.rfile.read(length).decode('utf-8')))

    for route_method, pattern, route_handler in _ROUTES:
        matched = pattern.match(split.path)
        if route_method != method or matched is None:
            continue
        try:
            response = route_handler(_Request(match=matched, query=query, form=form))
        except FormError as exc:
            response = _errorPage(400, 'That form did not parse', str(exc))
        except StoreConflictError as exc:
            response = _errorPage(409, 'The file changed under us', f'{exc}. Go back, refresh, and retry.')
        except StoreError as exc:
            response = _errorPage(500, 'A data file did not parse', str(exc))
        _send(handler, response)
        if response.restart:
            # exec replaces this process in place, so the new CONFIG loads and the
            # listening socket reopens on the same port a moment later
            handler.wfile.flush()
            LOG.info('server.restarting', extra={'reason': 'settings saved'})
            os.execv(sys.executable, [sys.executable, '-m', 'loom', 'serve', '--area', HOME_AREA, '--port', str(PORT)])
        return

    _send(handler, _errorPage(404, 'Not found', f'no route for {method} {esc(split.path)}'))


def _send(handler: _Handler, response: _Response) -> None:
    payload = response.body.encode('utf-8') if isinstance(response.body, str) else response.body
    handler.send_response(response.status)
    if response.location is not None:
        handler.send_header('Location', response.location)
    handler.send_header('Content-Type', response.content_type)
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _home(request: _Request) -> _Response:
    return _redirect(f'/{HOME_AREA}/calendar')


def _static(request: _Request) -> _Response:
    name = request.match.group('name')
    content_type = _STATIC_FILES.get(name)
    if content_type is None:
        return _errorPage(404, 'Not found', f'{esc(name)} is not a static file here')

    return _Response(status=200, body=(_STATIC_DIR / name).read_bytes(), content_type=content_type, location=None)


def _calendarPage(request: _Request) -> _Response:
    """The week view: everything loaded fresh, staleness derived, then rendered.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = _areaOf(request)
    now = datetime.now(UTC)
    _freshenArea(area, now)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    plan = area_store.loadPlan()
    events = area_store.loadEvents()
    fixed = area_store.loadFixed()

    stale = overdueTaskIds(tasks, now)
    edit_id = request.query.get('edit')
    edit_event = next((event for event in events if event.id == edit_id), None)
    body = renderCalendarPage(
        area,
        _weekMonday(request.query.get('week'), now),
        tasks=tasks,
        plan=plan,
        fixed=fixed,
        events=events,
        stale=stale,
        now=now,
        edit_event=edit_event,
    )
    return _html(body)


def _tasksPage(request: _Request) -> _Response:
    """The goal tree and Auto group, laid out with the saved plan's at-risk set.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = _areaOf(request)
    now = datetime.now(UTC)
    _freshenArea(area, now)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()

    plan = area_store.loadPlan()
    stale = overdueTaskIds(tasks, now)
    root = request.query.get('root')
    focus = request.query.get('focus')
    body = renderTasksPage(
        area,
        tasks=tasks,
        goals=area_store.loadGoals(),
        stale=stale,
        root=TaskId(root) if root else None,
        focus=TaskId(focus) if focus else None,
        order=area_store.loadPreferences().order,
        at_risk=frozenset(plan.at_risk) if plan is not None else frozenset(),
    )
    return _html(body)


def _deadlinesPage(request: _Request) -> _Response:
    area = _areaOf(request)
    now = datetime.now(UTC)
    _freshenArea(area, now)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    plan = area_store.loadPlan()

    entries = deadlineTasks(tasks, area_store.loadGoals(), plan, now)
    return _html(renderDeadlinesPage(area, entries=entries, plan=plan, tasks=tasks))


def _achievementsPage(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)

    return _html(renderAchievementsPage(area, tasks=area_store.loadTasks(), goals=area_store.loadGoals()))


def _recurringPage(request: _Request) -> _Response:
    area = _areaOf(request)
    _freshenArea(area, datetime.now(UTC))
    area_store = storeFor(area)

    body = renderRecurringPage(
        area,
        templates=area_store.loadRecurring(),
        steps=area_store.loadSteps(),
        goals=area_store.loadGoals(),
        tasks=area_store.loadTasks(),
    )
    return _html(body)


def _freshenArea(area: Area, now: datetime) -> None:
    """Spawn due recurring copies, and re-plan when any appeared."""
    if recurring.spawnDue(area, now):
        runPlan(area, now)


def _addRecurring(request: _Request) -> _Response:
    """Create a recurring template from the add form.

    Raises:
        FormError: the title or cadence is missing or malformed.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    templates = area_store.loadRecurring()

    days, every = _cadenceFields(request.form)
    template = RecurringTask(
        id=newRecurringId(),
        title=_requireField(request.form, 'title'),
        notes=request.form.get('notes', '').strip(),
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
        priority=Priority.MEDIUM,
        goal=_optionalGoalField(request.form),
        days=days,
        every_days=every,
        deadline_after_days=_optionalDayCountField(request.form, 'deadline_after_days'),
        wait_for_completion='wait_for_completion' in request.form,
        created=datetime.now(UTC),
        last_added=None,
    )
    templates[template.id] = template
    area_store.saveRecurring(templates)
    return _redirect(_returnTo(request, area))


def _editRecurring(request: _Request) -> _Response:
    """Apply the edit form to one template.

    Raises:
        FormError: the template does not exist, or a field is malformed.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    template = _recurringOf(request, templates)

    days, every = _cadenceFields(request.form)
    templates[template.id] = replace(
        template,
        title=_requireField(request.form, 'title'),
        notes=request.form.get('notes', '').strip(),
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
        priority=_enumField(request.form, 'priority', Priority),
        goal=_optionalGoalField(request.form),
        days=days,
        every_days=every,
        deadline_after_days=_optionalDayCountField(request.form, 'deadline_after_days'),
        wait_for_completion='wait_for_completion' in request.form,
    )
    area_store.saveRecurring(templates)
    return _redirect(_returnTo(request, area))


def _deleteRecurring(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    template = _recurringOf(request, templates)

    del templates[template.id]
    area_store.saveRecurring(templates)
    steps = area_store.loadSteps()
    if any(step.template == template.id for step in steps.values()):
        area_store.saveSteps({step_id: step for step_id, step in steps.items() if step.template != template.id})
    return _redirect(_returnTo(request, area))


def _addStep(request: _Request) -> _Response:
    """Add a step under a template, or under one of that template's steps.

    Raises:
        FormError: the template or parent step does not exist, or a field is malformed.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    template = _recurringOf(request, area_store.loadRecurring())
    steps = area_store.loadSteps()

    parent_text = request.form.get('parent', '').strip()
    parent = StepId(parent_text) if parent_text else None
    if parent is not None and (parent not in steps or steps[parent].template != template.id):
        raise FormError(f'step {parent} is not part of {template.title!r}')

    step = RecurringStep(
        id=newStepId(),
        template=template.id,
        parent=parent,
        title=_requireField(request.form, 'title'),
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
        created=datetime.now(UTC),
    )
    steps[step.id] = step
    area_store.saveSteps(steps)
    return _redirect(_returnTo(request, area))


def _editStep(request: _Request) -> _Response:
    """Apply a step's edit form; copies already spawned keep what they were given.

    Raises:
        FormError: the step does not exist, or a field is malformed.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    steps = area_store.loadSteps()
    step = _stepOf(request, steps)

    steps[step.id] = replace(
        step,
        title=_requireField(request.form, 'title'),
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
    )
    area_store.saveSteps(steps)
    return _redirect(_returnTo(request, area))


def _deleteStep(request: _Request) -> _Response:
    """Delete a step and every step beneath it.

    Raises:
        FormError: the step does not exist.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    steps = area_store.loadSteps()
    doomed = recurring.stepSubtree(_stepOf(request, steps).id, steps)
    area_store.saveSteps({step_id: step for step_id, step in steps.items() if step_id not in doomed})
    return _redirect(_returnTo(request, area))


def _pullAhead(request: _Request) -> _Response:
    """Spawn a copy now and restart the template's clock from now, then re-plan.

    Raises:
        FormError: the template does not exist, or it waits for completion and a copy
            is already open; nothing is written.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    template = _recurringOf(request, area_store.loadRecurring())
    if template.wait_for_completion and recurring.openCopies(area_store.loadTasks())[template.id]:
        raise FormError(f'{template.title!r} already has an open copy; finish it, or delay it to now')

    now = datetime.now(UTC)
    recurring.pullAhead(area, template.id, now)
    runPlan(area, now)
    return _redirect(_returnTo(request, area))


def _setNextDue(request: _Request) -> _Response:
    """Hand-set an interval template's next occurrence; the rhythm continues from that day.

    Raises:
        FormError: the template does not exist, runs on anchor days, or the date does not
            parse; nothing is written.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    templates = area_store.loadRecurring()
    template = _recurringOf(request, templates)
    if template.every_days is None:
        raise FormError(f'{template.title!r} runs on anchor days; change its days to realign it')

    templates[template.id] = replace(template, next_due=_requireDateField(request.form, 'next_due'))
    area_store.saveRecurring(templates)
    return _redirect(_returnTo(request, area))


def _delayToNow(request: _Request) -> _Response:
    """Merge the template's open copies into one dated today, restart its clock, re-plan.

    Raises:
        FormError: the template does not exist; nothing is written.
    """
    area = _areaOf(request)
    template = _recurringOf(request, storeFor(area).loadRecurring())

    now = datetime.now(UTC)
    recurring.delayToNow(area, template.id, now)
    runPlan(area, now)
    return _redirect(_returnTo(request, area))


def _addGoal(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    goals = area_store.loadGoals()

    goal = Goal(
        id=newGoalId(),
        title=_requireField(request.form, 'title'),
        notes='',
        created=datetime.now(UTC),
        completed=None,
    )
    goals[goal.id] = goal
    area_store.saveGoals(goals)
    return _redirect(_returnTo(request, area))


def _editGoal(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    goals = area_store.loadGoals()
    goal = _goalOf(request, goals)

    goals[goal.id] = replace(
        goal,
        title=_requireField(request.form, 'title'),
        notes=request.form.get('notes', '').strip(),
    )
    area_store.saveGoals(goals)
    return _redirect(_returnTo(request, area))


def _moveGoal(request: _Request) -> _Response:
    """Swap the goal one step up or down in the display order, renumbering every goal.

    Raises:
        FormError: the goal does not exist, is finished, or the direction is malformed.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    goals = area_store.loadGoals()
    goal = _goalOf(request, goals)

    direction = _requireField(request.form, 'direction')
    if direction not in ('up', 'down'):
        raise FormError(f'direction must be "up" or "down", not {direction!r}')

    ordered = sorted((candidate for candidate in goals.values() if candidate.completed is None), key=goalSortKey)
    index = next((position for position, candidate in enumerate(ordered) if candidate.id == goal.id), None)
    if index is None:
        raise FormError('a finished goal cannot be reordered')

    target = index - 1 if direction == 'up' else index + 1
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
    for position, moved in enumerate(ordered, start=1):
        goals[moved.id] = replace(moved, position=position)
    area_store.saveGoals(goals)
    return _redirect(_returnTo(request, area))


def _finishGoal(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    goals = area_store.loadGoals()
    goal = _goalOf(request, goals)

    goals[goal.id] = replace(goal, completed=datetime.now(UTC))
    area_store.saveGoals(goals)
    return _redirect(_returnTo(request, area))


def _reopenGoal(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    goals = area_store.loadGoals()
    goal = _goalOf(request, goals)

    goals[goal.id] = replace(goal, completed=None)
    area_store.saveGoals(goals)
    return _redirect(_returnTo(request, area))


def _settingsPage(request: _Request) -> _Response:
    return _html(renderSettingsPage(_areaOf(request), saved='saved' in request.query))


def _saveWindows(request: _Request) -> _Response:
    """Write this area's windows into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a window line does not parse; nothing is written.
    """
    area = _areaOf(request)
    lines = [line.strip() for line in _requireField(request.form, 'windows').splitlines() if line.strip()]
    try:
        saveAreaWindows(area, lines)
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _restartResponse(_returnTo(request, area))


def _saveAreaColor(request: _Request) -> _Response:
    """Write this area's calendar color into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: the color is not "#rrggbb"; nothing is written.
    """
    area = _areaOf(request)
    try:
        saveAreaColor(area, _requireField(request.form, 'color'))
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _restartResponse(_returnTo(request, area))


def _savePalette(request: _Request) -> _Response:
    """Write the [palette] presets into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "Name #rrggbb"; nothing is written.
    """
    try:
        savePalette(_colorLines(request.form, 'palette'))
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _restartResponse(_returnTo(request, _areaOf(request)))


def _saveCategories(request: _Request) -> _Response:
    """Write the [categories] rule table into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "pattern -> Category", the regex does not compile, or
            the category is not a palette name; nothing is written.
    """
    try:
        saveCategoryRules(_ruleLines(request.form, 'categories'))
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _restartResponse(_returnTo(request, _areaOf(request)))


def _addNewArea(request: _Request) -> _Response:
    """Append a new [areas.<name>] table to loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: the name is malformed, reserved, or taken, or the color is not
            "#rrggbb"; nothing is written.
    """
    name = _requireField(request.form, 'name').strip().lower()
    try:
        saveNewArea(name, _requireField(request.form, 'color').strip())
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    # land on the new area's settings page, where its hours can be tightened
    return _restartResponse(f'/{name}/settings?saved=1')


def _saveAreaOrder(request: _Request) -> _Response:
    """Save the switcher order from a drag and drop, then return to the page; no restart.

    Raises:
        FormError: a name is not an area, is hidden, or repeats; nothing is written.
    """
    names = [Area(name) for name in _requireField(request.form, 'order').split(',') if name]
    try:
        saveAreaOrder(names)
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _redirect(_returnTo(request, _areaOf(request)))


def _finishArea(request: _Request) -> _Response:
    """Mark this area finished in loom.toml, then restart on the home area, or another open one.

    Raises:
        FormError: this is the last open area; nothing is written.
    """
    area = _areaOf(request)
    try:
        saveAreaFinished(area, datetime.now(UTC).astimezone(CONFIG.timezone).date())
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    # land on home, else a switcher area before a hidden one
    still_open = sorted((other for other in openAreas() if other != area), key=lambda other: CONFIG.areas[other].hidden)
    landing = HOME_AREA if HOME_AREA in still_open else still_open[0]
    return _restartResponse(f'/{landing}/tasks')


def _reopenArea(request: _Request) -> _Response:
    """Clear this area's finished date in loom.toml, then restart and land on it.

    Raises:
        FormError: the area vanished from loom.toml; nothing is written.
    """
    area = _areaOf(request)
    try:
        saveAreaFinished(area, None)
    except ConfigError as exc:
        raise FormError(str(exc)) from exc

    return _restartResponse(f'/{area}/tasks')


def _ruleLines(form: dict[str, str], key: str) -> list[tuple[str, str]]:
    """Split a textarea of 'pattern -> Category' lines into (pattern, category) pairs.

    Raises:
        FormError: a line has no ' -> ' to split on.
    """
    entries = []
    for line in form.get(key, '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pattern, separator, category = stripped.rpartition(' -> ')
        if not separator or not pattern.strip() or not category.strip():
            raise FormError(f'{stripped!r} is not "pattern -> Category"')
        entries.append((pattern.strip(), category.strip()))

    return entries


def _colorLines(form: dict[str, str], key: str) -> list[tuple[str, str]]:
    """Split a textarea of 'Name #rrggbb' lines into (name, hex) pairs.

    Raises:
        FormError: a line has no space to split on. Hex validity is the config gate's job.
    """
    entries = []
    for line in form.get(key, '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name, _, value = stripped.rpartition(' ')
        if not name:
            raise FormError(f'{stripped!r} is not "Name #rrggbb"')
        entries.append((name.strip(), value.strip()))

    return entries


def _restartResponse(target: str) -> _Response:
    """The page shown while the server re-executes itself, then the target once it is back.

    Everything it needs is inline: the server goes down right after sending it, so a
    linked stylesheet would fail to load and leave a bare white page. It polls until the
    server answers instead of refreshing blindly into the browser's white error page;
    the meta refresh sits in <noscript>, so it only covers a browser without scripts.
    """
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: restarting</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<noscript><meta http-equiv="refresh" content="3;url={esc(target)}"></noscript>'
        f'<style>{_RESTART_STYLE}</style></head><body><main>'
        f'<div class="banner"><strong>Saved.</strong> Restarting the server with the new config&hellip; '
        f'<a href="{esc(target)}">continue</a></div></main>'
        f'<script>{_RESTART_POLL.replace("TARGET", json.dumps(target))}</script></body></html>'
    )
    return _Response(status=200, body=body, content_type=_HTML, location=None, restart=True)


def _addTask(request: _Request) -> _Response:
    """Create a task under an optional parent and goal, then re-plan the area.

    Raises:
        FormError: the title is missing, or a named parent does not exist.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()

    parent_text = request.form.get('parent', '')
    parent = TaskId(parent_text) if parent_text else None
    if parent is not None and parent not in tasks:
        raise FormError(f'parent {parent} does not exist')
    goal_text = request.form.get('goal', '')

    now = datetime.now(UTC)
    task = Task(
        id=newTaskId(),
        title=_requireField(request.form, 'title'),
        status=Status.TODO,
        parent=parent,
        goal=GoalId(goal_text) if goal_text else None,
        blocked_by=(),
        notes='',
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        deadline=_optionalDateField(request.form, 'deadline'),
        auto_schedule='auto_schedule' in request.form,
        priority=Priority.MEDIUM,
        created=now,
        completed=None,
    )
    tasks[task.id] = task
    area_store.saveTasks(tasks)
    runPlan(area, now)
    return _redirect(_returnTo(request, area))


def _editTask(request: _Request) -> _Response:
    """Apply the edit form to one task, then re-plan the area."""
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    now = datetime.now(UTC)
    status = _enumField(request.form, 'status', Status)
    completed = task.completed
    if status is Status.DONE and completed is None:
        completed = now
    if status is not Status.DONE:
        completed = None

    tasks[task.id] = replace(
        task,
        title=_requireField(request.form, 'title'),
        notes=request.form.get('notes', '').strip(),
        estimate_minutes=_optionalIntField(request.form, 'estimate_minutes'),
        deadline=_optionalDateField(request.form, 'deadline'),
        status=status,
        priority=_enumField(request.form, 'priority', Priority),
        auto_schedule='auto_schedule' in request.form,
        completed=completed,
        color=_colorField(request.form, task),
        not_before=_notBeforeField(request.form),
    )
    area_store.saveTasks(tasks)
    runPlan(area, now)
    return _redirect(_returnTo(request, area))


def _toggleOrder(request: _Request) -> _Response:
    """Flip the area between deadline-first and shortest-first, then re-plan in that order."""
    area = _areaOf(request)
    area_store = storeFor(area)
    preferences = area_store.loadPreferences()
    match preferences.order:
        case TaskOrder.DEADLINE:
            flipped = TaskOrder.SHORTEST
        case TaskOrder.SHORTEST:
            flipped = TaskOrder.DEADLINE

    area_store.savePreferences(replace(preferences, order=flipped))
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _tickTask(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    now = datetime.now(UTC)
    tasks[task.id] = replace(task, status=Status.DONE, completed=now)
    area_store.saveTasks(tasks)
    runPlan(area, now)
    return _redirect(_returnTo(request, area))


def _reopenTask(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    tasks[task.id] = replace(task, status=Status.TODO, completed=None)
    area_store.saveTasks(tasks)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _toggleAuto(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    tasks[task.id] = replace(task, auto_schedule=not task.auto_schedule)
    area_store.saveTasks(tasks)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _setEstimate(request: _Request) -> _Response:
    """Change one task's estimate, then re-plan; an empty value clears the estimate.

    The tree's inline minutes field and the calendar's drag-resize both post here.

    Raises:
        FormError: the task does not exist or the minutes are not a positive number.
    """
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    minutes = _optionalIntField(request.form, 'estimate_minutes')
    tasks[task.id] = replace(task, estimate_minutes=minutes)
    area_store.saveTasks(tasks)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _deleteTask(request: _Request) -> _Response:
    """Delete a leaf task and strip every dependency edge pointing at it."""
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    if any(other.parent == task.id for other in tasks.values()):
        raise FormError(f'{task.title!r} still has children; delete or move them first')

    del tasks[task.id]
    for other in list(tasks.values()):
        if task.id in other.blocked_by:
            tasks[other.id] = replace(
                other,
                blocked_by=tuple(dependency for dependency in other.blocked_by if dependency != task.id),
            )
    area_store.saveTasks(tasks)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _addDependency(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    dependency = TaskId(_requireField(request.form, 'blocked_by'))
    if dependency not in tasks:
        raise FormError(f'{dependency} does not exist (pick a task from the list)')
    if dependency == task.id:
        raise FormError('a task cannot block itself')

    if dependency not in task.blocked_by:
        tasks[task.id] = replace(task, blocked_by=(*task.blocked_by, dependency))
        area_store.saveTasks(tasks)
        runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _removeDependency(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    dependency = _requireField(request.form, 'blocked_by')
    tasks[task.id] = replace(task, blocked_by=tuple(kept for kept in task.blocked_by if kept != dependency))
    area_store.saveTasks(tasks)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _ackTask(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    tasks = area_store.loadTasks()
    task = _taskOf(request, tasks)

    now = datetime.now(UTC)
    area_store.appendEvent(
        LoggedEvent(
            id=newEventId(),
            kind=EventKind.ACK,
            at=now,
            start=None,
            end=None,
            note='',
            task=task.id,
            source=EventSource.MANUAL,
        ),
    )
    return _redirect(_returnTo(request, area))


def _logEvent(request: _Request) -> _Response:
    """Append a 'did' event from local day + times, optionally attributed to a task.

    Raises:
        FormError: a time field is malformed, or the named task does not exist.
    """
    area = _areaOf(request)
    area_store = storeFor(area)

    start, end = _localInterval(request.form)
    task_text = request.form.get('task', '')
    task = TaskId(task_text) if task_text else None
    if task is not None and task not in area_store.loadTasks():
        raise FormError(f'task {task} does not exist')

    area_store.appendEvent(
        LoggedEvent(
            id=newEventId(),
            kind=EventKind.DID,
            at=datetime.now(UTC),
            start=start,
            end=end,
            note=_requireField(request.form, 'note'),
            task=task,
            source=EventSource.MANUAL,
        ),
    )
    return _redirect(_returnTo(request, area))


def _editLoggedEvent(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    events = area_store.loadEvents()
    event = _eventOf(request, events)

    start, end = _localInterval(request.form)
    task_text = request.form.get('task', '')
    edited = replace(
        event,
        start=start,
        end=end,
        note=_requireField(request.form, 'note'),
        task=TaskId(task_text) if task_text else None,
    )
    area_store.rewriteEvents(tuple(edited if kept.id == event.id else kept for kept in events))
    return _redirect(_returnTo(request, area))


def _deleteLoggedEvent(request: _Request) -> _Response:
    area = _areaOf(request)
    area_store = storeFor(area)
    events = area_store.loadEvents()
    event = _eventOf(request, events)

    area_store.rewriteEvents(tuple(kept for kept in events if kept.id != event.id))
    return _redirect(_returnTo(request, area))


def _reschedule(request: _Request) -> _Response:
    area = _areaOf(request)
    runPlan(area, datetime.now(UTC))
    return _redirect(_returnTo(request, area))


def _areaOf(request: _Request) -> Area:
    return Area(request.match.group('area'))


def _taskOf(request: _Request, tasks: dict[TaskId, Task]) -> Task:
    task = tasks.get(TaskId(request.match.group('id')))
    if task is None:
        raise FormError(f'task {request.match.group("id")} does not exist')

    return task


def _recurringOf(request: _Request, templates: dict[RecurringId, RecurringTask]) -> RecurringTask:
    template = templates.get(RecurringId(request.match.group('id')))
    if template is None:
        raise FormError(f'recurring template {request.match.group("id")} does not exist')

    return template


def _stepOf(request: _Request, steps: dict[StepId, RecurringStep]) -> RecurringStep:
    step = steps.get(StepId(request.match.group('id')))
    if step is None:
        raise FormError(f'recurring step {request.match.group("id")} does not exist')

    return step


def _optionalGoalField(form: dict[str, str]) -> GoalId | None:
    text = form.get('goal', '').strip()
    return GoalId(text) if text else None


def _colorField(form: dict[str, str], task: Task) -> str | None:
    """The chosen palette preset name; the task's current name stays legal even if retired.

    Raises:
        FormError: the name is not in the palette and not the task's current color.
    """
    text = form.get('color', '').strip()
    if not text:
        return None
    if text not in CONFIG.palette and text != task.color:
        raise FormError(f'{text!r} is not a palette preset (add it on the Settings page)')

    return text


def _cadenceFields(form: dict[str, str]) -> tuple[frozenset[int], int | None]:
    """The template's cadence: anchored weekdays, or an every-N-days interval.

    Raises:
        FormError: both modes given, or neither.
    """
    days = frozenset(index for index, name in enumerate(DAY_NAMES) if f'day_{name.lower()}' in form)
    every = _optionalIntField(form, 'every_days')
    if bool(days) == (every is not None):
        raise FormError('pick weekdays or an every-N-days interval, one or the other')

    return days, every


def _optionalDayCountField(form: dict[str, str], key: str) -> int | None:
    """A blank field is None; unlike estimates, zero days is legal (due the same day)."""
    text = form.get(key, '').strip()
    if not text:
        return None
    if not text.isdigit():
        raise FormError(f'{key} must be a whole number of days')

    return int(text)


def _goalOf(request: _Request, goals: dict[GoalId, Goal]) -> Goal:
    goal = goals.get(GoalId(request.match.group('id')))
    if goal is None:
        raise FormError(f'goal {request.match.group("id")} does not exist')

    return goal


def _eventOf(request: _Request, events: tuple[LoggedEvent, ...]) -> LoggedEvent:
    event_id = request.match.group('id')
    event = next((event for event in events if event.id == event_id), None)
    if event is None:
        raise FormError(f'logged event {event_id} does not exist')

    return event


def _returnTo(request: _Request, area: Area) -> str:
    target = request.form.get('return_to', '')
    if target.startswith('/') and not target.startswith('//'):
        return target

    return f'/{area}/calendar'


def _weekMonday(text: str | None, now: datetime) -> date:
    if text:
        try:
            anchor = date.fromisoformat(text)
        except ValueError as exc:
            raise FormError(f'{text!r} is not a YYYY-MM-DD date') from exc
    else:
        anchor = now.astimezone(CONFIG.timezone).date()

    return anchor - timedelta(days=anchor.weekday())


def _localInterval(form: dict[str, str]) -> tuple[datetime, datetime]:
    """Turn the form's local day + HH:MM pair into aware UTC (start, end).

    Raises:
        FormError: a field is missing or malformed, or the end is not after the start.
    """
    day = _requireDateField(form, 'day')
    start_clock = _clockField(form, 'start')
    end_clock = _clockField(form, 'end')
    if end_clock <= start_clock:
        raise FormError('the end time must be after the start time')

    zone = CONFIG.timezone
    start = datetime.combine(day, start_clock, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day, end_clock, tzinfo=zone).astimezone(UTC)
    return start, end


def _requireField(form: dict[str, str], key: str) -> str:
    value = form.get(key, '').strip()
    if not value:
        raise FormError(f'{key} is required')

    return value


def _optionalIntField(form: dict[str, str], key: str) -> int | None:
    text = form.get(key, '').strip()
    if not text:
        return None
    if not text.isdigit() or int(text) <= 0:
        raise FormError(f'{key} must be a positive whole number')

    return int(text)


def _requireDateField(form: dict[str, str], key: str) -> date:
    text = _requireField(form, key)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FormError(f'{key}: {text!r} is not a YYYY-MM-DD date') from exc


def _optionalDateField(form: dict[str, str], key: str) -> date | None:
    if not form.get(key, '').strip():
        return None

    return _requireDateField(form, key)


def _notBeforeField(form: dict[str, str]) -> datetime | None:
    """The edit form's not-before day and optional time, read as local wall-clock.

    An empty day clears it; an empty time means the start of that day.

    Raises:
        FormError: the day or the time does not parse.
    """
    day = _optionalDateField(form, 'not_before_day')
    if day is None:
        return None

    clock = _clockField(form, 'not_before_time') if form.get('not_before_time', '').strip() else time(0)
    return datetime.combine(day, clock, tzinfo=CONFIG.timezone).astimezone(UTC)


def _clockField(form: dict[str, str], key: str) -> time:
    text = _requireField(form, key)
    try:
        return time.fromisoformat(text)
    except ValueError as exc:
        raise FormError(f'{key}: {text!r} is not a HH:MM time') from exc


def _enumField(form: dict[str, str], key: str, options: type[_E]) -> _E:
    text = _requireField(form, key)
    try:
        return options(text)
    except ValueError as exc:
        raise FormError(f'{key}: {text!r} is not one of {[option.value for option in options]}') from exc


def _html(body: str) -> _Response:
    return _Response(status=200, body=body, content_type=_HTML, location=None)


def _redirect(location: str) -> _Response:
    return _Response(status=303, body='', content_type=_HTML, location=location)


def _errorPage(status: int, heading: str, message: str) -> _Response:
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: {status}</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<link rel="stylesheet" href="/static/style.css"></head><body><main>'
        f'<div class="banner warn"><strong>{esc(heading)}</strong><p>{esc(message)}</p>'
        f'<p><a href="/">back to the calendar</a></p></div></main></body></html>'
    )
    return _Response(status=status, body=body, content_type=_HTML, location=None)


_AREA = f'(?P<area>{"|".join(CONFIG.areas)})'
_ROUTES: tuple[_Route, ...] = (
    ('GET', re.compile(r'^/$'), _home),
    ('GET', re.compile(r'^/static/(?P<name>[a-z_.]+)$'), _static),
    ('GET', re.compile(f'^/{_AREA}/calendar$'), _calendarPage),
    ('GET', re.compile(f'^/{_AREA}/tasks$'), _tasksPage),
    ('GET', re.compile(f'^/{_AREA}/deadlines$'), _deadlinesPage),
    ('GET', re.compile(f'^/{_AREA}/achievements$'), _achievementsPage),
    ('GET', re.compile(f'^/{_AREA}/recurring$'), _recurringPage),
    ('POST', re.compile(f'^/{_AREA}/recurring/add$'), _addRecurring),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/edit$'), _editRecurring),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/delete$'), _deleteRecurring),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/pull-ahead$'), _pullAhead),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/delay$'), _delayToNow),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/next$'), _setNextDue),
    ('POST', re.compile(f'^/{_AREA}/recurring/(?P<id>[a-z0-9_]+)/steps/add$'), _addStep),
    ('POST', re.compile(f'^/{_AREA}/recurring/steps/(?P<id>[a-z0-9_]+)/edit$'), _editStep),
    ('POST', re.compile(f'^/{_AREA}/recurring/steps/(?P<id>[a-z0-9_]+)/delete$'), _deleteStep),
    ('GET', re.compile(f'^/{_AREA}/settings$'), _settingsPage),
    ('POST', re.compile(f'^/{_AREA}/settings/windows$'), _saveWindows),
    ('POST', re.compile(f'^/{_AREA}/settings/color$'), _saveAreaColor),
    ('POST', re.compile(f'^/{_AREA}/settings/palette$'), _savePalette),
    ('POST', re.compile(f'^/{_AREA}/settings/categories$'), _saveCategories),
    ('POST', re.compile(f'^/{_AREA}/areas/add$'), _addNewArea),
    ('POST', re.compile(f'^/{_AREA}/areas/order$'), _saveAreaOrder),
    ('POST', re.compile(f'^/{_AREA}/area/finish$'), _finishArea),
    ('POST', re.compile(f'^/{_AREA}/area/reopen$'), _reopenArea),
    ('POST', re.compile(f'^/{_AREA}/goals/add$'), _addGoal),
    ('POST', re.compile(f'^/{_AREA}/goals/(?P<id>[a-z0-9_]+)/edit$'), _editGoal),
    ('POST', re.compile(f'^/{_AREA}/goals/(?P<id>[a-z0-9_]+)/move$'), _moveGoal),
    ('POST', re.compile(f'^/{_AREA}/goals/(?P<id>[a-z0-9_]+)/finish$'), _finishGoal),
    ('POST', re.compile(f'^/{_AREA}/goals/(?P<id>[a-z0-9_]+)/reopen$'), _reopenGoal),
    ('POST', re.compile(f'^/{_AREA}/tasks/add$'), _addTask),
    ('POST', re.compile(f'^/{_AREA}/tasks/order$'), _toggleOrder),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/edit$'), _editTask),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/tick$'), _tickTask),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/reopen$'), _reopenTask),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/auto$'), _toggleAuto),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/estimate$'), _setEstimate),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/delete$'), _deleteTask),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/deps/add$'), _addDependency),
    ('POST', re.compile(f'^/{_AREA}/tasks/(?P<id>[a-z0-9_]+)/deps/remove$'), _removeDependency),
    ('POST', re.compile(f'^/{_AREA}/ack/(?P<id>[a-z0-9_]+)$'), _ackTask),
    ('POST', re.compile(f'^/{_AREA}/log$'), _logEvent),
    ('POST', re.compile(f'^/{_AREA}/log/(?P<id>[a-z0-9_]+)/edit$'), _editLoggedEvent),
    ('POST', re.compile(f'^/{_AREA}/log/(?P<id>[a-z0-9_]+)/delete$'), _deleteLoggedEvent),
    ('POST', re.compile(f'^/{_AREA}/reschedule$'), _reschedule),
)


### vocabulary #########################################################################


_E = TypeVar('_E', bound=Enum)


@dataclass(frozen=True)
class _Request:
    match: re.Match[str]
    query: dict[str, str]
    form: dict[str, str]


@dataclass(frozen=True)
class _Response:
    status: int
    body: str | bytes
    content_type: str
    location: str | None
    restart: bool = False  # after sending: exec a fresh server so CONFIG reloads


_RouteHandler = Callable[[_Request], _Response]
_Route = tuple[str, re.Pattern[str], _RouteHandler]
