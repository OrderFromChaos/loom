"""Read and write one area's data files.

A Store owns one area directory (data/<area name>) holding:

    goals.json          id-keyed goals
    tasks.json          id-keyed tasks
    fixed.json          fixed events the scheduler must avoid
    plan.json           the current schedule, rewritten by every planner run
    plan_history.jsonl  append-only: blocks retired as their time passes
    events.jsonl        append-only: logged reality and stale acknowledgements
    recurring.json      id-keyed recurring templates
    recurring_steps.json  id-keyed steps: each names its template and parent step, and
                        every spawned copy gets the tree as nested subtasks
    preferences.json    per-area choices, e.g. {"format": 1, "order": "shortest-first"}

One tasks.json record:

    "t_9m2xw4ab": {"title": "Get passport photo taken", "status": "todo",
                   "parent": "t_7f3kq2vd", "goal": null, "blocked_by": [], "notes": "",
                   "estimate_minutes": 30, "deadline": null, "auto_schedule": true,
                   "priority": "high", "created": "2026-09-13T17:21:00+00:00", "completed": null,
                   "not_before": "2026-09-14T16:00:00+00:00"}

not_before, like every timestamp, is UTC; the scheduler places no block before it.

One events.jsonl line of each shape:

    {"id": "e_k2f9dq31", "kind": "did", "at": "2026-09-13T21:04:00+00:00",
     "start": "2026-09-13T19:30:00+00:00", "end": "2026-09-13T20:15:00+00:00",
     "note": "Sorted passport photos", "task": "t_9m2xw4ab", "source": "manual"}
    {"id": "e_p8s2mv7q", "kind": "ack", "at": "2026-09-15T08:30:00+00:00", "task": "t_7f3kq2vd"}

Writes are atomic: temp file, fsync, os.replace. A save refuses to overwrite a file that
changed on disk after this Store read it, so an edit made by hand or by an LLM while the
server runs is never silently lost; the caller reloads and retries. A malformed JSON file
raises StoreError; a malformed .jsonl line is skipped and tallied.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar

from loom.config import CONFIG
from loom.vocabulary import (
    DAY_NAMES,
    Area,
    EventId,
    EventKind,
    EventSource,
    FixedEvent,
    Goal,
    GoalId,
    LoggedEvent,
    Plan,
    PlanBlock,
    Preferences,
    Priority,
    Reason,
    RecurringId,
    RecurringStep,
    RecurringTask,
    Status,
    StepId,
    StoreConflictError,
    StoreError,
    Task,
    TaskId,
    TaskOrder,
    Unplaced,
)


_GOALS_FILE = 'goals.json'
_TASKS_FILE = 'tasks.json'
_RECURRING_FILE = 'recurring.json'
_STEPS_FILE = 'recurring_steps.json'
_FIXED_FILE = 'fixed.json'
_PLAN_FILE = 'plan.json'
_HISTORY_FILE = 'plan_history.jsonl'
_EVENTS_FILE = 'events.jsonl'
_PREFERENCES_FILE = 'preferences.json'
_FORMAT_VERSION = 1
_ID_ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567'
_ID_LENGTH = 8

LOG = logging.getLogger('loom')

_T = TypeVar('_T')

DEFAULT_PREFERENCES = Preferences(order=TaskOrder.DEADLINE)


def storeFor(area: Area) -> Store:
    return Store(CONFIG.data_root / area)


def newTaskId() -> TaskId:
    return TaskId('t_' + _newSlug())


def newGoalId() -> GoalId:
    return GoalId('g_' + _newSlug())


def newEventId() -> EventId:
    return EventId('e_' + _newSlug())


def newRecurringId() -> RecurringId:
    return RecurringId('r_' + _newSlug())


def newStepId() -> StepId:
    return StepId('s_' + _newSlug())


def _newSlug() -> str:
    return ''.join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


class Store:
    """Reads and writes the files of one area directory.

    Two stores coexist, one per area, and no call sees both. Load before save: the
    conflict guard compares against the file as this Store last read it.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._mtimes: dict[str, int | None] = {}
        directory.mkdir(parents=True, exist_ok=True)

    def loadGoals(self) -> dict[GoalId, Goal]:
        """Read goals.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a record is malformed.
        """
        raw = self._loadJson(_GOALS_FILE)
        if raw is None:
            return {}

        path = self.directory / _GOALS_FILE
        records = raw.get('goals')
        if not isinstance(records, dict):
            raise _reject(path, '"goals" must be an object of id-keyed records')

        return {GoalId(key): _parseGoal(GoalId(key), value, path) for key, value in records.items()}

    def saveGoals(self, goals: Mapping[GoalId, Goal]) -> None:
        payload: dict[str, object] = {
            'format': _FORMAT_VERSION,
            'goals': {goal_id: _goalToJson(goal) for goal_id, goal in goals.items()},
        }
        self._save(_GOALS_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadTasks(self) -> dict[TaskId, Task]:
        """Read tasks.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a record is malformed.
        """
        raw = self._loadJson(_TASKS_FILE)
        if raw is None:
            return {}

        path = self.directory / _TASKS_FILE
        records = raw.get('tasks')
        if not isinstance(records, dict):
            raise _reject(path, '"tasks" must be an object of id-keyed records')

        return {TaskId(key): _parseTask(TaskId(key), value, path) for key, value in records.items()}

    def saveTasks(self, tasks: Mapping[TaskId, Task]) -> None:
        payload: dict[str, object] = {
            'format': _FORMAT_VERSION,
            'tasks': {task_id: _taskToJson(task) for task_id, task in tasks.items()},
        }
        self._save(_TASKS_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadRecurring(self) -> dict[RecurringId, RecurringTask]:
        """Read recurring.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a template is malformed.
        """
        raw = self._loadJson(_RECURRING_FILE)
        if raw is None:
            return {}

        path = self.directory / _RECURRING_FILE
        records = raw.get('recurring')
        if not isinstance(records, dict):
            raise _reject(path, '"recurring" must be an object of id-keyed templates')

        return {RecurringId(key): _parseRecurring(RecurringId(key), value, path) for key, value in records.items()}

    def saveRecurring(self, templates: Mapping[RecurringId, RecurringTask]) -> None:
        payload: dict[str, object] = {
            'format': _FORMAT_VERSION,
            'recurring': {template_id: _recurringToJson(template) for template_id, template in templates.items()},
        }
        self._save(_RECURRING_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadSteps(self) -> dict[StepId, RecurringStep]:
        """Read recurring_steps.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a step is malformed.
        """
        raw = self._loadJson(_STEPS_FILE)
        if raw is None:
            return {}

        path = self.directory / _STEPS_FILE
        records = raw.get('steps')
        if not isinstance(records, dict):
            raise _reject(path, '"steps" must be an object of id-keyed steps')

        return {StepId(key): _parseStep(StepId(key), value, path) for key, value in records.items()}

    def saveSteps(self, steps: Mapping[StepId, RecurringStep]) -> None:
        payload: dict[str, object] = {
            'format': _FORMAT_VERSION,
            'steps': {step_id: _stepToJson(step) for step_id, step in steps.items()},
        }
        self._save(_STEPS_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadFixed(self) -> tuple[FixedEvent, ...]:
        """Read fixed.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or an event is malformed.
        """
        raw = self._loadJson(_FIXED_FILE)
        if raw is None:
            return ()

        path = self.directory / _FIXED_FILE
        records = raw.get('events')
        if not isinstance(records, list):
            raise _reject(path, '"events" must be a list')

        return tuple(_parseFixed(value, path) for value in records)

    def loadPlan(self) -> Plan | None:
        """The current plan, or None before the first planner run.

        Raises:
            StoreError: plan.json is unreadable, not JSON, or a block is malformed.
        """
        raw = self._loadJson(_PLAN_FILE)
        if raw is None:
            return None

        path = self.directory / _PLAN_FILE
        raw_blocks = raw.get('blocks')
        raw_unplaced = raw.get('unplaced')
        if not isinstance(raw_blocks, list) or not isinstance(raw_unplaced, list):
            raise _reject(path, '"blocks" and "unplaced" must be lists')

        try:
            generated = _timestamp(raw, 'generated')
            blocks = tuple(_parseBlock(value) for value in raw_blocks)
            unplaced = tuple(_parseUnplaced(value) for value in raw_unplaced)
        except ValueError as exc:
            raise _reject(path, str(exc)) from exc

        raw_at_risk = raw.get('at_risk', [])
        if not isinstance(raw_at_risk, list) or not all(isinstance(entry, str) for entry in raw_at_risk):
            raise _reject(path, '"at_risk" must be a list of task ids')

        return Plan(
            generated=generated,
            blocks=blocks,
            unplaced=unplaced,
            at_risk=tuple(TaskId(entry) for entry in raw_at_risk),
        )

    def savePlan(self, plan: Plan) -> None:
        payload: dict[str, object] = {
            'format': _FORMAT_VERSION,
            'generated': plan.generated.isoformat(),
            'blocks': [_blockToJson(block) for block in plan.blocks],
            'unplaced': [{'task': entry.task, 'reason': entry.reason.value} for entry in plan.unplaced],
            'at_risk': list(plan.at_risk),
        }
        self._save(_PLAN_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadPreferences(self) -> Preferences:
        """Read preferences.json; the defaults when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a value is malformed.
        """
        raw = self._loadJson(_PREFERENCES_FILE)
        if raw is None:
            return DEFAULT_PREFERENCES

        try:
            return Preferences(order=TaskOrder(_string(raw, 'order')))
        except ValueError as exc:
            raise _reject(self.directory / _PREFERENCES_FILE, str(exc)) from exc

    def savePreferences(self, preferences: Preferences) -> None:
        payload: dict[str, object] = {'format': _FORMAT_VERSION, 'order': preferences.order.value}
        self._save(_PREFERENCES_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

    def loadPlanHistory(self) -> tuple[PlanBlock, ...]:
        return self._loadJsonl(_HISTORY_FILE, _parseBlock)

    def retireBlocks(self, blocks: Iterable[PlanBlock], retired_at: datetime) -> None:
        rows = []
        for block in blocks:
            row = _blockToJson(block)
            row['retired'] = retired_at.isoformat()
            rows.append(row)

        if rows:
            self._append(_HISTORY_FILE, rows)

    def loadEvents(self) -> tuple[LoggedEvent, ...]:
        return self._loadJsonl(_EVENTS_FILE, _parseEvent)

    def appendEvent(self, event: LoggedEvent) -> None:
        self._append(_EVENTS_FILE, [_eventToJson(event)])

    def rewriteEvents(self, events: Iterable[LoggedEvent]) -> None:
        lines = [json.dumps(_eventToJson(event), ensure_ascii=False) for event in events]
        self._save(_EVENTS_FILE, ''.join(line + '\n' for line in lines))

    def _loadJson(self, name: str) -> dict[str, object] | None:
        """Read one JSON file and record its mtime, None when the file does not exist.

        Raises:
            StoreError: the file is unreadable, not JSON, or not this format version.
        """
        path = self.directory / name
        if not path.is_file():
            self._mtimes[name] = None
            return None

        self._mtimes[name] = path.stat().st_mtime_ns
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            raise _reject(path, f'unreadable: {exc}') from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _reject(path, f'not valid JSON: {exc}') from exc

        if not isinstance(parsed, dict):
            raise _reject(path, 'top level must be a JSON object')
        if parsed.get('format') != _FORMAT_VERSION:
            raise _reject(path, f'unsupported format {parsed.get("format")!r} (expected {_FORMAT_VERSION})')

        return parsed

    def _loadJsonl(self, name: str, parse_line: Callable[[object], _T]) -> tuple[_T, ...]:
        """Read one JSONL file through parse_line, skipping and tallying malformed lines.

        Raises:
            StoreError: the file is unreadable.
        """
        path = self.directory / name
        if not path.is_file():
            self._mtimes[name] = None
            return ()

        self._mtimes[name] = path.stat().st_mtime_ns
        try:
            # split on '\n' alone: splitlines() also splits on U+0085 and U+2028, which
            # json.dumps(ensure_ascii=False) writes raw inside string values
            lines = path.read_text(encoding='utf-8').split('\n')
        except OSError as exc:
            raise _reject(path, f'unreadable: {exc}') from exc

        # degrade and report: keep every parseable line, tally the rest
        parsed: list[_T] = []
        rejected = 0
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                parsed.append(parse_line(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                LOG.debug('store.rejected_line', extra={'file': str(path), 'line': number, 'reason': str(exc)})
                rejected += 1

        if rejected:
            LOG.warning('store.rejected_lines', extra={'file': str(path), 'rejected': rejected, 'total': len(lines)})

        return tuple(parsed)

    def _save(self, name: str, text: str) -> None:
        """Raises:
        StoreConflictError: the file changed on disk after this Store read it.
        """
        path = self.directory / name
        current = path.stat().st_mtime_ns if path.is_file() else None
        if current != self._mtimes.get(name):
            raise _rejectConflict(path)

        temp_path = path.with_name(path.name + '.tmp')
        with temp_path.open('w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)

        self._mtimes[name] = path.stat().st_mtime_ns

    def _append(self, name: str, rows: Iterable[dict[str, object]]) -> None:
        path = self.directory / name
        with path.open('a', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

        self._mtimes[name] = path.stat().st_mtime_ns


def _parseGoal(goal_id: GoalId, raw: object, path: Path) -> Goal:
    if not isinstance(raw, dict):
        raise _reject(path, f'{goal_id}: record must be an object')

    try:
        return Goal(
            id=goal_id,
            title=_string(raw, 'title'),
            notes=_optionalString(raw, 'notes') or '',
            created=_timestamp(raw, 'created'),
            completed=_optionalTimestamp(raw, 'completed'),
            position=_optionalPositiveInt(raw, 'position'),
        )
    except ValueError as exc:
        raise _reject(path, f'{goal_id}: {exc}') from exc


def _parseTask(task_id: TaskId, raw: object, path: Path) -> Task:
    """Construct a Task from one tasks.json record.

    Raises:
        StoreError: a field is missing or malformed; the message names the task and field.
    """
    if not isinstance(raw, dict):
        raise _reject(path, f'{task_id}: record must be an object')

    try:
        parent = _optionalString(raw, 'parent')
        goal = _optionalString(raw, 'goal')
        origin = _optionalString(raw, 'origin')
        return Task(
            id=task_id,
            title=_string(raw, 'title'),
            status=Status(_string(raw, 'status')),
            parent=TaskId(parent) if parent is not None else None,
            goal=GoalId(goal) if goal is not None else None,
            blocked_by=_taskIds(raw, 'blocked_by'),
            notes=_optionalString(raw, 'notes') or '',
            estimate_minutes=_optionalPositiveInt(raw, 'estimate_minutes'),
            deadline=_optionalDate(raw, 'deadline'),
            auto_schedule=_requireBool(raw, 'auto_schedule'),
            priority=Priority(_string(raw, 'priority')),
            created=_timestamp(raw, 'created'),
            completed=_optionalTimestamp(raw, 'completed'),
            origin=RecurringId(origin) if origin is not None else None,
            color=_optionalString(raw, 'color'),
            not_before=_optionalTimestamp(raw, 'not_before'),
        )
    except ValueError as exc:
        raise _reject(path, f'{task_id}: {exc}') from exc


def _parseRecurring(recurring_id: RecurringId, raw: object, path: Path) -> RecurringTask:
    """Construct a RecurringTask from one recurring.json record.

    Raises:
        StoreError: a field is missing or malformed; the message names the template.
    """
    if not isinstance(raw, dict):
        raise _reject(path, f'{recurring_id}: record must be an object')

    try:
        goal = _optionalString(raw, 'goal')
        deadline_after = raw.get('deadline_after_days')
        if deadline_after is not None and (
            not isinstance(deadline_after, int) or isinstance(deadline_after, bool) or deadline_after < 0
        ):
            raise ValueError("'deadline_after_days' must be a whole number of days or null")
        day_numbers = _dayNumbers(raw, 'days')
        every = _optionalPositiveInt(raw, 'every_days')
        if bool(day_numbers) == (every is not None):
            raise ValueError("exactly one of 'days' and 'every_days' must be set")
        next_due = _optionalDate(raw, 'next_due')
        if next_due is not None and every is None:
            raise ValueError("'next_due' applies to 'every_days' templates only; anchor days set their own dates")
        return RecurringTask(
            id=recurring_id,
            title=_string(raw, 'title'),
            notes=_optionalString(raw, 'notes') or '',
            estimate_minutes=_optionalPositiveInt(raw, 'estimate_minutes'),
            auto_schedule=_requireBool(raw, 'auto_schedule'),
            priority=Priority(_string(raw, 'priority')),
            goal=GoalId(goal) if goal is not None else None,
            days=day_numbers,
            every_days=every,
            deadline_after_days=deadline_after,
            # records written before the toggle existed keep their old behaviour: an
            # interval template waited for completion, an anchored one stacked copies
            wait_for_completion=_optionalBool(raw, 'wait_for_completion', default=every is not None),
            created=_timestamp(raw, 'created'),
            last_added=_optionalTimestamp(raw, 'last_added'),
            next_due=next_due,
        )
    except ValueError as exc:
        raise _reject(path, f'{recurring_id}: {exc}') from exc


def _parseStep(step_id: StepId, raw: object, path: Path) -> RecurringStep:
    """Construct a RecurringStep from one recurring_steps.json record.

    Raises:
        StoreError: a field is missing or malformed; the message names the step.
    """
    if not isinstance(raw, dict):
        raise _reject(path, f'{step_id}: record must be an object')

    try:
        parent = _optionalString(raw, 'parent')
        return RecurringStep(
            id=step_id,
            template=RecurringId(_string(raw, 'template')),
            parent=StepId(parent) if parent is not None else None,
            title=_string(raw, 'title'),
            estimate_minutes=_optionalPositiveInt(raw, 'estimate_minutes'),
            auto_schedule=_requireBool(raw, 'auto_schedule'),
            created=_timestamp(raw, 'created'),
        )
    except ValueError as exc:
        raise _reject(path, f'{step_id}: {exc}') from exc


def _parseFixed(raw: object, path: Path) -> FixedEvent:
    if not isinstance(raw, dict):
        raise _reject(path, 'fixed event must be an object')

    try:
        event = FixedEvent(title=_string(raw, 'title'), start=_timestamp(raw, 'start'), end=_timestamp(raw, 'end'))
    except ValueError as exc:
        raise _reject(path, f'fixed event: {exc}') from exc

    if event.end <= event.start:
        raise _reject(path, f'fixed event {event.title!r} ends before it starts')

    return event


def _parseBlock(raw: object) -> PlanBlock:
    if not isinstance(raw, dict):
        raise ValueError('block must be an object')

    block = PlanBlock(
        task=TaskId(_string(raw, 'task')),
        start=_timestamp(raw, 'start'),
        end=_timestamp(raw, 'end'),
        chunk=_requirePositiveInt(raw, 'chunk'),
        chunks=_requirePositiveInt(raw, 'chunks'),
    )
    if block.end <= block.start:
        raise ValueError(f'block for {block.task} ends before it starts')

    return block


def _parseUnplaced(raw: object) -> Unplaced:
    if not isinstance(raw, dict):
        raise ValueError('unplaced entry must be an object')

    return Unplaced(task=TaskId(_string(raw, 'task')), reason=Reason(_string(raw, 'reason')))


def _parseEvent(raw: object) -> LoggedEvent:
    """Construct a LoggedEvent from one events.jsonl line.

    An ack carries no interval and no note; a did must carry both timestamps.

    Raises:
        ValueError: a field is missing or malformed.
    """
    if not isinstance(raw, dict):
        raise ValueError('event must be an object')

    kind = EventKind(_string(raw, 'kind'))
    task_text = _optionalString(raw, 'task')
    task = TaskId(task_text) if task_text is not None else None
    match kind:
        case EventKind.DID:
            start = _timestamp(raw, 'start')
            end = _timestamp(raw, 'end')
            if end <= start:
                raise ValueError('event ends before it starts')
            return LoggedEvent(
                id=EventId(_string(raw, 'id')),
                kind=kind,
                at=_timestamp(raw, 'at'),
                start=start,
                end=end,
                note=_optionalString(raw, 'note') or '',
                task=task,
                source=EventSource(_string(raw, 'source')),
            )
        case EventKind.ACK:
            if task is None:
                raise ValueError('an ack must name a task')
            return LoggedEvent(
                id=EventId(_string(raw, 'id')),
                kind=kind,
                at=_timestamp(raw, 'at'),
                start=None,
                end=None,
                note='',
                task=task,
                source=EventSource.MANUAL,
            )


def _goalToJson(goal: Goal) -> dict[str, object]:
    return {
        'title': goal.title,
        'notes': goal.notes,
        'created': goal.created.isoformat(),
        'completed': goal.completed.isoformat() if goal.completed else None,
        'position': goal.position,
    }


def _taskToJson(task: Task) -> dict[str, object]:
    return {
        'title': task.title,
        'status': task.status.value,
        'parent': task.parent,
        'goal': task.goal,
        'blocked_by': list(task.blocked_by),
        'notes': task.notes,
        'estimate_minutes': task.estimate_minutes,
        'deadline': task.deadline.isoformat() if task.deadline else None,
        'auto_schedule': task.auto_schedule,
        'priority': task.priority.value,
        'created': task.created.isoformat(),
        'completed': task.completed.isoformat() if task.completed else None,
        'origin': task.origin,
        'color': task.color,
        'not_before': task.not_before.isoformat() if task.not_before else None,
    }


def _recurringToJson(template: RecurringTask) -> dict[str, object]:
    return {
        'title': template.title,
        'notes': template.notes,
        'estimate_minutes': template.estimate_minutes,
        'auto_schedule': template.auto_schedule,
        'priority': template.priority.value,
        'goal': template.goal,
        'days': [DAY_NAMES[number] for number in sorted(template.days)],
        'every_days': template.every_days,
        'deadline_after_days': template.deadline_after_days,
        'wait_for_completion': template.wait_for_completion,
        'created': template.created.isoformat(),
        'last_added': template.last_added.isoformat() if template.last_added else None,
        'next_due': template.next_due.isoformat() if template.next_due else None,
    }


def _stepToJson(step: RecurringStep) -> dict[str, object]:
    return {
        'template': step.template,
        'parent': step.parent,
        'title': step.title,
        'estimate_minutes': step.estimate_minutes,
        'auto_schedule': step.auto_schedule,
        'created': step.created.isoformat(),
    }


def _blockToJson(block: PlanBlock) -> dict[str, object]:
    return {
        'task': block.task,
        'start': block.start.isoformat(),
        'end': block.end.isoformat(),
        'chunk': block.chunk,
        'chunks': block.chunks,
    }


def _eventToJson(event: LoggedEvent) -> dict[str, object]:
    match event.kind:
        case EventKind.DID:
            return {
                'id': event.id,
                'kind': event.kind.value,
                'at': event.at.isoformat(),
                'start': event.start.isoformat() if event.start else None,
                'end': event.end.isoformat() if event.end else None,
                'note': event.note,
                'task': event.task,
                'source': event.source.value,
            }
        case EventKind.ACK:
            return {'id': event.id, 'kind': event.kind.value, 'at': event.at.isoformat(), 'task': event.task}


def _string(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f'{key!r} must be a string')

    return value


def _optionalString(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f'{key!r} must be a string or null')

    return value


def _requireBool(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if not isinstance(value, bool):
        raise ValueError(f'{key!r} must be true or false')

    return value


def _optionalBool(record: Mapping[str, object], key: str, *, default: bool) -> bool:
    if key not in record:
        return default

    return _requireBool(record, key)


def _requirePositiveInt(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f'{key!r} must be a positive integer')

    return value


def _optionalPositiveInt(record: Mapping[str, object], key: str) -> int | None:
    if record.get(key) is None:
        return None

    return _requirePositiveInt(record, key)


def _timestamp(record: Mapping[str, object], key: str) -> datetime:
    text = _string(record, key)
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'{key!r}: {text!r} is not an ISO-8601 timestamp') from exc

    if value.tzinfo is None:
        raise ValueError(f'{key!r}: {text!r} has no timezone')

    return value.astimezone(UTC)


def _optionalTimestamp(record: Mapping[str, object], key: str) -> datetime | None:
    if record.get(key) is None:
        return None

    return _timestamp(record, key)


def _optionalDate(record: Mapping[str, object], key: str) -> date | None:
    if record.get(key) is None:
        return None

    text = _string(record, key)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'{key!r}: {text!r} is not a YYYY-MM-DD date') from exc


def _dayNumbers(record: Mapping[str, object], key: str) -> frozenset[int]:
    value = record.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(f'{key!r} must be a list of day names')

    numbers = set()
    for name in value:
        if not isinstance(name, str) or name not in DAY_NAMES:
            raise ValueError(f'{key!r} entries must be one of {", ".join(DAY_NAMES)}')
        numbers.add(DAY_NAMES.index(name))

    return frozenset(numbers)


def _taskIds(record: Mapping[str, object], key: str) -> tuple[TaskId, ...]:
    value = record.get(key)
    if not isinstance(value, list):
        raise ValueError(f'{key!r} must be a list of task ids')

    ids = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f'{key!r} must be a list of task ids')
        ids.append(TaskId(item))

    return tuple(ids)


def _reject(path: Path, message: str) -> StoreError:
    LOG.warning('store.rejected', extra={'file': str(path), 'reason': message})
    return StoreError(f'{path}: {message}')


def _rejectConflict(path: Path) -> StoreConflictError:
    LOG.warning('store.conflict', extra={'file': str(path)})
    return StoreConflictError(f'{path} changed on disk after it was loaded; reload and retry')
