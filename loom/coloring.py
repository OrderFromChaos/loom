"""Pick a calendar color: manual category first, then category rules, then the area.

A category is a palette preset; its hex lives in loom.toml, so recoloring a category
recolors everything assigned to it, manually or by rule. Stored data carries at most a
category name.
"""

from __future__ import annotations

from loom.config import CONFIG
from loom.vocabulary import Area, Color, Task


def colorForTask(task: Task, area: Area) -> Color:
    """The task's manual category when set and known; otherwise the title rules."""
    if task.color is not None:
        preset = CONFIG.palette.get(task.color)
        if preset is not None:
            return preset

    return colorForTitle(task.title, area)


def colorForTitle(title: str, area: Area) -> Color:
    """The first [categories] rule whose regex matches decides; no match, area color."""
    category = categoryForTitle(title)
    if category is not None:
        preset = CONFIG.palette.get(category)
        if preset is not None:
            return preset

    return CONFIG.areas[area].color


def categoryForTitle(title: str) -> str | None:
    """The first matching rule's category name, None when no rule matches."""
    for rule in CONFIG.categories:
        if rule.pattern.search(title):
            return rule.category

    return None
