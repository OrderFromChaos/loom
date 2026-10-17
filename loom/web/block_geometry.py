"""Pure geometry for the week grid: minute spans to percent boxes and overlap columns.

Every value is in minutes since local midnight, so a DST day changes nothing here. The
caller converts wall-clock time to minutes per day. A span outside the visible window
maps to None, and a span across its edge is clipped.
"""

from __future__ import annotations

from collections.abc import Sequence

from loom.web.web_vocabulary import BlockBox


def computeBlockBoxes(
    spans: Sequence[tuple[int, int]],
    day_start_minute: int,
    day_end_minute: int,
) -> tuple[BlockBox | None, ...]:
    """Compute one box per span, in span order: percent geometry plus overlap sub-columns.

    Overlapping spans split their group's width into columns, and a span alone gets the
    whole lane. Columns are assigned in (start, end, position) order.
    """
    window = day_end_minute - day_start_minute
    clipped: list[tuple[int, int] | None] = []
    for start, end in spans:
        visible_start = max(start, day_start_minute)
        visible_end = min(end, day_end_minute)
        clipped.append((visible_start, visible_end) if visible_start < visible_end else None)

    columns = _assignColumns(clipped)

    boxes: list[BlockBox | None] = []
    for span, placed in zip(clipped, columns, strict=True):
        if span is None or placed is None:
            boxes.append(None)
            continue
        start, end = span
        boxes.append(
            BlockBox(
                top_percent=(start - day_start_minute) * 100.0 / window,
                height_percent=(end - start) * 100.0 / window,
                column=placed[0],
                columns=placed[1],
            ),
        )

    return tuple(boxes)


def _assignColumns(spans: Sequence[tuple[int, int] | None]) -> list[tuple[int, int] | None]:
    """Assign each span a (column, group column count) by interval partitioning, None for a None span.

    A group is a maximal run of transitively overlapping spans. Every member gets the same
    column count, so their widths line up.
    """
    ordered = sorted((span, index) for index, span in enumerate(spans) if span is not None)

    placed: list[tuple[int, int] | None] = [None] * len(spans)
    active: list[tuple[int, int]] = []  # (end_minute, column) of spans still open
    group: list[int] = []
    group_width = 0

    def closeGroup() -> None:
        for member in group:
            column = placed[member]
            if column is not None:
                placed[member] = (column[0], group_width)

    for span, index in ordered:
        start, end = span
        active = [(open_end, column) for open_end, column in active if open_end > start]
        if not active:
            closeGroup()
            group = []
            group_width = 0

        taken = {column for _, column in active}
        column = next(candidate for candidate in range(len(active) + 1) if candidate not in taken)
        active.append((end, column))
        placed[index] = (column, 0)
        group.append(index)
        group_width = max(group_width, column + 1)

    closeGroup()
    return placed
