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
    columns: int  # sub-column count of the overlap group


class MoveDirection(Enum):
    UP = 'up'
    DOWN = 'down'

    def computeOffset(self) -> int:
        match self:
            case MoveDirection.UP:
                return -1
            case MoveDirection.DOWN:
                return 1


class FormError(RuntimeError):
    """A submitted form field is absent or malformed."""
