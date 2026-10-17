"""Types shared between the web modules."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus

from loom.vocabulary import Area


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


@dataclass(frozen=True)
class Request:
    match: re.Match[str]
    query: Mapping[str, str]
    form: Mapping[str, str]
    home_area: Area  # where GET / redirects, from the command line


@dataclass(frozen=True)
class Response:
    status: HTTPStatus
    body: str | bytes
    content_type: str
    location: str | None
    restart: bool = False  # after sending: exec a fresh server so CONFIG reloads


RouteHandler = Callable[[Request], Response]
