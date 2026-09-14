"""Render the goal and task tree: infinite drilldown, inline editing, dependency chips.

Rendering walks the tree with an explicit stack, so a decomposition deeper than the
Python recursion limit still renders; a parent cycle from a hand edit becomes a visible
marker instead of a hang. ?root=<task id> narrows the page to one subtree, which is the
drill-down view; ?focus=<task id> opens the chain down to one task and highlights it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from loom import graph
from loom.config import CONFIG
from loom.vocabulary import Area, ChildIndex, Goal, GoalId, Priority, Status, Task, TaskId
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


_PRIORITY_CHOICES = tuple(Priority)


def renderTasksPage(
    area: Area,
    *,
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
    stale: frozenset[TaskId],
    root: TaskId | None,
    focus: TaskId | None,
) -> str:
    """The whole tree page: goal sections, or one subtree when `root` is set."""
    context = _PageContext(
        area=area,
        tasks=tasks,
        children=graph.childrenByParent(tasks),
        blocked=graph.dependencyBlocked(tasks),
        blocks_forward=_reverseDependencies(tasks),
        stale=stale,
        focus=focus,
        open_ids=_openIds(tasks, focus),
        here=_pagePath(area, root, focus),
    )

    if root is not None and root in tasks:
        body = _renderSubtreeView(context, tasks[root])
    else:
        body = _renderGoalSections(context, goals)

    datalist = _renderTaskDatalist(tasks)
    return renderPage('tasks', area, View.TASKS, body + datalist)


def _renderGoalSections(context: _PageContext, goals: Mapping[GoalId, Goal]) -> str:
    """Active goals with their root tasks, rootless tasks, finished goals, the add form."""
    starving = {goal.id for goal in graph.starvingGoals(goals, context.tasks)}
    roots = context.children.get(None, ())
    ordered = sorted(goals.values(), key=graph.goalSortKey)

    sections = []
    for goal in (goal for goal in ordered if goal.completed is None):
        hint = ' <span class="starving">out of open tasks &mdash; decompose?</span>' if goal.id in starving else ''
        goal_roots = tuple(task for task in roots if task.goal == goal.id)
        sections.append(
            f'<section class="goal">{_renderGoalHead(context, goal, hint)}'
            f'{_renderRootsAroundAddLine(context, goal_roots, goal.id)}</section>',
        )

    loose = tuple(task for task in roots if task.goal is None or task.goal not in goals)
    if loose:
        sections.append(
            f'<section class="goal"><h2>No goal</h2>{_renderRootsAroundAddLine(context, loose, None)}</section>',
        )

    add_goal = (
        f'<section class="goal add-goal"><form method="post" action="/{context.area.value}/goals/add" '
        f'class="add-form"><input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<input type="text" name="title" placeholder="new goal" required>'
        f'<button class="button">Add goal</button></form></section>'
    )
    finished = tuple(goal for goal in ordered if goal.completed is not None)
    return '\n'.join((*sections, add_goal, _renderFinishedGoals(context, finished, roots)))


def _renderRootsAroundAddLine(context: _PageContext, roots: tuple[Task, ...], goal: GoalId | None) -> str:
    """Open roots, then the always-visible add line, then finished roots below it."""
    open_roots = tuple(task for task in roots if graph.stillOpen(task))
    done_roots = tuple(task for task in roots if not graph.stillOpen(task))
    return (
        f'{_renderTree(context, open_roots)}'
        f'{_renderAddForm(context, parent=None, goal=goal)}'
        f'{_renderTree(context, done_roots)}'
    )


def _renderGoalHead(context: _PageContext, goal: Goal, hint: str) -> str:
    """A goal's heading row: title, starving hint, finish button, collapsed edit form."""
    return_to = esc(context.here)
    return (
        f'<div class="goal-head"><h2>{esc(goal.title)}{hint}</h2>'
        f'<details class="edit"><summary>edit</summary>'
        f'<form method="post" action="/{context.area.value}/goals/{goal.id}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<label>title <input type="text" name="title" required value="{esc(goal.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{esc(goal.notes)}"></label>'
        f'<button class="button">save</button></form></details>'
        f'<form method="post" action="/{context.area.value}/goals/{goal.id}/finish" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<button class="button small" title="mark this goal finished">finish</button></form>'
        f'<span class="reorder">{_renderMoveButton(context, goal, "up")}{_renderMoveButton(context, goal, "down")}'
        f'</span></div>'
    )


