"""Render the deadline page: every open deadline in one place, off the calendar.

The page includes the tasks the plan could not fit before their date.
"""

from __future__ import annotations

from collections.abc import Mapping

from loom.vocabulary import Area, DeadlineEntry, Plan, Reason, Task, TaskId
from loom.web import page_shell
from loom.web.web_vocabulary import View


_SOON_DAYS = 3  # a deadline this many days out or fewer is highlighted


def renderDeadlinesPage(
    area: Area,
    *,
    entries: tuple[DeadlineEntry, ...],
    plan: Plan | None,
    tasks: Mapping[TaskId, Task],
) -> str:
    """Render the deadlines page: the table, then any deadline the plan cannot meet."""
    if not entries:
        table = '<p class="empty">no open deadlines</p>'
    else:
        rows = '\n'.join(_renderDeadlineRow(area, entry) for entry in entries)
        table = (
            '<table class="deadlines"><thead><tr>'
            '<th>days left</th><th>task</th><th>goal</th><th>priority</th><th>planned</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
        )

    missed = [entry for entry in (plan.unplaced if plan is not None else ()) if entry.reason is Reason.MISSES_DEADLINE]
    banner = ''
    if missed:
        items = '\n'.join(
            f'<li><a href="/{area}/tasks?focus={entry.task}">'
            f'{page_shell.escapeHtml(page_shell.findTaskTitle(entry.task, tasks))}</a></li>'
            for entry in missed
        )
        banner = (
            f'<div class="banner warn"><strong>the plan cannot meet {len(missed)} deadline(s):</strong>'
            f'<ul>{items}</ul></div>'
        )

    return page_shell.renderPage('deadlines', area, View.DEADLINES, f'<h2>Deadlines</h2>{banner}{table}')


def _renderDeadlineRow(area: Area, entry: DeadlineEntry) -> str:
    """Render one table row: days left, task, goal, priority and planned minutes."""
    if entry.days_remaining < 0:
        days_class, days_text = 'days overdue', f'{-entry.days_remaining}d overdue'
    elif entry.days_remaining <= _SOON_DAYS:
        days_class, days_text = 'days soon', f'{entry.days_remaining}d'
    else:
        days_class, days_text = 'days', f'{entry.days_remaining}d'

    goal_text = page_shell.escapeHtml(entry.goal.title) if entry.goal is not None else '&mdash;'
    blocked = ' <span class="badge dep-blocked">dep-blocked</span>' if entry.dependency_blocked else ''
    if entry.task.estimate_minutes is not None:
        planned = f'{entry.minutes_planned}/{entry.task.estimate_minutes}m'
    else:
        planned = f'{entry.minutes_planned}m (no estimate)'

    return (
        f'<tr><td class="{days_class}">{days_text}</td>'
        f'<td><a href="/{area}/tasks?focus={entry.task.id}">{page_shell.escapeHtml(entry.task.title)}</a>{blocked}</td>'
        f'<td>{goal_text}</td><td>{entry.task.priority.value}</td><td>{planned}</td></tr>'
    )
