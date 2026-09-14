"""Pick a calendar color: a task's palette preset first, then keywords, then the area.

Stored data carries at most a preset name; the hex it resolves to lives in loom.toml,
so recoloring a preset recolors everything pointing at it.
"""

from __future__ import annotations

from loom.config import CONFIG
from loom.vocabulary import Area, Color, Task


def colorForTask(task: Task, area: Area) -> Color:
    """The task's palette preset when set and known; otherwise keyword matching."""
    if task.color is not None:
        preset = CONFIG.palette.get(task.color)
        if preset is not None:
            return preset

    return colorForTitle(task.title, area)


def colorForTitle(title: str, area: Area) -> Color:
    """Case-insensitive substring match, first [colors] entry in file order wins.

    tomllib preserves the table's file order, which is what makes "first" meaningful.
    No match falls back to the area's own color.
    """
    lowered = title.lower()
    for entry in CONFIG.colors:
        if entry.keyword.lower() in lowered:
            return entry.color

    return CONFIG.areas[area].color
