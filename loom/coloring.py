"""Pick a calendar color: manual category first, then category rules, then the area.

A category is a palette preset, and its hex is in loom.toml. Recoloring a category
recolors everything assigned to it, manually or by rule. A task record has at most a
category name.
"""

from __future__ import annotations

from loom.config import CONFIG
from loom.vocabulary import Area, Color, Task


def pickTaskColor(task: Task, area: Area) -> Color:
    """Pick the color of the task's manual category when it is set and known, otherwise pickTitleColor()."""
    if task.color is not None:
        preset = CONFIG.palette.get(task.color)
        if preset is not None:
            return preset

    return pickTitleColor(task.title, area)


def pickTitleColor(title: str, area: Area) -> Color:
    """Pick the color of the first [categories] rule that matches the title, otherwise the area color."""
    category = _findTitleCategory(title)
    if category is not None:
        preset = CONFIG.palette.get(category)
        if preset is not None:
            return preset

    return CONFIG.areas[area].color


def _findTitleCategory(title: str) -> str | None:
    for rule in CONFIG.categories:
        if rule.pattern.search(title):
            return rule.category

    return None
