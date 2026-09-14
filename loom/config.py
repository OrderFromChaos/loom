"""Parse loom.toml into the frozen CONFIG singleton.

Area window strings are 'Days HH:MM-HH:MM', with days a single name or an inclusive range:

    Mon-Fri 09:00-12:00
    Sat 10:00-18:00

Window and web times are local wall-clock in the configured timezone. The [colors] table
keeps its file order, and that order is the keyword match order. The first malformed
entry raises ConfigError; a half-valid config never runs.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomlkit

from loom.vocabulary import (
    DAY_NAMES,
    Area,
    AreaConfig,
    Color,
    Config,
    ConfigError,
    KeywordColor,
    SchedulerConfig,
    WebConfig,
    WeeklyWindow,
)


CONFIG_PATH = Path('loom.toml')

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def parseConfig(config_path: Path) -> Config:
    """Read the config file and construct a Config.

    Raises:
        ConfigError: the file is missing, not TOML, or any entry is malformed.
    """
    if not config_path.is_file():
        raise ConfigError(f'{config_path} not found (run from the project root)')

    try:
        with config_path.open('rb') as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{config_path}: {exc}') from exc

    return Config(
        timezone=_parseTimezone(_requireString(raw, 'timezone', 'top level')),
        data_root=Path(_requireString(raw, 'data_root', 'top level')),
        horizon_days=_requirePositiveInt(raw, 'horizon_days', 'top level'),
        scheduler=_parseScheduler(_requireTable(raw, 'scheduler', 'top level')),
        web=_parseWeb(_requireTable(raw, 'web', 'top level')),
        areas=_parseAreas(_requireTable(raw, 'areas', 'top level')),
        colors=_parseColors(raw.get('colors', {})),
        palette=_parsePalette(raw.get('palette', {})),
    )


def _parseTimezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f'loom.toml: unknown timezone {name!r}') from exc


def _parseScheduler(table: Mapping[str, object]) -> SchedulerConfig:
    parsed = SchedulerConfig(
        min_chunk_minutes=_requirePositiveInt(table, 'min_chunk_minutes', 'scheduler'),
        max_chunk_minutes=_requirePositiveInt(table, 'max_chunk_minutes', 'scheduler'),
        stale_grace_minutes=_requirePositiveInt(table, 'stale_grace_minutes', 'scheduler'),
    )
    if parsed.min_chunk_minutes > parsed.max_chunk_minutes:
        raise ConfigError('loom.toml [scheduler]: min_chunk_minutes exceeds max_chunk_minutes')

    return parsed


def _parseWeb(table: Mapping[str, object]) -> WebConfig:
    """Construct the web config; a day_end at or before day_start runs past midnight.

    Raises:
        ConfigError: a field is malformed, or scroll_start falls outside the day span.
    """
    parsed = WebConfig(
        port=_requirePositiveInt(table, 'port', 'web'),
        day_start=_parseClock(_requireString(table, 'day_start', 'web'), 'web'),
        day_end=_parseClock(_requireString(table, 'day_end', 'web'), 'web'),
        scroll_start=_parseClock(_requireString(table, 'scroll_start', 'web'), 'web'),
    )

    start = parsed.day_start.hour * 60 + parsed.day_start.minute
    end = parsed.day_end.hour * 60 + parsed.day_end.minute
    if end <= start:
        end += 24 * 60
    scroll = parsed.scroll_start.hour * 60 + parsed.scroll_start.minute
    if scroll < start:
        scroll += 24 * 60
    if not start <= scroll < end:
        raise ConfigError('loom.toml [web]: scroll_start must fall inside the day span')

    return parsed


def _parseAreas(table: Mapping[str, object]) -> dict[Area, AreaConfig]:
    """Construct the per-area window and color config.

    Raises:
        ConfigError: an area table is missing, extra, or malformed.
    """
    expected = {area.value for area in Area}
    if set(table) != expected:
        raise ConfigError(f'loom.toml [areas]: tables must be exactly {sorted(expected)}')

    areas: dict[Area, AreaConfig] = {}
    for area in Area:
        where = f'areas.{area.value}'
        sub = _requireTable(table, area.value, 'areas')
        raw_windows = sub.get('windows')
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ConfigError(f'loom.toml [{where}]: "windows" must be a non-empty list')

        windows = []
        for entry in raw_windows:
            if not isinstance(entry, str):
                raise ConfigError(f'loom.toml [{where}]: window entries must be strings')
            windows.append(parseWindow(entry, where))

        areas[area] = AreaConfig(windows=tuple(windows), color=_parseColor(sub.get('color'), where))

    return areas


def rawAreaWindows(area: Area) -> tuple[str, ...]:
    """This area's window strings exactly as loom.toml carries them right now.

    Read from the file rather than CONFIG, so the settings page shows the disk state
    even if it was hand-edited after startup.

    Raises:
        ConfigError: the file is missing, not TOML, or the windows list is malformed.
    """
    raw = _readRawConfig()
    areas = _requireTable(raw, 'areas', 'top level')
    sub = _requireTable(areas, area.value, 'areas')
    raw_windows = sub.get('windows')
    if not isinstance(raw_windows, list) or not all(isinstance(entry, str) for entry in raw_windows):
        raise ConfigError(f'loom.toml [areas.{area.value}]: "windows" must be a list of strings')

    return tuple(raw_windows)


def saveAreaWindows(area: Area, window_lines: Sequence[str]) -> None:
    """Validate the lines, then rewrite this area's windows in loom.toml in place.

    Comments and formatting elsewhere in the file survive (tomlkit round-trips them).
    CONFIG parses once at import, so the change takes effect on the next start.

    Raises:
        ConfigError: a line does not parse, or the file cannot be read.
    """
    if not window_lines:
        raise ConfigError('at least one window is required')
    for line in window_lines:
        parseWindow(line, f'areas.{area.value}')

    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    document['areas'][area.value]['windows'] = list(window_lines)
    _writeConfigDocument(document)


def rawPalette() -> tuple[tuple[str, str], ...]:
    """The [palette] entries as (name, hex) pairs, straight from loom.toml.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _rawColorTable('palette')


