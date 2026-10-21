"""Read the values of a request: its area, return target, and form fields.

A malformed value raises FormError with the field name and the expected form.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, time
from enum import Enum
from typing import TypeVar

from loom.vocabulary import Area
from loom.web.web_vocabulary import FormError, Request


def readArea(request: Request) -> Area:
    return Area(request.match.group('area'))


def findRecord(records: Mapping[_K, _V], record_id: _K, noun: str) -> _V:
    record = records.get(record_id)
    if record is None:
        raise FormError(f'{noun} {record_id} does not exist')

    return record


def readReturnTarget(request: Request, area: Area) -> str:
    target = request.form.get('return_to', '')
    # a target that starts with '//' is another host. Accept only a path on this one
    if target.startswith('/') and not target.startswith('//'):
        return target

    return f'/{area}/calendar'


def readFormText(form: Mapping[str, str], key: str) -> str:
    value = form.get(key, '').strip()
    if not value:
        raise FormError(f'{key} is required')

    return value


def readFormOptionalPositiveInt(form: Mapping[str, str], key: str) -> int | None:
    raw_number = form.get(key, '').strip()
    if not raw_number:
        return None
    # isdigit() admits non-ASCII digits such as '²'. Check isascii() too
    if not (raw_number.isascii() and raw_number.isdigit()) or int(raw_number) <= 0:
        raise FormError(f'{key} must be a positive whole number')

    return int(raw_number)


def readFormDate(form: Mapping[str, str], key: str) -> date:
    raw_date = readFormText(form, key)
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_date!r} is not a YYYY-MM-DD date') from exc


def readFormOptionalDate(form: Mapping[str, str], key: str) -> date | None:
    if not form.get(key, '').strip():
        return None

    return readFormDate(form, key)


def readFormClock(form: Mapping[str, str], key: str) -> time:
    raw_time = readFormText(form, key)
    try:
        return time.fromisoformat(raw_time)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_time!r} is not a HH:MM time') from exc


def readFormEnum(form: Mapping[str, str], key: str, options: type[_E]) -> _E:
    raw_value = readFormText(form, key)
    try:
        return options(raw_value)
    except ValueError as exc:
        raise FormError(f'{key}: {raw_value!r} is not one of {[option.value for option in options]}') from exc


### vocabulary #########################################################################


_E = TypeVar('_E', bound=Enum)
_K = TypeVar('_K')
_V = TypeVar('_V')
