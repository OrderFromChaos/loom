"""The POST routes that edit loom.toml: area hours, colors, the palette, category rules, and the areas.

Every route but the area order restarts the server, because CONFIG parses once at startup.
"""

from __future__ import annotations

from datetime import UTC, datetime

from loom import config, live_config
from loom.config import CONFIG
from loom.vocabulary import Area
from loom.web import request_fields, responses
from loom.web.web_vocabulary import FormError, Request, Response


def handleSaveWindows(request: Request) -> Response:
    """Write this area's windows into loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: a window line does not parse. Nothing is written.
    """
    area = request_fields.readArea(request)
    lines = [line.strip() for line in request_fields.readFormText(request.form, 'windows').splitlines() if line.strip()]
    live_config.saveAreaWindows(area, lines)
    return responses.buildRestartPage(request_fields.readReturnTarget(request, area))


def handleSaveAreaColor(request: Request) -> Response:
    """Write this area's calendar color into loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: the color is not "#rrggbb". Nothing is written.
    """
    area = request_fields.readArea(request)
    live_config.saveAreaColor(area, request_fields.readFormText(request.form, 'color'))
    return responses.buildRestartPage(request_fields.readReturnTarget(request, area))


def handleSavePalette(request: Request) -> Response:
    """Write the [palette] presets into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "Name #rrggbb". Nothing is written.
        ConfigError: a preset is malformed or still in use. Nothing is written.
    """
    entries = _parseLinePairs(request.form.get('palette', ''), separator=' ', shape='Name #rrggbb')
    live_config.savePalette(entries)
    return responses.buildRestartPage(request_fields.readReturnTarget(request, request_fields.readArea(request)))


def _parseLinePairs(raw_lines: str, *, separator: str, shape: str) -> list[tuple[str, str]]:
    """Split a textarea into (left, right) pairs at the last `separator` of each line.

    Raises:
        FormError: a line has no separator, or an empty side. The message has `shape`.
    """
    entries = []
    for line in raw_lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        left, found, right = stripped.rpartition(separator)
        if not (found and left.strip() and right.strip()):
            raise FormError(f'{stripped!r} is not "{shape}"')
        entries.append((left.strip(), right.strip()))

    return entries


def handleSaveCategories(request: Request) -> Response:
    """Write the [categories] rule table into loom.toml, then restart so CONFIG reloads.

    Raises:
        FormError: a line is not "pattern -> Category". Nothing is written.
        ConfigError: the regex does not compile, or the category is not a palette name.
            Nothing is written.
    """
    entries = _parseLinePairs(request.form.get('categories', ''), separator=' -> ', shape='pattern -> Category')
    live_config.saveCategoryRules(entries)
    return responses.buildRestartPage(request_fields.readReturnTarget(request, request_fields.readArea(request)))


def handleAddArea(request: Request) -> Response:
    """Append a new [areas.<name>] table to loom.toml, then restart so CONFIG reloads.

    Raises:
        ConfigError: the name is malformed, reserved, or taken, or the color is not
            "#rrggbb". Nothing is written.
    """
    area_name = request_fields.readFormText(request.form, 'name').strip().lower()
    live_config.saveNewArea(area_name, request_fields.readFormText(request.form, 'color').strip())
    # land on the new area's settings page, where its hours can be tightened
    return responses.buildRestartPage(f'/{area_name}/settings?saved=1')


def handleSaveAreaOrder(request: Request) -> Response:
    """Save the switcher order from a drag and drop, then return to the page. There is no restart.

    Raises:
        ConfigError: a name is not an area, is hidden, or repeats. Nothing is written.
    """
    areas = [
        Area(area_name) for area_name in request_fields.readFormText(request.form, 'order').split(',') if area_name
    ]
    live_config.saveAreaOrder(areas)
    return responses.buildReturnRedirect(request, request_fields.readArea(request))


def handleFinishArea(request: Request) -> Response:
    """Mark this area finished in loom.toml, then restart on the home area, or another open one.

    Raises:
        ConfigError: this is the last open area. Nothing is written.
    """
    area = request_fields.readArea(request)
    live_config.saveAreaFinished(area, config.computeLocalDate(datetime.now(UTC)))

    # land on home, else a switcher area before a hidden one
    still_open = sorted(
        (other for other in config.listOpenAreas() if other != area),
        key=lambda other: CONFIG.areas[other].hidden,
    )
    landing = request.home_area if request.home_area in still_open else still_open[0]
    return responses.buildRestartPage(f'/{landing}/tasks')


def handleReopenArea(request: Request) -> Response:
    """Clear this area's finished date in loom.toml, then restart and land on it.

    Raises:
        ConfigError: the area vanished from loom.toml. Nothing is written.
    """
    area = request_fields.readArea(request)
    live_config.saveAreaFinished(area, None)
    return responses.buildRestartPage(f'/{area}/tasks')
