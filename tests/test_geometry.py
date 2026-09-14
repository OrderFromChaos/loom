"""Property tests for the calendar block geometry.

Run from the project root, where loom.toml lives:

    $ uv run pytest tests/test_geometry.py
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loom.web.block_geometry import layoutDay


_DAY_START = 7 * 60
_DAY_END = 23 * 60

# a span is (start_minute, end_minute); the alias keeps the strategy annotation readable
_SpanList = list[tuple[int, int]]


def _spans() -> st.SearchStrategy[_SpanList]:
    span = st.tuples(st.integers(min_value=0, max_value=1500), st.integers(min_value=1, max_value=400)).map(
        lambda pair: (pair[0], pair[0] + pair[1]),
    )
    return st.lists(span, max_size=10)


@given(spans=_spans())
def testBoxesStayInsideTheWindowAndColumns(spans: _SpanList) -> None:
    """Every visible box sits in [0, 100] percent and inside its column count."""
    boxes = layoutDay(spans, _DAY_START, _DAY_END)

    assert len(boxes) == len(spans)
    for span, box in zip(spans, boxes, strict=True):
        start, end = span
        outside = end <= _DAY_START or start >= _DAY_END
        assert (box is None) == outside
        if box is None:
            continue
        assert 0.0 <= box.top_percent <= 100.0
        assert box.height_percent > 0.0
        assert box.top_percent + box.height_percent <= 100.0001
        assert 0 <= box.column < box.columns


@given(spans=_spans())
def testOverlappingSpansNeverShareAColumn(spans: _SpanList) -> None:
    boxes = layoutDay(spans, _DAY_START, _DAY_END)

    visible = [(span, box) for span, box in zip(spans, boxes, strict=True) if box is not None]
    for first_index, (first_span, first_box) in enumerate(visible):
        for second_span, second_box in visible[first_index + 1 :]:
            overlap = max(first_span[0], second_span[0], _DAY_START) < min(first_span[1], second_span[1], _DAY_END)
            if overlap:
                assert first_box.column != second_box.column
