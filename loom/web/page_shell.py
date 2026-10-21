"""The frame every page shares, and the small pieces of markup several pages render.

renderPage wraps a page body in the <head>, the view tabs and the area switcher. The
switcher has a link per open area, underlined in its calendar color and in readAreaOrder
order (drag one to reorder), then a + menu to add an area, open a hidden one (the demo
area) or reopen a finished one. A hidden or finished area that is being viewed is first,
struck through when finished.

Pass every interpolated value through escapeHtml().
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence

from loom import live_config
from loom.config import CONFIG
from loom.vocabulary import Area, Task, TaskId
from loom.web.web_vocabulary import View


def renderPage(
    title: str,
    area: Area,
    active: View,
    body_html: str,
) -> str:
    """Wrap a page body in the shared shell. `active` highlights the current tab."""
    tabs = '\n'.join(
        f'<a class="{"tab active" if view is active else "tab"}" href="/{area}/{view.value}">{view.value.title()}</a>'
        for view in View
    )
    switcher = _listSwitcherAreas()
    if area not in switcher:
        switcher = (area, *switcher)
    area_links = '\n'.join(_renderAreaLink(candidate, area, active) for candidate in switcher)
    here = f'/{area}/{active.value}'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{escapeHtml(title)} - loom</title>
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
    <input type="hidden" name="return_to" value="{escapeHtml(here)}">
    <input type="hidden" name="order" value="">
  </form>
</header>
<main class="view-{active.value}">
{body_html}
</main>
</body>
</html>
"""


def escapeHtml(raw_text: str) -> str:
    return html.escape(raw_text, quote=True)


def _listSwitcherAreas() -> tuple[Area, ...]:
    """List the open, unhidden areas: numbered ones by their order, then the rest in file order."""
    order = live_config.readAreaOrder()
    in_file = list(CONFIG.areas)
    return tuple(
        sorted(
            (candidate for candidate in in_file if _shownOnSwitcher(candidate)),
            key=lambda candidate: (candidate not in order, order.get(candidate, 0), in_file.index(candidate)),
        ),
    )


def _shownOnSwitcher(candidate: Area) -> bool:
    settings = CONFIG.areas[candidate]
    return settings.finished is None and not settings.hidden


def _renderAreaLink(candidate: Area, area: Area, active: View) -> str:
    finished = ' finished' if CONFIG.areas[candidate].finished is not None else ''
    drag = f' draggable="true" data-area-name="{candidate}"' if _shownOnSwitcher(candidate) else ''
    color = f' style="--area-color:{escapeHtml(CONFIG.areas[candidate].color)}"'
    if candidate == area:
        return f'<span class="area-now{finished}"{color}{drag}>{candidate}</span>'

    return f'<a class="area-link{finished}" href="/{candidate}/{active.value}"{color}{drag}>{candidate}</a>'


def _renderAreaMenu(area: Area) -> str:
    """Render the + beside the switcher: add an area, the hidden areas, and the finished ones to reopen."""
    NEW_AREA_COLOR = '#e3b341'
    hidden = ''.join(
        f'<li><a href="/{name}/tasks">{name}</a></li>'
        for name, settings in CONFIG.areas.items()
        if settings.hidden and settings.finished is None
    )
    hidden_list = f'<ul class="finished-areas">{hidden}</ul>' if hidden else ''
    finished = ''.join(
        f'<li><a href="/{name}/tasks">{name}</a> <span class="hint">finished {settings.finished.isoformat()}</span>'
        f'{renderPostButton(f"/{name}/area/reopen", "reopen", button_class="button small")}</li>'
        for name, settings in CONFIG.areas.items()
        if settings.finished is not None
    )
    finished_list = f'<ul class="finished-areas">{finished}</ul>' if finished else ''
    return (
        f'<details class="area-menu"><summary title="add an area, or open a hidden or finished one">+</summary>'
        f'<div class="area-menu-panel">'
        f'<form method="post" action="/{area}/areas/add" class="add-form">'
        # the browser's color popup opens to the right of its input, and this menu is at
        # the right edge of the window. Keep the swatch first
        f'<input type="color" name="color" value="{NEW_AREA_COLOR}" title="calendar color">'
        f'<input type="text" name="name" required placeholder="new area">'
        f'<button class="button small">Add</button></form>'
        f'{hidden_list}{finished_list}</div></details>'
    )


def renderPostButton(
    action: str,
    label_html: str,
    *,
    button_class: str,
    form_class: str = 'inline-form',
    return_to: str | None = None,
    title: str | None = None,
    hidden_fields: Sequence[tuple[str, str]] = (),
) -> str:
    """Render a form whose one button posts to `action`. Only label_html goes in unescaped."""
    fields = ''
    if return_to is not None:
        fields += f'<input type="hidden" name="return_to" value="{escapeHtml(return_to)}">'
    for name, value in hidden_fields:
        fields += f'<input type="hidden" name="{escapeHtml(name)}" value="{escapeHtml(value)}">'
    title_attribute = f' title="{escapeHtml(title)}"' if title is not None else ''

    return (
        f'<form method="post" action="{escapeHtml(action)}" class="{form_class}">{fields}'
        f'<button class="{button_class}"{title_attribute}>{label_html}</button></form>'
    )


def renderOptions(choices: Iterable[tuple[str, str]], selected_value: str | None) -> str:
    """Render one <option> per (value, label) pair, selecting the one whose value is selected_value."""
    return ''.join(
        f'<option value="{escapeHtml(value)}"{" selected" if value == selected_value else ""}>'
        f'{escapeHtml(label)}</option>'
        for value, label in choices
    )


def findTaskTitle(task_id: TaskId, tasks: Mapping[TaskId, Task]) -> str:
    task = tasks.get(task_id)
    return task.title if task is not None else f'missing task {task_id}'
