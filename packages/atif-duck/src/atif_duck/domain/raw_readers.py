# SPDX-License-Identifier: Apache-2.0

"""The raw readers' names and column shapes, shared by the registry and the lake.

The registry (:mod:`atif_duck.infrastructure.registry`) binds one relation per
corpus artifact kind under these names, and every business view reads them
and nothing else. The lake (:mod:`atif_duck.domain.lake`) derives its tables
from the same shapes, so a column declared here is declared once for both the
per-session path and the lake path.

The names are deliberately absent from :data:`atif_duck.domain.catalog.VIEW_NAMES`:
they describe corpus files rather than being queryable business surface.

Pure constants: no duckdb import, no filesystem access.
"""

from __future__ import annotations

from atif_duck.domain.columnar import META_COLUMNAR_KEY, SESSION_COLUMNS

#: The trajectory's top-level members minus ``steps``, plus the two
#: path-derived keys. ``v_raw_trajectories`` carries these, then ``corpus``.
RAW_TRAJECTORIES: str = "v_raw_trajectories"
#: One row per ATIF step, in the ``steps`` view's column shape.
RAW_STEPS: str = "v_raw_steps"
#: One row per tool call, in the ``tool_calls`` view's column shape.
RAW_TOOL_CALLS: str = "v_raw_tool_calls"
#: One row per observation result, in the ``tool_results`` view's column shape.
RAW_TOOL_RESULTS: str = "v_raw_tool_results"
#: One row per kept non-message record, in the ``session_events`` view's shape.
RAW_SESSION_EVENTS: str = "v_raw_session_events"
#: One row per RAW transcript record, from ``edges.jsonl``.
RAW_EDGES: str = "v_raw_edges"
#: One row per session, from ``loss_report.json``.
RAW_LOSS_REPORTS: str = "v_raw_loss_reports"
#: One row per complete session, from ``meta.json``.
RAW_META: str = "v_raw_meta"

#: The column every raw reader except the step-level four keys its session
#: on: the ``sessions/<id>/`` directory name.
SESSION_ID_PATH: str = "session_id_path"

#: The ``corpus`` column ``v_raw_trajectories`` ends with: the corpus
#: directory's name, which is how the lake and the ``sessions`` view name a
#: corpus.
CORPUS_COLUMN: str = "corpus"

#: ``v_raw_trajectories`` columns before ``corpus``, in order: the
#: trajectory's top-level members minus ``steps``, then the two path-derived
#: keys.
TRAJECTORY_RELATION_COLUMNS: tuple[tuple[str, str], ...] = (
    *(column for column in SESSION_COLUMNS if column[0] != SESSION_ID_PATH),
    ("trajectory_path", "VARCHAR"),
    (SESSION_ID_PATH, "VARCHAR"),
)

# Explicit projection for ``v_raw_edges``: one line per RAW transcript record
# per docs/CONTRACT.md. ``parent_uuid`` is declared VARCHAR outright: a root
# record leaves it null, so inferred typing resolves it as a NULL-vs-string
# JSON union and every downstream view then needs its own CAST. Declaring the
# type in the one explicit-columns reader pays that cost once.
EDGE_COLUMNS: dict[str, str] = {
    "uuid": "VARCHAR",
    "parent_uuid": "VARCHAR",
    "message_id": "VARCHAR",
    "type": "VARCHAR",
    "ts": "TIMESTAMP",
    "is_sidechain": "BOOLEAN",
    "is_compact_summary": "BOOLEAN",
    "source_file": "VARCHAR",
    "tool_use_ids": "JSON",
}

# Explicit projection for ``v_raw_loss_reports``: atif_converter
# ``LossReport.to_json()`` shape (record_counts / gaps_observed stay JSON —
# enum-keyed dict and list respectively).
LOSS_REPORT_COLUMNS: dict[str, str] = {
    "record_counts": "JSON",
    "records_total": "BIGINT",
    "records_converted": "BIGINT",
    # Added with session_events (converter schema 2). A report written before
    # then has no such key and reads as NULL, which the view keeps as NULL:
    # "not measured" rather than "none captured".
    "records_captured": "BIGINT",
    "records_dropped": "BIGINT",
    "gaps_observed": "JSON",
    "subagent_files_found": "BIGINT",
    "subagent_files_convertible": "BIGINT",
    "workflow_subagent_files_found": "BIGINT",
}

#: The path column ``v_raw_loss_reports`` adds (the ``loss_reports`` view
#: returns it).
LOSS_REPORT_PATH_COLUMN: tuple[str, str] = ("report_path", "VARCHAR")

# Explicit projection for ``v_raw_meta`` per docs/CONTRACT.md. An explicit
# ``columns=`` projection nulls a missing key rather than failing, which is
# what lets a corpus written before a key existed still register.
META_COLUMNS: dict[str, str] = {
    "session_id": "VARCHAR",
    "source_mtime_ns": "BIGINT",
    "source_files": "JSON",
    "harbor_version": "VARCHAR",
    "converter_version": "VARCHAR",
    "materialized_at": "VARCHAR",
    # Which agent wrote the transcript. Added 2026-09-11 with Codex support, so
    # a session materialized before then has no such key and reads as NULL.
    # Queries read ``sessions.agent`` (from the trajectory) instead; this
    # column is provenance for an operator reading meta.json directly.
    "agent": "VARCHAR",
    # Which columnar schema the session's parquet artifacts were written
    # against. Absent (NULL) on a session materialized before the artifacts
    # existed; that session is read from trajectory.json, which is always
    # correct.
    META_COLUMNAR_KEY: "BIGINT",
    # The converter's output version, and whether the source transcript still
    # exists. NULL on a session written before the key existed.
    "converter_schema": "BIGINT",
    "source_present": "BOOLEAN",
    "source_removed_at": "VARCHAR",
}


__all__ = [
    "CORPUS_COLUMN",
    "EDGE_COLUMNS",
    "LOSS_REPORT_COLUMNS",
    "LOSS_REPORT_PATH_COLUMN",
    "META_COLUMNS",
    "RAW_EDGES",
    "RAW_LOSS_REPORTS",
    "RAW_META",
    "RAW_SESSION_EVENTS",
    "RAW_STEPS",
    "RAW_TOOL_CALLS",
    "RAW_TOOL_RESULTS",
    "RAW_TRAJECTORIES",
    "SESSION_ID_PATH",
    "TRAJECTORY_RELATION_COLUMNS",
]
