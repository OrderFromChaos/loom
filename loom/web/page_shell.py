"""The frame every page shares: HTML shell, navigation, and escaping.

renderPage wraps a page body in the <head>, the hard work | personal switcher, the view
tabs and the reschedule button. esc() is the mandatory escape for every interpolated
value; no page module interpolates raw text.
"""

from __future__ import annotations

import html

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
        f'<a class="{"tab active" if view is active else "tab"}" '
        f'href="/{area.value}/{view.value}">{view.value.title()}</a>'
        for view in View
    )
    area_links = '\n'.join(
        f'<span class="area-now">{candidate.value}</span>'
        if candidate is area
        else f'<a class="area-link" href="/{candidate.value}/{active.value}">{candidate.value}</a>'
        for candidate in Area
    )
    here = f'/{area.value}/{active.value}'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - loom</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/calendar.js" defer></script>
<script src="/static/tree.js" defer></script>
</head>
<body data-area="{area.value}">
<header class="topbar">
  <span class="brand">loom</span>
  <nav class="tabs">{tabs}</nav>
  <span class="spacer"></span>
  <form method="post" action="/{area.value}/reschedule" class="inline-form">
    <input type="hidden" name="return_to" value="{esc(here)}">
    <button class="button" title="re-plan after editing files by hand">Reschedule</button>
  </form>
  <nav class="areas">
{area_links}
  </nav>
</header>
<main class="view-{active.value}">
{body}
</main>
</body>
</html>
"""
