# SPDX-License-Identifier: Apache-2.0

"""Agent-friendly output formatting for the atif-sql CLI.

One rule holds across every command: output goes through :func:`emit_rows`
(tabular) or :func:`emit_json` (structured), ``--format auto`` resolves to a
human table on a TTY and machine-readable JSON on a pipe, and classified
errors land on stderr as one readable line (TTY) or a JSON envelope (pipe)
with a stable exit code. A command that writes to stdout directly breaks the
pipe contract an agent depends on.

No dataframe library. The tabular surface is ``(columns, rows)`` straight off
the DuckDB cursor, so the table renderer is a small width-aligned formatter
and JSON/CSV go through the stdlib. That is what keeps this module importable
on the lean path — no duckdb, no dataframe stack — which the
fresh-interpreter lean-import test pins.

Unbounded results go through :func:`emit_cursor`, which walks the cursor in
``fetchmany`` batches and writes each batch straight to stdout, so the
client holds one batch of Python tuples instead of every row. DuckDB's
``memory_limit`` does not bound that client-side copy at all — it caps the
engine's own heap.

What streaming buys is measured, and it is narrower than "less memory
always". It wins when a large result comes off a cheap scan: 3,000,000 rows
out of ``range()`` peak near 18 MB of RSS above baseline streamed, against
roughly 540 MB under ``fetchall``. It does NOT win on a JSON-parsing view.
``SELECT * FROM steps`` over a real corpus (115,265 rows, 149 MB of JSON)
holds the view's scan resident near 3 GB for the whole write, and paired
runs put streaming and ``fetchall`` in one overlapping band with no
consistent ordering between them. Streaming is the default because it is
the mode whose client-side cost stays flat as a result grows, not because
it lowers the peak on every query.
"""

from __future__ import annotations

import csv
import json
import sys
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from atif_cli.errors import ClassifiedError

#: Cell truncation width, TABLE format ONLY. JSON and CSV emit the full value:
#: truncation is a terminal-legibility device, and silently shortening a value
#: on a machine-readable path would corrupt the data an agent reads back.
_TABLE_CELL_MAX = 120

#: Rows per ``fetchmany`` in :func:`emit_cursor`. Big enough that the
#: per-batch overhead is noise and the table renderer measures its column
#: widths over a representative sample; small enough that peak resident rows
#: stay bounded regardless of result size.
_STREAM_BATCH_ROWS = 10_000


class OutputFormat(StrEnum):
    """Supported output formats.

    ``AUTO`` resolves to ``TABLE`` when stdout is a TTY and ``JSON``
    otherwise. A string Enum is what lets cyclopts parse ``--format json``
    with no custom converter. The contract names ``auto|json|csv``; ``table``
    is what ``auto`` resolves to on a TTY, and it is accepted explicitly so a
    caller can force the human grid onto a pipe.
    """

    AUTO = "auto"
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


def resolve_format(fmt: OutputFormat | str) -> OutputFormat:
    """Resolve ``AUTO`` against the current stdout; no-op for explicit formats."""
    # `OutputFormat` is a `StrEnum`, so a member IS a str and the constructor is
    # the identity on one — no branch is needed to accept either input shape.
    resolved = OutputFormat(fmt)
    if resolved is not OutputFormat.AUTO:
        return resolved
    return OutputFormat.TABLE if sys.stdout.isatty() else OutputFormat.JSON


def _cell(value: Any) -> str:
    """Render one table cell: NULL marker, truncated, newline-flattened."""
    if value is None:
        return "∅"
    text = str(value).replace("\n", "\\n")
    if len(text) > _TABLE_CELL_MAX:
        return text[: _TABLE_CELL_MAX - 1] + "…"
    return text


def json_object_keys(columns: Sequence[str]) -> list[str]:
    """Column names made unique, so a JSON row object keeps every column.

    A JSON row object is keyed by column name, and SQL column names are not
    unique: any un-aliased self-join
    (``SELECT s.session_id, m.session_id FROM sessions s JOIN messages m
    USING (session_id)``) returns two columns with one name. Keying a dict
    by them keeps only the LAST value and returns fewer columns than were
    asked for, with no error. Repeats get a ``_<n>`` suffix instead, counting
    from 1 in column order and skipping suffixes already taken by an
    explicit alias, so the mapping is a pure function of ``columns``.
    """
    taken = set(columns)
    seen: set[str] = set()
    keys: list[str] = []
    for column in columns:
        if column not in seen:
            seen.add(column)
            keys.append(column)
            continue
        suffix = 1
        while f"{column}_{suffix}" in taken or f"{column}_{suffix}" in seen:
            suffix += 1
        key = f"{column}_{suffix}"
        seen.add(key)
        keys.append(key)
    return keys


def _batched(rows: Sequence[Sequence[Any]], size: int) -> Iterator[Sequence[Sequence[Any]]]:
    """Slice an in-memory row sequence into batches of at most ``size``."""
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def emit_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    fmt: OutputFormat | str = OutputFormat.AUTO,
) -> None:
    """Write an in-memory query result to stdout in the requested format.

    Parameters
    ----------
    columns
        Column names, from ``cursor.description``.
    rows
        Row tuples, from ``cursor.fetchall()``.
    fmt
        One of :class:`OutputFormat`; ``AUTO`` resolves per
        :func:`resolve_format`.

    JSON emits an ARRAY OF ROW OBJECTS, not a columns/rows pair: an agent
    parsing the result should not have to zip two lists to read a field. CSV
    emits a header + rows; TABLE a width-aligned plain-text grid with a
    trailing row count. Results whose size is not known to be small belong in
    :func:`emit_cursor`.
    """
    emit_row_batches(columns, _batched(rows, _STREAM_BATCH_ROWS), fmt)


