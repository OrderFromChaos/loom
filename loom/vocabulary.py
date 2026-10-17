"""Every type that crosses a module boundary in loom.

An identifier is a prefix and 8 random base32 characters: 't_' for a TaskId, 'g_' for a
GoalId, 'e_' for an EventId, 'r_' for a RecurringId and 's_' for a StepId. Every datetime
is timezone-aware and stored in UTC. Dates, WeeklyWindow times and WebConfig times are
local wall-clock in the configured timezone.
"""

from __future__ import annotations

import re
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
StepId = NewType('StepId', str)
Color = NewType('Color', str)
Area = NewType('Area', str)  # a data area name. The [areas.*] tables of loom.toml define the set

DAY_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')  # index = datetime.weekday()
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR


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

    def computeSortRank(self) -> int:
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


class TaskOrder(Enum):
    DEADLINE = 'deadline'  # deadlined tasks first, then priority: the default
    SHORTEST = 'shortest-first'  # least estimated time first: quick wins before long haul


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
    position: int | None = None  # 1-based display order. None sorts after every positioned goal


@dataclass(frozen=True)
class Task:
    id: TaskId
    title: str
    status: Status
    parent: TaskId | None
    goal: GoalId | None  # set on root tasks only. A descendant inherits the goal of its root
    blocked_by: tuple[TaskId, ...]
    notes: str
    estimate_minutes: int | None  # None means unestimated. Such a task stays unplaced, with no guessed estimate
    deadline: date | None  # local calendar day, due at its end
    auto_schedule: bool
    priority: Priority
    created: datetime
    completed: datetime | None
    origin: RecurringId | None = None  # the template this task was spawned from
    color: str | None = None  # palette preset NAME. None falls back to the category rules, then the area color
    not_before: datetime | None = None  # no block of this task starts earlier, e.g. before an office opens


# the children of each task. The None key groups the root tasks
ChildIndex = dict[TaskId | None, tuple[Task, ...]]


@dataclass(frozen=True)
class Preferences:
    order: TaskOrder  # the order of auto tasks in the plan and in the Auto group


@dataclass(frozen=True)
class RecurringTask:
    id: RecurringId
    title: str
    notes: str
    estimate_minutes: int | None
    auto_schedule: bool
    priority: Priority
    goal: GoalId | None  # the goal of each spawned copy
    days: frozenset[int]  # anchored weekday numbers, Monday = 0. Empty in interval mode
    every_days: int | None  # interval mode: days between occurrences. None in anchored mode
    deadline_after_days: int | None  # a copy's deadline is its occurrence date plus this. None: no deadline
    wait_for_completion: bool  # True: one open copy at most, and an interval restarts at completion
    created: datetime
    last_added: datetime | None  # spawn cursor: the clock the next occurrence counts from
    next_due: date | None = None  # interval only: a hand-set next occurrence, cleared once it spawns


@dataclass(frozen=True)
class RecurringStep:
    id: StepId
    template: RecurringId  # the template this step belongs to
    parent: StepId | None  # None: directly under the template. Steps nest to any depth
    title: str
    estimate_minutes: int | None
    auto_schedule: bool
    created: datetime  # sibling order, oldest first


# one template's steps under each parent step. The None key groups the top level
StepIndex = dict[StepId | None, tuple[RecurringStep, ...]]


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
    at_risk: tuple[TaskId, ...] = ()  # shortest-first only: deadlined tasks moved ahead so they fit


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
class TasksByBucket:
    """The four buckets of the standard ADHD ordering, listed and placed top to bottom."""

    at_risk: tuple[Task, ...]  # deadline at risk, soonest deadline first
    quickest: tuple[Task, ...]  # doable now, fewest estimated minutes first
    not_yet: tuple[Task, ...]  # not_before still ahead, soonest first
    unestimated: tuple[Task, ...]  # no estimate: listed, never placed, in input order

    def listPlacementQueue(self) -> tuple[Task, ...]:
        """Every placeable task, in bucket order. The unestimated bucket is left out."""
        return (*self.at_risk, *self.quickest, *self.not_yet)


@dataclass(frozen=True)
class OrderedPlacement:
    placement: Placement
    at_risk: frozenset[TaskId]  # deadlined tasks that jumped the shortest-first queue


@dataclass(frozen=True)
class WeeklyWindow:
    days: frozenset[int]  # weekday numbers, Monday = 0
    start: time
    end: time


@dataclass(frozen=True)
class CategoryRule:
    pattern: re.Pattern[str]  # searched against a task title, case-insensitive
    category: str  # a palette preset name, the source of the color


@dataclass(frozen=True)
class SchedulerConfig:
    min_chunk_minutes: int
    max_chunk_minutes: int
    stale_grace_minutes: int


@dataclass(frozen=True)
class WebConfig:
    port: int
    day_start: time  # top edge of the calendar grid
    day_end: time  # bottom edge. At or before day_start, the grid runs past midnight
    scroll_start: time  # where the grid is scrolled to on page load

    def computeGridStartMinute(self) -> int:
        return self.day_start.hour * MINUTES_PER_HOUR + self.day_start.minute

    def computeGridEndMinute(self) -> int:
        """Minutes from local midnight to the bottom edge, above MINUTES_PER_DAY when the grid runs past midnight."""
        end = self.day_end.hour * MINUTES_PER_HOUR + self.day_end.minute
        return end if end > self.computeGridStartMinute() else end + MINUTES_PER_DAY

    def computeScrollMinute(self) -> int:
        """Minutes from local midnight to scroll_start, above MINUTES_PER_DAY when it is after midnight."""
        scroll = self.scroll_start.hour * MINUTES_PER_HOUR + self.scroll_start.minute
        return scroll if scroll >= self.computeGridStartMinute() else scroll + MINUTES_PER_DAY


@dataclass(frozen=True)
class AreaConfig:
    windows: tuple[WeeklyWindow, ...]
    color: Color
    finished: date | None  # the day the area was finished: off the switcher and unplanned, with its data kept
    hidden: bool  # still planned, but off the switcher: reached from its + menu (the demo area)


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    data_root: Path
    horizon_days: int
    scheduler: SchedulerConfig
    web: WebConfig
    areas: Mapping[Area, AreaConfig]
    palette: Mapping[str, Color]  # the categories: named presets a task can point at
    categories: tuple[CategoryRule, ...]  # automatic assignment, in match order. The first match wins


@dataclass(frozen=True)
class DeadlineEntry:
    task: Task
    goal: Goal | None
    days_remaining: int  # negative when overdue
    dependency_blocked: bool
    minutes_planned: int  # minutes of this task in the current plan


class ConfigError(RuntimeError):
    """The loom.toml file is absent or malformed."""


class StoreError(RuntimeError):
    """A data file could not be read or parsed."""


class StoreConflictError(StoreError):
    """The file changed on disk after this Store loaded it."""


class TemplateNotFoundError(RuntimeError):
    """No recurring template has the requested id."""
