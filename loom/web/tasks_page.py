"""Render the goal and task tree: infinite drilldown, inline editing, dependency chips.

The tree walk uses an explicit stack, so a decomposition deeper than the Python recursion
limit still renders. A parent cycle from a hand edit renders as a visible marker.
?root=<task id> narrows the page to one subtree, the drill-down view. ?focus=<task id>
opens the chain down to one task and highlights it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time

from loom import config, graph, scheduler, triage
from loom.config import CONFIG
from loom.vocabulary import Area, ChildIndex, Goal, GoalId, Priority, Status, Task, TaskId, TaskOrder
from loom.web import page_shell
from loom.web.web_vocabulary import MoveDirection, View


def renderTasksPage(
    area: Area,
    *,
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
    root: TaskId | None,
    focus: TaskId | None,
    order: TaskOrder,
    at_risk: frozenset[TaskId],
) -> str:
    """Render the tree page: goal sections, or one subtree when `root` is set."""
    now = datetime.now(UTC)
    context = _PageContext(
        area=area,
        tasks=tasks,
        children=graph.groupChildrenByParent(tasks),
        blocked=graph.collectDependencyBlockedIds(tasks),
        blocks_forward=_groupDependentIds(tasks),
        stale=triage.collectOverdueTaskIds(tasks, now),
        focus=focus,
        open_ids=_collectFocusChainIds(tasks, focus),
        here=_buildPagePath(area, root, focus),
        id_prefix='',
        crumbs={},
        order=order,
        at_risk=at_risk,
        now=now,
        today=config.computeLocalDate(now),
    )

    if root is not None and root in tasks:
        body = _renderSubtreeView(context, tasks[root])
    else:
        body = _renderGoalSections(context, goals)

    datalist = _renderTaskDatalist(tasks)
    return page_shell.renderPage('tasks', area, View.TASKS, body + datalist)


def _renderSubtreeView(context: _PageContext, root_task: Task) -> str:
    crumbs = [f'<a href="/{context.area}/tasks">all</a>']
    crumbs.extend(
        f'<a href="/{context.area}/tasks?root={ancestor}">'
        f'{page_shell.escapeHtml(page_shell.findTaskTitle(ancestor, context.tasks))}</a>'
        for ancestor in reversed(graph.listAncestorIds(root_task.id, context.tasks))
    )
    crumbs.append(page_shell.escapeHtml(root_task.title))

    return (
        f'<nav class="crumbs">{" &rsaquo; ".join(crumbs)}</nav>'
        f'<section class="goal">{_renderTree(context, (root_task,))}</section>'
    )


def _renderGoalSections(context: _PageContext, goals: Mapping[GoalId, Goal]) -> str:
    """Render the auto group, then every goal section behind one folded line, then finished goals.

    The goal trees have every task, the auto tasks included, and fold away behind one
    'N goals' line with the add-goal form. That line is unfolded for a focus link, so the
    focused task is visible, and for an area with no goals yet, so the add-goal form is
    visible.
    """
    starving = {goal.id for goal in graph.listStarvingGoals(goals, context.tasks)}
    roots = context.children.get(None, ())
    ordered = sorted(goals.values(), key=graph.computeGoalSortKey)
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
        f'class="add-form"><input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
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
            _renderAutoGroup(context, goals, _listTopmostAutoTasks(context, goals, active)),
            goals_fold,
            _renderFinishedGoals(context, finished, roots),
            _renderAreaEnd(context),
        ),
    )


def _renderGoalHead(context: _PageContext, goal: Goal, hint: str) -> str:
    """Render a goal's summary row and its hidden edit panel: title, hint, edit, finish, reorder."""
    goal_path = f'/{context.area}/goals/{goal.id}'
    finish = page_shell.renderPostButton(
        f'{goal_path}/finish',
        'finish',
        button_class='button small',
        return_to=context.here,
        title='mark this goal finished',
    )
    return (
        f'<summary class="goal-head"><h2>{page_shell.escapeHtml(goal.title)}{hint}</h2>'
        f'<label class="row-toggle" for="goal-edit-{goal.id}">edit</label>'
        f'{finish}'
        f'<span class="reorder">{_renderMoveButton(context, goal, MoveDirection.UP)}'
        f'{_renderMoveButton(context, goal, MoveDirection.DOWN)}'
        f'</span></summary>'
        f'<input type="checkbox" id="goal-edit-{goal.id}" class="panel-state goal-edit-state">'
        f'<div class="panel edit-panel">'
        f'<form method="post" action="{goal_path}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<label>title <input type="text" name="title" required value="{page_shell.escapeHtml(goal.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{page_shell.escapeHtml(goal.notes)}"></label>'
        f'<button class="button">save</button></form></div>'
    )


