"""Render the goal and task tree: infinite drilldown, inline editing, dependency chips.

Rendering walks the tree with an explicit stack, so a decomposition deeper than the
Python recursion limit still renders; a parent cycle from a hand edit becomes a visible
marker instead of a hang. ?root=<task id> narrows the page to one subtree, which is the
drill-down view; ?focus=<task id> opens the chain down to one task and highlights it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time

from loom import graph
from loom.config import CONFIG
from loom.scheduler import standardADHDOrdering
from loom.vocabulary import Area, ChildIndex, Goal, GoalId, Priority, Status, Task, TaskId, TaskOrder
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


_PRIORITY_CHOICES = tuple(Priority)
_LANE_DIVIDER = '<hr class="lane-divider">'


def renderTasksPage(
    area: Area,
    *,
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
    stale: frozenset[TaskId],
    root: TaskId | None,
    focus: TaskId | None,
    order: TaskOrder,
    at_risk: frozenset[TaskId],
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
        id_prefix='',
        crumbs={},
        order=order,
        at_risk=at_risk,
    )

    if root is not None and root in tasks:
        body = _renderSubtreeView(context, tasks[root])
    else:
        body = _renderGoalSections(context, goals)

    datalist = _renderTaskDatalist(tasks)
    return renderPage('tasks', area, View.TASKS, body + datalist)


def _renderGoalSections(context: _PageContext, goals: Mapping[GoalId, Goal]) -> str:
    """The auto group, then every goal section behind one folded line, then finished goals.

    The goal trees hold everything, the auto tasks included, and fold away behind one
    'N goals' line together with the add-goal form. A focus link unfolds that line so it
    never lands on something invisible, and an area with no goals yet starts it unfolded
    so the add-goal form shows.
    """
    starving = {goal.id for goal in graph.starvingGoals(goals, context.tasks)}
    roots = context.children.get(None, ())
    ordered = sorted(goals.values(), key=graph.goalSortKey)
    active = tuple(goal for goal in ordered if goal.completed is None)

    sections = []
    for goal in active:
        hint = ' <span class="starving">out of open tasks &mdash; decompose?</span>' if goal.id in starving else ''
        goal_roots = tuple(task for task in roots if task.goal == goal.id)
        sections.append(
            f'<details class="goal" data-fold="goal-{goal.id}" open>{_renderGoalHead(context, goal, hint)}'
            f'{_renderRootsAroundAddLine(context, goal_roots, goal.id)}</details>',
        )

    loose = tuple(task for task in roots if task.goal is None or task.goal not in goals)
    if loose:
        sections.append(
            f'<details class="goal" data-fold="goal-loose" open>'
            f'<summary class="goal-head"><h2>No goal</h2></summary>'
            f'{_renderRootsAroundAddLine(context, loose, None)}</details>',
        )

    add_goal = (
        f'<section class="goal add-goal"><form method="post" action="/{context.area}/goals/add" '
        f'class="add-form"><input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<input type="text" name="title" placeholder="new goal" required>'
        f'<button class="button">Add goal</button></form></section>'
    )
    focused = context.focus is not None and context.focus in context.tasks
    goals_fold = (
        f'<details class="all-goals" data-fold="goals"{" open" if focused or not sections else ""}>'
        f'<summary>{len(sections)} goal{"s" if len(sections) != 1 else ""}</summary>'
        f'{"".join(sections)}{add_goal}</details>'
    )

    finished = tuple(goal for goal in ordered if goal.completed is not None)
    return '\n'.join(
        (
            _renderAutoGroup(context, goals, _topmostAutoTasks(context, goals, active)),
            goals_fold,
            _renderFinishedGoals(context, finished, roots),
            _renderAreaEnd(context),
        ),
    )


def _renderAreaEnd(context: _PageContext) -> str:
    """The last line of the goal list: finish this area, or reopen it once finished."""
    if CONFIG.areas[context.area].finished is None:
        action, label = 'finish', 'finish area'
        hint = f'take {context.area} off the switcher and stop planning it; its data stays, and + reopens it'
    else:
        action, label = 'reopen', 'reopen area'
        hint = f'put {context.area} back on the switcher and plan it again'

    return (
        f'<section class="area-end"><form method="post" action="/{context.area}/area/{action}" class="inline-form">'
        f'<button class="button small ghost" title="{esc(hint)}">{label}</button></form></section>'
    )


def _renderAutoGroup(context: _PageContext, goals: Mapping[GoalId, Goal], auto_tasks: tuple[Task, ...]) -> str:
    """Every open auto-scheduled task, each row labelled with its goal and ancestors.

    Rows list in goal order with not-yet-doable tasks at the bottom, or under
    shortest-first in the planner's own buckets (standardADHDOrdering), split by plain
    lines. These rows
    duplicate rows in the goal trees, so they render with prefixed element ids and fold
    keys; a toggle here never reaches the goal-tree copy.
    """
    if not auto_tasks:
        return ''

    now = datetime.now(UTC)
    match context.order:
        case TaskOrder.DEADLINE:
            lanes: tuple[tuple[Task, ...], ...] = (tuple(sorted(auto_tasks, key=lambda task: graph.notYet(task, now))),)
        case TaskOrder.SHORTEST:
            buckets = standardADHDOrdering(auto_tasks, context.at_risk, now)
            lanes = (buckets.at_risk, buckets.quickest, buckets.not_yet, buckets.unestimated)

    crumbs = {}
    for task in auto_tasks:
        goal_id = graph.effectiveGoal(task, context.tasks)
        path = [goals[goal_id].title] if goal_id is not None and goal_id in goals else []
        path.extend(
            _titleOf(ancestor, context.tasks) for ancestor in reversed(graph.ancestorsOf(task.id, context.tasks))
        )
        if path:
            crumbs[task.id] = f'<span class="crumb">{" &rsaquo; ".join(esc(part) for part in path)}</span>'

    lit = ' on' if context.order is TaskOrder.SHORTEST else ''
    toggle = (
        f'<form method="post" action="/{context.area}/tasks/order" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<button class="order-toggle{lit}" title="list and schedule the quickest tasks first">'
        f'shortest first</button></form>'
    )
    auto_context = replace(context, id_prefix='auto-', crumbs=crumbs)
    return (
        f'<details class="goal auto-group" data-fold="auto" open>'
        f'<summary class="goal-head"><h2>Auto</h2>{toggle}</summary>'
        f'{_LANE_DIVIDER.join(_renderTree(auto_context, lane) for lane in lanes if lane)}</details>'
    )


def _topmostAutoTasks(
    context: _PageContext,
    goals: Mapping[GoalId, Goal],
    active: tuple[Goal, ...],
) -> tuple[Task, ...]:
    """Open auto tasks in goal order then tree order, minus any under an open auto ancestor.

    A flagged task below a flagged ancestor already shows inside that ancestor's subtree.
    Roots of active goals come first, then goal-less roots, then roots of finished goals,
    whose flagged tasks the scheduler still places.
    """
    rank = {goal.id: position for position, goal in enumerate(active)}
    ordered_roots = sorted(context.children.get(None, ()), key=lambda root: _rootRank(root, rank, goals))
    stack = list(reversed(ordered_roots))
    found = []
    seen: set[TaskId] = set()
    while stack:
        task = stack.pop()
        if task.id in seen:
            continue
        seen.add(task.id)
        if task.auto_schedule and graph.stillOpen(task):
            found.append(task)
            continue
        stack.extend(reversed(context.children.get(task.id, ())))

    return tuple(found)


def _rootRank(root: Task, rank: Mapping[GoalId, int], goals: Mapping[GoalId, Goal]) -> int:
    """Active goals by position, then goal-less roots, then roots of finished goals."""
    if root.goal is not None and root.goal in rank:
        return rank[root.goal]
    if root.goal is None or root.goal not in goals:
        return len(rank)
    return len(rank) + 1


def _renderRootsAroundAddLine(context: _PageContext, roots: tuple[Task, ...], goal: GoalId | None) -> str:
    """Open roots, then the always-visible add line, then finished roots behind one line."""
    open_roots = tuple(task for task in roots if graph.stillOpen(task))
    done_roots = tuple(task for task in roots if not graph.stillOpen(task))

    finished = ''
    if done_roots:
        wrapper_open = any(root.id in context.open_ids for root in done_roots)
        finished = (
            f'<details class="finished-tasks" data-fold="fin-{goal or "loose"}"{" open" if wrapper_open else ""}>'
            f'<summary>{len(done_roots)} finished</summary>'
            f'{_renderTree(context, done_roots)}</details>'
        )

    return f'{_renderTree(context, open_roots)}{_renderAddForm(context, parent=None, goal=goal)}{finished}'


def _renderGoalHead(context: _PageContext, goal: Goal, hint: str) -> str:
    """A goal's summary row plus its hidden edit panel: title, hint, edit, finish, reorder."""
    return_to = esc(context.here)
    return (
        f'<summary class="goal-head"><h2>{esc(goal.title)}{hint}</h2>'
        f'<label class="row-toggle" for="goal-edit-{goal.id}">edit</label>'
        f'<form method="post" action="/{context.area}/goals/{goal.id}/finish" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<button class="button small" title="mark this goal finished">finish</button></form>'
        f'<span class="reorder">{_renderMoveButton(context, goal, "up")}{_renderMoveButton(context, goal, "down")}'
        f'</span></summary>'
        f'<input type="checkbox" id="goal-edit-{goal.id}" class="panel-state goal-edit-state">'
        f'<div class="panel edit-panel">'
        f'<form method="post" action="/{context.area}/goals/{goal.id}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<label>title <input type="text" name="title" required value="{esc(goal.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{esc(goal.notes)}"></label>'
        f'<button class="button">save</button></form></div>'
    )


