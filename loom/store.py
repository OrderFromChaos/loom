"""Read and write the files in one area's data directory.

One area directory, data/<area name>, has these files:

    goals.json          id-keyed goals
    tasks.json          id-keyed tasks
    fixed.json          fixed events the scheduler must avoid
    plan.json           the current schedule, rewritten by every planner run
    plan_history.jsonl  append-only: blocks retired as their time passes
    events.jsonl        append-only: logged reality and stale acknowledgements
    recurring.json      id-keyed recurring templates
    recurring_steps.json  id-keyed steps, each with its template and parent step
    preferences.json    per-area choices, e.g. {"format": 1, "order": "shortest-first"}

One tasks.json record:

    "t_9m2xw4ab": {"title": "Get passport photo taken", "status": "todo",
                   "parent": "t_7f3kq2vd", "goal": null, "blocked_by": [], "notes": "",
                   "estimate_minutes": 30, "deadline": null, "auto_schedule": true,
                   "priority": "high", "created": "2026-09-13T17:21:00+00:00", "completed": null,
                   "not_before": "2026-09-14T16:00:00+00:00"}

not_before, like every timestamp, is UTC.

One events.jsonl line of each shape:

    {"id": "e_k2f9dq31", "kind": "did", "at": "2026-09-13T21:04:00+00:00",
     "start": "2026-09-13T19:30:00+00:00", "end": "2026-09-13T20:15:00+00:00",
     "note": "Sorted passport photos", "task": "t_9m2xw4ab", "source": "manual"}
    {"id": "e_p8s2mv7q", "kind": "ack", "at": "2026-09-15T08:30:00+00:00", "task": "t_7f3kq2vd"}

Writes are atomic: temp file, fsync, os.replace. A save raises StoreConflictError when the
file changed on disk after this Store read it, so an edit made by hand or by an LLM while
the server runs is never lost. A malformed JSON file raises StoreError. A malformed .jsonl
line is skipped and tallied.
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

LOG = logging.getLogger('loom')

_DEFAULT_PREFERENCES = Preferences(order=TaskOrder.DEADLINE)


def openStore(area: Area) -> Store:
    return Store(CONFIG.data_root / area)


def generateTaskId() -> TaskId:
    return TaskId('t_' + _generateSlug())


def generateGoalId() -> GoalId:
    return GoalId('g_' + _generateSlug())


def generateEventId() -> EventId:
    return EventId('e_' + _generateSlug())


def generateRecurringId() -> RecurringId:
    return RecurringId('r_' + _generateSlug())


def generateStepId() -> StepId:
    return StepId('s_' + _generateSlug())


def _generateSlug() -> str:
    ID_ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567'
    ID_LENGTH = 8
    return ''.join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


class Store:
    """Reads and writes the files of one area directory.

    A save compares the file with the version this Store last read, so load before save.
    """

    def __init__(self, area_directory: Path) -> None:
        self.area_directory = area_directory
        self._mtimes: dict[str, int | None] = {}
        area_directory.mkdir(parents=True, exist_ok=True)

    def loadGoals(self) -> dict[GoalId, Goal]:
        """Read goals.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a record is malformed.
        """
        return {goal.id: goal for goal in self._loadRecords(_GOALS_FILE, 'goals', _parseGoal)}

    def saveGoals(self, goals: Mapping[GoalId, Goal]) -> None:
        self._saveJson(_GOALS_FILE, {'goals': {goal_id: _serializeGoal(goal) for goal_id, goal in goals.items()}})

    def loadTasks(self) -> dict[TaskId, Task]:
        """Read tasks.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a record is malformed.
        """
        return {task.id: task for task in self._loadRecords(_TASKS_FILE, 'tasks', _parseTask)}

    def saveTasks(self, tasks: Mapping[TaskId, Task]) -> None:
        self._saveJson(_TASKS_FILE, {'tasks': {task_id: _serializeTask(task) for task_id, task in tasks.items()}})

    def loadTemplates(self) -> dict[RecurringId, RecurringTask]:
        """Read recurring.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a template is malformed.
        """
        return {template.id: template for template in self._loadRecords(_RECURRING_FILE, 'recurring', _parseTemplate)}

    def saveTemplates(self, templates: Mapping[RecurringId, RecurringTask]) -> None:
        records = {template_id: _serializeTemplate(template) for template_id, template in templates.items()}
        self._saveJson(_RECURRING_FILE, {'recurring': records})

    def loadSteps(self) -> dict[StepId, RecurringStep]:
        """Read recurring_steps.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a step is malformed.
        """
        return {step.id: step for step in self._loadRecords(_STEPS_FILE, 'steps', _parseStep)}

    def saveSteps(self, steps: Mapping[StepId, RecurringStep]) -> None:
        self._saveJson(_STEPS_FILE, {'steps': {step_id: _serializeStep(step) for step_id, step in steps.items()}})

    def loadFixed(self) -> tuple[FixedEvent, ...]:
        """Read fixed.json, empty when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or an event is malformed.
        """
        raw = self._loadJson(_FIXED_FILE)
        if raw is None:
            return ()

        data_path = self.area_directory / _FIXED_FILE
        records = raw.get('events')
        if not isinstance(records, list):
            raise _reject(data_path, '"events" must be a list')

        return tuple(_parseFixed(raw_record, data_path) for raw_record in records)

    def loadPlan(self) -> Plan | None:
        """Read plan.json, None before the first planner run.

        Raises:
            StoreError: plan.json is unreadable, not JSON, or a block is malformed.
        """
        raw = self._loadJson(_PLAN_FILE)
        if raw is None:
            return None

        data_path = self.area_directory / _PLAN_FILE
        raw_blocks = raw.get('blocks')
        raw_unplaced = raw.get('unplaced')
        if not isinstance(raw_blocks, list) or not isinstance(raw_unplaced, list):
            raise _reject(data_path, '"blocks" and "unplaced" must be lists')

        try:
            generated = _readTimestamp(raw, 'generated')
            blocks = tuple(_parseBlock(raw_record) for raw_record in raw_blocks)
            unplaced = tuple(_parseUnplaced(raw_record) for raw_record in raw_unplaced)
        except ValueError as exc:
            raise _reject(data_path, str(exc)) from exc

        raw_at_risk = raw.get('at_risk', [])
        if not isinstance(raw_at_risk, list) or not all(isinstance(entry, str) for entry in raw_at_risk):
            raise _reject(data_path, '"at_risk" must be a list of task ids')

        return Plan(
            generated=generated,
            blocks=blocks,
            unplaced=unplaced,
            at_risk=tuple(TaskId(entry) for entry in raw_at_risk),
        )

    def savePlan(self, plan: Plan) -> None:
        payload: dict[str, object] = {
            'generated': plan.generated.isoformat(),
            'blocks': [_serializeBlock(block) for block in plan.blocks],
            'unplaced': [{'task': entry.task, 'reason': entry.reason.value} for entry in plan.unplaced],
            'at_risk': list(plan.at_risk),
        }
        self._saveJson(_PLAN_FILE, payload)

    def loadPreferences(self) -> Preferences:
        """Read preferences.json, the defaults when the file does not exist yet.

        Raises:
            StoreError: the file is unreadable, not JSON, or a value is malformed.
        """
        raw = self._loadJson(_PREFERENCES_FILE)
        if raw is None:
            return _DEFAULT_PREFERENCES

        try:
            return Preferences(order=TaskOrder(_readString(raw, 'order')))
        except ValueError as exc:
            raise _reject(self.area_directory / _PREFERENCES_FILE, str(exc)) from exc

    def savePreferences(self, preferences: Preferences) -> None:
        self._saveJson(_PREFERENCES_FILE, {'order': preferences.order.value})

    def loadPlanHistory(self) -> tuple[PlanBlock, ...]:
        return self._loadJsonl(_HISTORY_FILE, _parseBlock)

    def retireBlocks(self, blocks: Iterable[PlanBlock], retired_at: datetime) -> None:
        rows = []
        for block in blocks:
            row = _serializeBlock(block)
            row['retired'] = retired_at.isoformat()
            rows.append(row)

        if rows:
            self._append(_HISTORY_FILE, rows)

    def loadEvents(self) -> tuple[LoggedEvent, ...]:
        return self._loadJsonl(_EVENTS_FILE, _parseEvent)

    def appendEvent(self, event: LoggedEvent) -> None:
        self._append(_EVENTS_FILE, [_serializeEvent(event)])

    def rewriteEvents(self, events: Iterable[LoggedEvent]) -> None:
        lines = [json.dumps(_serializeEvent(event), ensure_ascii=False) for event in events]
        self._save(_EVENTS_FILE, ''.join(line + '\n' for line in lines))

    def _loadRecords(
        self,
        file_name: str,
        key: str,
        parse_record: Callable[[str, object, Path], _T],
    ) -> tuple[_T, ...]:
        """Read the id-keyed records under `key` of one JSON file, in file order.

        Raises:
            StoreError: the file is unreadable, not JSON, or a record is malformed.
        """
        raw = self._loadJson(file_name)
        if raw is None:
            return ()

        data_path = self.area_directory / file_name
        records = raw.get(key)
        if not isinstance(records, dict):
            raise _reject(data_path, f'"{key}" must be an object of id-keyed records')

        return tuple(parse_record(raw_id, raw_record, data_path) for raw_id, raw_record in records.items())

    def _loadJson(self, file_name: str) -> dict[str, object] | None:
        """Read one JSON file and record its mtime, None when the file does not exist.

        Raises:
            StoreError: the file is unreadable, not JSON, or not this format version.
        """
        data_path = self.area_directory / file_name
        if not data_path.is_file():
            self._mtimes[file_name] = None
            return None

        self._mtimes[file_name] = data_path.stat().st_mtime_ns
        try:
            file_text = data_path.read_text(encoding='utf-8')
        except OSError as exc:
            raise _reject(data_path, f'unreadable: {exc}') from exc

        try:
            parsed = json.loads(file_text)
        except json.JSONDecodeError as exc:
            raise _reject(data_path, f'not valid JSON: {exc}') from exc

        if not isinstance(parsed, dict):
            raise _reject(data_path, 'top level must be a JSON object')
        if parsed.get('format') != _FORMAT_VERSION:
            raise _reject(data_path, f'unsupported format {parsed.get("format")!r} (expected {_FORMAT_VERSION})')

        return parsed

    def _loadJsonl(self, file_name: str, parse_line: Callable[[object], _T]) -> tuple[_T, ...]:
        """Read one JSONL file through parse_line, skipping and tallying malformed lines.

        Raises:
            StoreError: the file is unreadable.
        """
        data_path = self.area_directory / file_name
        if not data_path.is_file():
            self._mtimes[file_name] = None
            return ()

        self._mtimes[file_name] = data_path.stat().st_mtime_ns
        try:
            # json.dumps(ensure_ascii=False) writes U+0085 and U+2028 raw inside strings, and
            # splitlines() splits on both. Split on '\n' alone
            lines = data_path.read_text(encoding='utf-8').split('\n')
        except OSError as exc:
            raise _reject(data_path, f'unreadable: {exc}') from exc

        parsed: list[_T] = []
        rejected = 0
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                parsed.append(parse_line(json.loads(line)))
            # json.JSONDecodeError is a ValueError
            except ValueError as exc:
                LOG.debug('store.rejected_line', extra={'file': str(data_path), 'line': number, 'reason': str(exc)})
                rejected += 1

        if rejected:
            LOG.warning(
                'store.rejected_lines',
                extra={'file': str(data_path), 'rejected': rejected, 'total': len(lines)},
            )

        return tuple(parsed)

    def _saveJson(self, file_name: str, payload: Mapping[str, object]) -> None:
        self._save(file_name, json.dumps({'format': _FORMAT_VERSION, **payload}, indent=2, ensure_ascii=False) + '\n')

    def _save(self, file_name: str, file_text: str) -> None:
        """Write one file atomically.

        Raises:
            StoreConflictError: the file changed on disk after this Store read it.
        """
        data_path = self.area_directory / file_name
        current = data_path.stat().st_mtime_ns if data_path.is_file() else None
        if current != self._mtimes.get(file_name):
            raise _rejectConflict(data_path)

        temp_path = data_path.with_name(data_path.name + '.tmp')
        with temp_path.open('w', encoding='utf-8') as handle:
            handle.write(file_text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(data_path)

        self._mtimes[file_name] = data_path.stat().st_mtime_ns

    def _append(self, file_name: str, rows: Iterable[dict[str, object]]) -> None:
        data_path = self.area_directory / file_name
        with data_path.open('a', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

        self._mtimes[file_name] = data_path.stat().st_mtime_ns


def _parseGoal(raw_id: str, raw_record: object, data_path: Path) -> Goal:
    if not isinstance(raw_record, dict):
        raise _reject(data_path, f'{raw_id}: record must be an object')

    try:
        return Goal(
            id=GoalId(raw_id),
            title=_readString(raw_record, 'title'),
            notes=_readOptionalString(raw_record, 'notes') or '',
            created=_readTimestamp(raw_record, 'created'),
            completed=_readOptionalTimestamp(raw_record, 'completed'),
            position=_readOptionalPositiveInt(raw_record, 'position'),
        )
    except ValueError as exc:
        raise _reject(data_path, f'{raw_id}: {exc}') from exc


def _parseTask(raw_id: str, raw_record: object, data_path: Path) -> Task:
    """Construct a Task from one tasks.json record.

    Raises:
        StoreError: a field is absent or malformed. The message has the task id and the field.
    """
    if not isinstance(raw_record, dict):
        raise _reject(data_path, f'{raw_id}: record must be an object')

    try:
        parent = _readOptionalString(raw_record, 'parent')
        goal = _readOptionalString(raw_record, 'goal')
        origin = _readOptionalString(raw_record, 'origin')
        return Task(
            id=TaskId(raw_id),
            title=_readString(raw_record, 'title'),
            status=Status(_readString(raw_record, 'status')),
            parent=TaskId(parent) if parent is not None else None,
            goal=GoalId(goal) if goal is not None else None,
            blocked_by=_readTaskIds(raw_record, 'blocked_by'),
            notes=_readOptionalString(raw_record, 'notes') or '',
            estimate_minutes=_readOptionalPositiveInt(raw_record, 'estimate_minutes'),
            deadline=_readOptionalDate(raw_record, 'deadline'),
            auto_schedule=_readBool(raw_record, 'auto_schedule'),
            priority=Priority(_readString(raw_record, 'priority')),
            created=_readTimestamp(raw_record, 'created'),
            completed=_readOptionalTimestamp(raw_record, 'completed'),
            origin=RecurringId(origin) if origin is not None else None,
            color=_readOptionalString(raw_record, 'color'),
            not_before=_readOptionalTimestamp(raw_record, 'not_before'),
        )
    except ValueError as exc:
        raise _reject(data_path, f'{raw_id}: {exc}') from exc


def _parseTemplate(raw_id: str, raw_record: object, data_path: Path) -> RecurringTask:
    """Construct a RecurringTask from one recurring.json record.

    Raises:
        StoreError: a field is absent or malformed. The message has the template id.
    """
    if not isinstance(raw_record, dict):
        raise _reject(data_path, f'{raw_id}: record must be an object')

    try:
        goal = _readOptionalString(raw_record, 'goal')
        day_numbers = _readDayNumbers(raw_record, 'days')
        every = _readOptionalPositiveInt(raw_record, 'every_days')
        if bool(day_numbers) == (every is not None):
            raise ValueError("exactly one of 'days' and 'every_days' must be set")
        next_due = _readOptionalDate(raw_record, 'next_due')
        if next_due is not None and every is None:
            raise ValueError("'next_due' applies to 'every_days' templates only. Anchor days set their own dates")
        return RecurringTask(
            id=RecurringId(raw_id),
            title=_readString(raw_record, 'title'),
            notes=_readOptionalString(raw_record, 'notes') or '',
            estimate_minutes=_readOptionalPositiveInt(raw_record, 'estimate_minutes'),
            auto_schedule=_readBool(raw_record, 'auto_schedule'),
            priority=Priority(_readString(raw_record, 'priority')),
            goal=GoalId(goal) if goal is not None else None,
            days=day_numbers,
            every_days=every,
            deadline_after_days=_readOptionalDayCount(raw_record, 'deadline_after_days'),
            # a record without the field predates it. Such an interval template waits for
            # completion, and an anchored one stacks copies
            wait_for_completion=_readOptionalBool(raw_record, 'wait_for_completion', default=every is not None),
            created=_readTimestamp(raw_record, 'created'),
            last_added=_readOptionalTimestamp(raw_record, 'last_added'),
            next_due=next_due,
        )
    except ValueError as exc:
        raise _reject(data_path, f'{raw_id}: {exc}') from exc


def _parseStep(raw_id: str, raw_record: object, data_path: Path) -> RecurringStep:
    """Construct a RecurringStep from one recurring_steps.json record.

    Raises:
        StoreError: a field is absent or malformed. The message has the step id.
    """
    if not isinstance(raw_record, dict):
        raise _reject(data_path, f'{raw_id}: record must be an object')

    try:
        parent = _readOptionalString(raw_record, 'parent')
        return RecurringStep(
            id=StepId(raw_id),
            template=RecurringId(_readString(raw_record, 'template')),
            parent=StepId(parent) if parent is not None else None,
            title=_readString(raw_record, 'title'),
            estimate_minutes=_readOptionalPositiveInt(raw_record, 'estimate_minutes'),
            auto_schedule=_readBool(raw_record, 'auto_schedule'),
            created=_readTimestamp(raw_record, 'created'),
        )
    except ValueError as exc:
        raise _reject(data_path, f'{raw_id}: {exc}') from exc


def _parseFixed(raw_record: object, data_path: Path) -> FixedEvent:
    if not isinstance(raw_record, dict):
        raise _reject(data_path, 'fixed event must be an object')

    try:
        event = FixedEvent(
            title=_readString(raw_record, 'title'),
            start=_readTimestamp(raw_record, 'start'),
            end=_readTimestamp(raw_record, 'end'),
        )
    except ValueError as exc:
        raise _reject(data_path, f'fixed event: {exc}') from exc

    if event.end <= event.start:
        raise _reject(data_path, f'fixed event {event.title!r} ends before it starts')

    return event


def _parseBlock(raw_record: object) -> PlanBlock:
    if not isinstance(raw_record, dict):
        raise ValueError('block must be an object')

    block = PlanBlock(
        task=TaskId(_readString(raw_record, 'task')),
        start=_readTimestamp(raw_record, 'start'),
        end=_readTimestamp(raw_record, 'end'),
        chunk=_readPositiveInt(raw_record, 'chunk'),
        chunks=_readPositiveInt(raw_record, 'chunks'),
    )
    if block.end <= block.start:
        raise ValueError(f'block for {block.task} ends before it starts')

    return block


def _parseUnplaced(raw_record: object) -> Unplaced:
    if not isinstance(raw_record, dict):
        raise ValueError('unplaced entry must be an object')

    return Unplaced(task=TaskId(_readString(raw_record, 'task')), reason=Reason(_readString(raw_record, 'reason')))


def _parseEvent(raw_record: object) -> LoggedEvent:
    """Construct a LoggedEvent from one events.jsonl line.

    An ack has no interval and no note. A did has both timestamps.

    Raises:
        ValueError: a field is absent or malformed.
    """
    if not isinstance(raw_record, dict):
        raise ValueError('event must be an object')

    kind = EventKind(_readString(raw_record, 'kind'))
    task_text = _readOptionalString(raw_record, 'task')
    task = TaskId(task_text) if task_text is not None else None
    match kind:
        case EventKind.DID:
            start = _readTimestamp(raw_record, 'start')
            end = _readTimestamp(raw_record, 'end')
            if end <= start:
                raise ValueError('event ends before it starts')
            return LoggedEvent(
                id=EventId(_readString(raw_record, 'id')),
                kind=kind,
                at=_readTimestamp(raw_record, 'at'),
                start=start,
                end=end,
                note=_readOptionalString(raw_record, 'note') or '',
                task=task,
                source=EventSource(_readString(raw_record, 'source')),
            )
        case EventKind.ACK:
            if task is None:
                raise ValueError('an ack must name a task')
            return LoggedEvent(
                id=EventId(_readString(raw_record, 'id')),
                kind=kind,
                at=_readTimestamp(raw_record, 'at'),
                start=None,
                end=None,
                note='',
                task=task,
                source=EventSource.MANUAL,
            )


def _serializeGoal(goal: Goal) -> dict[str, object]:
    return {
        'title': goal.title,
        'notes': goal.notes,
        'created': goal.created.isoformat(),
        'completed': goal.completed.isoformat() if goal.completed else None,
        'position': goal.position,
    }


def _serializeTask(task: Task) -> dict[str, object]:
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


def _serializeTemplate(template: RecurringTask) -> dict[str, object]:
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


def _serializeStep(step: RecurringStep) -> dict[str, object]:
    return {
        'template': step.template,
        'parent': step.parent,
        'title': step.title,
        'estimate_minutes': step.estimate_minutes,
        'auto_schedule': step.auto_schedule,
        'created': step.created.isoformat(),
    }


def _serializeBlock(block: PlanBlock) -> dict[str, object]:
    return {
        'task': block.task,
        'start': block.start.isoformat(),
        'end': block.end.isoformat(),
        'chunk': block.chunk,
        'chunks': block.chunks,
    }


def _serializeEvent(event: LoggedEvent) -> dict[str, object]:
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


def _readString(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f'{key!r} must be a string')

    return value


def _readOptionalString(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f'{key!r} must be a string or null')

    return value


def _readBool(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if not isinstance(value, bool):
        raise ValueError(f'{key!r} must be true or false')

    return value


def _readOptionalBool(record: Mapping[str, object], key: str, *, default: bool) -> bool:
    if key not in record:
        return default

    return _readBool(record, key)


def _readPositiveInt(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    # bool subclasses int. Reject it too
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f'{key!r} must be a positive integer')

    return value


def _readOptionalPositiveInt(record: Mapping[str, object], key: str) -> int | None:
    if record.get(key) is None:
        return None

    return _readPositiveInt(record, key)


def _readOptionalDayCount(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f'{key!r} must be a whole number of days or null')

    return value


def _readTimestamp(record: Mapping[str, object], key: str) -> datetime:
    raw_timestamp = _readString(record, key)
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError as exc:
        raise ValueError(f'{key!r}: {raw_timestamp!r} is not an ISO-8601 timestamp') from exc

    if timestamp.tzinfo is None:
        raise ValueError(f'{key!r}: {raw_timestamp!r} has no timezone')

    return timestamp.astimezone(UTC)


def _readOptionalTimestamp(record: Mapping[str, object], key: str) -> datetime | None:
    if record.get(key) is None:
        return None

    return _readTimestamp(record, key)


def _readOptionalDate(record: Mapping[str, object], key: str) -> date | None:
    if record.get(key) is None:
        return None

    raw_date = _readString(record, key)
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError(f'{key!r}: {raw_date!r} is not a YYYY-MM-DD date') from exc


def _readDayNumbers(record: Mapping[str, object], key: str) -> frozenset[int]:
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


def _readTaskIds(record: Mapping[str, object], key: str) -> tuple[TaskId, ...]:
    value = record.get(key)
    if not isinstance(value, list):
        raise ValueError(f'{key!r} must be a list of task ids')

    ids = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f'{key!r} must be a list of task ids')
        ids.append(TaskId(item))

    return tuple(ids)


def _reject(data_path: Path, reason: str) -> StoreError:
    LOG.warning('store.rejected', extra={'file': str(data_path), 'reason': reason})
    return StoreError(f'{data_path}: {reason}')


def _rejectConflict(data_path: Path) -> StoreConflictError:
    LOG.warning('store.conflict', extra={'file': str(data_path)})
    return StoreConflictError(f'{data_path} changed on disk after it was loaded. Reload and retry')


### vocabulary #########################################################################


_T = TypeVar('_T')