def _renderMoveButton(context: _PageContext, goal: Goal, direction: MoveDirection) -> str:
    return page_shell.renderPostButton(
        f'/{context.area}/goals/{goal.id}/move',
        _pickMoveArrow(direction),
        button_class='button small ghost',
        return_to=context.here,
        title=f'move {direction.value}',
        hidden_fields=(('direction', direction.value),),
    )


def _pickMoveArrow(direction: MoveDirection) -> str:
    match direction:
        case MoveDirection.UP:
            return '&#9650;'
        case MoveDirection.DOWN:
            return '&#9660;'


def _renderRootsAroundAddLine(context: _PageContext, roots: tuple[Task, ...], goal: GoalId | None) -> str:
    """Render the open roots, then the always-visible add line, then finished roots behind one line."""
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

    return f'{_renderTree(context, open_roots)}{_renderTaskAddForm(context, parent=None, goal=goal)}{finished}'


def _renderAutoGroup(context: _PageContext, goals: Mapping[GoalId, Goal], auto_tasks: tuple[Task, ...]) -> str:
    """Render every open auto-scheduled task, each row labelled with its goal and ancestors.

    Rows are in goal order with not-yet-doable tasks at the bottom, or, under
    shortest-first, in the buckets of scheduler.sortIntoBuckets, split by plain lines.
    Each row is a copy of a row in the goal trees, so its element ids and fold keys have a
    prefix. A toggle here leaves the goal-tree copy as it is.
    """
    LANE_DIVIDER = '<hr class="lane-divider">'
    if not auto_tasks:
        return ''

    now = context.now
    match context.order:
        case TaskOrder.DEADLINE:
            lanes: tuple[tuple[Task, ...], ...] = (
                tuple(sorted(auto_tasks, key=lambda task: graph.notYetDoable(task, now))),
            )
        case TaskOrder.SHORTEST:
            buckets = scheduler.sortIntoBuckets(auto_tasks, context.at_risk, now)
            lanes = (buckets.at_risk, buckets.quickest, buckets.not_yet, buckets.unestimated)

    crumbs = {}
    for task in auto_tasks:
        goal_id = graph.findEffectiveGoal(task, context.tasks)
        path = [goals[goal_id].title] if goal_id is not None and goal_id in goals else []
        path.extend(
            page_shell.findTaskTitle(ancestor, context.tasks)
            for ancestor in reversed(graph.listAncestorIds(task.id, context.tasks))
        )
        if path:
            crumbs[task.id] = (
                f'<span class="crumb">{" &rsaquo; ".join(page_shell.escapeHtml(part) for part in path)}</span>'
            )

    lit = ' on' if context.order is TaskOrder.SHORTEST else ''
    toggle = page_shell.renderPostButton(
        f'/{context.area}/tasks/order',
        'shortest first',
        button_class=f'order-toggle{lit}',
        return_to=context.here,
        title='list and schedule the quickest tasks first',
    )
    auto_context = replace(context, id_prefix='auto-', crumbs=crumbs)
    return (
        f'<details class="goal auto-group" data-fold="auto" open>'
        f'<summary class="goal-head"><h2>Auto</h2>{toggle}</summary>'
        f'{LANE_DIVIDER.join(_renderTree(auto_context, lane) for lane in lanes if lane)}</details>'
    )