def emit_cursor(cursor: Any, fmt: OutputFormat | str = OutputFormat.AUTO) -> None:
    """Write a DuckDB cursor's whole result to stdout without materializing it.

    Reads ``cursor.description`` for the column names, then drains
    ``cursor.fetchmany`` in :data:`_STREAM_BATCH_ROWS` batches through
    :func:`emit_row_batches`. Duck-typed on purpose: this module stays off
    the ``import duckdb`` path.

    Output is written as batches arrive, so a driver error raised partway
    through the scan surfaces AFTER some rows are already on stdout.
    """
    columns = [d[0] for d in cursor.description or ()]

    def batches() -> Iterator[Sequence[Sequence[Any]]]:
        while True:
            batch = cursor.fetchmany(_STREAM_BATCH_ROWS)
            if not batch:
                return
            yield batch

    emit_row_batches(columns, batches(), fmt)


def emit_row_batches(
    columns: Sequence[str],
    batches: Iterable[Sequence[Sequence[Any]]],
    fmt: OutputFormat | str = OutputFormat.AUTO,
) -> None:
    """Write successive row batches to stdout in the requested format.

    Each batch is serialized and written before the next is pulled, so peak
    memory is one batch rather than the whole result. The JSON array
    brackets and commas, the CSV header, and the TABLE header/rule/row-count
    are emitted around the stream.
    """
    resolved = resolve_format(fmt)
    if resolved is OutputFormat.JSON:
        _write_json_stream(columns, batches)
        return
    if resolved is OutputFormat.CSV:
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        for batch in batches:
            writer.writerows(batch)
        return
    _write_table_stream(columns, batches)


def _write_json_stream(
    columns: Sequence[str],
    batches: Iterable[Sequence[Sequence[Any]]],
) -> None:
    """Emit a JSON array of row objects, one batch of elements at a time."""
    keys = json_object_keys(columns)
    sys.stdout.write("[")
    first = True
    for batch in batches:
        for row in batch:
            if not first:
                sys.stdout.write(", ")
            first = False
            sys.stdout.write(json.dumps(dict(zip(keys, row, strict=True)), default=str))
    sys.stdout.write("]\n")


def _write_table_stream(
    columns: Sequence[str],
    batches: Iterable[Sequence[Sequence[Any]]],
) -> None:
    """Emit a width-aligned plain-text grid, sizing columns off the first batch.

    Column widths are fixed by the header plus the first batch, which is the
    widest window available without holding the whole result. A later batch
    with a wider cell overflows its column rather than being truncated —
    misalignment is preferable to hiding a value.
    """
    stream = iter(batches)
    head = [[_cell(v) for v in row] for row in next(stream, ())]
    widths = [
        max(len(str(col)), *(len(r[i]) for r in head)) if head else len(str(col))
        for i, col in enumerate(columns)
    ]

    def line(cells: Sequence[str]) -> str:
        return " | ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True))

    sys.stdout.write(line([str(col) for col in columns]) + "\n")
    sys.stdout.write("-+-".join("-" * w for w in widths) + "\n")
    total = 0
    for row in head:
        sys.stdout.write(line(row) + "\n")
        total += 1
    for batch in stream:
        for raw_row in batch:
            sys.stdout.write(line([_cell(v) for v in raw_row]) + "\n")
            total += 1
    sys.stdout.write(f"({total} row{'s' if total != 1 else ''})\n")


def emit_json(payload: Any, fmt: OutputFormat | str = OutputFormat.AUTO) -> None:
    """Write a non-tabular payload as pretty JSON (schema, status, reports).

    Every format reduces to one JSON document here, because CSV over a nested
    dict has no meaning. ``fmt`` is accepted for call-site symmetry with
    :func:`emit_rows` and is never branched on.
    Callers that want a bespoke human layout handle the TABLE branch
    themselves before calling this.
    """
    del fmt
    sys.stdout.write(json.dumps(payload, indent=2, default=str))
    sys.stdout.write("\n")


def emit_error(err: ClassifiedError, fmt: OutputFormat | str = OutputFormat.AUTO) -> None:
    """Write a classified error to stderr in the requested format.

    Agents running ``--format json`` (or piping) get the structured
    envelope; humans on a TTY get a readable line plus the hint. The caller
    exits with :attr:`ClassifiedError.exit_code`.
    """
    resolved = resolve_format(fmt)
    if resolved is OutputFormat.TABLE:
        sys.stderr.write(f"[{err.kind}] {err.message}\n")
        if err.hint:
            sys.stderr.write(f"hint: {err.hint}\n")
    else:
        sys.stderr.write(json.dumps(err.to_payload(), default=str))
        sys.stderr.write("\n")


__all__ = [
    "OutputFormat",
    "emit_cursor",
    "emit_error",
    "emit_json",
    "emit_row_batches",
    "emit_rows",
    "json_object_keys",
    "resolve_format",
]
