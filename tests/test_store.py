"""Round-trip and conflict-guard tests for loom.store, against real temp directories.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_store.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loom.store import Store
from loom.vocabulary import (
    EventId,
    EventKind,
    EventSource,
    Goal,
    GoalId,
    LoggedEvent,
    Priority,
    Status,
    StoreConflictError,
    StoreError,
    Task,
    TaskId,
)


def _taskIds() -> st.SearchStrategy[TaskId]:
    return st.from_regex(r't_[a-z2-7]{8}', fullmatch=True).map(TaskId)


def _goalIds() -> st.SearchStrategy[GoalId]:
    return st.from_regex(r'g_[a-z2-7]{8}', fullmatch=True).map(GoalId)


def _utcTimestamps() -> st.SearchStrategy[datetime]:
    return st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 1, 1), timezones=st.just(UTC))


def _tasks() -> st.SearchStrategy[Task]:
    return st.builds(
        Task,
        id=_taskIds(),
        title=st.text(max_size=40),
        status=st.sampled_from(Status),
        parent=st.none() | _taskIds(),
        goal=st.none() | _goalIds(),
        blocked_by=st.lists(_taskIds(), max_size=3, unique=True).map(tuple),
        notes=st.text(max_size=40),
        estimate_minutes=st.none() | st.integers(min_value=1, max_value=600),
        deadline=st.none() | st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 1, 1)),
        auto_schedule=st.booleans(),
        priority=st.sampled_from(Priority),
        created=_utcTimestamps(),
        completed=st.none() | _utcTimestamps(),
    )


def _didEvents() -> st.SearchStrategy[LoggedEvent]:
    """Generate 'did' events whose end always follows their start, as the parse gate requires."""

    def build(
        event_id: EventId,
        at: datetime,
        start: datetime,
        minutes: int,
        note: str,
        task: TaskId | None,
    ) -> LoggedEvent:
        """Assemble the event from a start plus a positive duration.

        Raises:
            OverflowError: start plus minutes leaves the datetime range (unreachable at these bounds).
        """
        return LoggedEvent(
            id=event_id,
            kind=EventKind.DID,
            at=at,
            start=start,
            end=start + timedelta(minutes=minutes),
            note=note,
            task=task,
            source=EventSource.MANUAL,
        )

    return st.builds(
        build,
        event_id=st.from_regex(r'e_[a-z2-7]{8}', fullmatch=True).map(EventId),
        at=_utcTimestamps(),
        start=_utcTimestamps(),
        minutes=st.integers(min_value=1, max_value=600),
        note=st.text(max_size=40),
        task=st.none() | _taskIds(),
    )


@given(task_list=st.lists(_tasks(), max_size=8, unique_by=lambda task: task.id))
def testTasksSurviveASaveLoadRoundTrip(task_list: list[Task]) -> None:
    tasks = {task.id: task for task in task_list}
    with tempfile.TemporaryDirectory() as temp:
        Store(Path(temp)).saveTasks(tasks)
        assert Store(Path(temp)).loadTasks() == tasks


@given(
    goals=st.builds(
        Goal,
        id=_goalIds(),
        title=st.text(max_size=40),
        notes=st.text(max_size=40),
        created=_utcTimestamps(),
        completed=st.none() | _utcTimestamps(),
        position=st.none() | st.integers(min_value=1, max_value=50),
    ).map(lambda goal: {goal.id: goal}),
)
def testGoalsSurviveASaveLoadRoundTrip(goals: dict[GoalId, Goal]) -> None:
    with tempfile.TemporaryDirectory() as temp:
        Store(Path(temp)).saveGoals(goals)
        assert Store(Path(temp)).loadGoals() == goals


@given(events=st.lists(_didEvents(), max_size=6))
def testEventsSurviveAppendsAndAReload(events: list[LoggedEvent]) -> None:
    with tempfile.TemporaryDirectory() as temp:
        writer = Store(Path(temp))
        for event in events:
            writer.appendEvent(event)
        assert Store(Path(temp)).loadEvents() == tuple(events)


def testAckEventSurvivesARoundTrip(tmp_path: Path) -> None:
    ack = LoggedEvent(
        id=EventId('e_abcdefgh'),
        kind=EventKind.ACK,
        at=datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
        start=None,
        end=None,
        note='',
        task=TaskId('t_abcdefgh'),
        source=EventSource.MANUAL,
    )
    Store(tmp_path).appendEvent(ack)
    assert Store(tmp_path).loadEvents() == (ack,)


def testSaveRefusesAFileThisStoreNeverRead(tmp_path: Path) -> None:
    early = Store(tmp_path)
    early.loadTasks()

    other = Store(tmp_path)
    other.loadTasks()
    other.saveTasks({})

    with pytest.raises(StoreConflictError):
        early.saveTasks({})


def testMalformedEventLineIsSkippedNotFatal(tmp_path: Path) -> None:
    good = '{"id": "e_abcdefgh", "kind": "ack", "at": "2026-09-15T08:30:00+00:00", "task": "t_abcdefgh"}'
    (tmp_path / 'events.jsonl').write_text(good + '\nnot json at all\n', encoding='utf-8')

    events = Store(tmp_path).loadEvents()

    assert len(events) == 1
    assert events[0].kind is EventKind.ACK


def testMalformedTasksFileRaisesStoreError(tmp_path: Path) -> None:
    (tmp_path / 'tasks.json').write_text('{"format": 1, "tasks": {"t_x": {"title": 3}}}', encoding='utf-8')

    with pytest.raises(StoreError):
        Store(tmp_path).loadTasks()