def _listTopmostAutoTasks(
    context: _PageContext,
    goals: Mapping[GoalId, Goal],
    active: tuple[Goal, ...],
) -> tuple[Task, ...]:
    """List the open auto tasks in goal order, then tree order, minus any under an open auto ancestor.

    A flagged task below a flagged ancestor is already in that ancestor's subtree. Roots
    sort by _computeRootRank.
    """
    rank = {goal.id: position for position, goal in enumerate(active)}
    ordered_roots = sorted(context.children.get(None, ()), key=lambda root: _computeRootRank(root, rank, goals))
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


def _computeRootRank(root: Task, rank: Mapping[GoalId, int], goals: Mapping[GoalId, Goal]) -> int:
    """Rank a root: active goals by position, then goal-less roots, then roots of finished goals."""
    if root.goal is not None and root.goal in rank:
        return rank[root.goal]
    if root.goal is None or root.goal not in goals:
        return len(rank)
    return len(rank) + 1


def _renderFinishedGoals(context: _PageContext, finished: tuple[Goal, ...], roots: tuple[Task, ...]) -> str:
    """Render the collapsed list of finished goals, each with a reopen button and its tasks."""
    if not finished:
        return ''

    parts = []
    for goal in finished:
        finished_on = config.computeLocalDate(goal.completed).isoformat() if goal.completed else ''
        goal_roots = tuple(task for task in roots if task.goal == goal.id)
        reopen = page_shell.renderPostButton(
            f'/{context.area}/goals/{goal.id}/reopen',
            'reopen',
            button_class='button small',
            return_to=context.here,
        )
        parts.append(
            f'<div class="goal-head"><h2 class="done">{page_shell.escapeHtml(goal.title)}</h2>'
            f'<span class="badge">finished {finished_on}</span>'
            f'{reopen}</div>'
            f'{_renderTree(context, goal_roots) if goal_roots else ""}',
        )

    inner = '\n'.join(parts)
    return (
        f'<details class="goal finished-goals"><summary>{len(finished)} finished '
        f'goal{"s" if len(finished) != 1 else ""}</summary>{inner}</details>'
    )


def _renderAreaEnd(context: _PageContext) -> str:
    """Render the last line of the goal list: finish this area, or reopen it once finished."""
    if CONFIG.areas[context.area].finished is None:
        action, label = 'finish', 'finish area'
        hint = f'take {context.area} off the switcher and stop planning it; its data stays, and + reopens it'
    else:
        action, label = 'reopen', 'reopen area'
        hint = f'put {context.area} back on the switcher and plan it again'

    button = page_shell.renderPostButton(
        f'/{context.area}/area/{action}',
        label,
        button_class='button small ghost',
        title=hint,
    )
    return f'<section class="area-end">{button}</section>'


