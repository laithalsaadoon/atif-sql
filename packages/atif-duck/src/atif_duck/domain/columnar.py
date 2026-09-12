# SPDX-License-Identifier: Apache-2.0

"""The typed columnar artifact contract: names, schema version, column shapes.

Beside the four JSON artifacts in ``<corpus_root>/sessions/<id>/``, a session
may carry four Parquet files that hold what the transcript-derived views need,
already typed, so a query never parses ``trajectory.json``:

* ``session.parquet``      one row: the trajectory's top-level members minus
  ``steps`` (what the ``sessions`` view reads), keyed by ``session_id_path``.
* ``steps.parquet``        one row per ATIF step, exactly the ``steps`` view's
  columns.
* ``tool_calls.parquet``   one row per tool call, exactly the ``tool_calls``
  view's columns.
* ``tool_results.parquet`` one row per observation result, exactly the
  ``tool_results`` view's columns.

atif-duck owns this shape (the views define what the columns mean) and writes
it through :class:`atif_duck.infrastructure.columnar.ColumnarArtifactProducer`;
atif-corpus calls that producer through its ``ArtifactProducer`` port without
knowing these names. The registry reads a session from these files only when
``meta.json`` carries ``columnar_schema`` equal to
:data:`COLUMNAR_SCHEMA_VERSION` and all four files are present; otherwise it
reads the session's ``trajectory.json`` as before, so a corpus materialized
before this contract existed (or by a build with a different schema version)
still queries correctly.

Pure constants and path arithmetic: no duckdb import, no filesystem access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atif_duck.domain.catalog import VIEW_SCHEMA

if TYPE_CHECKING:
    from pathlib import Path

#: Bump when any column, type, or filename below changes. A session whose
#: ``meta.json`` names another version is read from ``trajectory.json``.
COLUMNAR_SCHEMA_VERSION: int = 1

#: The ``meta.json`` key that records which columnar schema a session's
#: parquet files were written against. Absent on sessions materialized
#: before the columnar artifacts existed, which is a legal, JSON-read state.
META_COLUMNAR_KEY: str = "columnar_schema"

SESSION_PARQUET: str = "session.parquet"
STEPS_PARQUET: str = "steps.parquet"
TOOL_CALLS_PARQUET: str = "tool_calls.parquet"
TOOL_RESULTS_PARQUET: str = "tool_results.parquet"

#: Every columnar filename, in the order the producer writes them.
COLUMNAR_FILENAMES: tuple[str, ...] = (
    SESSION_PARQUET,
    STEPS_PARQUET,
    TOOL_CALLS_PARQUET,
    TOOL_RESULTS_PARQUET,
)

#: ``session.parquet`` columns. ``session_id_path`` is the session directory
#: name, the canonical key every view uses as ``session_id``; the rest are the
#: trajectory's top-level members with the types the JSON reader projects them
#: to (``atif_duck.infrastructure.registry._TRAJECTORY_COLUMNS``), minus
#: ``steps``.
SESSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("session_id_path", "VARCHAR"),
    ("schema_version", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("trajectory_id", "VARCHAR"),
    ("agent", "STRUCT(name VARCHAR, version VARCHAR, model_name VARCHAR, extra JSON)"),
    (
        "final_metrics",
        (
            "STRUCT(total_prompt_tokens BIGINT, total_completion_tokens BIGINT, "
            "total_cached_tokens BIGINT, total_cost_usd DOUBLE, total_steps BIGINT, extra JSON)"
        ),
    ),
    ("extra", "JSON"),
)

#: Column shape per columnar file. The three step-derived files carry exactly
#: their view's catalog columns, so the catalog stays the one place a column
#: is declared and the schema drift test covers these files too.
COLUMNAR_SCHEMAS: dict[str, tuple[tuple[str, str], ...]] = {
    SESSION_PARQUET: SESSION_COLUMNS,
    STEPS_PARQUET: VIEW_SCHEMA["steps"],
    TOOL_CALLS_PARQUET: VIEW_SCHEMA["tool_calls"],
    TOOL_RESULTS_PARQUET: VIEW_SCHEMA["tool_results"],
}

#: Smallest byte count a readable parquet file can have (``PAR1`` + footer +
#: footer length + ``PAR1``). A shorter file is what a crashed writer leaves
#: behind; the registry treats it as absent and reads the session from JSON.
MIN_PARQUET_BYTES: int = 16


def columnar_paths(session_dir: Path) -> tuple[Path, ...]:
    """The four columnar artifact paths inside one session directory."""
    return tuple(session_dir / name for name in COLUMNAR_FILENAMES)


def is_current_columnar_schema(value: object) -> bool:
    """True when a ``meta.json`` ``columnar_schema`` value names this build's schema.

    Anything but the exact integer (absent key, ``None``, a string, another
    version) answers ``False`` and sends the session down the JSON path.
    """
    return (
        isinstance(value, int) and not isinstance(value, bool) and value == COLUMNAR_SCHEMA_VERSION
    )


__all__ = [
    "COLUMNAR_FILENAMES",
    "COLUMNAR_SCHEMAS",
    "COLUMNAR_SCHEMA_VERSION",
    "META_COLUMNAR_KEY",
    "MIN_PARQUET_BYTES",
    "SESSION_COLUMNS",
    "SESSION_PARQUET",
    "STEPS_PARQUET",
    "TOOL_CALLS_PARQUET",
    "TOOL_RESULTS_PARQUET",
    "columnar_paths",
    "is_current_columnar_schema",
]
