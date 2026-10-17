"""Derive structure from the task graph: hierarchy walks, dependency blocking, integrity problems.

Every walk is iterative, so a decomposition hundreds of levels deep cannot overflow the
stack. A hand-edited file can have a parent cycle or a dangling reference. No walk loops
on a cycle or fails on a dangling reference, and listIntegrityProblems returns both.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from loom.vocabulary import ChildIndex, Goal, GoalId, Status, Task, TaskId


def collectSubtreeIds(root: TaskId, tasks: Mapping[TaskId, Task]) -> frozenset[TaskId]:
    """Collect a task and every task beneath it, even through a parent cycle."""
    children = groupChildrenByParent(tasks)
    found: set[TaskId] = set()
    stack = [root]
    while stack:
        task_id = stack.pop()
        if task_id in found:
            continue
        found.add(task_id)
        stack.extend(child.id for child in children.get(task_id, ()))

    return frozenset(found)


def groupChildrenByParent(tasks: Mapping[TaskId, Task]) -> ChildIndex:
    """Group tasks under their parent id. The None key groups the roots.

    Siblings sort by (not stillOpen, created, id).
    """
    grouped: dict[TaskId | None, list[Task]] = {}
    for task in tasks.values():
        grouped.setdefault(task.parent, []).append(task)

    return {
        parent: tuple(sorted(group, key=lambda task: (not stillOpen(task), task.created, task.id)))
        for parent, group in grouped.items()
    }


def listStarvingGoals(goals: Mapping[GoalId, Goal], tasks: Mapping[TaskId, Task]) -> tuple[Goal, ...]:
    """List the unfinished goals with no open task left, the prompt to decompose further."""
    fed = _collectOpenGoalIds(tasks)
    return tuple(goal for goal_id, goal in sorted(goals.items()) if goal_id not in fed and goal.completed is None)


def listIntegrityProblems(tasks: Mapping[TaskId, Task], goals: Mapping[GoalId, Goal]) -> tuple[str, ...]:
    """List human-readable problems: dangling references, parent cycles, and finished work above open work."""
    problems = []
    open_parent_ids = {task.parent for task in tasks.values() if stillOpen(task)}
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
        if task.status is Status.DONE and task.id in open_parent_ids:
            problems.append(f'{task.id}: done, but it still has open children')

    problems.extend(f'{task_id}: parent chain loops' for task_id in sorted(_collectCycleMembers(tasks)))

    open_goal_ids = _collectOpenGoalIds(tasks)
    problems.extend(
        f'{goal_id}: finished, but it still has open tasks'
        for goal_id, goal in sorted(goals.items())
        if goal.completed is not None and goal_id in open_goal_ids
    )

    return tuple(problems)


def _collectOpenGoalIds(tasks: Mapping[TaskId, Task]) -> frozenset[GoalId]:
    """Collect the goals that have at least one open task, directly or through an ancestor."""
    goal_ids = set()
    for task in tasks.values():
        if not stillOpen(task):
            continue
        goal_id = findEffectiveGoal(task, tasks)
        if goal_id is not None:
            goal_ids.add(goal_id)

    return frozenset(goal_ids)


def _collectCycleMembers(tasks: Mapping[TaskId, Task]) -> frozenset[TaskId]:
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


def findEffectiveGoal(task: Task, tasks: Mapping[TaskId, Task]) -> GoalId | None:
    """Find the goal set on this task, or on its nearest ancestor that has one."""
    current = task
    seen = {task.id}
    while current.goal is None and current.parent is not None:
        parent = tasks.get(current.parent)
        if parent is None or parent.id in seen:
            return None
        seen.add(parent.id)
        current = parent

    return current.goal


def listAncestorIds(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> tuple[TaskId, ...]:
    """List the parent chain from the task upward, nearest first. The list ends before a repeated or dangling id."""
    chain: list[TaskId] = []
    seen = {task_id}
    current = tasks.get(task_id)
    while current is not None and current.parent is not None and current.parent not in seen:
        chain.append(current.parent)
        seen.add(current.parent)
        current = tasks.get(current.parent)

    return tuple(chain)


def collectDependencyBlockedIds(tasks: Mapping[TaskId, Task]) -> frozenset[TaskId]:
    """Collect the ids of tasks with a blocked_by target that exists and is not done.

    A dangling target does not block, and listIntegrityProblems returns it.
    """
    blocked = set()
    for task in tasks.values():
        for dependency_id in task.blocked_by:
            dependency = tasks.get(dependency_id)
            if dependency is not None and dependency.status is not Status.DONE:
                blocked.add(task.id)
                break

    return frozenset(blocked)


def computeGoalSortKey(goal: Goal) -> tuple[bool, int, datetime, GoalId]:
    """Display order: positioned goals first by position, then unpositioned by age."""
    return (goal.position is None, goal.position or 0, goal.created, goal.id)


def notYetDoable(task: Task, now: datetime) -> bool:
    """True while the task's not_before is still ahead."""
    return task.not_before is not None and task.not_before > now


def stillOpen(task: Task) -> bool:
    match task.status:
        case Status.TODO | Status.IN_PROGRESS | Status.BLOCKED:
            return True
        case Status.DONE | Status.DROPPED:
            return False
