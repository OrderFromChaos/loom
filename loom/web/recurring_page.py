"""Render the recurring page: maintenance templates and their cadence.

Each template card has the copy it spawns and when, then its step tree. Every spawned
copy gets the steps as subtasks. A step row has the same row tools as a task row (edit,
+). The edit panel has the task edit fields plus the cadence fields. Spawned copies are
in the tasks tree, and this page has only an open-copy count for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from loom import config, graph, recurring
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
from loom.web import page_shell
from loom.web.web_vocabulary import View


def renderRecurringPage(
    area: Area,
    *,
    templates: Mapping[RecurringId, RecurringTask],
    steps: Mapping[StepId, RecurringStep],
    goals: Mapping[GoalId, Goal],
    tasks: Mapping[TaskId, Task],
) -> str:
    """Render the recurring page: one card per template, then the add form."""
    context = _RecurringContext(
        area=area,
        here=f'/{area}/recurring',
        goals=goals,
        today=config.computeLocalDate(datetime.now(UTC)),
    )
    open_counts = recurring.countOpenCopies(tasks)
    done_dates = recurring.collectLastDoneDates(tasks)
    ordered = sorted(templates.values(), key=lambda template: (template.created, template.id))
    cards = '\n'.join(
        _renderTemplateCard(
            context,
            template,
            open_copies=open_counts[template.id],
            last_done=done_dates.get(template.id),
            step_children=recurring.groupStepsByParent(steps, template.id),
        )
        for template in ordered
    )
    return page_shell.renderPage(
        'recurring',
        area,
        View.RECURRING,
        f'<h2>Recurring</h2>{cards}{_renderTemplateAddForm(context)}',
    )


def _renderTemplateCard(
    context: _RecurringContext,
    template: RecurringTask,
    *,
    open_copies: int,
    last_done: date | None,
    step_children: StepIndex,
) -> str:
    """Render one template card: badges, add-step, the cadence corrections, step tree, edit panel.

    Delay to now is offered while a copy is open. Pull ahead is offered unless the template
    waits for completion and a copy is already open, because that would be a second copy.
    """
    goal = context.goals.get(template.goal) if template.goal is not None else None
    if template.every_days is not None:
        badges = [f'<span class="badge">every {template.every_days}d</span>']
    else:
        badges = [f'<span class="badge">on {_formatDays(template.days)}</span>']
    if template.deadline_after_days is not None:
        badges.append(f'<span class="badge deadline">due +{template.deadline_after_days}d</span>')
    if template.estimate_minutes is not None:
        badges.append(f'<span class="badge">{template.estimate_minutes}m</span>')
    if template.auto_schedule:
        badges.append('<span class="badge auto">auto</span>')
    if template.wait_for_completion:
        badges.append('<span class="badge">waits for completion</span>')
    badges.append(f'<span class="badge">&rarr; {page_shell.escapeHtml(goal.title) if goal else "no goal"}</span>')

    if open_copies:
        badges.append(f'<span class="badge doing">{open_copies} open</span>')
    if recurring.listOwedOccurrences(template, context.today, last_done=last_done, open_copy=open_copies > 0):
        badges.append('<span class="badge stale-badge">due now</span>')
    else:
        due = recurring.computeNextDue(template, last_done, open_copy=open_copies > 0)
        if due is not None and template.every_days is not None:
            badges.append(_renderNextDueForm(context, template, due))
        elif due is not None and not (template.wait_for_completion and open_copies):
            # anchor days with a waiting copy open: nothing spawns until it is done
            badges.append(f'<span class="badge">next {due.isoformat()}</span>')

    template_path = f'/{context.area}/recurring/{template.id}'
    actions = ''
    if open_copies:
        actions += page_shell.renderPostButton(
            f'{template_path}/delay',
            'delay to now',
            button_class='button small',
            return_to=context.here,
            title='merge the open copies into one due as if spawned today; the next counts from today',
        )
    if not (template.wait_for_completion and open_copies):
        actions += page_shell.renderPostButton(
            f'{template_path}/pull-ahead',
            'pull ahead',
            button_class='button small',
            return_to=context.here,
            title='spawn a copy now; the next counts from today',
        )
    add_toggle = f'<label class="row-toggle" for="step-add-{template.id}" title="add a step">+</label>'
    return (
        f'<section class="goal recurring-card"><div class="goal-head">'
        f'<h2>{page_shell.escapeHtml(template.title)}</h2>{"".join(badges)}{add_toggle}{actions}</div>'
        f'<input type="checkbox" id="step-add-{template.id}" class="panel-state add-state">'
        f'<div class="steps">{_renderStepTree(context, template, step_children)}</div>'
        f'<div class="panel child-adder">{_renderStepAddForm(context, template, None)}</div>'
        f'{_renderTemplateEditPanel(context, template)}</section>'
    )


def _renderNextDueForm(context: _RecurringContext, template: RecurringTask, due: date) -> str:
    """Render the interval's next date as an editable field. Setting a day realigns the rhythm."""
    return (
        f'<form method="post" action="/{context.area}/recurring/{template.id}/next" class="inline-form next-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<label class="badge">next <input type="date" name="next_due" required value="{due.isoformat()}"></label>'
        f'<button class="button small ghost" title="spawn the next copy on this day; the rhythm continues from it">'
        f'set</button></form>'
    )