def _renderMoveButton(context: _PageContext, goal: Goal, direction: str) -> str:
    arrow = '&#9650;' if direction == 'up' else '&#9660;'
    return (
        f'<form method="post" action="/{context.area}/goals/{goal.id}/move" class="inline-form">'
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
            f'<form method="post" action="/{context.area}/goals/{goal.id}/reopen" class="inline-form">'
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
    crumbs = [f'<a href="/{context.area}/tasks">all</a>']
    crumbs.extend(
        f'<a href="/{context.area}/tasks?root={ancestor}">{esc(_titleOf(ancestor, context.tasks))}</a>'
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
            prefix = context.id_prefix
            notes = f'<p class="notes">{esc(task.notes)}</p>' if task.notes else ''
            parts.append(
                f'<details class="task" id="{prefix}task-{task.id}" data-task="{prefix}{task.id}"{done_attr}'
                f'{" open" if is_open else ""}>'
                f'<summary>{_renderSummary(context, task)}</summary>'
                f'<input type="checkbox" id="{prefix}edit-state-{task.id}" class="panel-state edit-state">'
                f'<input type="checkbox" id="{prefix}add-state-{task.id}" class="panel-state add-state">'
                f'{_renderEditPanel(context, task)}'
                f'<div class="children">{notes}',
            )
            child_group = context.children.get(task.id, ())
            open_children = tuple(child for child in child_group if graph.stillOpen(child))
            done_children = tuple(child for child in child_group if not graph.stillOpen(child))

            stack.append(('html', f'</div>{_renderChildAdder(context, task)}</details>'))
            stack.append(('exit', str(task.id)))
            if done_children:
                # finished siblings hide behind one collapsed line at the bottom
                wrapper_open = any(child.id in context.open_ids for child in done_children)
                stack.append(('html', '</details>'))
                stack.extend(('task', child) for child in reversed(done_children))
                stack.append(
                    (
                        'html',
                        f'<details class="finished-tasks" data-fold="{prefix}fin-{task.id}"'
                        f'{" open" if wrapper_open else ""}>'
                        f'<summary>{len(done_children)} finished</summary>',
                    ),
                )
            stack.extend(('task', child) for child in reversed(open_children))

    return ''.join(parts)


def _renderSummary(context: _PageContext, task: Task) -> str:
    """The one-line summary row: caret, tick, title, and state badges.

    The caret marks a task with anything folded inside (subtasks or notes); a collapsed
    parent also shows how many open steps it hides. Leaves keep an empty caret slot so
    sibling rows line up.
    """
    child_group = context.children.get(task.id, ())
    open_count = sum(1 for child in child_group if graph.stillOpen(child))
    caret = f'<span class="twist">{"&#9656;" if child_group or task.notes else ""}</span>'
    sub_count = f'<span class="sub-count">{open_count}</span>' if open_count else ''

    tick = ''
    if graph.stillOpen(task):
        tick = (
            f'<form method="post" action="/{context.area}/tasks/{task.id}/tick" class="tick-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
            f'<button class="tick" title="mark done">&check;</button></form>'
        )
    elif task.status is Status.DONE:
        tick = (
            f'<form method="post" action="/{context.area}/tasks/{task.id}/reopen" class="tick-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
            f'<button class="tick undo" title="mark not done">&check;</button></form>'
        )

    badges = []
    if task.estimate_minutes is not None and not graph.stillOpen(task):
        badges.append(f'<span class="badge">{task.estimate_minutes}m</span>')
    if task.not_before is not None and graph.stillOpen(task) and task.not_before > datetime.now(UTC):
        badges.append(f'<span class="badge not-before">from {_localMoment(task.not_before)}</span>')
    if task.deadline is not None:
        overdue = ' overdue' if task.deadline < _today() else ''
        badges.append(f'<span class="badge deadline{overdue}">due {task.deadline.isoformat()}</span>')
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

    row_fields = ''
    if graph.stillOpen(task):
        row_fields = f'{_renderEstimateField(context, task)}{_renderAutoToggle(context, task)}'
    prefix = context.id_prefix
    tools = (
        f'{row_fields}'
        f'<label class="row-toggle" for="{prefix}edit-state-{task.id}">edit</label>'
        f'<label class="row-toggle" for="{prefix}add-state-{task.id}" title="add a step under this">+</label>'
    )
    drill = f'<a class="drill" href="/{context.area}/tasks?root={task.id}" title="drill in">&#8618;</a>'
    return (
        f'{caret}{tick}<span class="{title_class}">{esc(task.title)}</span>{context.crumbs.get(task.id, "")}'
        f'{sub_count}{"".join(badges)}'
        f'{_renderChips(context, task)}{tools}{drill}'
    )


def _renderEstimateField(context: _PageContext, task: Task) -> str:
    """The inline minutes field: type a number and press Enter; an empty value clears it."""
    value = task.estimate_minutes if task.estimate_minutes is not None else ''
    return (
        f'<form method="post" action="/{context.area}/tasks/{task.id}/estimate" class="inline-form est-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<input type="number" name="estimate_minutes" min="1" value="{value}" placeholder="min" '
        f'title="estimated minutes; Enter saves">'
        f'<span>m</span></form>'
    )


def _renderAutoToggle(context: _PageContext, task: Task) -> str:
    """The auto-scheduling pill: lit when on, one click flips it."""
    state = ' on' if task.auto_schedule else ''
    return (
        f'<form method="post" action="/{context.area}/tasks/{task.id}/auto" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<button class="auto-toggle{state}" title="toggle auto-scheduling">auto</button></form>'
    )


def _renderChips(context: _PageContext, task: Task) -> str:
    """Dependency chips, inline in the row: what this task needs, and what it blocks."""
    chips: list[str] = []
    chips.extend(
        f'<span class="chip">&#9940; needs '
        f'<a href="/{context.area}/tasks?focus={dependency}">{esc(_titleOf(dependency, context.tasks))}</a>'
        f'<form method="post" action="/{context.area}/tasks/{task.id}/deps/remove" class="inline-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
        f'<input type="hidden" name="blocked_by" value="{dependency}">'
        f'<button class="chip-x" title="remove dependency">&times;</button></form></span>'
        for dependency in task.blocked_by
    )
    chips.extend(
        f'<span class="chip">&rarr; blocks <a href="/{context.area}/tasks?focus={downstream}">'
        f'{esc(_titleOf(downstream, context.tasks))}</a></span>'
        for downstream in context.blocks_forward.get(task.id, ())
    )

    return ''.join(chips)


def _renderChildAdder(context: _PageContext, task: Task) -> str:
    """The add-a-step line the row's + reveals; it sits below the task's children."""
    return f'<div class="panel child-adder">{_renderAddForm(context, parent=task.id, goal=None)}</div>'


def _renderEditPanel(context: _PageContext, task: Task) -> str:
    """The collapsed edit form: fields, dependency add, and leaf-only delete."""
    action = f'/{context.area}/tasks/{task.id}/edit'
    return_to = f'{context.here}'
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
    not_before_day = not_before_time = ''
    if task.not_before is not None:
        local = task.not_before.astimezone(CONFIG.timezone)
        not_before_day = local.date().isoformat()
        not_before_time = local.strftime('%H:%M') if local.time() != time(0) else ''
    checked = ' checked' if task.auto_schedule else ''

    delete = ''
    if not context.children.get(task.id):
        delete = (
            f'<form method="post" action="/{context.area}/tasks/{task.id}/delete" class="inline-form">'
            f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
            f'<button class="button danger">delete</button></form>'
        )

    return (
        f'<div class="panel edit-panel">'
        f'<form method="post" action="{action}" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
        f'<label>title <input type="text" name="title" required value="{esc(task.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{esc(task.notes)}"></label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" value="{estimate}"></label>'
        f'<label>deadline <input type="date" name="deadline" value="{deadline}"></label>'
        f'<label>not before <input type="date" name="not_before_day" value="{not_before_day}"> '
        f'<input type="time" name="not_before_time" value="{not_before_time}"></label>'
        f'<label>status <select name="status">{status_options}</select></label>'
        f'<label>priority <select name="priority">{priority_options}</select></label>'
        f'<label>color {_colorSelect(task)}</label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="/{context.area}/tasks/{task.id}/deps/add" class="dep-form">'
        f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
        f'<input list="all-tasks" name="blocked_by" placeholder="blocked by (pick a task)">'
        f'<button class="button">add dependency</button></form>'
        f'{delete}</div>'
    )


def _renderAddForm(context: _PageContext, parent: TaskId | None, goal: GoalId | None) -> str:
    parent_field = f'<input type="hidden" name="parent" value="{parent}">' if parent is not None else ''
    goal_field = f'<input type="hidden" name="goal" value="{goal}">' if goal is not None else ''
    return (
        f'<form method="post" action="/{context.area}/tasks/add" class="add-form">'
        f'<input type="hidden" name="return_to" value="{esc(context.here)}">'
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

    return f'/{area}/tasks{query}'


def _titleOf(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> str:
    task = tasks.get(task_id)
    return task.title if task is not None else f'missing task {task_id}'


def _localMoment(moment: datetime) -> str:
    """'2026-09-28 09:00' in local time; a bare date when it falls at midnight."""
    local = moment.astimezone(CONFIG.timezone)
    if local.time() == time(0):
        return local.date().isoformat()

    return local.strftime('%Y-%m-%d %H:%M')


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
    id_prefix: str  # '' in the goal trees; the auto group's duplicate rows need unique ids
    crumbs: Mapping[TaskId, str]  # rendered goal-and-ancestor path shown after a row's title
    order: TaskOrder  # the area's chosen order for the Auto group and the scheduler
    at_risk: frozenset[TaskId]  # from the plan: deadlined tasks that jumped the shortest-first queue
