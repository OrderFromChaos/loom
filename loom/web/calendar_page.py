"""Render the week calendar: planned blocks beside logged reality, banners above.

Two lanes per day column: planned blocks and fixed events on the left, logged 'did'
events on the right, overlapping in the middle for a visual comparison. Geometry is in
wall-clock minutes per day, so a DST week renders correctly. Stale blocks are hatched,
and unplaced and stale tasks get banners above the grid.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from loom import coloring, config, triage
from loom.config import CONFIG
from loom.vocabulary import (
    DAY_NAMES,
    MINUTES_PER_DAY,
    MINUTES_PER_HOUR,
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
from loom.web import block_geometry, page_shell
from loom.web.web_vocabulary import BlockBox, View


_PLANNED_LANE = (2.0, 62.0)  # percent of the day column, left and right edge
_ACTUAL_LANE = (50.0, 98.0)  # overlaps the planned lane, for a side-by-side comparison
_FULL_LANE = (2.0, 98.0)  # used when only one lane has anything that day
_HOUR_REM = 3.2  # vertical scale of the scrollable grid


def renderCalendarPage(
    area: Area,
    week_monday: date,
    *,
    tasks: Mapping[TaskId, Task],
    plan: Plan | None,
    fixed: tuple[FixedEvent, ...],
    events: tuple[LoggedEvent, ...],
    now: datetime,
    edit_event: LoggedEvent | None,
) -> str:
    """Render the calendar page of one week."""
    context = _CalendarContext(
        area=area,
        tasks=tasks,
        here=f'/{area}/calendar?week={week_monday.isoformat()}',
        now=now,
        stale=triage.collectOverdueTaskIds(tasks, now),
    )
    days = [week_monday + timedelta(days=offset) for offset in range(len(DAY_NAMES))]
    planned = (*_buildPlannedItems(context, plan), *_buildFixedItems(context, fixed))
    actual = _buildActualItems(context, events)

    columns = '\n'.join(_renderDay(context, day, planned, actual) for day in days)
    grid_start, grid_end = CONFIG.web.computeGridStartMinute(), CONFIG.web.computeGridEndMinute()
    day_rem = (grid_end - grid_start) / MINUTES_PER_HOUR * _HOUR_REM
    body = (
        f'{_renderWeekNav(context, week_monday)}'
        f'{_renderUnplacedBanner(context, plan)}'
        f'{_renderStaleBanner(context)}'
        f'<div class="week-scroll"><div class="week" style="--day-rem:{day_rem:.1f}rem" '
        f'data-day-start="{grid_start}" data-day-end="{grid_end}" '
        f'data-scroll-start="{CONFIG.web.computeScrollMinute()}">\n'
        f'{_renderGutter()}\n{columns}\n</div></div>'
        f'{_renderLogPanel(context, edit_event)}'
    )
    return page_shell.renderPage(f'{week_monday.isoformat()} calendar', area, View.CALENDAR, body)


def _buildPlannedItems(context: _CalendarContext, plan: Plan | None) -> tuple[_CalendarItem, ...]:
    """Build the plan blocks, with staleness, tick and resize actions resolved."""
    area = context.area
    items = []
    for block in plan.blocks if plan is not None else ():
        title = page_shell.findTaskTitle(block.task, context.tasks)
        if block.chunks > 1:
            title = f'{title} ({block.chunk}/{block.chunks})'
        css_classes = 'planned'
        if block.task in context.stale and block.end <= context.now:
            css_classes = 'planned stale'
        task = context.tasks.get(block.task)
        tickable = task is not None and task.completed is None

        # dragging the bottom edge adjusts the task's estimate by the block's growth
        resize_attributes = ''
        if task is not None and task.completed is None and task.estimate_minutes is not None:
            resize_attributes = (
                f'data-kind="planned" data-endpoint="/{area}/tasks/{block.task}/estimate" '
                f'data-estimate="{task.estimate_minutes}"'
            )

        items.append(
            _CalendarItem(
                title=title,
                start=block.start,
                end=block.end,
                color=coloring.pickTaskColor(task, area) if task is not None else coloring.pickTitleColor(title, area),
                css_classes=css_classes,
                tick_action=f'/{area}/tasks/{block.task}/tick' if tickable else None,
                link=f'/{area}/tasks?focus={block.task}',
                resize_attributes=resize_attributes,
            ),
        )

    return tuple(items)


def _buildFixedItems(context: _CalendarContext, fixed: tuple[FixedEvent, ...]) -> tuple[_CalendarItem, ...]:
    return tuple(
        _CalendarItem(
            title=event.title,
            start=event.start,
            end=event.end,
            color=coloring.pickTitleColor(event.title, context.area),
            css_classes='fixed',
            tick_action=None,
            link=None,
            resize_attributes='',
        )
        for event in fixed
    )


def _buildActualItems(context: _CalendarContext, events: tuple[LoggedEvent, ...]) -> tuple[_CalendarItem, ...]:
    """Build one item per logged 'did' event. An event with a task and no note shows the task's title.

    The title links to the log panel, prefilled for editing. The resize attributes have the
    event's local fields, so a drag of the bottom edge can re-post the edit form.
    """
    timezone = CONFIG.timezone
    items = []
    for event in events:
        if event.kind is not EventKind.DID or event.start is None or event.end is None:
            continue
        title = event.note
        if event.task is not None and not title:
            title = page_shell.findTaskTitle(event.task, context.tasks)

        local_start = event.start.astimezone(timezone)
        local_end = event.end.astimezone(timezone)
        resize_attributes = (
            f'data-kind="actual" data-endpoint="/{context.area}/log/{event.id}/edit" '
            f'data-day="{local_start.date().isoformat()}" data-start="{local_start.strftime("%H:%M")}" '
            f'data-end="{local_end.strftime("%H:%M")}" data-note="{page_shell.escapeHtml(event.note)}" '
            f'data-task-ref="{event.task or ""}"'
        )
        items.append(
            _CalendarItem(
                title=title,
                start=event.start,
                end=event.end,
                color=coloring.pickTitleColor(title, context.area),
                css_classes='actual',
                tick_action=None,
                link=f'{context.here}&edit={event.id}#log-panel',
                resize_attributes=resize_attributes,
            ),
        )

    return tuple(items)


def _renderDay(
    context: _CalendarContext,
    day: date,
    planned: tuple[_CalendarItem, ...],
    actual: tuple[_CalendarItem, ...],
) -> str:
    """Render one day column: hour lines, then the planned lane, then the actual lane.

    A lane widens to the whole column on a day where the other lane is empty.
    """
    HALF_HOUR = MINUTES_PER_HOUR // 2
    grid_start, grid_end = CONFIG.web.computeGridStartMinute(), CONFIG.web.computeGridEndMinute()
    window = grid_end - grid_start
    lines = '\n'.join(
        f'<div class="hour-line{" half" if mark % MINUTES_PER_HOUR else ""}" '
        f'style="top:{(mark - grid_start) * 100.0 / window}%"></div>'
        for mark in _listMinuteMarks(HALF_HOUR)
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
        spans = [(_computeMinuteOfDay(day, item.start), _computeMinuteOfDay(day, item.end)) for item in on_day]
        for item, box in zip(on_day, block_geometry.computeBlockBoxes(spans, grid_start, grid_end), strict=True):
            if box is not None:
                rendered_blocks.append(_renderCalendarItem(context, item, box, lane))

    head_class = 'day-head today' if day == config.computeLocalDate(context.now) else 'day-head'
    blocks = '\n'.join(rendered_blocks)
    return (
        f'<div class="day" data-date="{day.isoformat()}">'
        f'<div class="{head_class}">{DAY_NAMES[day.weekday()]} {day.day}</div>'
        f'<div class="day-body">{lines}\n{blocks}</div></div>'
    )


def _renderCalendarItem(
    context: _CalendarContext,
    item: _CalendarItem,
    box: BlockBox,
    lane: tuple[float, float],
) -> str:
    """Render one absolutely positioned block: tooltip, click-through link, tick, resize handle."""
    lane_left, lane_right = lane
    width = (lane_right - lane_left) / box.columns
    left = lane_left + box.column * width

    start_text = item.start.astimezone(CONFIG.timezone).strftime('%H:%M')
    end_text = item.end.astimezone(CONFIG.timezone).strftime('%H:%M')

    tick = ''
    if item.tick_action is not None:
        tick = page_shell.renderPostButton(
            item.tick_action,
            '&check;',
            button_class='tick',
            form_class='tick-form',
            return_to=context.here,
            title='mark done',
        )
    title = page_shell.escapeHtml(item.title)
    if item.link is not None:
        title = f'<a href="{item.link}">{title}</a>'
    handle = '<div class="resize-handle" title="drag to change the duration"></div>' if item.resize_attributes else ''

    style = (
        f'top:{box.top_percent:.3f}%;height:{box.height_percent:.3f}%;'
        f'left:{left:.2f}%;width:{width:.2f}%;--block-color:{item.color}'
    )
    tooltip = page_shell.escapeHtml(f'{start_text}-{end_text}  {item.title}')
    return (
        f'<div class="block {item.css_classes}" style="{style}" title="{tooltip}" {item.resize_attributes}>'
        f'<span class="block-title">{title}</span> '
        f'<span class="block-time">{start_text}&ndash;{end_text}</span>{tick}{handle}</div>'
    )


def _renderWeekNav(context: _CalendarContext, week_monday: date) -> str:
    """Render the week arrows and title on the left, the re-plan button on the right."""
    previous_week = (week_monday - timedelta(weeks=1)).isoformat()
    next_week = (week_monday + timedelta(weeks=1)).isoformat()
    base = f'/{context.area}/calendar'
    reschedule = page_shell.renderPostButton(
        f'/{context.area}/reschedule',
        'Reschedule',
        button_class='button',
        form_class='inline-form reschedule',
        return_to=context.here,
        title='re-plan after editing files by hand',
    )
    return (
        f'<div class="week-nav">'
        f'<a class="button" href="{base}?week={previous_week}">&larr;</a>'
        f'<a class="button" href="{base}">today</a>'
        f'<a class="button" href="{base}?week={next_week}">&rarr;</a>'
        f'<span class="week-title">week of {week_monday.isoformat()}</span>'
        f'{reschedule}'
        f'</div>'
    )


def _renderUnplacedBanner(context: _CalendarContext, plan: Plan | None) -> str:
    if plan is None or not plan.unplaced:
        return ''

    entries = '\n'.join(
        f'<li><a href="/{context.area}/tasks?focus={entry.task}">'
        f'{page_shell.escapeHtml(page_shell.findTaskTitle(entry.task, context.tasks))}</a>'
        f' &mdash; {_describeReason(entry.reason)}</li>'
        for entry in plan.unplaced
    )
    return f'<div class="banner warn"><strong>{len(plan.unplaced)} not scheduled:</strong><ul>{entries}</ul></div>'


def _describeReason(reason: Reason) -> str:
    match reason:
        case Reason.NO_ESTIMATE:
            return 'no duration estimate'
        case Reason.DEPENDENCY_BLOCKED:
            return 'waiting on an unfinished dependency'
        case Reason.NO_ROOM:
            return 'no free time left in the horizon'
        case Reason.MISSES_DEADLINE:
            return 'cannot finish before its deadline'


def _renderStaleBanner(context: _CalendarContext) -> str:
    """Render the stale-task banner: open tasks past their deadline, '' when there are none."""
    if not context.stale:
        return ''

    tasks = context.tasks
    entries = '\n'.join(
        f'<li><a href="/{context.area}/tasks?focus={task_id}">'
        f'{page_shell.escapeHtml(page_shell.findTaskTitle(task_id, tasks))}</a></li>'
        for task_id in sorted(context.stale, key=lambda task_id: (tasks[task_id].deadline, task_id))
    )
    heading = f'<strong>{len(context.stale)} stale (past deadline):</strong>'
    return f'<div class="banner stale-banner">{heading}<ul>{entries}</ul></div>'


def _renderGutter() -> str:
    grid_start, grid_end = CONFIG.web.computeGridStartMinute(), CONFIG.web.computeGridEndMinute()
    window = grid_end - grid_start
    labels = '\n'.join(
        f'<div class="hour-label" style="top:{(mark - grid_start) * 100.0 / window}%">'
        f'{mark % MINUTES_PER_DAY // MINUTES_PER_HOUR:02d}</div>'
        for mark in _listMinuteMarks(MINUTES_PER_HOUR)
    )
    return f'<div class="gutter"><div class="day-head"></div><div class="day-body">{labels}</div></div>'


def _renderLogPanel(context: _CalendarContext, edit_event: LoggedEvent | None) -> str:
    """Render the log-an-event form, prefilled for an edit, otherwise blank for a new entry."""
    area = context.area
    timezone = CONFIG.timezone
    if edit_event is not None and edit_event.start is not None and edit_event.end is not None:
        action = f'/{area}/log/{edit_event.id}/edit'
        heading = 'Edit logged event'
        day_value = edit_event.start.astimezone(timezone).date().isoformat()
        start_value = edit_event.start.astimezone(timezone).strftime('%H:%M')
        end_value = edit_event.end.astimezone(timezone).strftime('%H:%M')
        note_value = edit_event.note
        task_value = edit_event.task
        delete = page_shell.renderPostButton(
            f'/{area}/log/{edit_event.id}/delete',
            'delete',
            button_class='button danger',
            return_to=context.here,
        )
    else:
        action = f'/{area}/log'
        heading = 'Log what actually happened'
        day_value = ''
        start_value = ''
        end_value = ''
        note_value = ''
        task_value = None
        delete = ''

    by_title = sorted(context.tasks.values(), key=lambda task: task.title.lower())
    options = page_shell.renderOptions((('', '(no task)'), *((task.id, task.title) for task in by_title)), task_value)
    note_attribute = page_shell.escapeHtml(note_value)
    return (
        f'<div class="log-panel" id="log-panel"><h2>{heading}</h2>'
        f'<form method="post" action="{action}" class="log-form" id="log-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<label>date <input type="date" name="day" required value="{day_value}"></label>'
        f'<label>from <input type="time" name="start" required value="{start_value}"></label>'
        f'<label>to <input type="time" name="end" required value="{end_value}"></label>'
        f'<label>what <input type="text" name="note" required id="log-note" value="{note_attribute}"></label>'
        f'<label>task <select name="task">{options}</select></label>'
        f'<button class="button">save</button></form>{delete}</div>'
    )


def _visibleOnDay(day: date, item: _CalendarItem) -> bool:
    return (
        _computeMinuteOfDay(day, item.end) > CONFIG.web.computeGridStartMinute()
        and _computeMinuteOfDay(day, item.start) < CONFIG.web.computeGridEndMinute()
    )


def _computeMinuteOfDay(day: date, moment: datetime) -> int:
    """Compute wall-clock minutes from the midnight that starts `day`.

    A moment on another day falls outside [0, MINUTES_PER_DAY]. So an after-midnight
    moment falls inside a grid whose day_end runs past 24:00, on the previous day's
    column. computeBlockBoxes clips the part outside the grid.
    """
    local = moment.astimezone(CONFIG.timezone)
    return (local.date() - day).days * MINUTES_PER_DAY + local.hour * MINUTES_PER_HOUR + local.minute


def _listMinuteMarks(step: int) -> tuple[int, ...]:
    """List the grid line positions: every `step` minutes across the visible window."""
    first = -(-CONFIG.web.computeGridStartMinute() // step) * step
    return tuple(range(first, CONFIG.web.computeGridEndMinute() + 1, step))


### vocabulary #########################################################################


@dataclass(frozen=True)
class _CalendarContext:
    area: Area
    tasks: Mapping[TaskId, Task]
    here: str  # this page's URL, the return target of every form on it
    now: datetime
    stale: frozenset[TaskId]


@dataclass(frozen=True)
class _CalendarItem:
    title: str
    start: datetime
    end: datetime
    color: Color
    css_classes: str
    tick_action: str | None  # the tick form's URL. None: no tick
    link: str | None  # where the title click goes: the task, or the edit form
    resize_attributes: str  # data-* attributes for drag-resize, '' when not resizable
