"""Render the achievements page: completed tasks grouped by day, newest day first.

Each day has its count. Within a day, tasks are in completion order, so a day reads as
it went. Days and times are local wall-clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from loom import coloring, config, graph
from loom.config import CONFIG
from loom.vocabulary import DAY_NAMES, Area, Goal, GoalId, Status, Task, TaskId
from loom.web import page_shell
from loom.web.web_vocabulary import View


def renderAchievementsPage(
    area: Area,
    *,
    tasks: Mapping[TaskId, Task],
    goals: Mapping[GoalId, Goal],
) -> str:
    """Render the achievements page: one section per day with a completion counter."""
    by_day: dict[date, list[Task]] = {}
    for task in tasks.values():
        if task.status is Status.DONE and task.completed is not None:
            by_day.setdefault(config.computeLocalDate(task.completed), []).append(task)

    sections = []
    for day in sorted(by_day, reverse=True):
        finished = sorted(by_day[day], key=lambda task: task.completed or task.created)
        rows = '\n'.join(_renderAchievementRow(area, task, goals, tasks) for task in finished)
        count = len(finished)
        sections.append(
            f'<section class="goal ach-day"><div class="goal-head">'
            f'<h2>{DAY_NAMES[day.weekday()]} {day.isoformat()}</h2>'
            f'<span class="badge done-count">{count} done</span></div>'
            f'{rows}</section>',
        )

    return page_shell.renderPage('achievements', area, View.ACHIEVEMENTS, '\n'.join(sections))


def _renderAchievementRow(
    area: Area,
    task: Task,
    goals: Mapping[GoalId, Goal],
    tasks: Mapping[TaskId, Task],
) -> str:
    """Render one completed task: time, color dot, title link, and its goal when it has one."""
    finished_at = task.completed.astimezone(CONFIG.timezone).strftime('%H:%M') if task.completed else ''
    goal_id = graph.findEffectiveGoal(task, tasks)
    goal = goals.get(goal_id) if goal_id is not None else None
    goal_chip = f'<span class="badge">{page_shell.escapeHtml(goal.title)}</span>' if goal is not None else ''

    return (
        f'<div class="ach-row"><span class="ach-time">{finished_at}</span>'
        f'<span class="ach-dot" style="--block-color:{coloring.pickTaskColor(task, area)}"></span>'
        f'<a href="/{area}/tasks?focus={task.id}">{page_shell.escapeHtml(task.title)}</a>{goal_chip}</div>'
    )