def _renderTree(context: _PageContext, roots: tuple[Task, ...]) -> str:
    """Render nested <details> with an explicit stack. A repeated ancestor renders as a cycle marker."""
    parts: list[str] = []
    stack: list[_TreeStep] = [_OpenTask(task) for task in reversed(roots)]
    path: set[TaskId] = set()

    while stack:
        match stack.pop():
            case _EmitHtml(markup):
                parts.append(markup)
            case _LeaveTask(task_id):
                path.discard(task_id)
            case _OpenTask(task):
                if task.id in path:
                    title = page_shell.escapeHtml(task.title)
                    parts.append(f'<div class="cycle">&#9888; cycle: {title} is its own ancestor</div>')
                    continue
                path.add(task.id)
                # a finished task collapses even at root level. The focus chain still opens it
                is_open = task.id in context.open_ids or (task.parent is None and graph.stillOpen(task))
                done_attr = '' if graph.stillOpen(task) else ' data-done="1"'
                prefix = context.id_prefix
                notes = f'<p class="notes">{page_shell.escapeHtml(task.notes)}</p>' if task.notes else ''
                parts.append(
                    f'<details class="task" id="{prefix}task-{task.id}" data-task="{prefix}{task.id}"{done_attr}'
                    f'{" open" if is_open else ""}>'
                    f'<summary>{_renderTaskSummary(context, task)}</summary>'
                    f'<input type="checkbox" id="{prefix}edit-state-{task.id}" class="panel-state edit-state">'
                    f'<input type="checkbox" id="{prefix}add-state-{task.id}" class="panel-state add-state">'
                    f'{_renderTaskEditPanel(context, task)}'
                    f'<div class="children">{notes}',
                )
                child_group = context.children.get(task.id, ())
                open_children = tuple(child for child in child_group if graph.stillOpen(child))
                done_children = tuple(child for child in child_group if not graph.stillOpen(child))

                stack.append(_EmitHtml(f'</div>{_renderChildAdder(context, task)}</details>'))
                stack.append(_LeaveTask(task.id))
                if done_children:
                    # finished siblings hide behind one collapsed line at the bottom
                    wrapper_open = any(child.id in context.open_ids for child in done_children)
                    stack.append(_EmitHtml('</details>'))
                    stack.extend(_OpenTask(child) for child in reversed(done_children))
                    stack.append(
                        _EmitHtml(
                            f'<details class="finished-tasks" data-fold="{prefix}fin-{task.id}"'
                            f'{" open" if wrapper_open else ""}>'
                            f'<summary>{len(done_children)} finished</summary>',
                        ),
                    )
                stack.extend(_OpenTask(child) for child in reversed(open_children))

    return ''.join(parts)


def _renderTaskSummary(context: _PageContext, task: Task) -> str:
    """Render the one-line summary row: caret, tick, title, and state badges.

    A task with subtasks or notes folded inside gets a caret, and a parent gets its count
    of open subtasks. A leaf gets an empty caret slot, so sibling rows line up.
    """
    child_group = context.children.get(task.id, ())
    open_count = sum(1 for child in child_group if graph.stillOpen(child))
    caret = f'<span class="twist">{"&#9656;" if child_group or task.notes else ""}</span>'
    sub_count = f'<span class="sub-count">{open_count}</span>' if open_count else ''

    task_path = f'/{context.area}/tasks/{task.id}'
    tick = ''
    if graph.stillOpen(task):
        tick = page_shell.renderPostButton(
            f'{task_path}/tick',
            '&check;',
            button_class='tick',
            form_class='tick-form',
            return_to=context.here,
            title='mark done',
        )
    elif task.status is Status.DONE:
        tick = page_shell.renderPostButton(
            f'{task_path}/reopen',
            '&check;',
            button_class='tick undo',
            form_class='tick-form',
            return_to=context.here,
            title='mark not done',
        )

    badges = []
    if task.estimate_minutes is not None and not graph.stillOpen(task):
        badges.append(f'<span class="badge">{task.estimate_minutes}m</span>')
    if task.not_before is not None and graph.stillOpen(task) and task.not_before > context.now:
        badges.append(f'<span class="badge not-before">from {_formatLocalMoment(task.not_before)}</span>')
    if task.deadline is not None:
        overdue = ' overdue' if task.deadline < context.today else ''
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
    title = page_shell.escapeHtml(task.title)
    return (
        f'{caret}{tick}<span class="{title_class}">{title}</span>{context.crumbs.get(task.id, "")}'
        f'{sub_count}{"".join(badges)}'
        f'{_renderDependencyChips(context, task)}{tools}{drill}'
    )


def _renderEstimateField(context: _PageContext, task: Task) -> str:
    """Render the inline minutes field. Enter saves the number, and an empty value clears it."""
    value = task.estimate_minutes if task.estimate_minutes is not None else ''
    return (
        f'<form method="post" action="/{context.area}/tasks/{task.id}/estimate" class="inline-form est-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'<input type="number" name="estimate_minutes" min="1" value="{value}" placeholder="min" '
        f'title="estimated minutes; Enter saves">'
        f'<span>m</span></form>'
    )


