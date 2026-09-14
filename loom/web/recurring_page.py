"""Render the recurring page: maintenance templates and their cadence.

Each template shows what it will stamp out and when; the edit panel mirrors the task
edit form plus the cadence fields. Copies land in the tasks tree, so this page never
lists spawned tasks beyond the open-copy marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from loom import graph, recurring
from loom.config import CONFIG
from loom.vocabulary import DAY_NAMES, Area, Goal, GoalId, Priority, RecurringId, RecurringTask, Task, TaskId
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


def renderRecurringPage(
    area: Area,
    *,
    templates: Mapping[RecurringId, RecurringTask],
    goals: Mapping[GoalId, Goal],
    tasks: Mapping[TaskId, Task],
) -> str:
    """The whole recurring page: one card per template, then the add form."""
    here = f'/{area.value}/recurring'
    open_origins = openOrigins(tasks)
    done_dates = recurring.lastDoneDates(tasks)
    ordered = sorted(templates.values(), key=lambda template: (template.created, template.id))
    cards = '\n'.join(
        _renderCard(
            area,
            template,
            goals,
            here,
            has_open_copy=template.id in open_origins,
            last_done=done_dates.get(template.id),
        )
        for template in ordered
    )
    return renderPage(
        'recurring',
        area,
        View.RECURRING,
        f'<h2>Recurring</h2>{cards}{_renderAddForm(area, goals, here)}',
    )


def _renderCard(
    area: Area,
    template: RecurringTask,
    goals: Mapping[GoalId, Goal],
    here: str,
    *,
    has_open_copy: bool,
    last_done: date | None,
) -> str:
    """One template card: summary badges, spawn-now, edit panel, delete."""
    goal = goals.get(template.goal) if template.goal is not None else None
    if template.every_days is not None:
        badges = [f'<span class="badge">every {template.every_days}d</span>']
    else:
        badges = [f'<span class="badge">on {_dayText(template.days)}</span>']
    if template.deadline_after_days is not None:
        badges.append(f'<span class="badge deadline">due +{template.deadline_after_days}d</span>')
    if template.estimate_minutes is not None:
        badges.append(f'<span class="badge">{template.estimate_minutes}m</span>')
    if template.auto_schedule:
        badges.append('<span class="badge auto">auto</span>')
    badges.append(f'<span class="badge">&rarr; {esc(goal.title) if goal else "no goal"}</span>')

    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()
    if has_open_copy:
        badges.append('<span class="badge doing">copy open</span>')
    if recurring.owedOccurrences(template, today, last_done=last_done, open_copy=has_open_copy):
        badges.append('<span class="badge stale-badge">due now</span>')
    else:
        due = recurring.nextDue(template, last_done)
        if due is not None:
            badges.append(f'<span class="badge">next {due.isoformat()}</span>')

    spawn = (
        f'<form method="post" action="/{area.value}/recurring/{template.id}/spawn" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button small" title="create a copy right now">spawn now</button></form>'
    )
    return (
        f'<section class="goal recurring-card"><div class="goal-head">'
        f'<h2>{esc(template.title)}</h2>{"".join(badges)}{spawn}</div>'
        f'{_renderEditPanel(area, template, goals, here)}</section>'
    )


def _renderEditPanel(
    area: Area,
    template: RecurringTask,
    goals: Mapping[GoalId, Goal],
    here: str,
) -> str:
    """The collapsed edit panel: the task-like fields plus cadence, then delete."""
    estimate = template.estimate_minutes if template.estimate_minutes is not None else ''
    deadline_after = template.deadline_after_days if template.deadline_after_days is not None else ''
    checked = ' checked' if template.auto_schedule else ''
    priority_options = ''.join(
        f'<option value="{priority.value}"{" selected" if priority is template.priority else ""}>'
        f'{priority.value}</option>'
        for priority in Priority
    )

    return (
        f'<details class="edit"><summary>edit</summary>'
        f'<form method="post" action="/{area.value}/recurring/{template.id}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<label>title <input type="text" name="title" required value="{esc(template.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{esc(template.notes)}"></label>'
        f'<span class="day-picker">{_dayCheckboxes(template.days)}</span>'
        f'<label>or every <input type="number" name="every_days" min="1" '
        f'value="{template.every_days if template.every_days is not None else ""}" class="minutes"> days</label>'
        f'<label>deadline after <input type="number" name="deadline_after_days" min="0" '
        f'value="{deadline_after}" class="minutes"> days</label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" '
        f'value="{estimate}" class="minutes"></label>'
        f'<label>goal {_goalSelect(goals, template.goal)}</label>'
        f'<label>priority <select name="priority">{priority_options}</select></label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="/{area.value}/recurring/{template.id}/delete" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button danger">delete template</button></form>'
        f'</details>'
    )


def _renderAddForm(area: Area, goals: Mapping[GoalId, Goal], here: str) -> str:
    return (
        f'<section class="goal add-goal"><form method="post" action="/{area.value}/recurring/add" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<input type="text" name="title" placeholder="new recurring task" required>'
        f'<span class="day-picker">{_dayCheckboxes(frozenset())}</span>'
        f'<label>or every <input type="number" name="every_days" min="1" placeholder="&mdash;" '
        f'class="minutes"> days</label>'
        f'<label>deadline after <input type="number" name="deadline_after_days" min="0" '
        f'placeholder="&mdash;" class="minutes"> days</label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" '
        f'placeholder="&mdash;" class="minutes"></label>'
        f'<label>goal {_goalSelect(goals, None)}</label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule" checked> auto</label>'
        f'<button class="button">Add</button></form></section>'
    )


def _dayCheckboxes(selected: frozenset[int]) -> str:
    return ''.join(
        f'<label class="check"><input type="checkbox" name="day_{name.lower()}"'
        f'{" checked" if index in selected else ""}> {name}</label>'
        for index, name in enumerate(DAY_NAMES)
    )


def _dayText(days: frozenset[int]) -> str:
    if len(days) == len(DAY_NAMES):
        return 'every day'

    return ', '.join(DAY_NAMES[number] for number in sorted(days))


def _goalSelect(goals: Mapping[GoalId, Goal], selected: GoalId | None) -> str:
    options = ['<option value="">(no goal)</option>']
    options.extend(
        f'<option value="{goal.id}"{" selected" if goal.id == selected else ""}>{esc(goal.title)}</option>'
        for goal in sorted(goals.values(), key=graph.goalSortKey)
        if goal.completed is None
    )
    return f'<select name="goal">{"".join(options)}</select>'


def openOrigins(tasks: Mapping[TaskId, Task]) -> frozenset[RecurringId]:
    """Template ids that still have an open spawned copy in the tree."""
    return frozenset(task.origin for task in tasks.values() if task.origin is not None and graph.stillOpen(task))