def _renderStepTree(context: _RecurringContext, template: RecurringTask, children: StepIndex) -> str:
    """Render the template's steps as nested rows. The walk uses an explicit stack."""
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
            f'{_renderStepEditPanel(context, item)}<div class="children">',
        )
        stack.append(
            f'</div><div class="panel child-adder">{_renderStepAddForm(context, template, item.id)}</div></details>',
        )
        stack.extend(reversed(children.get(item.id, ())))

    return ''.join(parts)


def _renderStepRow(step: RecurringStep, *, has_children: bool) -> str:
    """Render a step like a task row, with an inert tick box. A step is ticked on each copy."""
    caret = f'<span class="twist">{"&#9656;" if has_children else ""}</span>'
    tick = '<span class="tick inert" title="ticked on each spawned copy, in the tasks view"></span>'
    badges = ''
    if step.estimate_minutes is not None:
        badges += f'<span class="badge">{step.estimate_minutes}m</span>'
    if step.auto_schedule:
        badges += '<span class="badge auto">auto</span>'
    return (
        f'{caret}{tick}<span class="title">{page_shell.escapeHtml(step.title)}</span>{badges}'
        f'<label class="row-toggle" for="step-edit-{step.id}">edit</label>'
        f'<label class="row-toggle" for="step-add-{step.id}" title="add a step under this">+</label>'
    )


def _renderStepEditPanel(context: _RecurringContext, step: RecurringStep) -> str:
    step_path = f'/{context.area}/recurring/steps/{step.id}'
    estimate = step.estimate_minutes if step.estimate_minutes is not None else ''
    checked = ' checked' if step.auto_schedule else ''
    delete = page_shell.renderPostButton(
        f'{step_path}/delete',
        'delete',
        button_class='button danger',
        return_to=context.here,
    )
    return (
        f'<div class="panel edit-panel">'
        f'<form method="post" action="{step_path}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<label>title <input type="text" name="title" required value="{page_shell.escapeHtml(step.title)}"></label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" value="{estimate}" '
        f'class="minutes"></label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'{delete}</div>'
    )


def _renderStepAddForm(context: _RecurringContext, template: RecurringTask, parent: StepId | None) -> str:
    """Render the add-a-step line: under `parent`, or at the template's top level when it is None."""
    parent_field = f'<input type="hidden" name="parent" value="{parent}">' if parent is not None else ''
    return (
        f'<form method="post" action="/{context.area}/recurring/{template.id}/steps/add" class="add-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">{parent_field}'
        f'<input type="text" name="title" placeholder="add a step" required>'
        f'<input type="number" name="estimate_minutes" min="1" placeholder="min" class="minutes">'
        f'<label class="check"><input type="checkbox" name="auto_schedule"> auto</label>'
        f'<button class="button">Add</button></form>'
    )