def _renderAutoToggle(context: _PageContext, task: Task) -> str:
    """Render the auto-scheduling pill: lit when on, flipped by one click."""
    state = ' on' if task.auto_schedule else ''
    return page_shell.renderPostButton(
        f'/{context.area}/tasks/{task.id}/auto',
        'auto',
        button_class=f'auto-toggle{state}',
        return_to=context.here,
        title='toggle auto-scheduling',
    )


def _renderDependencyChips(context: _PageContext, task: Task) -> str:
    """Render the dependency chips in the row: this task's blocked_by targets, then the tasks it blocks."""
    chips: list[str] = []
    for dependency in task.blocked_by:
        remove = page_shell.renderPostButton(
            f'/{context.area}/tasks/{task.id}/deps/remove',
            '&times;',
            button_class='chip-x',
            return_to=context.here,
            title='remove dependency',
            hidden_fields=(('blocked_by', dependency),),
        )
        chips.append(
            f'<span class="chip">&#9940; needs '
            f'<a href="/{context.area}/tasks?focus={dependency}">'
            f'{page_shell.escapeHtml(page_shell.findTaskTitle(dependency, context.tasks))}</a>'
            f'{remove}</span>',
        )
    chips.extend(
        f'<span class="chip">&rarr; blocks <a href="/{context.area}/tasks?focus={downstream}">'
        f'{page_shell.escapeHtml(page_shell.findTaskTitle(downstream, context.tasks))}</a></span>'
        for downstream in context.blocks_forward.get(task.id, ())
    )

    return ''.join(chips)


def _renderChildAdder(context: _PageContext, task: Task) -> str:
    """Render the add-a-step line the row's + reveals, below the task's children."""
    return f'<div class="panel child-adder">{_renderTaskAddForm(context, parent=task.id, goal=None)}</div>'


