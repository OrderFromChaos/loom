"""Loom: goals, infinitely nested tasks, and an auto-scheduled calendar.

Run from the project root, where loom.toml lives:

    $ uv run loom serve              # the web UI; / lands on the work area
    $ uv run loom serve --area demo  # / lands on the demo area instead
    $ uv run loom serve --port 8322  # a second instance beside the always-on one
    $ uv run loom plan               # re-plan every area and print each result
    $ uv run loom check              # report integrity problems in the data files

deploy/loom.service runs `loom serve` as a systemd user service from login on; its
header has the install, restart and log commands.

Modules in reading order:

    vocabulary  every shared type
    config      loom.toml -> the frozen CONFIG singleton
    store       reading and writing one area's data files
    graph       hierarchy walks, dependency blocking, actionability
    timemap     weekly windows -> concrete free UTC slots
    scheduler   candidate selection and greedy placement
    recurring   templates that stamp out real tasks on a cadence
    triage      staleness and deadline pressure, derived at read time
    coloring    keyword -> calendar color
    web         the local web UI: server, pages, geometry, static files

Exit codes:
    0  success; for check, no problems found
    1  check found problems, or a data file failed to parse
    2  the command line or loom.toml was unusable
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from loom.vocabulary import Area, ConfigError, StoreError


EXIT_SUCCESS = 0
EXIT_FINDINGS = 1
EXIT_BAD_SETUP = 2

_STANDARD_RECORD_KEYS = frozenset(logging.LogRecord('', 0, '', 0, '', None, None).__dict__) | {'message', 'asctime'}
_LOCATION_KEYS = ('module', 'lineno', 'funcName')

LOG = logging.getLogger('loom')


def main() -> int:
    """Dispatch one command.

    Raises:
        Nothing: config and data failures become messages and exit codes 2 and 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('serve', 'plan', 'check'))
    # the valid names live in loom.toml, which only loads inside the try below
    parser.add_argument('--area', default='work', help='the area / lands on')
    parser.add_argument('--port', type=int, default=None, help='listen here instead of [web] port in loom.toml')
    args = parser.parse_args()
    configureLogging()

    try:
        match args.command:
            case 'serve':
                return servePages(Area(args.area), args.port)
            case 'plan':
                return printPlans()
            case 'check':
                return reportProblems()
            case _:
                parser.error(f'unknown command {args.command!r}')
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_BAD_SETUP
    except StoreError as exc:
        print(exc, file=sys.stderr)
        return EXIT_FINDINGS


def configureLogging() -> None:
    global LOG
    handler = logging.StreamHandler()
    handler.setFormatter(FieldFormatter())
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def servePages(home: Area, port: int | None) -> int:
    # imported inside the command: loom.config parses loom.toml at import time, and a
    # malformed config should print one line and exit 2, not die in an import traceback
    from loom.config import CONFIG
    from loom.web import server

    if home not in CONFIG.areas:
        raise ConfigError(f'--area {home!r} is not in loom.toml; areas: {", ".join(CONFIG.areas)}')

    return server.serveForever(home, port if port is not None else CONFIG.web.port)


def printPlans() -> int:
    from loom import recurring, scheduler
    from loom.config import openAreas

    now = datetime.now(UTC)
    for area in openAreas():
        spawned = recurring.spawnDue(area, now)
        if spawned:
            print(f'{area}: {spawned} recurring task(s) spawned')
        plan = scheduler.runPlan(area, now)
        print(f'{area}: {len(plan.blocks)} blocks planned, {len(plan.unplaced)} unplaced')
        for entry in plan.unplaced:
            print(f'  unplaced {entry.task}: {entry.reason.value}')

    return EXIT_SUCCESS


def reportProblems() -> int:
    from loom.config import CONFIG
    from loom.graph import checkIntegrity
    from loom.store import storeFor

    total = 0
    for area in CONFIG.areas:
        area_store = storeFor(area)
        problems = checkIntegrity(area_store.loadTasks(), area_store.loadGoals())
        for problem in problems:
            print(f'{area}: {problem}')
        total += len(problems)

    print(f'{total} problems found')
    return EXIT_FINDINGS if total else EXIT_SUCCESS


### vocabulary #########################################################################


class FieldFormatter(logging.Formatter):
    """Render the event name plus every extra field, so tallies reach the operator."""

    def format(self, record: logging.LogRecord) -> str:
        extra = {key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_KEYS}
        located = {key: getattr(record, key) for key in _LOCATION_KEYS}
        fields = ' '.join(f'{key}={value}' for key, value in sorted({**located, **extra}.items()))
        return f'{record.levelname} {record.getMessage()} {fields}'


if __name__ == '__main__':
    sys.exit(main())