def _renderMoveButton(context: _PageContext, goal: Goal, direction: str) -> str:
    arrow = '&#9650;' if direction == 'up' else '&#9660;'
    return (
        f'<form method="post" action="/{context.area.value}/goals/{goal.id}/move" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<input type="hidden" name="direction" value="{direction}">'
        f'<button class="button small ghost" title="move {direction}">{arrow}</button></form>'
    )


def _renderFinishedGoals(context: _PageContext, finished: tuple[Goal, ...], roots: tuple[Task, ...]) -> str:
    """The collapsed list of finished goals, each with a reopen button and its tasks."""
    if not finished:
        return ''

    parts = []
    for goal in finished:
        finished_on = goal.completed.astimezone(CONFIG.timezone).date().isoformat() if goal.completed else ''
        goal_roots = tuple(task for task in roots if task.goal == goal.id)
        parts.append(
            f'<div class="goal-head"><h2 class="done">{esc(goal.title)}</h2>'
            f'<span class="badge">finished {finished_on}</span>'
            f'<form method="post" action="/{context.area.value}/goals/{goal.id}/reopen" class="inline-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
            f'<button class="button small">reopen</button></form></div>'
            f'{_renderTree(context, goal_roots) if goal_roots else ""}',
        )

    inner = '\n'.join(parts)
    return (
        f'<details class="goal finished-goals"><summary>{len(finished)} finished '
        f'goal{"s" if len(finished) != 1 else ""}</summary>{inner}</details>'
    )


def _renderSubtreeView(context: _PageContext, root_task: Task) -> str:
    crumbs = [f'<a href="/{context.area.value}/tasks">all</a>']
    crumbs.extend(
        f'<a href="/{context.area.value}/tasks?root={ancestor}">{esc(_titleOf(ancestor, context.tasks))}</a>'
        for ancestor in reversed(graph.ancestorsOf(root_task.id, context.tasks))
    )
    crumbs.append(esc(root_task.title))

    return (
        f'<nav class="crumbs">{" &rsaquo; ".join(crumbs)}</nav>'
        f'<section class="goal">{_renderTree(context, (root_task,))}</section>'
    )


def _renderTree(context: _PageContext, roots: tuple[Task, ...]) -> str:
    """Emit nested <details> with an explicit stack; a repeated ancestor renders as a cycle marker."""
    parts: list[str] = []
    stack: list[tuple[str, Task | str]] = [('task', task) for task in reversed(roots)]
    path: set[TaskId] = set()

    while stack:
        kind, payload = stack.pop()
        if kind == 'html' and isinstance(payload, str):
            parts.append(payload)
        elif kind == 'exit' and isinstance(payload, str):
            path.discard(TaskId(payload))
        elif kind == 'task' and isinstance(payload, Task):
            task = payload
            if task.id in path:
                parts.append(f'<div class="cycle">&#9888; cycle: {esc(task.title)} is its own ancestor</div>')
                continue
            path.add(task.id)
            # a finished task collapses even at root level; the focus chain still opens it
            is_open = task.id in context.open_ids or (task.parent is None and graph.stillOpen(task))
            done_attr = '' if graph.stillOpen(task) else ' data-done="1"'
            notes = f'<p class="notes">{esc(task.notes)}</p>' if task.notes else ''
            parts.append(
                f'<details class="task" id="task-{task.id}" data-task="{task.id}"{done_attr}'
                f'{" open" if is_open else ""}>'
                f'<summary>{_renderSummary(context, task)}</summary><div class="children">{notes}',
            )
            stack.append(('html', '</div></details>'))
            stack.append(('exit', str(task.id)))
            stack.extend(('task', child) for child in reversed(context.children.get(task.id, ())))

    return ''.join(parts)


