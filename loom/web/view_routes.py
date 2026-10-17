"""The GET routes: the pages of an area, the home redirect, and the static files.

Each page route reads the area's data files again. The calendar, tasks, deadlines and
recurring routes first spawn the recurring copies that are due.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

from loom import config, recurring, scheduler, store, triage
from loom.vocabulary import Area, TaskId
from loom.web import (
    achievements_page,
    calendar_page,
    deadlines_page,
    recurring_page,
    request_fields,
    responses,
    settings_page,
    tasks_page,
)
from loom.web.web_vocabulary import FormError, Request, Response


_STATIC_DIR = Path(__file__).resolve().parent / 'static'
_STATIC_FILES = {
    'style.css': 'text/css; charset=utf-8',
    'calendar.js': 'text/javascript; charset=utf-8',
    'tree.js': 'text/javascript; charset=utf-8',
    'pickers.js': 'text/javascript; charset=utf-8',
    'areas.js': 'text/javascript; charset=utf-8',
    'restore.js': 'text/javascript; charset=utf-8',
}


def handleHome(request: Request) -> Response:
    return responses.buildRedirect(f'/{request.home_area}/calendar')


def handleStatic(request: Request) -> Response:
    file_name = request.match.group('name')
    content_type = _STATIC_FILES.get(file_name)
    if content_type is None:
        return responses.buildErrorPage(HTTPStatus.NOT_FOUND, 'Not found', f'{file_name} is not a static file here')

    body = (_STATIC_DIR / file_name).read_bytes()
    return Response(status=HTTPStatus.OK, body=body, content_type=content_type, location=None)


def handleCalendarPage(request: Request) -> Response:
    """Render the week view from freshly loaded files.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = request_fields.readArea(request)
    now = datetime.now(UTC)
    refreshArea(area, now)
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
    return responses.buildHtmlResponse(body)


def refreshArea(area: Area, now: datetime) -> None:
    """Spawn due recurring copies, and re-plan when any appeared."""
    if recurring.spawnDueCopies(area, now):
        scheduler.runPlan(area, now)


def _computeWeekMonday(raw_week: str | None, now: datetime) -> date:
    if raw_week:
        try:
            anchor = date.fromisoformat(raw_week)
        except ValueError as exc:
            raise FormError(f'{raw_week!r} is not a YYYY-MM-DD date') from exc
    else:
        anchor = config.computeLocalDate(now)

    return anchor - timedelta(days=anchor.weekday())


def handleTasksPage(request: Request) -> Response:
    """Render the goal tree and Auto group, with the saved plan's at-risk set.

    Raises:
        StoreError: a data file of this area is unreadable or malformed.
    """
    area = request_fields.readArea(request)
    refreshArea(area, datetime.now(UTC))
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
    return responses.buildHtmlResponse(body)


def handleDeadlinesPage(request: Request) -> Response:
    area = request_fields.readArea(request)
    now = datetime.now(UTC)
    refreshArea(area, now)
    area_store = store.openStore(area)
    tasks = area_store.loadTasks()
    plan = area_store.loadPlan()

    entries = triage.listDeadlineEntries(tasks, area_store.loadGoals(), plan, now)
    return responses.buildHtmlResponse(
        deadlines_page.renderDeadlinesPage(area, entries=entries, plan=plan, tasks=tasks),
    )


def handleAchievementsPage(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)

    body = achievements_page.renderAchievementsPage(area, tasks=area_store.loadTasks(), goals=area_store.loadGoals())
    return responses.buildHtmlResponse(body)


def handleRecurringPage(request: Request) -> Response:
    area = request_fields.readArea(request)
    refreshArea(area, datetime.now(UTC))
    area_store = store.openStore(area)

    body = recurring_page.renderRecurringPage(
        area,
        templates=area_store.loadTemplates(),
        steps=area_store.loadSteps(),
        goals=area_store.loadGoals(),
        tasks=area_store.loadTasks(),
    )
    return responses.buildHtmlResponse(body)


def handleSettingsPage(request: Request) -> Response:
    return responses.buildHtmlResponse(
        settings_page.renderSettingsPage(request_fields.readArea(request), saved='saved' in request.query),
    )