def _renderTaskEditPanel(context: _PageContext, task: Task) -> str:
    """Render the collapsed edit form: fields, dependency add, and leaf-only delete."""
    task_path = f'/{context.area}/tasks/{task.id}'
    status_options = page_shell.renderOptions(((status.value, status.value) for status in Status), task.status.value)
    priority_options = page_shell.renderOptions(
        ((priority.value, priority.value) for priority in Priority),
        task.priority.value,
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
        delete = page_shell.renderPostButton(
            f'{task_path}/delete',
            'delete',
            button_class='button danger',
            return_to=context.here,
        )

    return_to = page_shell.escapeHtml(context.here)
    return (
        f'<div class="panel edit-panel">'
        f'<form method="post" action="{task_path}/edit" class="edit-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<label>title <input type="text" name="title" required value="{page_shell.escapeHtml(task.title)}"></label>'
        f'<label>notes <input type="text" name="notes" value="{page_shell.escapeHtml(task.notes)}"></label>'
        f'<label>minutes <input type="number" name="estimate_minutes" min="1" value="{estimate}"></label>'
        f'<label>deadline <input type="date" name="deadline" value="{deadline}"></label>'
        f'<label>not before <input type="date" name="not_before_day" value="{not_before_day}"> '
        f'<input type="time" name="not_before_time" value="{not_before_time}"></label>'
        f'<label>status <select name="status">{status_options}</select></label>'
        f'<label>priority <select name="priority">{priority_options}</select></label>'
        f'<label>color {_renderColorSelect(task)}</label>'
        f'<label class="check"><input type="checkbox" name="auto_schedule"{checked}> auto-schedule</label>'
        f'<button class="button">save</button></form>'
        f'<form method="post" action="{task_path}/deps/add" class="dep-form">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        f'<input list="all-tasks" name="blocked_by" placeholder="blocked by (pick a task)">'
        f'<button class="button">add dependency</button></form>'
        f'{delete}</div>'
    )


def _renderTaskAddForm(context: _PageContext, parent: TaskId | None, goal: GoalId | None) -> str:
    parent_field = f'<input type="hidden" name="parent" value="{parent}">' if parent is not None else ''
    goal_field = f'<input type="hidden" name="goal" value="{goal}">' if goal is not None else ''
    return (
        f'<form method="post" action="/{context.area}/tasks/add" class="add-form">'
        f'<input type="hidden" name="return_to" value="{page_shell.escapeHtml(context.here)}">'
        f'{parent_field}{goal_field}'
        f'<input type="text" name="title" placeholder="add a step" required>'
        f'<input type="number" name="estimate_minutes" min="1" placeholder="min" class="minutes">'
        f'<label class="check"><input type="checkbox" name="auto_schedule"> auto</label>'
        f'<button class="button">Add</button></form>'
    )


def _renderColorSelect(task: Task) -> str:
    """Render the palette preset choices, the task's retired preset included, so a save does not clear it."""
    names = list(CONFIG.palette)
    if task.color is not None and task.color not in CONFIG.palette:
        names.append(task.color)

    options = page_shell.renderOptions((('', '(auto)'), *((name, name) for name in names)), task.color)
    return f'<select name="color">{options}</select>'


def _renderTaskDatalist(tasks: Mapping[TaskId, Task]) -> str:
    options = ''.join(
        f'<option value="{task.id}" label="{page_shell.escapeHtml(task.title)}"></option>'
        for task in sorted(tasks.values(), key=lambda task: task.title.lower())
    )
    return f'<datalist id="all-tasks">{options}</datalist>'


def _groupDependentIds(tasks: Mapping[TaskId, Task]) -> dict[TaskId, tuple[TaskId, ...]]:
    forward: dict[TaskId, list[TaskId]] = {}
    for task in tasks.values():
        for dependency in task.blocked_by:
            forward.setdefault(dependency, []).append(task.id)

    return {task_id: tuple(sorted(downstream)) for task_id, downstream in forward.items()}


def _collectFocusChainIds(tasks: Mapping[TaskId, Task], focus: TaskId | None) -> frozenset[TaskId]:
    if focus is None:
        return frozenset()

    return frozenset((focus, *graph.listAncestorIds(focus, tasks)))


def _buildPagePath(area: Area, root: TaskId | None, focus: TaskId | None) -> str:
    query = ''
    if root is not None:
        query = f'?root={root}'
    elif focus is not None:
        query = f'?focus={focus}'

    return f'/{area}/tasks{query}'


def _formatLocalMoment(moment: datetime) -> str:
    """Format a moment as '2026-09-28 09:00' in local time, or a bare date at midnight."""
    local = moment.astimezone(CONFIG.timezone)
    if local.time() == time(0):
        return local.date().isoformat()

    return local.strftime('%Y-%m-%d %H:%M')


### vocabulary #########################################################################


@dataclass(frozen=True)
class _PageContext:
    area: Area
    tasks: Mapping[TaskId, Task]
    children: ChildIndex
    blocked: frozenset[TaskId]
    blocks_forward: Mapping[TaskId, tuple[TaskId, ...]]
    stale: frozenset[TaskId]
    focus: TaskId | None
    open_ids: frozenset[TaskId]
    here: str
    id_prefix: str  # '' in the goal trees, 'auto-' for the duplicate rows of the auto group
    crumbs: Mapping[TaskId, str]  # rendered goal-and-ancestor path shown after a row's title
    order: TaskOrder  # the area's chosen order for the Auto group and the scheduler
    at_risk: frozenset[TaskId]  # from the plan: deadlined tasks that jumped the shortest-first queue
    now: datetime
    today: date


@dataclass(frozen=True)
class _OpenTask:
    task: Task


@dataclass(frozen=True)
class _EmitHtml:
    markup: str


@dataclass(frozen=True)
class _LeaveTask:
    task_id: TaskId


# one step of the explicit-stack tree walk
_TreeStep = _OpenTask | _EmitHtml | _LeaveTask