def rawKeywordColors() -> tuple[tuple[str, str], ...]:
    """The [colors] entries as (keyword, hex) pairs, straight from loom.toml.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _rawColorTable('colors')


def savePalette(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [palette] in loom.toml; takes effect on the next start.

    Raises:
        ConfigError: a name is empty or repeated, or a color is not "#rrggbb".
    """
    _saveColorTable('palette', entries)


def saveKeywordColors(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [colors] in loom.toml; takes effect on the next start.

    Raises:
        ConfigError: a keyword is empty or repeated, or a color is not "#rrggbb".
    """
    _saveColorTable('colors', entries)


def _rawColorTable(table_name: str) -> tuple[tuple[str, str], ...]:
    raw = _readRawConfig()
    table = raw.get(table_name, {})
    if not isinstance(table, dict) or not all(isinstance(value, str) for value in table.values()):
        raise ConfigError(f'loom.toml [{table_name}]: must be a table of name = "#rrggbb"')

    return tuple((key, value) for key, value in table.items())


def _saveColorTable(table_name: str, entries: Sequence[tuple[str, str]]) -> None:
    """Validate the pairs, then update the named loom.toml table in place.

    Raises:
        ConfigError: a name is empty or repeated, or a color is not "#rrggbb".
    """
    seen = set()
    for name, value in entries:
        if not name:
            raise ConfigError(f'loom.toml [{table_name}]: a name is empty')
        if name in seen:
            raise ConfigError(f'loom.toml [{table_name}]: {name!r} appears twice')
        seen.add(name)
        _parseColor(value, table_name)

    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    if table_name in document:
        # mutate the existing table instead of replacing it: replacement drops the
        # comments tomlkit attaches to the table object
        table = document[table_name]
        for key in list(table):
            if key not in dict(entries):
                del table[key]
        for name, value in entries:
            table[name] = value
    else:
        document[table_name] = dict(entries)
    _writeConfigDocument(document)


def _readRawConfig() -> dict[str, object]:
    if not CONFIG_PATH.is_file():
        raise ConfigError(f'{CONFIG_PATH} not found (run from the project root)')

    try:
        with CONFIG_PATH.open('rb') as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{CONFIG_PATH}: {exc}') from exc


def _writeConfigDocument(document: tomlkit.TOMLDocument) -> None:
    temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + '.tmp')
    with temp_path.open('w', encoding='utf-8') as handle:
        handle.write(tomlkit.dumps(document))
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(CONFIG_PATH)


def parseWindow(text: str, where: str) -> WeeklyWindow:
    parts = text.split(' ')
    if len(parts) != 2 or parts[1].count('-') != 1:
        raise ConfigError(f'loom.toml [{where}]: window {text!r} is not "Days HH:MM-HH:MM"')

    start_text, end_text = parts[1].split('-')
    window = WeeklyWindow(
        days=_parseDays(parts[0], where),
        start=_parseClock(start_text, where),
        end=_parseClock(end_text, where),
    )
    if window.start >= window.end:
        raise ConfigError(f'loom.toml [{where}]: window {text!r} ends before it starts')

    return window


def _parseDays(text: str, where: str) -> frozenset[int]:
    names = text.split('-')
    if not all(name in DAY_NAMES for name in names):
        raise ConfigError(f'loom.toml [{where}]: unknown day in {text!r} (use {", ".join(DAY_NAMES)})')

    if len(names) == 1:
        return frozenset({DAY_NAMES.index(names[0])})
    if len(names) != 2 or DAY_NAMES.index(names[0]) > DAY_NAMES.index(names[1]):
        raise ConfigError(f'loom.toml [{where}]: day range {text!r} must run forward, like "Mon-Fri"')

    return frozenset(range(DAY_NAMES.index(names[0]), DAY_NAMES.index(names[1]) + 1))


def _parseClock(text: str, where: str) -> time:
    try:
        return time.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f'loom.toml [{where}]: {text!r} is not a HH:MM time') from exc


def _parseColors(table: object) -> tuple[KeywordColor, ...]:
    if not isinstance(table, dict):
        raise ConfigError('loom.toml [colors]: must be a table of keyword = "#rrggbb"')

    return tuple(KeywordColor(keyword=key, color=_parseColor(value, 'colors')) for key, value in table.items())


def _parsePalette(table: object) -> dict[str, Color]:
    if not isinstance(table, dict):
        raise ConfigError('loom.toml [palette]: must be a table of name = "#rrggbb"')

    return {key: _parseColor(value, 'palette') for key, value in table.items()}


def _parseColor(value: object, where: str) -> Color:
    if not isinstance(value, str) or not _HEX_COLOR.match(value):
        raise ConfigError(f'loom.toml [{where}]: color must be "#rrggbb", got {value!r}')

    return Color(value)


def _requireString(table: Mapping[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f'loom.toml [{where}]: {key!r} must be a string')

    return value


def _requirePositiveInt(table: Mapping[str, object], key: str, where: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'loom.toml [{where}]: {key!r} must be a positive integer')

    return value


def _requireTable(table: Mapping[str, object], key: str, where: str) -> dict[str, object]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f'loom.toml [{where}]: [{key}] table is missing')

    return value


CONFIG = parseConfig(CONFIG_PATH)
