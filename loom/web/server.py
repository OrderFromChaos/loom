"""HTTP plumbing: the route table, dispatch, and the single-threaded server.

Zero cache: every GET re-reads the data files through the store's parse gate. An edit
made by hand or by an LLM is on the next refresh, and a malformed file gets an error
page with the file's path. Mutations are plain form POSTs answered with 303 redirects,
and every task-mutating route re-plans its area before redirecting. The server is
single-threaded: requests run one at a time, so a read-modify-write never interleaves
with another request.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time as clock
from datetime import UTC, datetime
from http import HTTPMethod, HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlsplit

from loom import config
from loom.config import CONFIG
from loom.vocabulary import Area, ConfigError, StoreConflictError, StoreError
from loom.web import (
    event_routes,
    goal_routes,
    recurring_routes,
    responses,
    settings_routes,
    task_routes,
    view_routes,
)
from loom.web.web_vocabulary import FormError, Request, Response, RouteHandler


_LISTEN_HOST = '127.0.0.1'
_HOUSEKEEPING_SECONDS = 300

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
            view_routes.refreshArea(area, now)
        except StoreError as exc:
            LOG.warning('housekeeping.skipped', extra={'area': area, 'error': str(exc)})


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
        request = Request(match=matched, query=query, form=form, home_area=_HOME_AREA)
        try:
            response = route_handler(request)
        except FormError as exc:
            response = responses.buildErrorPage(HTTPStatus.BAD_REQUEST, 'That form did not parse', str(exc))
        except ConfigError as exc:
            # a POST that edits loom.toml raises ConfigError for a bad value, and the file is unchanged
            if method is HTTPMethod.POST:
                response = responses.buildErrorPage(HTTPStatus.BAD_REQUEST, 'That form did not parse', str(exc))
            else:
                heading = 'loom.toml did not parse'
                response = responses.buildErrorPage(HTTPStatus.INTERNAL_SERVER_ERROR, heading, str(exc))
        except StoreConflictError as exc:
            message = f'{exc}. Go back, refresh, and retry.'
            response = responses.buildErrorPage(HTTPStatus.CONFLICT, 'The file changed under us', message)
        except StoreError as exc:
            heading = 'A data file did not parse'
            response = responses.buildErrorPage(HTTPStatus.INTERNAL_SERVER_ERROR, heading, str(exc))
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

    message = f'no route for {method.value} {split.path}'
    _send(handler, responses.buildErrorPage(HTTPStatus.NOT_FOUND, 'Not found', message))


def _send(handler: _Handler, response: Response) -> None:
    payload = response.body.encode('utf-8') if isinstance(response.body, str) else response.body
    handler.send_response(response.status)
    if response.location is not None:
        handler.send_header('Location', response.location)
    handler.send_header('Content-Type', response.content_type)
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


_AREA = f'(?P<area>{"|".join(CONFIG.areas)})'
_ID = '(?P<id>[a-z0-9_]+)'
_ROUTES: tuple[_Route, ...] = (
    (HTTPMethod.GET, re.compile(r'^/$'), view_routes.handleHome),
    (HTTPMethod.GET, re.compile(r'^/static/(?P<name>[a-z_.]+)$'), view_routes.handleStatic),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/calendar$'), view_routes.handleCalendarPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/tasks$'), view_routes.handleTasksPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/deadlines$'), view_routes.handleDeadlinesPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/achievements$'), view_routes.handleAchievementsPage),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/recurring$'), view_routes.handleRecurringPage),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/add$'), recurring_routes.handleAddTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/edit$'), recurring_routes.handleEditTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/delete$'), recurring_routes.handleDeleteTemplate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/pull-ahead$'), recurring_routes.handlePullAhead),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/delay$'), recurring_routes.handleDelayToNow),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/next$'), recurring_routes.handleSetNextDue),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/{_ID}/steps/add$'), recurring_routes.handleAddStep),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/steps/{_ID}/edit$'), recurring_routes.handleEditStep),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/recurring/steps/{_ID}/delete$'), recurring_routes.handleDeleteStep),
    (HTTPMethod.GET, re.compile(f'^/{_AREA}/settings$'), view_routes.handleSettingsPage),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/windows$'), settings_routes.handleSaveWindows),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/color$'), settings_routes.handleSaveAreaColor),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/palette$'), settings_routes.handleSavePalette),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/settings/categories$'), settings_routes.handleSaveCategories),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/areas/add$'), settings_routes.handleAddArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/areas/order$'), settings_routes.handleSaveAreaOrder),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/area/finish$'), settings_routes.handleFinishArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/area/reopen$'), settings_routes.handleReopenArea),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/add$'), goal_routes.handleAddGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/edit$'), goal_routes.handleEditGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/move$'), goal_routes.handleMoveGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/finish$'), goal_routes.handleFinishGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/goals/{_ID}/reopen$'), goal_routes.handleReopenGoal),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/add$'), task_routes.handleAddTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/order$'), task_routes.handleToggleOrder),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/edit$'), task_routes.handleEditTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/tick$'), task_routes.handleTickTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/reopen$'), task_routes.handleReopenTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/auto$'), task_routes.handleToggleAuto),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/estimate$'), task_routes.handleSetEstimate),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/delete$'), task_routes.handleDeleteTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/deps/add$'), task_routes.handleAddDependency),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/tasks/{_ID}/deps/remove$'), task_routes.handleRemoveDependency),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/ack/{_ID}$'), event_routes.handleAckTask),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log$'), event_routes.handleLogEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log/{_ID}/edit$'), event_routes.handleEditLoggedEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/log/{_ID}/delete$'), event_routes.handleDeleteLoggedEvent),
    (HTTPMethod.POST, re.compile(f'^/{_AREA}/reschedule$'), task_routes.handleReschedule),
)


### vocabulary #########################################################################


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, HTTPMethod.GET)

    def do_POST(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's contract
        _dispatch(self, HTTPMethod.POST)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        LOG.debug('http.request', extra={'line': format % args})


_Route = tuple[HTTPMethod, re.Pattern[str], RouteHandler]
