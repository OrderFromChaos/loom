"""The POST routes that add, edit, reorder, finish and reopen goals."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from loom import graph, store
from loom.vocabulary import Goal, GoalId
from loom.web import request_fields, responses
from loom.web.web_vocabulary import FormError, MoveDirection, Request, Response


def handleAddGoal(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()

    goal = Goal(
        id=store.generateGoalId(),
        title=request_fields.readFormText(request.form, 'title'),
        notes='',
        created=datetime.now(UTC),
        completed=None,
    )
    goals[goal.id] = goal
    area_store.saveGoals(goals)
    return responses.buildReturnRedirect(request, area)


def handleEditGoal(request: Request) -> Response:
    def editGoal(goal: Goal) -> Goal:
        return replace(
            goal,
            title=request_fields.readFormText(request.form, 'title'),
            notes=request.form.get('notes', '').strip(),
        )

    return _handleGoalChange(request, editGoal)


def _handleGoalChange(request: Request, change: Callable[[Goal], Goal]) -> Response:
    """Replace the route's goal with change(goal), save, and redirect.

    Raises:
        FormError: the goal does not exist, or `change` raised it. Nothing is written.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()
    goal = request_fields.findRecord(goals, GoalId(request.match.group('id')), 'goal')

    goals[goal.id] = change(goal)
    area_store.saveGoals(goals)
    return responses.buildReturnRedirect(request, area)


def handleMoveGoal(request: Request) -> Response:
    """Swap the goal one step up or down in the display order, renumbering every goal.

    Raises:
        FormError: the goal does not exist, is finished, or the direction is malformed.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    goals = area_store.loadGoals()
    goal = request_fields.findRecord(goals, GoalId(request.match.group('id')), 'goal')

    direction = request_fields.readFormEnum(request.form, 'direction', MoveDirection)
    ordered = sorted(
        (candidate for candidate in goals.values() if candidate.completed is None),
        key=graph.computeGoalSortKey,
    )
    index = next((position for position, candidate in enumerate(ordered) if candidate.id == goal.id), None)
    if index is None:
        raise FormError('a finished goal cannot be reordered')

    target = index + direction.computeOffset()
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
    for position, moved in enumerate(ordered, start=1):
        goals[moved.id] = replace(moved, position=position)
    area_store.saveGoals(goals)
    return responses.buildReturnRedirect(request, area)


def handleFinishGoal(request: Request) -> Response:
    return _handleGoalChange(request, lambda goal: replace(goal, completed=datetime.now(UTC)))


def handleReopenGoal(request: Request) -> Response:
    return _handleGoalChange(request, lambda goal: replace(goal, completed=None))
