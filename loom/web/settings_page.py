"""Render the settings page: working hours, the color palette, and keyword colors.

All three sections edit loom.toml in place and restart the server, because CONFIG
parses once at startup; each save button says so. The palette and keyword tables are
global; the working hours belong to the page's area.
"""

from __future__ import annotations

from loom.config import CONFIG, rawAreaWindows, rawKeywordColors, rawPalette
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
        f'{notice}{_renderWindowsSection(area)}{_renderPaletteSection(area)}{_renderKeywordSection(area)}',
    )


def _renderWindowsSection(area: Area) -> str:
    lines = '\n'.join(rawAreaWindows(area))
    return (
        f'<h2>{area.value} hours</h2>'
        f'<p class="hint">One per line, local time ({esc(str(CONFIG.timezone))}): '
        f'<code>Mon-Fri 09:00-12:00</code> or <code>Sat 10:00-18:00</code></p>'
        f'<form method="post" action="/{area.value}/settings/windows" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area.value}/settings?saved=1">'
        f'<textarea name="windows" rows="6" spellcheck="false">{esc(lines)}</textarea>'
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
        f'<form method="post" action="/{area.value}/settings/palette" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area.value}/settings?saved=1">'
        f'<textarea name="palette" rows="7" spellcheck="false">{esc(lines)}</textarea>'
        f'<button class="button">Save and restart</button></form>'
    )


def _renderKeywordSection(area: Area) -> str:
    lines = '\n'.join(f'{keyword} {value}' for keyword, value in rawKeywordColors())
    return (
        f'<h2>Keyword colors</h2>'
        f'<p class="hint">One per line: <code>gym #2ea043</code></p>'
        f'<form method="post" action="/{area.value}/settings/colors" class="settings-form">'
        f'<input type="hidden" name="return_to" value="/{area.value}/settings?saved=1">'
        f'<textarea name="colors" rows="5" spellcheck="false">{esc(lines)}</textarea>'
        f'<button class="button">Save and restart</button></form>'
    )
