"""The POST routes that append to events.jsonl or rewrite it: a logged 'did', an edit or delete of one, and an ack."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from loom import store
from loom.config import CONFIG
from loom.vocabulary import EventKind, EventSource, LoggedEvent, TaskId
from loom.web import request_fields, responses
from loom.web.web_vocabulary import FormError, Request, Response


def handleAckTask(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    task = request_fields.findRecord(area_store.loadTasks(), TaskId(request.match.group('id')), 'task')

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
    return responses.buildReturnRedirect(request, area)


def handleLogEvent(request: Request) -> Response:
    """Append a 'did' event from local day + times, optionally attributed to a task.

    Raises:
        FormError: a time field is malformed, or the named task does not exist.
    """
    area = request_fields.readArea(request)
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
            note=request_fields.readFormText(request.form, 'note'),
            task=task,
            source=EventSource.MANUAL,
        ),
    )
    return responses.buildReturnRedirect(request, area)


def _readFormInterval(form: Mapping[str, str]) -> tuple[datetime, datetime]:
    """Read the form's local day and HH:MM pair as aware UTC (start, end).

    Raises:
        FormError: a field is absent or malformed, or the end is not after the start.
    """
    day = request_fields.readFormDate(form, 'day')
    start_clock = request_fields.readFormClock(form, 'start')
    end_clock = request_fields.readFormClock(form, 'end')
    if end_clock <= start_clock:
        raise FormError('the end time must be after the start time')

    timezone = CONFIG.timezone
    start = datetime.combine(day, start_clock, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(day, end_clock, tzinfo=timezone).astimezone(UTC)
    return start, end


def handleEditLoggedEvent(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    events = area_store.loadEvents()
    event = _findLoggedEvent(request, events)

    start, end = _readFormInterval(request.form)
    raw_task = request.form.get('task', '')
    edited = replace(
        event,
        start=start,
        end=end,
        note=request_fields.readFormText(request.form, 'note'),
        task=TaskId(raw_task) if raw_task else None,
    )
    area_store.rewriteEvents(tuple(edited if kept.id == event.id else kept for kept in events))
    return responses.buildReturnRedirect(request, area)


def _findLoggedEvent(request: Request, events: tuple[LoggedEvent, ...]) -> LoggedEvent:
    event_id = request.match.group('id')
    event = next((event for event in events if event.id == event_id), None)
    if event is None:
        raise FormError(f'logged event {event_id} does not exist')

    return event


def handleDeleteLoggedEvent(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    events = area_store.loadEvents()
    event = _findLoggedEvent(request, events)

    area_store.rewriteEvents(tuple(kept for kept in events if kept.id != event.id))
    return responses.buildReturnRedirect(request, area)