def _renderSummary(context: _PageContext, task: Task) -> str:
    """The one-line summary row: tick, title, and state badges."""
    tick = ''
    if graph.stillOpen(task):
        tick = (
            f'<form method="post" action="/{context.area.value}/tasks/{task.id}/tick" class="tick-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}#task-{task.id}">'
            f'<button class="tick" title="mark done">&check;</button></form>'
        )
    elif task.status is Status.DONE:
        tick = '<span class="done-mark" title="done">&check;</span>'

    badges = []
    if task.estimate_minutes is not None:
        badges.append(f'<span class="badge">{task.estimate_minutes}m</span>')
    if task.deadline is not None:
        overdue = ' overdue' if task.deadline < _today() else ''
        badges.append(f'<span class="badge deadline{overdue}">due {task.deadline.isoformat()}</span>')
    if task.auto_schedule:
        badges.append('<span class="badge auto">auto</span>')
    if task.status is Status.BLOCKED:
        badges.append('<span class="badge waiting">waiting</span>')
    if task.status is Status.IN_PROGRESS:
        badges.append('<span class="badge doing">doing</span>')
    if task.id in context.blocked:
        badges.append('<span class="badge dep-blocked">dep-blocked</span>')
    if task.id in context.stale:
        badges.append('<span class="badge stale-badge">stale</span>')

    title_class = 'title'
    if task.status in (Status.DONE, Status.DROPPED):
        title_class = 'title done'
    if task.id == context.focus:
        title_class += ' focused'

    tools = f'{_renderEditPanel(context, task)}{_renderAdder(context, task)}'
    drill = f'<a class="drill" href="/{context.area.value}/tasks?root={task.id}" title="drill in">&#8618;</a>'
    return (
        f'{tick}<span class="{title_class}">{esc(task.title)}</span>{"".join(badges)}'
        f'{_renderChips(context, task)}{tools}{drill}'
    )


def _renderChips(context: _PageContext, task: Task) -> str:
    """Dependency chips, inline in the row: what this task needs, and what it blocks."""
    chips: list[str] = []
    chips.extend(
        f'<span class="chip">&#9940; needs '
        f'<a href="/{context.area.value}/tasks?focus={dependency}">{esc(_titleOf(dependency, context.tasks))}</a>'
        f'<form method="post" action="/{context.area.value}/tasks/{task.id}/deps/remove" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}#task-{task.id}">'
        f'<input type="hidden" name="blocked_by" value="{dependency}">'
        f'<button class="chip-x" title="remove dependency">&times;</button></form></span>'
        for dependency in task.blocked_by
    )
    chips.extend(
        f'<span class="chip">&rarr; blocks <a href="/{context.area.value}/tasks?focus={downstream}">'
        f'{esc(_titleOf(downstream, context.tasks))}</a></span>'
        for downstream in context.blocks_forward.get(task.id, ())
    )

    return ''.join(chips)


def _renderAdder(context: _PageContext, task: Task) -> str:
    """The + toggle in the row; opening it reveals the add-a-step line."""
    return (
        f'<details class="adder inline-panel"><summary title="add a step under this">+</summary>'
        f'<div class="panel">{_renderAddForm(context, parent=task.id, goal=None)}</div></details>'
    )


def _renderEditPanel(context: _PageContext, task: Task) -> str:
    """The collapsed edit form: fields, dependency add, and leaf-only delete."""
    action = f'/{context.area.value}/tasks/{task.id}/edit'
    return_to = f'{context.here}#task-{task.id}'
    status_options = ''.join(
        f'<option value="{status.value}"{" selected" if status is task.status else ""}>{status.value}</option>'
        for status in Status
    )
    priority_options = ''.join(
        f'<option value="{priority.value}"{" selected" if priority is task.priority else ""}>{priority.value}</option>'
        for priority in _PRIORITY_CHOICES
    )
    estimate = task.estimate_minutes if task.estimate_minutes is not None else ''
    deadline = task.deadline.isoformat() if task.deadline is not None else ''
    checked = ' checked' if task.auto_schedule else ''

    delete = ''
    if not context.children.get(task.id):
        delete = (
            f'<form method="post" action="/{context.area.value}/tasks/{task.id}/delete" class="inline-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
            f'<button class="button danger">delete</button></form>'
        )

    return (
        f'<details class="edit inline-panel"><summary>edit</summary><div class="panel">'
        f'<form method="post" action="{action}" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
        f'<label>title <input type="text" name="title" required value="{esc(task.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{esc(task.notes)}"></label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" value="{estimate}"></label>'
        f'<label>deadline <input type="date" name="deadline" value="{deadline}"></label>'
        f'<label>status <select name="status">{status_options}</select></label>'
        f'<label>priority <select name="priority">{priority_options}</select></label>'
        f'<label>color {_colorSelect(task)}</label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="/{context.area.value}/tasks/{task.id}/deps/add" class="dep-form">'
        f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
        f'<input list="all-tasks" name="blocked_by" placeholder="blocked by (pick a task)">'
        f'<button class="button">add dependency</button></form>'
        f'{delete}</div></details>'
    )


