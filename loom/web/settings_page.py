"""Render the settings page: the area's hours and calendar color, then the shared colors.

Every section edits loom.toml in place and restarts the server, because CONFIG parses
once at startup. The palette and category sections are global. The hours and calendar
color belong to the page's area.
"""

from __future__ import annotations

from loom import live_config
from loom.config import CONFIG
from loom.vocabulary import Area
from loom.web import page_shell
from loom.web.web_vocabulary import View


def renderSettingsPage(area: Area, *, saved: bool) -> str:
    """Render the settings page. `saved` shows the after-restart confirmation banner."""
    notice = ''
    if saved:
        notice = '<div class="banner"><strong>Saved.</strong> The server restarted with the new config.</div>'

    return page_shell.renderPage(
        'settings',
        area,
        View.SETTINGS,
        f'{notice}{_renderWindowsSection(area)}{_renderAreaColorSection(area)}'
        f'{_renderPaletteSection(area)}{_renderCategorySection(area)}',
    )


def _renderWindowsSection(area: Area) -> str:
    lines = '\n'.join(live_config.readAreaWindowLines(area))
    return (
        f'<h2>{area} hours</h2>'
        f'<p class="hint">One per line, local time ({page_shell.escapeHtml(str(CONFIG.timezone))}): '
        f'<code>Mon-Fri 09:00-12:00</code> or <code>Sat 10:00-18:00</code></p>'
        f'{_renderLinesForm(area, "windows", 6, lines)}'
    )


def _renderAreaColorSection(area: Area) -> str:
    color_input = f'<input type="color" name="color" value="{page_shell.escapeHtml(CONFIG.areas[area].color)}">'
    return f'<h2>{area} calendar color</h2>{_renderSaveForm(area, "color", color_input)}'


def _renderPaletteSection(area: Area) -> str:
    entries = live_config.readPaletteEntries()
    lines = '\n'.join(f'{name} {value}' for name, value in entries)
    swatches = ''.join(
        f'<span class="swatch" style="--block-color:{page_shell.escapeHtml(value)}">'
        f'{page_shell.escapeHtml(name)}</span>'
        for name, value in entries
    )
    return (
        f'<h2>Color palette</h2>'
        f'<p class="hint">One per line: <code>Self-Improvement #a371f7</code></p>'
        f'<p>{swatches}</p>'
        f'{_renderLinesForm(area, "palette", 7, lines)}'
    )


def _renderCategorySection(area: Area) -> str:
    lines = '\n'.join(f'{pattern} -> {category}' for pattern, category in live_config.readCategoryRuleEntries())
    return (
        f'<h2>Category rules</h2>'
        f'<p class="hint">One per line, regex &rarr; palette category, first match wins: '
        f'<code>standup|sync -> W-2 Work</code></p>'
        f'{_renderLinesForm(area, "categories", 6, lines)}'
    )


def _renderLinesForm(
    area: Area,
    setting: str,
    rows: int,
    lines: str,
) -> str:
    """Render the save form for `setting`, with `lines` in a textarea of `rows` rows.

    Raises:
        Nothing: the lines are escaped, and the call reads no file.
    """
    textarea = f'<textarea name="{setting}" rows="{rows}" spellcheck="false">{page_shell.escapeHtml(lines)}</textarea>'
    return _renderSaveForm(area, setting, textarea)


def _renderSaveForm(area: Area, setting: str, field_html: str) -> str:
    return (
        f'<form method="post" action="/{area}/settings/{setting}" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area}/settings?saved=1">'
        f'{field_html}<button class="button">Save and restart</button></form>'
    )
