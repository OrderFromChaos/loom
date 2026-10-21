"""Build the responses a route returns: a page, a redirect, an error page, or the restart page."""

from __future__ import annotations

import json
from http import HTTPStatus

from loom.vocabulary import Area
from loom.web import page_shell, request_fields
from loom.web.web_vocabulary import Request, Response


_HTML_CONTENT_TYPE = 'text/html; charset=utf-8'


def buildHtmlResponse(body: str) -> Response:
    return Response(status=HTTPStatus.OK, body=body, content_type=_HTML_CONTENT_TYPE, location=None)


def buildRedirect(location: str) -> Response:
    return Response(status=HTTPStatus.SEE_OTHER, body='', content_type=_HTML_CONTENT_TYPE, location=location)


def buildReturnRedirect(request: Request, area: Area) -> Response:
    return buildRedirect(request_fields.readReturnTarget(request, area))


def buildErrorPage(status: HTTPStatus, heading: str, message: str) -> Response:
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: {status.value}</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<link rel="stylesheet" href="/static/style.css"></head><body><main>'
        f'<div class="banner warn"><strong>{page_shell.escapeHtml(heading)}</strong>'
        f'<p>{page_shell.escapeHtml(message)}</p>'
        f'<p><a href="/">back to the calendar</a></p></div></main></body></html>'
    )
    return Response(status=status, body=body, content_type=_HTML_CONTENT_TYPE, location=None)


def buildRestartPage(target_path: str) -> Response:
    """Build the page shown while the server re-executes itself. It loads the target once the server is back.

    The server goes down right after sending the page, so a linked stylesheet would fail
    to load and leave a bare white page. The style and script are inline. The script
    polls until the server answers, and a blind refresh would land on the browser's white
    error page. The meta refresh is in <noscript>, so it covers only a browser without
    scripts.
    """
    RESTART_STYLE = (
        'html,body{margin:0;background:#0d1117;color:#e6edf3;'
        "font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif}"
        'main{padding:0.75rem 1rem}a{color:#58a6ff}'
        '.banner{border:1px solid #30363d;border-radius:8px;background:#161b22;padding:0.6rem 0.9rem;margin:0.6rem 0}'
    )
    RESTART_POLL = (
        '(function () {\n'
        '  const target = TARGET;\n'
        '  function poll() {\n'
        "    fetch('/static/style.css', { cache: 'no-store' })\n"
        '      .then((response) => { if (response.ok) window.location.replace(target); else setTimeout(poll, 300); })\n'
        '      .catch(() => setTimeout(poll, 300));\n'
        '  }\n'
        '  setTimeout(poll, 300);\n'
        '})();'
    )
    target_attribute = page_shell.escapeHtml(target_path)
    body = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>loom: restarting</title>'
        f'<meta name="color-scheme" content="dark">'
        f'<noscript><meta http-equiv="refresh" content="3;url={target_attribute}"></noscript>'
        f'<style>{RESTART_STYLE}</style></head><body><main>'
        f'<div class="banner"><strong>Saved.</strong> Restarting the server with the new config&hellip; '
        f'<a href="{target_attribute}">continue</a></div></main>'
        f'<script>{RESTART_POLL.replace("TARGET", json.dumps(target_path))}</script></body></html>'
    )
    return Response(status=HTTPStatus.OK, body=body, content_type=_HTML_CONTENT_TYPE, location=None, restart=True)
