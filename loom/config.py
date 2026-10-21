"""Parse loom.toml into the frozen CONFIG singleton.

Area window strings are 'Days HH:MM-HH:MM', with days a single name or an inclusive range:

    Mon-Fri 09:00-12:00
    Sat 10:00-18:00

Window and web times are local wall-clock in the configured timezone. The [categories]
rules match in file order. The first malformed entry raises ConfigError, so a half-valid
config never runs.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loom.vocabulary import (
    DAY_NAMES,
    Area,
    AreaConfig,
    CategoryRule,
    Color,
    Config,
    ConfigError,
    SchedulerConfig,
    WebConfig,
    WeeklyWindow,
)


CONFIG_PATH = Path('loom.toml')


def parseConfig(config_path: Path) -> Config:
    """Read the config file and construct a Config.

    Raises:
        ConfigError: the file is missing, not TOML, or any entry is malformed.
    """
    raw = readConfigTable(config_path)
    palette = _parsePalette(raw.get('palette', {}))
    return Config(
        timezone=_parseTimezone(_readTableString(raw, 'timezone', 'top level')),
        data_root=Path(_readTableString(raw, 'data_root', 'top level')),
        horizon_days=readTablePositiveInt(raw, 'horizon_days', 'top level'),
        scheduler=_parseScheduler(readSubtable(raw, 'scheduler', 'top level')),
        web=_parseWeb(readSubtable(raw, 'web', 'top level')),
        areas=_parseAreas(readSubtable(raw, 'areas', 'top level')),
        palette=palette,
        categories=_parseCategories(raw.get('categories', {}), palette),
    )


def readConfigTable(config_path: Path) -> dict[str, object]:
    if not config_path.is_file():
        raise ConfigError(f'{config_path} not found (run from the project root)')

    try:
        with config_path.open('rb') as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{config_path}: {exc}') from exc


def _parsePalette(table: object) -> dict[str, Color]:
    if not isinstance(table, dict):
        raise ConfigError('loom.toml [palette]: must be a table of name = "#rrggbb"')

    return {key: parseColor(value, 'palette') for key, value in table.items()}


def parseColor(raw_color: object, section: str) -> Color:
    HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
    if not isinstance(raw_color, str) or not HEX_COLOR.match(raw_color):
        raise ConfigError(f'loom.toml [{section}]: color must be "#rrggbb", got {raw_color!r}')

    return Color(raw_color)


def _readTableString(table: Mapping[str, object], key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f'loom.toml [{section}]: {key!r} must be a string')

    return value


def _parseTimezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f'loom.toml: unknown timezone {timezone_name!r}') from exc


def readTablePositiveInt(table: Mapping[str, object], key: str, section: str) -> int:
    value = table.get(key)
    # bool subclasses int. Reject it too
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'loom.toml [{section}]: {key!r} must be a positive integer')

    return value


def readSubtable(table: Mapping[str, object], key: str, section: str) -> dict[str, object]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f'loom.toml [{section}]: [{key}] table is missing')

    return value


def _parseScheduler(table: Mapping[str, object]) -> SchedulerConfig:
    parsed = SchedulerConfig(
        min_chunk_minutes=readTablePositiveInt(table, 'min_chunk_minutes', 'scheduler'),
        max_chunk_minutes=readTablePositiveInt(table, 'max_chunk_minutes', 'scheduler'),
        stale_grace_minutes=readTablePositiveInt(table, 'stale_grace_minutes', 'scheduler'),
    )
    if parsed.min_chunk_minutes > parsed.max_chunk_minutes:
        raise ConfigError('loom.toml [scheduler]: min_chunk_minutes exceeds max_chunk_minutes')

    return parsed


def _parseWeb(table: Mapping[str, object]) -> WebConfig:
    """Construct the web config.

    Raises:
        ConfigError: a field is malformed, or scroll_start falls outside the day span.
    """
    parsed = WebConfig(
        port=readTablePositiveInt(table, 'port', 'web'),
        day_start=_parseClock(_readTableString(table, 'day_start', 'web'), 'web'),
        day_end=_parseClock(_readTableString(table, 'day_end', 'web'), 'web'),
        scroll_start=_parseClock(_readTableString(table, 'scroll_start', 'web'), 'web'),
    )
    if not parsed.computeGridStartMinute() <= parsed.computeScrollMinute() < parsed.computeGridEndMinute():
        raise ConfigError('loom.toml [web]: scroll_start must fall inside the day span')

    return parsed


def _parseClock(clock_text: str, section: str) -> time:
    try:
        return time.fromisoformat(clock_text)
    except ValueError as exc:
        raise ConfigError(f'loom.toml [{section}]: {clock_text!r} is not a HH:MM time') from exc


def _parseAreas(table: Mapping[str, object]) -> dict[Area, AreaConfig]:
    """Construct the AreaConfig of each [areas.*] table.

    Raises:
        ConfigError: no areas, a bad area name, or a malformed table.
    """
    if not table:
        raise ConfigError('loom.toml [areas]: at least one [areas.<name>] table is required')

    areas: dict[Area, AreaConfig] = {}
    for area_name in table:
        checkAreaName(area_name)
        section = f'areas.{area_name}'
        area_table = readSubtable(table, area_name, 'areas')
        raw_windows = area_table.get('windows')
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ConfigError(f'loom.toml [{section}]: "windows" must be a non-empty list')

        windows = []
        for entry in raw_windows:
            if not isinstance(entry, str):
                raise ConfigError(f'loom.toml [{section}]: window entries must be strings')
            windows.append(parseWindow(entry, section))

        finished = area_table.get('finished')
        # datetime subclasses date. Reject it too
        if finished is not None and (not isinstance(finished, date) or isinstance(finished, datetime)):
            raise ConfigError(f'loom.toml [{section}]: "finished" must be a bare date, like 2026-10-16')

        hidden = area_table.get('hidden', False)
        if not isinstance(hidden, bool):
            raise ConfigError(f'loom.toml [{section}]: "hidden" must be true or false')
        if 'order' in area_table:
            # the order can change without a restart. Validate it here, and leave it out of CONFIG
            readTablePositiveInt(area_table, 'order', section)

        areas[Area(area_name)] = AreaConfig(
            windows=tuple(windows),
            color=parseColor(area_table.get('color'), section),
            finished=finished,
            hidden=hidden,
        )

    if all(settings.finished is not None for settings in areas.values()):
        raise ConfigError('loom.toml [areas]: every area is finished. At least one must stay open')

    return areas


def checkAreaName(area_name: str) -> None:
    # area names become URL path segments and data/ directory names
    AREA_NAME = re.compile(r'^[a-z][a-z0-9-]*$')
    RESERVED_AREA_NAMES = frozenset({'static'})
    if not AREA_NAME.match(area_name):
        raise ConfigError(f'area name {area_name!r} must be a lowercase slug, like "zed" or "side-project"')
    if area_name in RESERVED_AREA_NAMES:
        raise ConfigError(f'area name {area_name!r} is reserved')


def parseWindow(window_text: str, section: str) -> WeeklyWindow:
    parts = window_text.split(' ')
    if len(parts) != 2 or parts[1].count('-') != 1:
        raise ConfigError(f'loom.toml [{section}]: window {window_text!r} is not "Days HH:MM-HH:MM"')

    start_text, end_text = parts[1].split('-')
    window = WeeklyWindow(
        days=_parseDays(parts[0], section),
        start=_parseClock(start_text, section),
        end=_parseClock(end_text, section),
    )
    if window.start >= window.end:
        raise ConfigError(f'loom.toml [{section}]: window {window_text!r} ends before it starts')

    return window


def _parseDays(days_text: str, section: str) -> frozenset[int]:
    names = days_text.split('-')
    if not all(name in DAY_NAMES for name in names):
        raise ConfigError(f'loom.toml [{section}]: unknown day in {days_text!r} (use {", ".join(DAY_NAMES)})')

    if len(names) == 1:
        return frozenset({DAY_NAMES.index(names[0])})
    if len(names) != 2 or DAY_NAMES.index(names[0]) > DAY_NAMES.index(names[1]):
        raise ConfigError(f'loom.toml [{section}]: day range {days_text!r} must run forward, like "Mon-Fri"')

    return frozenset(range(DAY_NAMES.index(names[0]), DAY_NAMES.index(names[1]) + 1))


def _parseCategories(table: object, palette: Mapping[str, Color]) -> tuple[CategoryRule, ...]:
    """Construct the regex-to-category rules, validated against the palette.

    Raises:
        ConfigError: a pattern does not compile, or points at an unknown category.
    """
    if not isinstance(table, dict):
        raise ConfigError('loom.toml [categories]: must be a table of pattern = "Category"')

    rules = []
    for pattern_text, category in table.items():
        if not isinstance(category, str) or category not in palette:
            raise ConfigError(
                f'loom.toml [categories]: {pattern_text!r} points at {category!r}, which is not a palette category',
            )
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f'loom.toml [categories]: {pattern_text!r} is not a valid regex: {exc}') from exc
        rules.append(CategoryRule(pattern=pattern, category=category))

    return tuple(rules)


def listOpenAreas() -> tuple[Area, ...]:
    """List the areas that are not finished, in file order."""
    return tuple(area for area, settings in CONFIG.areas.items() if settings.finished is None)


def computeLocalDate(moment: datetime) -> date:
    return moment.astimezone(CONFIG.timezone).date()


CONFIG = parseConfig(CONFIG_PATH)
