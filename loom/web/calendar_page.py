"""Render the week calendar: planned blocks beside logged reality, banners above.

Two lanes per day column: planned blocks and fixed events on the left, logged 'did'
events on the right, overlapping in the middle so plan and reality compare visually.
Geometry works in wall-clock minutes per day, so a DST week draws correctly. Stale
blocks are hatched; unplaced and stale tasks get banners above the grid.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from loom.coloring import colorForTask, colorForTitle
from loom.config import CONFIG
from loom.vocabulary import (
    Area,
    Color,
    EventKind,
    FixedEvent,
    LoggedEvent,
    Plan,
    Reason,
    Task,
    TaskId,
)
from loom.web.block_geometry import layoutDay
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import BlockBox, View


_PLANNED_LANE = (2.0, 62.0)  # percent of the day column, left and right edge
_ACTUAL_LANE = (50.0, 98.0)  # overlaps the planned lane so the two compare
_FULL_LANE = (2.0, 98.0)  # used when only one lane has anything that day
_HOUR_REM = 3.2  # vertical scale of the scrollable grid
_DAY_LABELS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def renderCalendarPage(
    area: Area,
    week_monday: date,
    *,
    tasks: Mapping[TaskId, Task],
    plan: Plan | None,
    fixed: tuple[FixedEvent, ...],
    events: tuple[LoggedEvent, ...],
    stale: frozenset[TaskId],
    now: datetime,
    edit_event: LoggedEvent | None,
) -> str:
    """The whole calendar page for one week, as HTML."""
    here = f'/{area.value}/calendar?week={week_monday.isoformat()}'
    days = [week_monday + timedelta(days=offset) for offset in range(7)]
    planned = _plannedItems(area, tasks, plan, fixed, stale, now)
    actual = _actualItems(area, tasks, events, here)

    columns = '\n'.join(
        _renderDay(day, planned, actual, here, today=now.astimezone(CONFIG.timezone).date()) for day in days
    )
    day_rem = (_dayEndMinute() - _dayStartMinute()) / 60.0 * _HOUR_REM
    body = (
        f'{_renderWeekNav(area, week_monday)}'
        f'{_renderUnplacedBanner(area, tasks, plan)}'
        f'{_renderStaleBanner(area, tasks, stale, here)}'
        f'<div class="week-scroll"><div class="week" style="--day-rem:{day_rem:.1f}rem" '
        f'data-day-start="{_dayStartMinute()}" data-day-end="{_dayEndMinute()}" '
        f'data-scroll-start="{_scrollMinute()}">\n'
        f'{_renderGutter()}\n{columns}\n</div></div>'
        f'{_renderLogPanel(area, tasks, here, edit_event)}'
    )
    return renderPage(f'{week_monday.isoformat()} calendar', area, View.CALENDAR, body)


def _renderWeekNav(area: Area, week_monday: date) -> str:
    previous_week = (week_monday - timedelta(days=7)).isoformat()
    next_week = (week_monday + timedelta(days=7)).isoformat()
    base = f'/{area.value}/calendar'
    return (
        f'<div class="week-nav">'
        f'<a class="button" href="{base}?week={previous_week}">&larr;</a>'
        f'<a class="button" href="{base}">today</a>'
        f'<a class="button" href="{base}?week={next_week}">&rarr;</a>'
        f'<span class="week-title">week of {week_monday.isoformat()}</span>'
        f'</div>'
    )


def _renderUnplacedBanner(area: Area, tasks: Mapping[TaskId, Task], plan: Plan | None) -> str:
    if plan is None or not plan.unplaced:
        return ''

    entries = '\n'.join(
        f'<li><a href="/{area.value}/tasks?focus={entry.task}">{_titleOf(entry.task, tasks)}</a>'
        f' &mdash; {_reasonText(entry.reason)}</li>'
        for entry in plan.unplaced
    )
    return f'<div class="banner warn"><strong>{len(plan.unplaced)} not scheduled:</strong><ul>{entries}</ul></div>'


def _renderStaleBanner(
    area: Area,
    tasks: Mapping[TaskId, Task],
    stale: frozenset[TaskId],
    here: str,
) -> str:
    """The stale-task banner with per-task ack buttons, empty when nothing is stale."""
    if not stale:
        return ''

    entries = '\n'.join(
        f'<li><a href="/{area.value}/tasks?focus={task_id}">{_titleOf(task_id, tasks)}</a>'
        f'<form method="post" action="/{area.value}/ack/{task_id}" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button small">ack</button></form></li>'
        for task_id in sorted(stale)
    )
    return (
        f'<div class="banner stale-banner"><strong>{len(stale)} stale '
        f'(planned time passed, no tick or log):</strong><ul>{entries}</ul></div>'
    )


def _renderGutter() -> str:
    window_start, window_end = _dayStartMinute(), _dayEndMinute()
    window = window_end - window_start
    labels = '\n'.join(
        f'<div class="hour-label" style="top:{(mark - window_start) * 100.0 / window}%">{(mark // 60) % 24:02d}</div>'
        for mark in _minuteMarks(60)
    )
    return f'<div class="gutter"><div class="day-head"></div><div class="day-body">{labels}</div></div>'


def _renderDay(
    day: date,
    planned: tuple[_CalendarItem, ...],
    actual: tuple[_CalendarItem, ...],
    here: str,
    today: date,
) -> str:
    """One day column: hour lines, then the planned lane, then the actual lane.

    A lane widens to the whole column on a day where the other lane is empty.
    """
    window_start, window_end = _dayStartMinute(), _dayEndMinute()
    window = window_end - window_start
    lines = '\n'.join(
        f'<div class="hour-line{" half" if mark % 60 else ""}" '
        f'style="top:{(mark - window_start) * 100.0 / window}%"></div>'
        for mark in _minuteMarks(30)
    )

    day_planned = [item for item in planned if _visibleOnDay(day, item)]
    day_actual = [item for item in actual if _visibleOnDay(day, item)]
    lanes = [(_PLANNED_LANE, day_planned), (_ACTUAL_LANE, day_actual)]
    if not day_actual:
        lanes = [(_FULL_LANE, day_planned)]
    elif not day_planned:
        lanes = [(_FULL_LANE, day_actual)]

    rendered_blocks = []
    for lane, on_day in lanes:
        spans = [(_minutesInto(day, item.start), _minutesInto(day, item.end)) for item in on_day]
        for item, box in zip(on_day, layoutDay(spans, window_start, window_end), strict=True):
            if box is not None:
                rendered_blocks.append(_renderItem(item, box, lane, here))

    head_class = 'day-head today' if day == today else 'day-head'
    blocks = '\n'.join(rendered_blocks)
    return (
        f'<div class="day" data-date="{day.isoformat()}">'
        f'<div class="{head_class}">{_DAY_LABELS[day.weekday()]} {day.day}</div>'
        f'<div class="day-body">{lines}\n{blocks}</div></div>'
    )


def _renderItem(
    item: _CalendarItem,
    box: BlockBox,
    lane: tuple[float, float],
    here: str,
) -> str:
    """One absolutely positioned block: tooltip, click-through link, tick, resize handle."""
    lane_left, lane_right = lane
    width = (lane_right - lane_left) / box.columns
    left = lane_left + box.column * width

    zone = CONFIG.timezone
    start_text = item.start.astimezone(zone).strftime('%H:%M')
    end_text = item.end.astimezone(zone).strftime('%H:%M')

    tick = ''
    if item.tick_task is not None:
        tick = (
            f'<form method="post" action="{item.tick_action}" class="tick-form">'
            f'<input type="hidden" name="return_to" value="{esc(here)}">'
            f'<button class="tick" title="mark done">&check;</button></form>'
        )
    title = esc(item.title)
    if item.link is not None:
        title = f'<a href="{item.link}">{title}</a>'
    handle = '<div class="resize-handle" title="drag to change the duration"></div>' if item.resize_attrs else ''

    style = (
        f'top:{box.top_percent:.3f}%;height:{box.height_percent:.3f}%;'
        f'left:{left:.2f}%;width:{width:.2f}%;--block-color:{item.color}'
    )
    tooltip = esc(f'{start_text}-{end_text}  {item.title}')
    return (
        f'<div class="block {item.css}" style="{style}" title="{tooltip}" {item.resize_attrs}>'
        f'<span class="block-title">{title}</span> '
        f'<span class="block-time">{start_text}&ndash;{end_text}</span>{tick}{handle}</div>'
    )


def _renderLogPanel(
    area: Area,
    tasks: Mapping[TaskId, Task],
    here: str,
    edit_event: LoggedEvent | None,
) -> str:
    """The log-an-event form; prefilled for an edit, otherwise blank for a new entry."""
    zone = CONFIG.timezone
    if edit_event is not None and edit_event.start is not None and edit_event.end is not None:
        action = f'/{area.value}/log/{edit_event.id}/edit'
        heading = 'Edit logged event'
        day_value = edit_event.start.astimezone(zone).date().isoformat()
        start_value = edit_event.start.astimezone(zone).strftime('%H:%M')
        end_value = edit_event.end.astimezone(zone).strftime('%H:%M')
        note_value = edit_event.note
        task_value = edit_event.task
        delete = (
            f'<form method="post" action="/{area.value}/log/{edit_event.id}/delete" class="inline-form">'
            f'<input type="hidden" name="return_to" value="{esc(here)}">'
            f'<button class="button danger">delete</button></form>'
        )
    else:
        action = f'/{area.value}/log'
        heading = 'Log what actually happened'
        day_value = ''
        start_value = ''
        end_value = ''
        note_value = ''
        task_value = None
        delete = ''

    options = ['<option value="">(no task)</option>']
    options.extend(
        f'<option value="{task.id}"{" selected" if task.id == task_value else ""}>{esc(task.title)}</option>'
        for task in sorted(tasks.values(), key=lambda task: task.title.lower())
    )
    return (
        f'<div class="log-panel" id="log-panel"><h2>{heading}</h2>'
        f'<form method="post" action="{action}" class="log-form" id="log-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<label>date <input type="date" name="day" required value="{day_value}"></label>'
        f'<label>from <input type="time" name="start" required value="{start_value}"></label>'
        f'<label>to <input type="time" name="end" required value="{end_value}"></label>'
        f'<label>what <input type="text" name="note" required id="log-note" value="{esc(note_value)}"></label>'
        f'<label>task <select name="task">{"".join(options)}</select></label>'
        f'<button class="button">save</button></form>{delete}</div>'
    )


def _plannedItems(
    area: Area,
    tasks: Mapping[TaskId, Task],
    plan: Plan | None,
    fixed: tuple[FixedEvent, ...],
    stale: frozenset[TaskId],
    now: datetime,
) -> tuple[_CalendarItem, ...]:
    """Plan blocks plus fixed events, with staleness, tick and resize actions resolved."""
    items = []
    for block in plan.blocks if plan is not None else ():
        title = _titleOf(block.task, tasks)
        if block.chunks > 1:
            title = f'{title} ({block.chunk}/{block.chunks})'
        css = 'planned'
        if block.task in stale and block.end <= now:
            css = 'planned stale'
        task = tasks.get(block.task)
        tickable = task is not None and task.completed is None

        # dragging the bottom edge adjusts the task's estimate by the block's growth
        resize_attrs = ''
        if task is not None and task.completed is None and task.estimate_minutes is not None:
            resize_attrs = (
                f'data-kind="planned" data-endpoint="/{area.value}/tasks/{block.task}/estimate" '
                f'data-estimate="{task.estimate_minutes}"'
            )

        items.append(
            _CalendarItem(
                title=title,
                start=block.start,
                end=block.end,
                color=colorForTask(task, area) if task is not None else colorForTitle(title, area),
                css=css,
                tick_task=block.task if tickable else None,
                tick_action=f'/{area.value}/tasks/{block.task}/tick',
                link=f'/{area.value}/tasks?focus={block.task}',
                resize_attrs=resize_attrs,
            ),
        )

    items.extend(
        _CalendarItem(
            title=event.title,
            start=event.start,
            end=event.end,
            color=colorForTitle(event.title, area),
            css='fixed',
            tick_task=None,
            tick_action='',
            link=None,
            resize_attrs='',
        )
        for event in fixed
    )
    return tuple(items)


def _actualItems(
    area: Area,
    tasks: Mapping[TaskId, Task],
    events: tuple[LoggedEvent, ...],
    here: str,
) -> tuple[_CalendarItem, ...]:
    """One item per logged 'did' event; a bare attribution borrows its task's title.

    The title links to the log panel prefilled for editing; the resize attributes carry
    the event's local fields so dragging the bottom edge can re-post the edit form.
    """
    zone = CONFIG.timezone
    items = []
    for event in events:
        if event.kind is not EventKind.DID or event.start is None or event.end is None:
            continue
        title = event.note
        if event.task is not None and not title:
            title = _titleOf(event.task, tasks)

        local_start = event.start.astimezone(zone)
        local_end = event.end.astimezone(zone)
        resize_attrs = (
            f'data-kind="actual" data-endpoint="/{area.value}/log/{event.id}/edit" '
            f'data-day="{local_start.date().isoformat()}" data-start="{local_start.strftime("%H:%M")}" '
            f'data-end="{local_end.strftime("%H:%M")}" data-note="{esc(event.note)}" '
            f'data-task-ref="{event.task or ""}"'
        )
        items.append(
            _CalendarItem(
                title=title,
                start=event.start,
                end=event.end,
                color=colorForTitle(title, area),
                css='actual',
                tick_task=None,
                tick_action='',
                link=f'{here}&edit={event.id}#log-panel',
                resize_attrs=resize_attrs,
            ),
        )

    return tuple(items)


def _titleOf(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> str:
    task = tasks.get(task_id)
    return task.title if task is not None else f'missing task {task_id}'


def _reasonText(reason: Reason) -> str:
    match reason:
        case Reason.NO_ESTIMATE:
            return 'no duration estimate'
        case Reason.DEPENDENCY_BLOCKED:
            return 'waiting on an unfinished dependency'
        case Reason.NO_ROOM:
            return 'no free time left in the horizon'
        case Reason.MISSES_DEADLINE:
            return 'cannot finish before its deadline'


def _visibleOnDay(day: date, item: _CalendarItem) -> bool:
    return _minutesInto(day, item.end) > _dayStartMinute() and _minutesInto(day, item.start) < _dayEndMinute()


def _minutesInto(day: date, moment: datetime) -> int:
    """Wall-clock minutes relative to `day`'s midnight; other days land outside [0, 1440].

    An after-midnight moment therefore falls inside a grid whose day_end runs past
    24:00, on the previous day's column; layoutDay clips whatever lies outside.
    """
    local = moment.astimezone(CONFIG.timezone)
    return (local.date() - day).days * 24 * 60 + local.hour * 60 + local.minute


def _minuteMarks(step: int) -> tuple[int, ...]:
    """Grid line positions: every `step` minutes across the visible window."""
    first = -(-_dayStartMinute() // step) * step
    return tuple(range(first, _dayEndMinute() + 1, step))


def _dayStartMinute() -> int:
    return CONFIG.web.day_start.hour * 60 + CONFIG.web.day_start.minute


def _dayEndMinute() -> int:
    end = CONFIG.web.day_end.hour * 60 + CONFIG.web.day_end.minute
    if end <= _dayStartMinute():
        end += 24 * 60  # the grid runs past midnight

    return end


def _scrollMinute() -> int:
    minute = CONFIG.web.scroll_start.hour * 60 + CONFIG.web.scroll_start.minute
    if minute < _dayStartMinute():
        minute += 24 * 60

    return minute


### vocabulary #########################################################################


@dataclass(frozen=True)
class _CalendarItem:
    title: str
    start: datetime
    end: datetime
    color: Color
    css: str
    tick_task: TaskId | None
    tick_action: str
    link: str | None  # where the title click goes: the task, or the edit form
    resize_attrs: str  # data-* attributes for drag-resize, '' when not resizable
