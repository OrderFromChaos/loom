"""The frame every page shares: HTML shell, navigation, and escaping.

renderPage wraps a page body in the <head>, the view tabs and the area switcher, each
area underlined in its calendar color: open areas as links in their loom.toml order (drag one to reorder), then
a + menu that adds an area, links the hidden ones (the demo area), and reopens finished
ones. A hidden or finished area being viewed shows leftmost, struck through when
finished.

esc() is the mandatory escape for every interpolated value; no page module
interpolates raw text.
"""

from __future__ import annotations

import html

from loom.config import CONFIG, rawAreaOrder
from loom.vocabulary import Area
from loom.web.web_vocabulary import View


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def renderPage(
    title: str,
    area: Area,
    active: View,
    body: str,
) -> str:
    """Wrap a page body in the shared shell; `active` highlights the current tab."""
    tabs = '\n'.join(
        f'<a class="{"tab active" if view is active else "tab"}" href="/{area}/{view.value}">{view.value.title()}</a>'
        for view in View
    )
    switcher = _switcherAreas()
    if area not in switcher:
        switcher.insert(0, area)
    area_links = '\n'.join(_renderAreaLink(candidate, area, active) for candidate in switcher)
    here = f'/{area}/{active.value}'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{esc(title)} - loom</title>
<script>
  // hold painting until restore.js has reapplied folds and scroll; the timeout is a
  // failsafe so a failed script never leaves the page blank
  document.documentElement.classList.add('restoring');
  setTimeout(() => document.documentElement.classList.remove('restoring'), 1000);
</script>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/calendar.js" defer></script>
<script src="/static/tree.js" defer></script>
<script src="/static/pickers.js" defer></script>
<script src="/static/areas.js" defer></script>
<script src="/static/restore.js" defer></script>
</head>
<body data-area="{area}">
<header class="topbar">
  <span class="brand">loom</span>
  <nav class="tabs">{tabs}</nav>
  <span class="spacer"></span>
  <nav class="areas">
{area_links}
{_renderAreaMenu(area)}
  </nav>
  <form method="post" action="/{area}/areas/order" class="area-order-form" hidden>
    <input type="hidden" name="return_to" value="{esc(here)}">
    <input type="hidden" name="order" value="">
  </form>
</header>
<main class="view-{active.value}">
{body}
</main>
</body>
</html>
"""


def _switcherAreas() -> list[Area]:
    """Open, unhidden areas: numbered ones by their order, then the rest in file order."""
    order = rawAreaOrder()
    in_file = list(CONFIG.areas)
    return sorted(
        (candidate for candidate in in_file if _onSwitcher(candidate)),
        key=lambda candidate: (candidate not in order, order.get(candidate, 0), in_file.index(candidate)),
    )


def _renderAreaLink(candidate: Area, area: Area, active: View) -> str:
    finished = ' finished' if CONFIG.areas[candidate].finished is not None else ''
    drag = f' draggable="true" data-area-name="{candidate}"' if _onSwitcher(candidate) else ''
    color = f' style="--area-color:{esc(CONFIG.areas[candidate].color)}"'
    if candidate == area:
        return f'<span class="area-now{finished}"{color}{drag}>{candidate}</span>'

    return f'<a class="area-link{finished}" href="/{candidate}/{active.value}"{color}{drag}>{candidate}</a>'


def _onSwitcher(candidate: Area) -> bool:
    settings = CONFIG.areas[candidate]
    return settings.finished is None and not settings.hidden


def _renderAreaMenu(area: Area) -> str:
    """The + beside the switcher: add an area, the hidden areas, and the finished ones to reopen."""
    hidden = ''.join(
        f'<li><a href="/{name}/tasks">{name}</a></li>'
        for name, settings in CONFIG.areas.items()
        if settings.hidden and settings.finished is None
    )
    hidden_list = f'<ul class="finished-areas">{hidden}</ul>' if hidden else ''
    finished = ''.join(
        f'<li><a href="/{name}/tasks">{name}</a> <span class="hint">finished {settings.finished.isoformat()}</span>'
        f'<form method="post" action="/{name}/area/reopen" class="inline-form">'
        f'<button class="button small">reopen</button></form></li>'
        for name, settings in CONFIG.areas.items()
        if settings.finished is not None
    )
    finished_list = f'<ul class="finished-areas">{finished}</ul>' if finished else ''
    return (
        f'<details class="area-menu"><summary title="add an area, or open a hidden or finished one">+</summary>'
        f'<div class="area-menu-panel">'
        f'<form method="post" action="/{area}/areas/add" class="add-form">'
        # the swatch leads: the browser's color popup opens rightwards from it, and this
        # menu hugs the window's right edge
        f'<input type="color" name="color" value="#e3b341" title="calendar color">'
        f'<input type="text" name="name" required placeholder="new area">'
        f'<button class="button small">Add</button></form>'
        f'{hidden_list}{finished_list}</div></details>'
    )
