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
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomlkit
from tomlkit.items import Comment, Item, Key, Table, Whitespace

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

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')

# area names become URL path segments and data/ directory names
_AREA_NAME = re.compile(r'^[a-z][a-z0-9-]*$')
_RESERVED_AREA_NAMES = frozenset({'static'})
DEFAULT_AREA_WINDOWS = ('Mon-Sun 09:00-21:00',)


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

    palette = _parsePalette(raw.get('palette', {}))
    return Config(
        timezone=_parseTimezone(_requireString(raw, 'timezone', 'top level')),
        data_root=Path(_requireString(raw, 'data_root', 'top level')),
        horizon_days=_requirePositiveInt(raw, 'horizon_days', 'top level'),
        scheduler=_parseScheduler(_requireTable(raw, 'scheduler', 'top level')),
        web=_parseWeb(_requireTable(raw, 'web', 'top level')),
        areas=_parseAreas(_requireTable(raw, 'areas', 'top level')),
        palette=palette,
        categories=_parseCategories(raw.get('categories', {}), palette),
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
    """Construct the per-area window and color config; the [areas.*] tables define the set.

    Raises:
        ConfigError: no areas, a bad area name, or a malformed table.
    """
    if not table:
        raise ConfigError('loom.toml [areas]: at least one [areas.<name>] table is required')

    areas: dict[Area, AreaConfig] = {}
    for name in table:
        _checkAreaName(name)
        where = f'areas.{name}'
        sub = _requireTable(table, name, 'areas')
        raw_windows = sub.get('windows')
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ConfigError(f'loom.toml [{where}]: "windows" must be a non-empty list')

        windows = []
        for entry in raw_windows:
            if not isinstance(entry, str):
                raise ConfigError(f'loom.toml [{where}]: window entries must be strings')
            windows.append(parseWindow(entry, where))

        finished = sub.get('finished')
        if finished is not None and (not isinstance(finished, date) or isinstance(finished, datetime)):
            raise ConfigError(f'loom.toml [{where}]: "finished" must be a bare date, like 2026-10-16')

        hidden = sub.get('hidden', False)
        if not isinstance(hidden, bool):
            raise ConfigError(f'loom.toml [{where}]: "hidden" must be true or false')
        if 'order' in sub:
            _requirePositiveInt(sub, 'order', where)  # read fresh per page by rawAreaOrder

        areas[Area(name)] = AreaConfig(
            windows=tuple(windows),
            color=_parseColor(sub.get('color'), where),
            finished=finished,
            hidden=hidden,
        )

    if all(settings.finished is not None for settings in areas.values()):
        raise ConfigError('loom.toml [areas]: every area is finished; at least one must stay open')

    return areas


def openAreas() -> tuple[Area, ...]:
    """The areas still in use, in file order: what the switcher, housekeeping and planning walk."""
    return tuple(area for area, settings in CONFIG.areas.items() if settings.finished is None)


def saveAreaFinished(area: Area, finished: date | None) -> None:
    """Mark an area finished on `finished`, or reopen it with None; takes effect on the next start.

    Only the [areas.<name>] table changes: the area's data folder is never touched.

    Raises:
        ConfigError: the area is not in loom.toml, or finishing it would leave no area open.
    """
    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    tables = document['areas']
    if area not in tables:
        raise ConfigError(f'the area {area!r} is not in loom.toml')

    if finished is None:
        if 'finished' in tables[area]:
            del tables[area]['finished']
    else:
        if not any(name != area and 'finished' not in tables[name] for name in tables):
            raise ConfigError(f'{area!r} is the last open area; open or add another before finishing it')
        _setKeepingTrivia(tables[area], 'finished', finished)
    _writeConfigDocument(document)


def rawAreaOrder() -> dict[Area, int]:
    """Each area's switcher position as loom.toml carries it right now; unnumbered areas are absent.

    Read from the file rather than CONFIG, so a drag-and-drop reorder shows on the next
    page without a restart.

    Raises:
        ConfigError: the file is missing, not TOML, or an order is not a positive integer.
    """
    tables = _requireTable(_readRawConfig(), 'areas', 'top level')
    order: dict[Area, int] = {}
    for name in tables:
        sub = _requireTable(tables, name, 'areas')
        if 'order' in sub:
            order[Area(name)] = _requirePositiveInt(sub, 'order', f'areas.{name}')

    return order


def saveAreaOrder(names: Sequence[Area]) -> None:
    """Number the switcher's areas 1, 2, 3... in the given order; shows on the next page.

    Hidden areas lose any number, since they never sit in the switcher's order. Finished
    areas keep theirs, so a reopened area returns to its old place.

    Raises:
        ConfigError: a name is not an area, is hidden, or appears twice; nothing is written.
    """
    if len(set(names)) != len(names):
        raise ConfigError('an area appears twice in the new order')

    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    tables = document['areas']
    for name in names:
        if name not in tables:
            raise ConfigError(f'the area {name!r} is not in loom.toml')
        if tables[name].get('hidden') is True:
            raise ConfigError(f'{name!r} is hidden; hidden areas have no place in the switcher')

    for position, name in enumerate(names, start=1):
        _setKeepingTrivia(tables[name], 'order', position)
    for name in tables:
        if tables[name].get('hidden') is True and 'order' in tables[name]:
            del tables[name]['order']
    _writeConfigDocument(document)


def _setKeepingTrivia(table: Table, key: str, value: object) -> None:
    """Set a key in a table without splitting the comment that introduces the next section."""
    trailing = _popTrailingTrivia(table)
    table[key] = value
    table.value.body.extend(trailing)


def _popTrailingTrivia(table: Table) -> list[tuple[Key | None, Item]]:
    """Detach the blank lines and comments after a table's last key, in file order.

    Those comments introduce the NEXT section of the file. A key added to the table, or
    a table appended after it, would land below them and split them from the section
    they describe; the caller re-attaches them after its addition.
    """
    trailing: list[tuple[Key | None, Item]] = []
    body = table.value.body
    while body and body[-1][0] is None and isinstance(body[-1][1], (Comment, Whitespace)):
        trailing.insert(0, body.pop())

    return trailing


def saveNewArea(name: str, color: str) -> None:
    """Append a new [areas.<name>] table to loom.toml; takes effect on the next start.

    The area starts with an every-day window; its hours section on the settings page
    tightens that.

    Raises:
        ConfigError: the name is malformed, reserved, or taken, or the color is not
            "#rrggbb".
    """
    _checkAreaName(name)
    parsed_color = _parseColor(color, f'areas.{name}')

    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    if name in document['areas']:
        raise ConfigError(f'the area {name!r} already exists')

    # the comments introducing the next section move onto the new table's tail
    trailing = _popTrailingTrivia(document['areas'][list(document['areas'])[-1]])
    area_table = tomlkit.table()
    area_table['windows'] = list(DEFAULT_AREA_WINDOWS)
    area_table['color'] = str(parsed_color)
    document['areas'][name] = area_table
    document['areas'][name].value.body.extend(trailing)
    _writeConfigDocument(document)


def _checkAreaName(name: str) -> None:
    if not _AREA_NAME.match(name):
        raise ConfigError(f'area name {name!r} must be a lowercase slug, like "zed" or "side-project"')
    if name in _RESERVED_AREA_NAMES:
        raise ConfigError(f'area name {name!r} is reserved')


def rawAreaWindows(area: Area) -> tuple[str, ...]:
    """This area's window strings exactly as loom.toml carries them right now.

    Read from the file rather than CONFIG, so the settings page shows the disk state
    even if it was hand-edited after startup.

    Raises:
        ConfigError: the file is missing, not TOML, or the windows list is malformed.
    """
    raw = _readRawConfig()
    areas = _requireTable(raw, 'areas', 'top level')
    sub = _requireTable(areas, area, 'areas')
    raw_windows = sub.get('windows')
    if not isinstance(raw_windows, list) or not all(isinstance(entry, str) for entry in raw_windows):
        raise ConfigError(f'loom.toml [areas.{area}]: "windows" must be a list of strings')

    return tuple(raw_windows)


def saveAreaColor(area: Area, color: str) -> None:
    """Rewrite this area's calendar color in loom.toml; takes effect on the next start.

    Raises:
        ConfigError: the color is not "#rrggbb", or the file cannot be read.
    """
    parsed = _parseColor(color, f'areas.{area}')
    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    document['areas'][area]['color'] = str(parsed)
    _writeConfigDocument(document)


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
        parseWindow(line, f'areas.{area}')

    document = tomlkit.parse(CONFIG_PATH.read_text(encoding='utf-8'))
    document['areas'][area]['windows'] = list(window_lines)
    _writeConfigDocument(document)


def rawPalette() -> tuple[tuple[str, str], ...]:
    """The [palette] entries as (name, hex) pairs, straight from loom.toml.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _rawColorTable('palette')


def rawCategoryRules() -> tuple[tuple[str, str], ...]:
    """The [categories] entries as (pattern, category) pairs, straight from loom.toml.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _rawColorTable('categories')


def savePalette(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [palette] in loom.toml; takes effect on the next start.

    Raises:
        ConfigError: a name is empty or repeated, a color is not "#rrggbb", or the
            change would orphan a [categories] rule pointing at a removed name.
    """
    seen = set()
    for name, value in entries:
        if not name:
            raise ConfigError('loom.toml [palette]: a name is empty')
        if name in seen:
            raise ConfigError(f'loom.toml [palette]: {name!r} appears twice')
        seen.add(name)
        _parseColor(value, 'palette')

    for pattern_text, category in rawCategoryRules():
        if category not in seen:
            raise ConfigError(f'{category!r} is still used by the category rule {pattern_text!r}')

    _writeTable('palette', entries)


def saveCategoryRules(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [categories] in loom.toml; takes effect on the next start.

    Raises:
        ConfigError: a pattern is empty, repeated, or does not compile, or a category
            is not a palette name.
    """
    palette_names = {name for name, _ in rawPalette()}
    seen = set()
    for pattern_text, category in entries:
        if not pattern_text:
            raise ConfigError('loom.toml [categories]: a pattern is empty')
        if pattern_text in seen:
            raise ConfigError(f'loom.toml [categories]: {pattern_text!r} appears twice')
        seen.add(pattern_text)
        if category not in palette_names:
            raise ConfigError(f'{category!r} is not a palette category')
        try:
            re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f'{pattern_text!r} is not a valid regex: {exc}') from exc

    _writeTable('categories', entries)


def _rawColorTable(table_name: str) -> tuple[tuple[str, str], ...]:
    raw = _readRawConfig()
    table = raw.get(table_name, {})
    if not isinstance(table, dict) or not all(isinstance(value, str) for value in table.values()):
        raise ConfigError(f'loom.toml [{table_name}]: must be a table of string values')

    return tuple((key, value) for key, value in table.items())


def _writeTable(table_name: str, entries: Sequence[tuple[str, str]]) -> None:
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