def _renderTemplateEditPanel(context: _RecurringContext, template: RecurringTask) -> str:
    """Render the collapsed edit panel: the task-like fields plus cadence, then delete."""
    template_path = f'/{context.area}/recurring/{template.id}'
    estimate = template.estimate_minutes if template.estimate_minutes is not None else ''
    every_days = template.every_days if template.every_days is not None else ''
    deadline_after = template.deadline_after_days if template.deadline_after_days is not None else ''
    checked = ' checked' if template.auto_schedule else ''
    waits = ' checked' if template.wait_for_completion else ''
    priority_options = page_shell.renderOptions(
        ((priority.value, priority.value) for priority in Priority),
        template.priority.value,
    )
    delete = page_shell.renderPostButton(
        f'{template_path}/delete',
        'delete template',
        button_class='button danger',
        return_to=context.here,
    )

    return (
        f'<details class="edit"><summary>edit</summary>'
        f'<form method="post" action="{template_path}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<label>title <input type="text" name="title" required value="{page_shell.escapeHtml(template.title)}">'
        f'</label>'
        f'<label>notes <input type="text" name="notes" value="{page_shell.escapeHtml(template.notes)}"></label>'
        f'<span class="day-picker">{_renderDayCheckboxes(template.days)}</span>'
        f'<label>or every <input type="number" name="every_days" min="1" '
        f'value="{every_days}" class="minutes"> days</label>'
        f'<label>deadline after <input type="number" name="deadline_after_days" min="0" '
        f'value="{deadline_after}" class="minutes"> days</label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" '
        f'value="{estimate}" class="minutes"></label>'
        f'<label>goal {_renderGoalSelect(context.goals, template.goal)}</label>'
        f'<label>priority <select name="priority">{priority_options}</select></label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<label class="check"><input type="checkbox" name="wait_for_completion"{waits}> '
        f'wait to spawn until completion</label>'
        f'<button class="button">save</button></form>'
        f'{delete}'
        f'</details>'
    )


def _renderTemplateAddForm(context: _RecurringContext) -> str:
    return (
        f'<section class="goal add-goal"><form method="post" action="/{context.area}/recurring/add" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<input type="text" name="title" placeholder="new recurring task" required>'
        f'<span class="day-picker">{_renderDayCheckboxes(frozenset())}</span>'
        f'<label>or every <input type="number" name="every_days" min="1" placeholder="&mdash;" '
        f'class="minutes"> days</label>'
        f'<label>deadline after <input type="number" name="deadline_after_days" min="0" '
        f'placeholder="&mdash;" class="minutes"> days</label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" '
        f'placeholder="&mdash;" class="minutes"></label>'
        f'<label>goal {_renderGoalSelect(context.goals, None)}</label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule" checked> auto</label>'
        f'<label class="check"><input type="checkbox" name="wait_for_completion"> '
        f'wait to spawn until completion</label>'
        f'<button class="button">Add</button></form></section>'
    )


def _renderDayCheckboxes(selected: frozenset[int]) -> str:
    return ''.join(
        f'<label class="check"><input type="checkbox" name="day_{name.lower()}"'
        f'{" checked" if index in selected else ""}> {name}</label>'
        for index, name in enumerate(DAY_NAMES)
    )


def _formatDays(days: frozenset[int]) -> str:
    if len(days) == len(DAY_NAMES):
        return 'every day'

    return ', '.join(DAY_NAMES[number] for number in sorted(days))


def _renderGoalSelect(goals: Mapping[GoalId, Goal], selected: GoalId | None) -> str:
    open_goals = (goal for goal in sorted(goals.values(), key=graph.computeGoalSortKey) if goal.completed is None)
    options = page_shell.renderOptions((('', '(no goal)'), *((goal.id, goal.title) for goal in open_goals)), selected)
    return f'<select name="goal">{options}</select>'


### vocabulary #########################################################################


@dataclass(frozen=True)
class _RecurringContext:
    area: Area
    here: str  # this page's URL, the return target of every form on it
    goals: Mapping[GoalId, Goal]
    today: date
