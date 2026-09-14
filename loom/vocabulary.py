"""Every type that crosses a module boundary in loom.

Identifiers are short random slugs: a TaskId is 't_' plus 8 base32 characters, a GoalId
'g_...', an EventId 'e_...'. Every datetime is timezone-aware and stored in UTC. Dates,
WeeklyWindow times and WebConfig times are local wall-clock in the configured timezone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import NewType
from zoneinfo import ZoneInfo


TaskId = NewType('TaskId', str)
GoalId = NewType('GoalId', str)
EventId = NewType('EventId', str)
RecurringId = NewType('RecurringId', str)
Color = NewType('Color', str)

DAY_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')  # index = datetime.weekday()


class Area(Enum):
    WORK = 'work'
    PERSONAL = 'personal'
    DEMO = 'demo'  # sample data lives here, never in the real areas


class Status(Enum):
    TODO = 'todo'
    IN_PROGRESS = 'in-progress'
    BLOCKED = 'blocked'
    DONE = 'done'
    DROPPED = 'dropped'


class Priority(Enum):
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'

    def rank(self) -> int:
        """Sort position: HIGH sorts first."""
        match self:
            case Priority.HIGH:
                return 0
            case Priority.MEDIUM:
                return 1
            case Priority.LOW:
                return 2


class Reason(Enum):
    NO_ESTIMATE = 'no-estimate'
    DEPENDENCY_BLOCKED = 'dependency-blocked'
    NO_ROOM = 'no-room'
    MISSES_DEADLINE = 'misses-deadline'


class EventKind(Enum):
    DID = 'did'
    ACK = 'ack'


class EventSource(Enum):
    MANUAL = 'manual'
    ACTIVITYWATCH = 'activitywatch'


@dataclass(frozen=True)
class Goal:
    id: GoalId
    title: str
    notes: str
    created: datetime
    completed: datetime | None
    position: int | None = None  # 1-based display order; None sorts after every positioned goal


@dataclass(frozen=True)
class Task:
    id: TaskId
    title: str
    status: Status
    parent: TaskId | None
    goal: GoalId | None  # set on root tasks only; descendants inherit via graph.py
    blocked_by: tuple[TaskId, ...]
    notes: str
    estimate_minutes: int | None  # None = unestimated; the scheduler reports, never guesses
    deadline: date | None  # local calendar day, due at its end
    auto_schedule: bool
    priority: Priority
    created: datetime
    completed: datetime | None
    origin: RecurringId | None = None  # the template this task was spawned from
    color: str | None = None  # palette preset NAME; None falls back to keyword then area color


@dataclass(frozen=True)
class RecurringTask:
    id: RecurringId
    title: str
    notes: str
    estimate_minutes: int | None
    auto_schedule: bool
    priority: Priority
    goal: GoalId | None  # where a spawned copy lands
    days: frozenset[int]  # anchored weekday numbers, Monday = 0; empty in interval mode
    every_days: int | None  # interval mode: days since the last completed copy; None in anchored mode
    deadline_after_days: int | None  # copy's deadline = its occurrence date + this; None = no deadline
    created: datetime
    last_added: datetime | None  # spawn cursor: when a copy was last spawned


@dataclass(frozen=True)
class FixedEvent:
    title: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class LoggedEvent:
    id: EventId
    kind: EventKind
    at: datetime  # when the entry was written
    start: datetime | None  # None on an ack
    end: datetime | None
    note: str
    task: TaskId | None  # None when the event is unattributed
    source: EventSource


@dataclass(frozen=True)
class PlanBlock:
    task: TaskId
    start: datetime
    end: datetime
    chunk: int  # 1-based position among this task's chunks
    chunks: int


@dataclass(frozen=True)
class Unplaced:
    task: TaskId
    reason: Reason


@dataclass(frozen=True)
class Plan:
    generated: datetime
    blocks: tuple[PlanBlock, ...]
    unplaced: tuple[Unplaced, ...]


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Selection:
    candidates: tuple[Task, ...]
    rejected: tuple[Unplaced, ...]


@dataclass(frozen=True)
class Placement:
    blocks: tuple[PlanBlock, ...]
    unplaced: tuple[Unplaced, ...]


@dataclass(frozen=True)
class WeeklyWindow:
    days: frozenset[int]  # weekday numbers, Monday = 0
    start: time
    end: time


@dataclass(frozen=True)
class KeywordColor:
    keyword: str
    color: Color


@dataclass(frozen=True)
class SchedulerConfig:
    min_chunk_minutes: int
    max_chunk_minutes: int
    stale_grace_minutes: int


@dataclass(frozen=True)
class WebConfig:
    port: int
    day_start: time  # top edge of the calendar grid
    day_end: time  # bottom edge; at or before day_start means it runs past midnight
    scroll_start: time  # where the grid is scrolled to on page load


@dataclass(frozen=True)
class AreaConfig:
    windows: tuple[WeeklyWindow, ...]
    color: Color


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    data_root: Path
    horizon_days: int
    scheduler: SchedulerConfig
    web: WebConfig
    areas: Mapping[Area, AreaConfig]
    colors: tuple[KeywordColor, ...]  # match order: first hit wins
    palette: Mapping[str, Color]  # named presets a task can point at by name


# a mapping stays a mapping: the None key groups the root tasks
ChildIndex = dict[TaskId | None, tuple[Task, ...]]


@dataclass(frozen=True)
class DeadlineEntry:
    task: Task
    goal: Goal | None
    days_remaining: int  # negative when overdue
    dependency_blocked: bool
    minutes_planned: int  # minutes the current plan gives this task


class ConfigError(RuntimeError):
    """loom.toml is missing or malformed."""


class StoreError(RuntimeError):
    """A data file could not be read or parsed."""


class StoreConflictError(StoreError):
    """The file changed on disk after this Store loaded it."""


class IntegrityError(RuntimeError):
    """Task references do not resolve: a dangling id or a parent cycle."""
