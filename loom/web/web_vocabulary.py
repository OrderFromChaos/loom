"""Types shared between the web modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class View(Enum):
    CALENDAR = 'calendar'
    TASKS = 'tasks'
    RECURRING = 'recurring'
    DEADLINES = 'deadlines'
    ACHIEVEMENTS = 'achievements'
    SETTINGS = 'settings'


@dataclass(frozen=True)
class BlockBox:
    top_percent: float
    height_percent: float
    column: int  # 0-based sub-column within its overlap group
    columns: int  # sub-columns the overlap group needs


class FormError(RuntimeError):
    """A submitted form field is missing or malformed."""
