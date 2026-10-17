"""Finishing and reopening areas in loom.toml, against a throwaway copy of the file.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_config.py
"""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

import pytest

from loom import config, live_config
from loom.vocabulary import Area, ConfigError


_TWO_AREAS = """timezone = "America/Los_Angeles"
data_root = "data"
horizon_days = 14

[scheduler]
min_chunk_minutes = 25
max_chunk_minutes = 90
stale_grace_minutes = 30

[web]
port = 8321
day_start = "08:00"
day_end = "22:00"
scroll_start = "08:00"

# the work area
[areas.work]
windows = ["Mon-Fri 09:00-17:00"]
color = "#58a6ff"

# the side project
[areas.zed]
windows = ["Mon-Sun 09:00-21:00"]
color = "#e3b341"
"""


@pytest.fixture(name='scratch_config')
def writeScratchConfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / 'loom.toml'
    path.write_text(_TWO_AREAS, encoding='utf-8')
    monkeypatch.setattr(config, 'CONFIG_PATH', path)
    return path


def testFinishThenReopenRoundTrips(scratch_config: Path) -> None:
    live_config.saveAreaFinished(Area('zed'), date(2026, 10, 16))

    finished = config.parseConfig(scratch_config)
    assert finished.areas[Area('zed')].finished == date(2026, 10, 16)
    assert finished.areas[Area('work')].finished is None
    assert '# the work area' in scratch_config.read_text(encoding='utf-8')

    live_config.saveAreaFinished(Area('zed'), None)

    assert scratch_config.read_text(encoding='utf-8') == _TWO_AREAS


def testFinishedLandsAboveTheNextSectionsComment(scratch_config: Path) -> None:
    """The new key goes into the area's table, above the comment for the next table."""
    live_config.saveAreaFinished(Area('work'), date(2026, 10, 16))

    text = scratch_config.read_text(encoding='utf-8')
    assert 'color = "#58a6ff"\nfinished = 2026-10-16\n\n# the side project\n[areas.zed]' in text


def testTheLastOpenAreaCannotBeFinished(scratch_config: Path) -> None:
    live_config.saveAreaFinished(Area('zed'), date(2026, 10, 16))

    with pytest.raises(ConfigError, match='last open area'):
        live_config.saveAreaFinished(Area('work'), date(2026, 10, 16))


def testAConfigWithEveryAreaFinishedIsRefused(scratch_config: Path) -> None:
    text = _TWO_AREAS.replace('color = "#58a6ff"', 'color = "#58a6ff"\nfinished = 2026-10-01')
    scratch_config.write_text(text.replace('color = "#e3b341"', 'color = "#e3b341"\nfinished = 2026-10-02'))

    with pytest.raises(ConfigError, match='every area is finished'):
        config.parseConfig(scratch_config)


def testHiddenParsesAndMustBeABool(scratch_config: Path) -> None:
    scratch_config.write_text(_TWO_AREAS.replace('color = "#e3b341"', 'color = "#e3b341"\nhidden = true'))
    assert config.parseConfig(scratch_config).areas[Area('zed')].hidden
    assert not config.parseConfig(scratch_config).areas[Area('work')].hidden

    scratch_config.write_text(_TWO_AREAS.replace('color = "#e3b341"', 'color = "#e3b341"\nhidden = "yes"'))
    with pytest.raises(ConfigError, match='hidden'):
        config.parseConfig(scratch_config)


def testAreaOrderNumbersTheSwitcherAndSkipsHidden(scratch_config: Path) -> None:
    """Saving an order numbers each switcher area. A hidden area's stray number is dropped."""
    hidden_demo = '\n[areas.demo]\nwindows = ["Mon-Sun 09:00-21:00"]\ncolor = "#8957e5"\nhidden = true\norder = 9\n'
    scratch_config.write_text(_TWO_AREAS + hidden_demo, encoding='utf-8')

    live_config.saveAreaOrder([Area('zed'), Area('work')])

    assert live_config.readAreaOrder() == {Area('zed'): 1, Area('work'): 2}
    text = scratch_config.read_text(encoding='utf-8')
    assert 'color = "#58a6ff"\norder = 2\n\n# the side project\n[areas.zed]' in text
    config.parseConfig(scratch_config)


_THIRD_AREA = '\n# the errands\n[areas.errands]\nwindows = ["Sat 09:00-12:00"]\ncolor = "#3fb950"\n'


@pytest.mark.parametrize('order', list(itertools.permutations([Area('work'), Area('zed'), Area('errands')])))
def testAnyAreaOrderReadsBackAndKeepsTheComments(scratch_config: Path, order: tuple[Area, ...]) -> None:
    scratch_config.write_text(_TWO_AREAS + _THIRD_AREA, encoding='utf-8')

    live_config.saveAreaOrder(list(order))

    assert live_config.readAreaOrder() == {area: position for position, area in enumerate(order, start=1)}
    text = scratch_config.read_text(encoding='utf-8')
    for comment, table in (('the work area', 'work'), ('the side project', 'zed'), ('the errands', 'errands')):
        assert f'# {comment}\n[areas.{table}]' in text
    config.parseConfig(scratch_config)


def testAreaOrderRefusesHiddenAndRepeatedNames(scratch_config: Path) -> None:
    hidden_zed = _TWO_AREAS.replace('color = "#e3b341"', 'color = "#e3b341"\nhidden = true')
    scratch_config.write_text(hidden_zed, encoding='utf-8')

    with pytest.raises(ConfigError, match='hidden'):
        live_config.saveAreaOrder([Area('zed'), Area('work')])
    with pytest.raises(ConfigError, match='twice'):
        live_config.saveAreaOrder([Area('work'), Area('work')])
    assert scratch_config.read_text(encoding='utf-8') == hidden_zed


def testAreaColorSavesAndRejectsNonHex(scratch_config: Path) -> None:
    live_config.saveAreaColor(Area('zed'), '#123abc')
    assert config.parseConfig(scratch_config).areas[Area('zed')].color == '#123abc'

    with pytest.raises(ConfigError, match='rrggbb'):
        live_config.saveAreaColor(Area('zed'), 'teal')
