"""Render the achievements page: completed tasks grouped by day, newest day first.

Each day carries its count; within a day, tasks run in completion order, so scrolling a
day reads as the day actually went. Days and times are local wall-clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from loom import graph
from loom.coloring import colorForTask
from loom.config import CONFIG
from loom.vocabulary import DAY_NAMES, Area, Goal, GoalId, Status, Task, TaskId
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


def renderAchievementsPage(
    area: Area,
    *,
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
) -> str:
    """The whole achievements page: one section per day with a completion counter."""
    zone = CONFIG.timezone
    by_day: dict[date, list[Task]] = {}
    for task in tasks.values():
        if task.status is Status.DONE and task.completed is not None:
            by_day.setdefault(task.completed.astimezone(zone).date(), []).append(task)

    sections = []
    for day in sorted(by_day, reverse=True):
        finished = sorted(by_day[day], key=lambda task: task.completed or task.created)
        rows = '\n'.join(_renderRow(area, task, goals, tasks) for task in finished)
        count = len(finished)
        sections.append(
            f'<section class="goal ach-day"><div class="goal-head">'
            f'<h2>{DAY_NAMES[day.weekday()]} {day.isoformat()}</h2>'
            f'<span class="badge done-count">{count} done</span></div>'
            f'{rows}</section>',
        )

    return renderPage('achievements', area, View.ACHIEVEMENTS, '\n'.join(sections))


def _renderRow(
    area: Area,
    task: Task,
    goals: Mapping[GoalId, Goal],
    tasks: Mapping[TaskId, Task],
) -> str:
    """One completed task: time, color dot, title link, and its goal when it has one."""
    finished_at = task.completed.astimezone(CONFIG.timezone).strftime('%H:%M') if task.completed else ''
    goal_id = graph.effectiveGoal(task, tasks)
    goal = goals.get(goal_id) if goal_id is not None else None
    goal_chip = f'<span class="badge">{esc(goal.title)}</span>' if goal is not None else ''

    return (
        f'<div class="ach-row"><span class="ach-time">{finished_at}</span>'
        f'<span class="ach-dot" style="--block-color:{colorForTask(task, area)}"></span>'
        f'<a href="/{area}/tasks?focus={task.id}">{esc(task.title)}</a>{goal_chip}</div>'
    )
