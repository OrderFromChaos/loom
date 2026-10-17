"""Render the recurring page: maintenance templates and their cadence.

Each template shows what it will stamp out and when, and its step tree: nested steps
that every spawned copy carries as subtasks, edited with the same row tools as the task
tree (edit, +). The edit panel mirrors the task edit form plus the cadence fields.
Copies land in the tasks tree, so this page never lists spawned tasks beyond the
open-copy marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from loom import graph, recurring
from loom.config import CONFIG
from loom.vocabulary import (
    DAY_NAMES,
    Area,
    Goal,
    GoalId,
    Priority,
    RecurringId,
    RecurringStep,
    RecurringTask,
    StepId,
    StepIndex,
    Task,
    TaskId,
)
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


def renderRecurringPage(
    area: Area,
    *,
    templates: Mapping[RecurringId, RecurringTask],
    steps: Mapping[StepId, RecurringStep],
    goals: Mapping[GoalId, Goal],
    tasks: Mapping[TaskId, Task],
) -> str:
    """The whole recurring page: one card per template, then the add form."""
    here = f'/{area}/recurring'
    open_counts = recurring.openCopies(tasks)
    done_dates = recurring.lastDoneDates(tasks)
    ordered = sorted(templates.values(), key=lambda template: (template.created, template.id))
    cards = '\n'.join(
        _renderCard(
            area,
            template,
            goals,
            here,
            open_copies=open_counts[template.id],
            last_done=done_dates.get(template.id),
            step_children=recurring.stepsByParent(steps, template.id),
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
    open_copies: int,
    last_done: date | None,
    step_children: StepIndex,
) -> str:
    """One template card: badges, add-step, the cadence corrections, step tree, edit panel.

    Delay to now shows while a copy is open. Pull ahead shows unless the template waits
    for completion and a copy is already open, since that would be a second copy.
    """
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
    if template.wait_for_completion:
        badges.append('<span class="badge">waits for completion</span>')
    badges.append(f'<span class="badge">&rarr; {esc(goal.title) if goal else "no goal"}</span>')

    today = datetime.now(UTC).astimezone(CONFIG.timezone).date()
    if open_copies:
        badges.append(f'<span class="badge doing">{open_copies} open</span>')
    if recurring.owedOccurrences(template, today, last_done=last_done, open_copy=open_copies > 0):
        badges.append('<span class="badge stale-badge">due now</span>')
    else:
        due = recurring.nextDue(template, last_done, open_copy=open_copies > 0)
        if due is not None and template.every_days is not None:
            badges.append(_renderNextDueForm(area, template, here, due=due))
        elif due is not None and not (template.wait_for_completion and open_copies):
            # anchor days with a waiting copy open: nothing spawns until it is done
            badges.append(f'<span class="badge">next {due.isoformat()}</span>')

    actions = ''
    if open_copies:
        actions += _renderCadenceAction(
            area,
            template,
            here,
            action='delay',
            label='delay to now',
            hint='merge the open copies into one due as if spawned today; the next counts from today',
        )
    if not (template.wait_for_completion and open_copies):
        actions += _renderCadenceAction(
            area,
            template,
            here,
            action='pull-ahead',
            label='pull ahead',
            hint='spawn a copy now; the next counts from today',
        )
    add_toggle = f'<label class="row-toggle" for="step-add-{template.id}" title="add a step">+</label>'
    return (
        f'<section class="goal recurring-card"><div class="goal-head">'
        f'<h2>{esc(template.title)}</h2>{"".join(badges)}{add_toggle}{actions}</div>'
        f'<input type="checkbox" id="step-add-{template.id}" class="panel-state add-state">'
        f'<div class="steps">{_renderStepTree(area, template, step_children, here)}</div>'
        f'<div class="panel child-adder">{_renderStepAddForm(area, template, None, here)}</div>'
        f'{_renderEditPanel(area, template, goals, here)}</section>'
    )


def _renderNextDueForm(area: Area, template: RecurringTask, here: str, *, due: date) -> str:
    """The interval's next date as an editable field: pick a day and set it to realign.

    Raises:
        Nothing: it only formats.
    """
    return (
        f'<form method="post" action="/{area}/recurring/{template.id}/next" class="inline-form next-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<label class="badge">next <input type="date" name="next_due" required value="{due.isoformat()}"></label>'
        f'<button class="button small ghost" title="spawn the next copy on this day; the rhythm continues from it">'
        f'set</button></form>'
    )


def _renderCadenceAction(
    area: Area,
    template: RecurringTask,
    here: str,
    *,
    action: str,
    label: str,
    hint: str,
) -> str:
    """One cadence-correction button on a template card.

    Raises:
        Nothing: it only formats.
    """
    return (
        f'<form method="post" action="/{area}/recurring/{template.id}/{action}" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button small" title="{esc(hint)}">{label}</button></form>'
    )


def _renderStepTree(
    area: Area,
    template: RecurringTask,
    children: StepIndex,
    here: str,
) -> str:
    """The template's steps as nested rows, walked with an explicit stack like the task tree."""
    parts: list[str] = []
    stack: list[RecurringStep | str] = list(reversed(children.get(None, ())))
    seen: set[StepId] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        if item.id in seen:
            continue
        seen.add(item.id)
        parts.append(
            f'<details class="task step" data-task="step-{item.id}" open>'
            f'<summary>{_renderStepRow(item, has_children=bool(children.get(item.id)))}</summary>'
            f'<input type="checkbox" id="step-edit-{item.id}" class="panel-state edit-state">'
            f'<input type="checkbox" id="step-add-{item.id}" class="panel-state add-state">'
            f'{_renderStepEditPanel(area, item, here)}<div class="children">',
        )
        stack.append(
            f'</div><div class="panel child-adder">{_renderStepAddForm(area, template, item.id, here)}</div></details>',
        )
        stack.extend(reversed(children.get(item.id, ())))

    return ''.join(parts)


