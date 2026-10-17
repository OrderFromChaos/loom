"""The POST routes that edit recurring templates and their steps, or correct a template's clock."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime

from loom import recurring, scheduler, store
from loom.vocabulary import (
    DAY_NAMES,
    GoalId,
    Priority,
    RecurringId,
    RecurringStep,
    RecurringTask,
    StepId,
    TemplateNotFoundError,
)
from loom.web import request_fields, responses
from loom.web.web_vocabulary import FormError, Request, Response


def handleAddTemplate(request: Request) -> Response:
    """Create a recurring template from the add form.

    Raises:
        FormError: the title or cadence is missing or malformed.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()

    blank = RecurringTask(
        id=store.generateRecurringId(),
        title='',
        notes='',
        estimate_minutes=None,
        auto_schedule=False,
        priority=Priority.MEDIUM,
        goal=None,
        days=frozenset(),
        every_days=None,
        deadline_after_days=None,
        wait_for_completion=False,
        created=datetime.now(UTC),
        last_added=None,
    )
    template = _applyTemplateForm(request.form, blank)
    templates[template.id] = template
    area_store.saveTemplates(templates)
    return responses.buildReturnRedirect(request, area)


def _applyTemplateForm(form: Mapping[str, str], template: RecurringTask) -> RecurringTask:
    """Apply the add or edit form to a template. A form with no priority leaves the template's as it is.

    Raises:
        FormError: the cadence, title or another field is missing or malformed.
    """
    days, every = _readFormCadence(form)
    return replace(
        template,
        title=request_fields.readFormText(form, 'title'),
        notes=form.get('notes', '').strip(),
        estimate_minutes=request_fields.readFormOptionalPositiveInt(form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in form,
        priority=request_fields.readFormEnum(form, 'priority', Priority) if 'priority' in form else template.priority,
        goal=_readFormOptionalGoal(form),
        days=days,
        every_days=every,
        deadline_after_days=_readFormOptionalDayCount(form, 'deadline_after_days'),
        wait_for_completion='wait_for_completion' in form,
    )


def _readFormCadence(form: Mapping[str, str]) -> tuple[frozenset[int], int | None]:
    """Read the template's cadence: anchored weekdays, or an every-N-days interval.

    Raises:
        FormError: both modes given, or neither.
    """
    days = frozenset(index for index, name in enumerate(DAY_NAMES) if f'day_{name.lower()}' in form)
    every = request_fields.readFormOptionalPositiveInt(form, 'every_days')
    if bool(days) == (every is not None):
        raise FormError('pick weekdays or an every-N-days interval, one or the other')

    return days, every


def _readFormOptionalGoal(form: Mapping[str, str]) -> GoalId | None:
    raw_goal = form.get('goal', '').strip()
    return GoalId(raw_goal) if raw_goal else None


def _readFormOptionalDayCount(form: Mapping[str, str], key: str) -> int | None:
    """Read a day count. A blank field is None, and zero days is valid (due the same day)."""
    raw_count = form.get(key, '').strip()
    if not raw_count:
        return None
    # isdigit() admits non-ASCII digits such as '²'. Check isascii() too
    if not (raw_count.isascii() and raw_count.isdigit()):
        raise FormError(f'{key} must be a whole number of days')

    return int(raw_count)


def handleEditTemplate(request: Request) -> Response:
    """Apply the edit form to one template.

    Raises:
        FormError: the template does not exist, or a field is malformed.
    """
    return _handleTemplateChange(request, lambda template: _applyTemplateForm(request.form, template))


def _handleTemplateChange(request: Request, change: Callable[[RecurringTask], RecurringTask]) -> Response:
    """Replace the route's template with change(template), save, and redirect.

    Raises:
        FormError: the template does not exist, or `change` raised it. Nothing is written.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = request_fields.findRecord(templates, RecurringId(request.match.group('id')), 'recurring template')

    templates[template.id] = change(template)
    area_store.saveTemplates(templates)
    return responses.buildReturnRedirect(request, area)


def handleDeleteTemplate(request: Request) -> Response:
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    templates = area_store.loadTemplates()
    template = request_fields.findRecord(templates, RecurringId(request.match.group('id')), 'recurring template')

    del templates[template.id]
    area_store.saveTemplates(templates)
    steps = area_store.loadSteps()
    if any(step.template == template.id for step in steps.values()):
        area_store.saveSteps({step_id: step for step_id, step in steps.items() if step.template != template.id})
    return responses.buildReturnRedirect(request, area)


def handlePullAhead(request: Request) -> Response:
    """Spawn a copy now and restart the template's clock from now, then re-plan.

    Raises:
        FormError: the template does not exist, or it waits for completion and a copy
            is already open. Nothing is written.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    template = request_fields.findRecord(
        area_store.loadTemplates(),
        RecurringId(request.match.group('id')),
        'recurring template',
    )
    if template.wait_for_completion and recurring.countOpenCopies(area_store.loadTasks())[template.id]:
        raise FormError(f'{template.title!r} already has an open copy. Finish it, or delay it to now')

    now = datetime.now(UTC)
    recurring.pullAhead(area, template.id, now)
    scheduler.runPlan(area, now)
    return responses.buildReturnRedirect(request, area)


def handleDelayToNow(request: Request) -> Response:
    """Merge the template's open copies into one dated today, restart its clock, re-plan.

    Raises:
        FormError: the template does not exist. Nothing is written.
    """
    area = request_fields.readArea(request)
    template_id = RecurringId(request.match.group('id'))
    now = datetime.now(UTC)
    try:
        recurring.delayToNow(area, template_id, now)
    except TemplateNotFoundError as exc:
        raise FormError(f'recurring template {template_id} does not exist') from exc

    scheduler.runPlan(area, now)
    return responses.buildReturnRedirect(request, area)


def handleSetNextDue(request: Request) -> Response:
    """Hand-set an interval template's next occurrence. The rhythm continues from that day.

    Raises:
        FormError: the template does not exist, runs on anchor days, or the date does not
            parse. Nothing is written.
    """

    def setNextDue(template: RecurringTask) -> RecurringTask:
        if template.every_days is None:
            raise FormError(f'{template.title!r} runs on anchor days. Change its days to realign it')
        return replace(template, next_due=request_fields.readFormDate(request.form, 'next_due'))

    return _handleTemplateChange(request, setNextDue)


def handleAddStep(request: Request) -> Response:
    """Add a step under a template, or under one of that template's steps.

    Raises:
        FormError: the template or parent step does not exist, or a field is malformed.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    template = request_fields.findRecord(
        area_store.loadTemplates(),
        RecurringId(request.match.group('id')),
        'recurring template',
    )
    steps = area_store.loadSteps()

    raw_parent = request.form.get('parent', '').strip()
    parent = StepId(raw_parent) if raw_parent else None
    if parent is not None and (parent not in steps or steps[parent].template != template.id):
        raise FormError(f'step {parent} is not part of {template.title!r}')

    step = RecurringStep(
        id=store.generateStepId(),
        template=template.id,
        parent=parent,
        title=request_fields.readFormText(request.form, 'title'),
        estimate_minutes=request_fields.readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
        created=datetime.now(UTC),
    )
    steps[step.id] = step
    area_store.saveSteps(steps)
    return responses.buildReturnRedirect(request, area)


def handleEditStep(request: Request) -> Response:
    """Apply a step's edit form. Copies already spawned are left as they are.

    Raises:
        FormError: the step does not exist, or a field is malformed.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    steps = area_store.loadSteps()
    step = request_fields.findRecord(steps, StepId(request.match.group('id')), 'recurring step')

    steps[step.id] = replace(
        step,
        title=request_fields.readFormText(request.form, 'title'),
        estimate_minutes=request_fields.readFormOptionalPositiveInt(request.form, 'estimate_minutes'),
        auto_schedule='auto_schedule' in request.form,
    )
    area_store.saveSteps(steps)
    return responses.buildReturnRedirect(request, area)


def handleDeleteStep(request: Request) -> Response:
    """Delete a step and every step beneath it.

    Raises:
        FormError: the step does not exist.
    """
    area = request_fields.readArea(request)
    area_store = store.openStore(area)
    steps = area_store.loadSteps()
    step = request_fields.findRecord(steps, StepId(request.match.group('id')), 'recurring step')
    doomed = recurring.collectStepSubtreeIds(step.id, steps)
    area_store.saveSteps({step_id: kept for step_id, kept in steps.items() if step_id not in doomed})
    return responses.buildReturnRedirect(request, area)
