"""Derive structure from the task graph: hierarchy walks, dependency blocking, actionability.

Every walk is iterative, so a decomposition hundreds of levels deep cannot overflow the
stack. Hand-edited files can hold a parent cycle or a dangling reference; every walk
tolerates both, and checkIntegrity names them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from loom.vocabulary import ChildIndex, Goal, GoalId, Status, Task, TaskId


_OPEN_STATUSES = frozenset({Status.TODO, Status.IN_PROGRESS, Status.BLOCKED})


def stillOpen(task: Task) -> bool:
    return task.status in _OPEN_STATUSES


def childrenByParent(tasks: Mapping[TaskId, Task]) -> ChildIndex:
    """Group tasks under their parent id (None groups the roots).

    Siblings sort by (finished, created, id): open tasks keep their age order and done
    or dropped ones sink below them, so finished work stays out of the way.
    """
    grouped: dict[TaskId | None, list[Task]] = {}
    for task in tasks.values():
        grouped.setdefault(task.parent, []).append(task)

    return {
        parent: tuple(sorted(group, key=lambda task: (not stillOpen(task), task.created, task.id)))
        for parent, group in grouped.items()
    }


def effectiveGoal(task: Task, tasks: Mapping[TaskId, Task]) -> GoalId | None:
    """The goal set on this task or inherited from the nearest ancestor that names one."""
    current = task
    seen = {task.id}
    while current.goal is None and current.parent is not None:
        parent = tasks.get(current.parent)
        if parent is None or parent.id in seen:
            return None
        seen.add(parent.id)
        current = parent

    return current.goal


def ancestorsOf(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> tuple[TaskId, ...]:
    """Parent chain from the task upward, nearest first, stopping at a cycle or dangling id."""
    chain: list[TaskId] = []
    seen = {task_id}
    current = tasks.get(task_id)
    while current is not None and current.parent is not None and current.parent not in seen:
        chain.append(current.parent)
        seen.add(current.parent)
        current = tasks.get(current.parent)

    return tuple(chain)


def dependencyBlocked(tasks: Mapping[TaskId, Task]) -> frozenset[TaskId]:
    """Ids of tasks with at least one blocked_by target that exists and is not done.

    A dangling target does not block; checkIntegrity reports it instead.
    """
    blocked = set()
    for task in tasks.values():
        for dependency_id in task.blocked_by:
            dependency = tasks.get(dependency_id)
            if dependency is not None and dependency.status is not Status.DONE:
                blocked.add(task.id)
                break

    return frozenset(blocked)


def actionableTasks(tasks: Mapping[TaskId, Task]) -> tuple[Task, ...]:
    """Tasks ready to act on: todo, every dependency done, and no open children.

    A parent with open children is a summary, ticked after its leaves.
    """
    blocked = dependencyBlocked(tasks)
    parents_with_open_children = {task.parent for task in tasks.values() if task.parent is not None and stillOpen(task)}
    ready = [
        task
        for task in tasks.values()
        if task.status is Status.TODO and task.id not in blocked and task.id not in parents_with_open_children
    ]
    ready.sort(key=lambda task: (task.priority.rank(), task.created, task.id))

    return tuple(ready)


def goalSortKey(goal: Goal) -> tuple[bool, int, datetime, GoalId]:
    """Display order: positioned goals first by position, then unpositioned by age."""
    return (goal.position is None, goal.position or 0, goal.created, goal.id)


def starvingGoals(goals: Mapping[GoalId, Goal], tasks: Mapping[TaskId, Task]) -> tuple[Goal, ...]:
    """Unfinished goals with no open task left: the prompt to decompose further."""
    fed = set()
    for task in tasks.values():
        if stillOpen(task):
            goal_id = effectiveGoal(task, tasks)
            if goal_id is not None:
                fed.add(goal_id)

    return tuple(goal for goal_id, goal in sorted(goals.items()) if goal_id not in fed and goal.completed is None)


def checkIntegrity(tasks: Mapping[TaskId, Task], goals: Mapping[GoalId, Goal]) -> tuple[str, ...]:
    """Human-readable problems: dangling references, parent cycles, done parents of open work."""
    problems = []
    for task in sorted(tasks.values(), key=lambda task: task.id):
        if task.parent is not None and task.parent not in tasks:
            problems.append(f'{task.id}: parent {task.parent} does not exist')
        if task.goal is not None and task.goal not in goals:
            problems.append(f'{task.id}: goal {task.goal} does not exist')
        problems.extend(
            f'{task.id}: blocked_by {dependency} does not exist'
            for dependency in task.blocked_by
            if dependency not in tasks
        )
        if task.status is Status.DONE and task.id in {other.parent for other in tasks.values() if stillOpen(other)}:
            problems.append(f'{task.id}: done, but it still has open children')

    problems.extend(f'{task_id}: parent chain loops' for task_id in sorted(_cycleMembers(tasks)))

    open_goal_ids = {
        goal_id for task in tasks.values() if stillOpen(task) and (goal_id := effectiveGoal(task, tasks)) is not None
    }
    problems.extend(
        f'{goal_id}: finished, but it still has open tasks'
        for goal_id, goal in sorted(goals.items())
        if goal.completed is not None and goal_id in open_goal_ids
    )

    return tuple(problems)


def _cycleMembers(tasks: Mapping[TaskId, Task]) -> frozenset[TaskId]:
    cyclic = set()
    for task in tasks.values():
        seen = {task.id}
        current = task
        while current.parent is not None:
            if current.parent in seen:
                cyclic.add(task.id)
                break
            seen.add(current.parent)
            parent = tasks.get(current.parent)
            if parent is None:
                break
            current = parent

    return frozenset(cyclic)