def _renderStepRow(step: RecurringStep, *, has_children: bool) -> str:
    """A step laid out like a task row, its tick box drawn but inert: steps are ticked on copies."""
    caret = f'<span class="twist">{"&#9656;" if has_children else ""}</span>'
    tick = '<span class="tick inert" title="ticked on each spawned copy, in the tasks view"></span>'
    badges = ''
    if step.estimate_minutes is not None:
        badges += f'<span class="badge">{step.estimate_minutes}m</span>'
    if step.auto_schedule:
        badges += '<span class="badge auto">auto</span>'
    return (
        f'{caret}{tick}<span class="title">{esc(step.title)}</span>{badges}'
        f'<label class="row-toggle" for="step-edit-{step.id}">edit</label>'
        f'<label class="row-toggle" for="step-add-{step.id}" title="add a step under this">+</label>'
    )


def _renderStepEditPanel(area: Area, step: RecurringStep, here: str) -> str:
    estimate = step.estimate_minutes if step.estimate_minutes is not None else ''
    checked = ' checked' if step.auto_schedule else ''
    return (
        f'<div class="panel edit-panel">'
        f'<form method="post" action="/{area}/recurring/steps/{step.id}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<label>title <input type="text" name="title" required value="{esc(step.title)}"></label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" value="{estimate}" '
        f'class="minutes"></label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="/{area}/recurring/steps/{step.id}/delete" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button danger">delete</button></form></div>'
    )


def _renderStepAddForm(
    area: Area,
    template: RecurringTask,
    parent: StepId | None,
    here: str,
) -> str:
    """The add-a-step line: under `parent`, or at the template's top level when it is None.

    Raises:
        Nothing: it only formats.
    """
    parent_field = f'<input type="hidden" name="parent" value="{parent}">' if parent is not None else ''
    return (
        f'<form method="post" action="/{area}/recurring/{template.id}/steps/add" class="add-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">{parent_field}'
        f'<input type="text" name="title" placeholder="add a step" required>'
        f'<input type="number" name="estimate_minutes" min="1" placeholder="min" class="minutes">'
        f'<label class="check"><input type="checkbox" name="auto_schedule"> auto</label>'
        f'<button class="button">Add</button></form>'
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
    waits = ' checked' if template.wait_for_completion else ''
    priority_options = ''.join(
        f'<option value="{priority.value}"{" selected" if priority is template.priority else ""}>'
        f'{priority.value}</option>'
        for priority in Priority
    )

    return (
        f'<details class="edit"><summary>edit</summary>'
        f'<form method="post" action="/{area}/recurring/{template.id}/edit" class="edit-form">'
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
        f'<label class="check"><input type="checkbox" name="wait_for_completion"{waits}> '
        f'wait to spawn until completion</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="/{area}/recurring/{template.id}/delete" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(here)}">'
        f'<button class="button danger">delete template</button></form>'
        f'</details>'
    )


def _renderAddForm(area: Area, goals: Mapping[GoalId, Goal], here: str) -> str:
    return (
        f'<section class="goal add-goal"><form method="post" action="/{area}/recurring/add" class="edit-form">'
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
        f'<label class="check"><input type="checkbox" name="wait_for_completion"> '
        f'wait to spawn until completion</label>'
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
