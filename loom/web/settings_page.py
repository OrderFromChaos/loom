"""Render the settings page: the area's hours and calendar color, then the shared colors.

Every section edits loom.toml in place and restarts the server, because CONFIG parses
once at startup; each save button says so. The palette and category sections are
global; the hours and calendar color belong to the page's area. Areas are added and reopened from
the + beside the area switcher, and finished from the bottom of the tasks page.
"""

from __future__ import annotations

from loom.config import CONFIG, rawAreaWindows, rawCategoryRules, rawPalette
from loom.vocabulary import Area
from loom.web.page_shell import esc, renderPage
from loom.web.web_vocabulary import View


def renderSettingsPage(area: Area, saved: bool) -> str:
    """The whole settings page; `saved` shows the after-restart confirmation banner."""
    notice = ''
    if saved:
        notice = '<div class="banner"><strong>Saved.</strong> The server restarted with the new config.</div>'

    return renderPage(
        'settings',
        area,
        View.SETTINGS,
        f'{notice}{_renderWindowsSection(area)}{_renderAreaColorSection(area)}'
        f'{_renderPaletteSection(area)}{_renderCategorySection(area)}',
    )


def _renderWindowsSection(area: Area) -> str:
    lines = '\n'.join(rawAreaWindows(area))
    return (
        f'<h2>{area} hours</h2>'
        f'<p class="hint">One per line, local time ({esc(str(CONFIG.timezone))}): '
        f'<code>Mon-Fri 09:00-12:00</code> or <code>Sat 10:00-18:00</code></p>'
        f'<form method="post" action="/{area}/settings/windows" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area}/settings?saved=1">'
        f'<textarea name="windows" rows="6" spellcheck="false">{esc(lines)}</textarea>'
        f'<button class="button">Save and restart</button></form>'
    )


def _renderAreaColorSection(area: Area) -> str:
    return (
        f'<h2>{area} calendar color</h2>'
        f'<form method="post" action="/{area}/settings/color" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area}/settings?saved=1">'
        f'<input type="color" name="color" value="{esc(CONFIG.areas[area].color)}">'
        f'<button class="button">Save and restart</button></form>'
    )


def _renderPaletteSection(area: Area) -> str:
    lines = '\n'.join(f'{name} {value}' for name, value in rawPalette())
    swatches = ''.join(
        f'<span class="swatch" style="--block-color:{esc(value)}">{esc(name)}</span>' for name, value in rawPalette()
    )
    return (
        f'<h2>Color palette</h2>'
        f'<p class="hint">One per line: <code>Self-Improvement #a371f7</code></p>'
        f'<p>{swatches}</p>'
        f'<form method="post" action="/{area}/settings/palette" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area}/settings?saved=1">'
        f'<textarea name="palette" rows="7" spellcheck="false">{esc(lines)}</textarea>'
        f'<button class="button">Save and restart</button></form>'
    )


def _renderCategorySection(area: Area) -> str:
    lines = '\n'.join(f'{pattern} -> {category}' for pattern, category in rawCategoryRules())
    return (
        f'<h2>Category rules</h2>'
        f'<p class="hint">One per line, regex &rarr; palette category, first match wins: '
        f'<code>standup|sync -> W-2 Work</code></p>'
        f'<form method="post" action="/{area}/settings/categories" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area}/settings?saved=1">'
        f'<textarea name="categories" rows="6" spellcheck="false">{esc(lines)}</textarea>'
        f'<button class="button">Save and restart</button></form>'
    )
