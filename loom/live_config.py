"""loom.toml as it is on disk now: fresh reads and in-place edits for the settings page.

CONFIG parses once at import, so a saved change takes effect on the next start. The area
order is the exception: readAreaOrder reads the file on every call, so the next page has
the new order. tomlkit round-trips the comments and formatting of the rest of the file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import date

import tomlkit
from tomlkit.items import Comment, Item, Key, Table, Whitespace

from loom import config
from loom.vocabulary import Area, ConfigError


_DEFAULT_AREA_WINDOWS = ('Mon-Sun 09:00-21:00',)


def saveAreaFinished(area: Area, finished: date | None) -> None:
    """Mark an area finished on `finished`, or reopen it with None.

    Only the [areas.<name>] table changes. The area's data folder is left as it is.

    Raises:
        ConfigError: the area is not in loom.toml, or finishing it would leave no area open.
    """
    document = _readConfigDocument()
    tables = document['areas']
    if area not in tables:
        raise ConfigError(f'the area {area!r} is not in loom.toml')

    if finished is None:
        if 'finished' in tables[area]:
            del tables[area]['finished']
    else:
        if not any(name != area and 'finished' not in tables[name] for name in tables):
            raise ConfigError(f'{area!r} is the last open area. Open or add another before finishing it')
        _setKeepingTrivia(tables[area], 'finished', finished)
    _writeConfigDocument(document)


def readAreaOrder() -> dict[Area, int]:
    """Read the switcher position of each area from loom.toml. An area with no order is absent.

    Raises:
        ConfigError: the file is missing, not TOML, or an order is not a positive integer.
    """
    tables = config.readSubtable(config.readConfigTable(config.CONFIG_PATH), 'areas', 'top level')
    order: dict[Area, int] = {}
    for area_name in tables:
        area_table = config.readSubtable(tables, area_name, 'areas')
        if 'order' in area_table:
            order[Area(area_name)] = config.readTablePositiveInt(area_table, 'order', f'areas.{area_name}')

    return order


def saveAreaOrder(areas: Sequence[Area]) -> None:
    """Number the switcher's areas 1, 2, 3... in the given order.

    A hidden area loses its number. A finished area's number stays, so a reopened area
    returns to its old place.

    Raises:
        ConfigError: an area is not in loom.toml, is hidden, or appears twice. Nothing is written.
    """
    if len(set(areas)) != len(areas):
        raise ConfigError('an area appears twice in the new order')

    document = _readConfigDocument()
    tables = document['areas']
    for area in areas:
        if area not in tables:
            raise ConfigError(f'the area {area!r} is not in loom.toml')
        if tables[area].get('hidden') is True:
            raise ConfigError(f'{area!r} is hidden. Hidden areas have no place in the switcher')

    for position, area in enumerate(areas, start=1):
        _setKeepingTrivia(tables[area], 'order', position)
    for area_name in tables:
        if tables[area_name].get('hidden') is True and 'order' in tables[area_name]:
            del tables[area_name]['order']
    _writeConfigDocument(document)


def saveNewArea(area_name: str, raw_color: str) -> None:
    """Append a new [areas.<name>] table to loom.toml.

    Raises:
        ConfigError: the name is malformed, reserved, or taken, or the color is not
            "#rrggbb".
    """
    config.checkAreaName(area_name)
    color = config.parseColor(raw_color, f'areas.{area_name}')

    document = _readConfigDocument()
    if area_name in document['areas']:
        raise ConfigError(f'the area {area_name!r} already exists')

    # the comments above the next section move to the end of the new table
    trailing = _popTrailingTrivia(document['areas'][list(document['areas'])[-1]])
    area_table = tomlkit.table()
    area_table['windows'] = list(_DEFAULT_AREA_WINDOWS)
    area_table['color'] = str(color)
    document['areas'][area_name] = area_table
    document['areas'][area_name].value.body.extend(trailing)
    _writeConfigDocument(document)


def readAreaWindowLines(area: Area) -> tuple[str, ...]:
    """Read this area's window strings from loom.toml, hand edits included.

    Raises:
        ConfigError: the file is missing, not TOML, or the windows list is malformed.
    """
    raw = config.readConfigTable(config.CONFIG_PATH)
    areas = config.readSubtable(raw, 'areas', 'top level')
    area_table = config.readSubtable(areas, area, 'areas')
    raw_windows = area_table.get('windows')
    if not isinstance(raw_windows, list) or not all(isinstance(entry, str) for entry in raw_windows):
        raise ConfigError(f'loom.toml [areas.{area}]: "windows" must be a list of strings')

    return tuple(raw_windows)


def saveAreaWindows(area: Area, window_lines: Sequence[str]) -> None:
    """Validate the lines, then rewrite this area's windows in loom.toml.

    Raises:
        ConfigError: a line does not parse, or the file cannot be read.
    """
    if not window_lines:
        raise ConfigError('at least one window is required')
    for line in window_lines:
        config.parseWindow(line, f'areas.{area}')

    document = _readConfigDocument()
    document['areas'][area]['windows'] = list(window_lines)
    _writeConfigDocument(document)


def saveAreaColor(area: Area, raw_color: str) -> None:
    """Rewrite this area's calendar color in loom.toml.

    Raises:
        ConfigError: the color is not "#rrggbb", or the file cannot be read.
    """
    color = config.parseColor(raw_color, f'areas.{area}')
    document = _readConfigDocument()
    document['areas'][area]['color'] = str(color)
    _writeConfigDocument(document)


def readPaletteEntries() -> tuple[tuple[str, str], ...]:
    """Read the [palette] entries from loom.toml as (name, hex) pairs.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _readStringTable('palette')