def _renderAddForm(context: _PageContext, parent: TaskId | None, goal: GoalId | None) -> str:
    anchor = f'#task-{parent}' if parent is not None else ''
    parent_field = f'<input type="hidden" name="parent" value="{parent}">' if parent is not None else ''
    goal_field = f'<input type="hidden" name="goal" value="{goal}">' if goal is not None else ''
    return (
        f'<form method="post" action="/{context.area.value}/tasks/add" class="add-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}{anchor}">'
        f'{parent_field}{goal_field}'
        f'<input type="text" name="title" placeholder="add a step" required>'
        f'<input type="number" name="estimate_minutes" min="1" placeholder="min" class="minutes">'
        f'<label class="check"><input type="checkbox" name="auto_schedule"> auto</label>'
        f'<button class="button">Add</button></form>'
    )


def _colorSelect(task: Task) -> str:
    """Palette preset choices; a name no longer in the palette stays selectable so a save keeps it."""
    names = list(CONFIG.palette)
    if task.color is not None and task.color not in CONFIG.palette:
        names.append(task.color)

    options = ['<option value="">(auto)</option>']
    options.extend(
        f'<option value="{esc(name)}"{" selected" if name == task.color else ""}>{esc(name)}</option>' for name in names
    )
    return f'<select name="color">{"".join(options)}</select>'


def _renderTaskDatalist(tasks: Mapping[TaskId, Task]) -> str:
    options = ''.join(
        f'<option value="{task.id}" label="{esc(task.title)}"></option>'
        for task in sorted(tasks.values(), key=lambda task: task.title.lower())
    )
    return f'<datalist id="all-tasks">{options}</datalist>'


def _reverseDependencies(tasks: Mapping[TaskId, Task]) -> dict[TaskId, tuple[TaskId, ...]]:
    forward: dict[TaskId, list[TaskId]] = {}
    for task in tasks.values():
        for dependency in task.blocked_by:
            forward.setdefault(dependency, []).append(task.id)

    return {task_id: tuple(sorted(downstream)) for task_id, downstream in forward.items()}


def _openIds(tasks: Mapping[TaskId, Task], focus: TaskId | None) -> frozenset[TaskId]:
    if focus is None:
        return frozenset()

    return frozenset((focus, *graph.ancestorsOf(focus, tasks)))


def _pagePath(area: Area, root: TaskId | None, focus: TaskId | None) -> str:
    query = ''
    if root is not None:
        query = f'?root={root}'
    elif focus is not None:
        query = f'?focus={focus}'

    return f'/{area.value}/tasks{query}'


def _titleOf(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> str:
    task = tasks.get(task_id)
    return task.title if task is not None else f'missing task {task_id}'


def _today() -> date:
    return datetime.now(UTC).astimezone(CONFIG.timezone).date()


### vocabulary #########################################################################


@dataclass(frozen=True)
class _PageContext:
    area: Area
    tasks: Mapping[TaskId, Task]
    children: ChildIndex
    blocked: frozenset[TaskId]
    blocks_forward: dict[TaskId, tuple[TaskId, ...]]
    stale: frozenset[TaskId]
    focus: TaskId | None
    open_ids: frozenset[TaskId]
    here: str
