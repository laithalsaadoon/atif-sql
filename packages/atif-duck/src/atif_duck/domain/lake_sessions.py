# SPDX-License-Identifier: Apache-2.0

"""The statements that read one corpus's sessions back out of the lake, a batch at a time.

The LLM pipelines (atif-analytics) walk a corpus session by session: bounds
for every session, then the steps of the ones a gate or the renderer needs.
:class:`atif_duck.infrastructure.lake_sessions.LakeSessionReader` runs these
statements for them (atif-cli adapts its rows to atif-analytics' session
port, since the two packages may not import each other).

Every statement is a module constant over the lake tables of
:mod:`atif_duck.domain.lake`, so the AST audit in
``tests/test_sql_text_boundaries.py`` reads them like any other constant. The
corpus and the session ids are bound parameters; a batch of ids is one
``VARCHAR[]`` parameter.

Row order. A step's tool calls and results have no ordinal column; their
order is the order the writer inserted them in, which the lake keeps as
``rowid`` (DuckLake numbers a data file's rows from its ``row_id_start``, and
merges keep the numbers). The writer inserts the step-level tables
(:data:`atif_duck.domain.lake.SOURCE_ORDERED_TABLES`) on one thread so that order is the source
order; the statements below sort by ``step_id`` and then ``rowid``.

Pure constants: no duckdb import, no filesystem access.
"""

from __future__ import annotations

from atif_duck.domain.lake import LAKE_ALIAS, SESSION_KEY
from atif_duck.domain.raw_readers import CORPUS_COLUMN
from atif_duck.domain.sql_literal import SqlFragment

_SCOPE: str = f"{CORPUS_COLUMN} = ? AND {SESSION_KEY} IN (SELECT unnest(?::VARCHAR[]))"
_ORDER: str = f"ORDER BY {SESSION_KEY}, step_id, rowid"

#: ``(session_id, materialized_at, source_mtime_ns)`` for every session of a
#: corpus (param: corpus). What a session's ``meta.json`` said when the lake
#: loaded it, so a reader can tell a current session from one the lake holds an
#: older write of.
SESSION_FRESHNESS_SQL: SqlFragment = SqlFragment(
    f"SELECT {SESSION_KEY}, materialized_at, source_mtime_ns "  # noqa: S608  # nosec B608 - constants only; corpus is bound
    f"FROM {LAKE_ALIAS}.session_meta WHERE {CORPUS_COLUMN} = ?"
)

#: ``(session_id, newest record ts)`` for every session of a corpus (param:
#: corpus): the checkpoint's first bound, from ``edges`` like the file path.
LAST_EDGE_TS_SQL: SqlFragment = SqlFragment(
    f"SELECT {SESSION_KEY}, max(ts) FROM {LAKE_ALIAS}.edges "  # noqa: S608  # nosec B608 - constants only; corpus is bound
    f"WHERE {CORPUS_COLUMN} = ? GROUP BY 1"
)

#: Every step of a batch of sessions without its tool payloads (params:
#: corpus, ids): ``(session_id, step_id, ts, source, message, is_sidechain,
#: is_compact_summary, source_uuids)``.
TURNS_SQL: SqlFragment = SqlFragment(
    "SELECT "  # noqa: S608  # nosec B608 - constants only; corpus and ids are bound
    f"{SESSION_KEY}, step_id, ts, source, message, is_sidechain, is_compact_summary, "
    f"CAST(source_uuids AS VARCHAR) FROM {LAKE_ALIAS}.steps WHERE {_SCOPE} {_ORDER}"
)

#: ``(session_id, step_id)`` of every step with an error result in a batch
#: (params: corpus, ids). The gates need the flag, not the result bodies.
ERROR_STEPS_SQL: SqlFragment = SqlFragment(
    f"SELECT DISTINCT {SESSION_KEY}, step_id FROM {LAKE_ALIAS}.tool_results "  # noqa: S608  # nosec B608 - constants only; corpus and ids are bound
    f"WHERE {_SCOPE} AND is_error"
)

#: Every tool call of a batch, in source order (params: corpus, ids):
#: ``(session_id, step_id, tool_name, tool_input)`` with the input as JSON text.
TOOL_CALLS_SQL: SqlFragment = SqlFragment(
    f"SELECT {SESSION_KEY}, step_id, tool_name, CAST(tool_input AS VARCHAR) "  # noqa: S608  # nosec B608 - constants only; corpus and ids are bound
    f"FROM {LAKE_ALIAS}.tool_calls WHERE {_SCOPE} {_ORDER}"
)

#: Every tool result of a batch, in source order (params: corpus, ids):
#: ``(session_id, step_id, tool_use_id, content, is_error)`` with the content
#: as JSON text.
TOOL_RESULTS_SQL: SqlFragment = SqlFragment(
    f"SELECT {SESSION_KEY}, step_id, tool_use_id, CAST(content AS VARCHAR), is_error "  # noqa: S608  # nosec B608 - constants only; corpus and ids are bound
    f"FROM {LAKE_ALIAS}.tool_results WHERE {_SCOPE} {_ORDER}"
)

#: The non-empty raw-record uuids of one session (params: corpus, session id).
EDGE_UUIDS_SQL: SqlFragment = SqlFragment(
    f"SELECT DISTINCT uuid FROM {LAKE_ALIAS}.edges "  # noqa: S608  # nosec B608 - constants only; corpus and the id are bound
    f"WHERE {CORPUS_COLUMN} = ? AND {SESSION_KEY} = ? AND uuid IS NOT NULL AND uuid <> ''"
)

__all__ = [
    "EDGE_UUIDS_SQL",
    "ERROR_STEPS_SQL",
    "LAST_EDGE_TS_SQL",
    "SESSION_FRESHNESS_SQL",
    "TOOL_CALLS_SQL",
    "TOOL_RESULTS_SQL",
    "TURNS_SQL",
]