def savePalette(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [palette] in loom.toml.

    Raises:
        ConfigError: a name is empty or repeated, a color is not "#rrggbb", or the
            change would orphan a [categories] rule pointing at a removed name.
    """
    seen = set()
    for name, raw_color in entries:
        if not name:
            raise ConfigError('loom.toml [palette]: a name is empty')
        if name in seen:
            raise ConfigError(f'loom.toml [palette]: {name!r} appears twice')
        seen.add(name)
        config.parseColor(raw_color, 'palette')

    for pattern_text, category in readCategoryRuleEntries():
        if category not in seen:
            raise ConfigError(f'{category!r} is still used by the category rule {pattern_text!r}')

    _writeStringTable('palette', entries)


def readCategoryRuleEntries() -> tuple[tuple[str, str], ...]:
    """Read the [categories] entries from loom.toml as (pattern, category) pairs.

    Raises:
        ConfigError: the file is missing, not TOML, or an entry is malformed.
    """
    return _readStringTable('categories')


def saveCategoryRules(entries: Sequence[tuple[str, str]]) -> None:
    """Rewrite [categories] in loom.toml.

    Raises:
        ConfigError: a pattern is empty, repeated, or does not compile, or a category
            is not a palette name.
    """
    palette_names = {name for name, _ in readPaletteEntries()}
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

    _writeStringTable('categories', entries)


def _readConfigDocument() -> tomlkit.TOMLDocument:
    return tomlkit.parse(config.CONFIG_PATH.read_text(encoding='utf-8'))


def _setKeepingTrivia(table: Table, key: str, value: object) -> None:
    """Set a key in a table, and keep the comment above the next section attached to that section."""
    trailing = _popTrailingTrivia(table)
    table[key] = value
    table.value.body.extend(trailing)


def _popTrailingTrivia(table: Table) -> list[tuple[Key | None, Item]]:
    """Detach the blank lines and comments after a table's last key, in file order.

    Those comments are about the NEXT section of the file. A key added to the table, or a
    table appended after it, would land below them and split them from that section. The
    caller re-attaches them after its addition.
    """
    trailing: list[tuple[Key | None, Item]] = []
    body = table.value.body
    while body and body[-1][0] is None and isinstance(body[-1][1], (Comment, Whitespace)):
        trailing.insert(0, body.pop())

    return trailing


def _writeConfigDocument(document: tomlkit.TOMLDocument) -> None:
    config_path = config.CONFIG_PATH
    temp_path = config_path.with_name(config_path.name + '.tmp')
    with temp_path.open('w', encoding='utf-8') as handle:
        handle.write(tomlkit.dumps(document))
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(config_path)


def _readStringTable(table_name: str) -> tuple[tuple[str, str], ...]:
    table = config.readConfigTable(config.CONFIG_PATH).get(table_name, {})
    if not isinstance(table, dict) or not all(isinstance(value, str) for value in table.values()):
        raise ConfigError(f'loom.toml [{table_name}]: must be a table of string values')

    return tuple((key, value) for key, value in table.items())


def _writeStringTable(table_name: str, entries: Sequence[tuple[str, str]]) -> None:
    document = _readConfigDocument()
    if table_name in document:
        # tomlkit attaches comments to the table object. Edit the table in place to keep them
        table = document[table_name]
        for key in list(table):
            if key not in dict(entries):
                del table[key]
        for name, value in entries:
            table[name] = value
    else:
        document[table_name] = dict(entries)
    _writeConfigDocument(document)
