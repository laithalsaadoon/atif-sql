# SPDX-License-Identifier: Apache-2.0

"""The DuckLake the corpora are queried through: tables, partitions, schema identity.

One lake holds every corpus (both Claude Code corpora and the Codex one). Each
lake table is the registry's raw reader of the same artifact kind
(:mod:`atif_duck.domain.raw_readers`) with two identity columns in front,
``corpus`` (the corpus directory's name) and ``agent`` (the agent the corpus
holds). The per-session artifacts stay the source of truth: the lake is
loaded FROM the registry's raw relations over them, so a lake row is by
construction the row the per-session path would have produced, and the views
bind over either with no change.

Nothing here is declared twice. Every payload column comes from the raw
reader shapes, which come from the catalog and the columnar contract; the
only lake-local decisions are the identity columns, the rename rule for a
source column that collides with one, the derived ``started_at`` the sessions
table is partitioned by, and the partition specs.

Partitioning: every table is partitioned by ``agent`` and ``corpus`` first,
then by month where a row has a time. ``corpus`` is there because a query is
scoped to one corpus by default, and a corpus-pure file is what lets that scope
skip the other corpora's files. It also keeps compaction from merging two
corpora's rows into one file. ``agent`` is implied by ``corpus`` (one corpus
holds one agent), so it adds no files, only a readable directory level. The
two one-row-per-session tables (``loss_reports``, ``session_meta``) have no
time column and stay at ``agent``/``corpus``: splitting a few thousand small
rows by month would multiply files without pruning anything a query asks for.

Schema identity: the lake records :data:`LAKE_SCHEMA_VERSION`, a digest of
the rendered table definitions (:func:`lake_schema_digest`), and the columnar
schema version it was loaded under. Any of the three differing from the
running code makes the lake STALE: the writer rebuilds it and a reader falls
back to the per-session path. ``tests/test_lake_schema.py`` pins the digest
beside the version, so a change to any shape the lake derives from fails until
someone decides whether it also needs a version bump.

Pure constants and string building: no duckdb import, no filesystem access.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from atif_duck.domain.catalog import VIEW_SCHEMA
from atif_duck.domain.columnar import COLUMNAR_SCHEMA_VERSION
from atif_duck.domain.raw_readers import (
    CORPUS_COLUMN,
    EDGE_COLUMNS,
    LOSS_REPORT_COLUMNS,
    LOSS_REPORT_PATH_COLUMN,
    META_COLUMNS,
    RAW_EDGES,
    RAW_LOSS_REPORTS,
    RAW_META,
    RAW_SESSION_EVENTS,
    RAW_STEPS,
    RAW_TOOL_CALLS,
    RAW_TOOL_RESULTS,
    RAW_TRAJECTORIES,
    SESSION_ID_PATH,
    TRAJECTORY_RELATION_COLUMNS,
)
from atif_duck.domain.sql_literal import SqlFragment

#: Bump when the lake's meaning changes in a way the table definitions do not
#: show (what a row is loaded from, how a key is derived). A change the
#: definitions DO show moves :func:`lake_schema_digest` on its own; the pin test
#: makes the author decide whether it also warrants a bump.
LAKE_SCHEMA_VERSION: int = 1

#: The name the lake is attached under on every connection that reads or
#: writes it. Not part of the queryable surface: the views are.
LAKE_ALIAS: str = "atif_lake"

#: The metadata database DuckLake attaches beside :data:`LAKE_ALIAS`.
LAKE_METADATA_ALIAS: str = f"__ducklake_metadata_{LAKE_ALIAS}"

#: The identity every lake table starts with.
AGENT_COLUMN: str = "agent"
SESSION_KEY: str = "session_id"
IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    (CORPUS_COLUMN, "VARCHAR"),
    (AGENT_COLUMN, "VARCHAR"),
    (SESSION_KEY, "VARCHAR"),
)

#: A source column whose name is one of the identity names is stored under
#: this prefix (the trajectory's own ``agent`` struct becomes ``src_agent``).
COLLISION_PREFIX: str = "src_"

#: ``lake_info`` keys (the lake's recorded schema identity).
INFO_SCHEMA_VERSION: str = "lake_schema_version"
INFO_SCHEMA_DIGEST: str = "lake_schema_digest"
INFO_COLUMNAR_SCHEMA: str = "columnar_schema_version"

#: The two bookkeeping tables. Their shape never changes, so a stale lake can
#: still say which corpora it holds and a rebuild can find them again.
LAKE_INFO_TABLE: str = "lake_info"
CORPORA_TABLE: str = "corpora"
LAKE_INFO_COLUMNS: tuple[tuple[str, str], ...] = (("key", "VARCHAR"), ("value", "VARCHAR"))
CORPORA_COLUMNS: tuple[tuple[str, str], ...] = (
    (CORPUS_COLUMN, "VARCHAR"),
    (AGENT_COLUMN, "VARCHAR"),
    ("corpus_root", "VARCHAR"),
    ("registered_at", "VARCHAR"),
)

#: The source for the sessions table: the trajectory rows plus the first
#: step's time, which the table is partitioned by.
_SESSIONS_SOURCE: str = (
    f"(SELECT t.*, s.started_at FROM {RAW_TRAJECTORIES} t LEFT JOIN "  # noqa: S608  # nosec B608 - module constants only
    f"(SELECT session_id, min(ts) AS started_at FROM {RAW_STEPS} GROUP BY session_id) s "
    f"ON s.session_id = t.{SESSION_ID_PATH})"
)

_MONTHLY: tuple[str, ...] = ("year({ts})", "month({ts})")


@dataclass(frozen=True, slots=True)
class LakeColumn:
    """One payload column: its lake name, type, and the source column it copies."""

    name: str
    sql_type: str
    source: str
    #: Computed at load time rather than copied from the relation, so the
    #: reader view does not select it back (``sessions.started_at``).
    derived: bool = False


@dataclass(frozen=True, slots=True)
class LakeTable:
    """One lake table and the raw relation it is loaded from and read back as."""

    #: Table name inside the lake.
    name: str
    #: The raw reader (a registry relation name) whose shape the table stores.
    relation: str
    #: What the load selects from: the relation itself, or a constant subquery.
    load_source: str
    #: The source column holding the session key (``session_id`` or ``session_id_path``).
    key_source: str
    #: Payload columns after the identity columns, in order.
    payload: tuple[LakeColumn, ...]
    #: Partition expressions, after the identity-derived ``agent, corpus``.
    partition_by: tuple[str, ...]
    #: Source columns the relation exposes that the reader view must rebuild
    #: from the identity (``corpus`` on the trajectories relation).
    relation_extras: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[tuple[str, str], ...]:
        """Every lake column, identity first."""
        return (*IDENTITY_COLUMNS, *((c.name, c.sql_type) for c in self.payload))


def _payload(
    columns: tuple[tuple[str, str], ...], *, key_source: str, skip: tuple[str, ...] = ()
) -> tuple[LakeColumn, ...]:
    """Map a relation's columns onto lake payload columns (key and identity renamed)."""
    reserved = {name for name, _ in IDENTITY_COLUMNS}
    out: list[LakeColumn] = []
    for name, sql_type in columns:
        if name == key_source or name in skip:
            continue
        lake_name = f"{COLLISION_PREFIX}{name}" if name in reserved else name
        out.append(LakeColumn(name=lake_name, sql_type=sql_type, source=name))
    return tuple(out)


def _monthly(ts: str) -> tuple[str, ...]:
    return tuple(part.format(ts=ts) for part in _MONTHLY)


def _view_table(name: str, relation: str, view: str) -> LakeTable:
    return LakeTable(
        name=name,
        relation=relation,
        load_source=relation,
        key_source=SESSION_KEY,
        payload=_payload(VIEW_SCHEMA[view], key_source=SESSION_KEY),
        partition_by=_monthly("ts"),
    )


#: Every lake table, in load order. The sessions table reads the steps
#: relation for ``started_at``, which the registry binds before the load runs.
LAKE_TABLES: tuple[LakeTable, ...] = (
    LakeTable(
        name="sessions",
        relation=RAW_TRAJECTORIES,
        load_source=_SESSIONS_SOURCE,
        key_source=SESSION_ID_PATH,
        payload=(
            *_payload(TRAJECTORY_RELATION_COLUMNS, key_source=SESSION_ID_PATH),
            LakeColumn(name="started_at", sql_type="TIMESTAMP", source="started_at", derived=True),
        ),
        partition_by=_monthly("started_at"),
        relation_extras=(CORPUS_COLUMN,),
    ),
    _view_table("steps", RAW_STEPS, "steps"),
    _view_table("tool_calls", RAW_TOOL_CALLS, "tool_calls"),
    _view_table("tool_results", RAW_TOOL_RESULTS, "tool_results"),
    _view_table("session_events", RAW_SESSION_EVENTS, "session_events"),
    LakeTable(
        name="edges",
        relation=RAW_EDGES,
        load_source=RAW_EDGES,
        key_source=SESSION_ID_PATH,
        payload=_payload(tuple(EDGE_COLUMNS.items()), key_source=SESSION_ID_PATH),
        partition_by=_monthly("ts"),
    ),
    LakeTable(
        name="loss_reports",
        relation=RAW_LOSS_REPORTS,
        load_source=RAW_LOSS_REPORTS,
        key_source=SESSION_ID_PATH,
        payload=_payload(
            (*LOSS_REPORT_COLUMNS.items(), LOSS_REPORT_PATH_COLUMN), key_source=SESSION_ID_PATH
        ),
        partition_by=(),
    ),
    LakeTable(
        name="session_meta",
        relation=RAW_META,
        load_source=RAW_META,
        key_source=SESSION_ID_PATH,
        payload=_payload(tuple(META_COLUMNS.items()), key_source=SESSION_ID_PATH),
        partition_by=(),
    ),
)

LAKE_TABLE_NAMES: tuple[str, ...] = tuple(table.name for table in LAKE_TABLES)


# ---------------------------------------------------------------------------
# Statements. Every one is a module constant built from LAKE_TABLES, so the
# AST audit in tests/test_sql_text_boundaries.py can prove each placeholder
# is catalog text; the only values that ever vary (corpus, agent, session
# ids) are bound parameters.
# ---------------------------------------------------------------------------

#: ``CREATE TABLE`` for the two bookkeeping tables.
BOOTSTRAP_CREATE_SQL: tuple[SqlFragment, ...] = (
    SqlFragment(
        f"CREATE TABLE {LAKE_ALIAS}.{LAKE_INFO_TABLE} "
        f"({', '.join(f'{name} {sql_type}' for name, sql_type in LAKE_INFO_COLUMNS)})"
    ),
    SqlFragment(
        f"CREATE TABLE {LAKE_ALIAS}.{CORPORA_TABLE} "
        f"({', '.join(f'{name} {sql_type}' for name, sql_type in CORPORA_COLUMNS)})"
    ),
)

#: ``CREATE TABLE`` per lake table.
CREATE_TABLE_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"CREATE TABLE {LAKE_ALIAS}.{t.name} "
        f"({', '.join(f'{name} {sql_type}' for name, sql_type in t.columns)})"
    )
    for t in LAKE_TABLES
}

#: ``ALTER TABLE ... SET PARTITIONED BY`` per lake table: agent and corpus
#: first, then the table's own time partition.
PARTITION_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"ALTER TABLE {LAKE_ALIAS}.{t.name} SET PARTITIONED BY "
        f"({', '.join((AGENT_COLUMN, CORPUS_COLUMN, *t.partition_by))})"
    )
    for t in LAKE_TABLES
}

#: Delete one corpus's rows for a bound list of session ids (params: corpus, ids).
DELETE_SESSIONS_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"DELETE FROM {LAKE_ALIAS}.{t.name} WHERE {CORPUS_COLUMN} = ? "  # noqa: S608  # nosec B608 - constants only; corpus and ids are bound
        f"AND {SESSION_KEY} IN (SELECT unnest(?::VARCHAR[]))"
    )
    for t in LAKE_TABLES
}

#: Delete every row of one corpus (param: corpus).
DELETE_CORPUS_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(f"DELETE FROM {LAKE_ALIAS}.{t.name} WHERE {CORPUS_COLUMN} = ?")  # noqa: S608  # nosec B608 - constants only; corpus is bound
    for t in LAKE_TABLES
}

#: Copy the bound raw relation's rows into the lake (params: corpus, agent).
#: Every column is cast to its declared type, so a relation that infers a
#: looser type (an all-NULL column) still lands in the table's shape.
INSERT_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"INSERT INTO {LAKE_ALIAS}.{t.name} ({', '.join(name for name, _ in t.columns)}) "  # noqa: S608  # nosec B608 - constants only; corpus and agent are bound
        f"SELECT CAST(? AS VARCHAR), CAST(? AS VARCHAR), CAST({t.key_source} AS VARCHAR), "
        f"{', '.join(f'CAST({c.source} AS {c.sql_type})' for c in t.payload)} "
        f"FROM {t.load_source}"
    )
    for t in LAKE_TABLES
}

#: Each lake table read back in its raw relation's shape, every corpus. A
#: reader appends ``WHERE corpus = <literal>`` to scope it.
READER_SELECT_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        "SELECT "  # noqa: S608  # nosec B608 - constants only
        + ", ".join(
            (
                *(f"{c.name} AS {c.source}" for c in t.payload if not c.derived),
                f"{SESSION_KEY} AS {t.key_source}",
                *t.relation_extras,
            )
        )
        + f" FROM {LAKE_ALIAS}.{t.name}"
    )
    for t in LAKE_TABLES
}

#: Per-session ``(session_id, rows, content hash)`` over the registry's raw
#: relation, columns cast as the load casts them. ``sum(hash(...))`` rather
#: than ``bit_xor``: order-free like an XOR, but two identical rows do not
#: cancel out.
ARTIFACT_HASH_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"SELECT CAST({t.key_source} AS VARCHAR) AS session_id, count(*) AS n, "  # noqa: S608  # nosec B608 - constants only
        f"sum(CAST(hash({', '.join(f'CAST({c.source} AS {c.sql_type})' for c in t.payload)}) "
        f"AS HUGEINT)) AS h FROM {t.load_source} GROUP BY 1"
    )
    for t in LAKE_TABLES
}

#: The same over the lake table for one corpus (param: corpus).
LAKE_HASH_SQL: dict[str, SqlFragment] = {
    t.name: SqlFragment(
        f"SELECT {SESSION_KEY} AS session_id, count(*) AS n, "  # noqa: S608  # nosec B608 - constants only; corpus is bound
        f"sum(CAST(hash({', '.join(c.name for c in t.payload)}) AS HUGEINT)) AS h "
        f"FROM {LAKE_ALIAS}.{t.name} WHERE {CORPUS_COLUMN} = ? GROUP BY 1"
    )
    for t in LAKE_TABLES
}


def lake_schema_digest() -> str:
    """sha256 over every table's name, columns and partition spec, bookkeeping included."""
    spec = {
        "tables": [
            {"name": t.name, "columns": t.columns, "partition_by": t.partition_by}
            for t in LAKE_TABLES
        ],
        LAKE_INFO_TABLE: LAKE_INFO_COLUMNS,
        CORPORA_TABLE: CORPORA_COLUMNS,
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()


def expected_lake_info() -> dict[str, str]:
    """The ``lake_info`` rows a lake written by this code carries."""
    return {
        INFO_SCHEMA_VERSION: str(LAKE_SCHEMA_VERSION),
        INFO_SCHEMA_DIGEST: lake_schema_digest(),
        INFO_COLUMNAR_SCHEMA: str(COLUMNAR_SCHEMA_VERSION),
    }


def lake_info_mismatches(recorded: dict[str, str]) -> tuple[str, ...]:
    """The ``lake_info`` keys whose recorded value differs from this code's (sorted)."""
    expected = expected_lake_info()
    return tuple(sorted(key for key, value in expected.items() if recorded.get(key) != value))


__all__ = [
    "AGENT_COLUMN",
    "ARTIFACT_HASH_SQL",
    "BOOTSTRAP_CREATE_SQL",
    "COLLISION_PREFIX",
    "CORPORA_COLUMNS",
    "CORPORA_TABLE",
    "CREATE_TABLE_SQL",
    "DELETE_CORPUS_SQL",
    "DELETE_SESSIONS_SQL",
    "IDENTITY_COLUMNS",
    "INFO_COLUMNAR_SCHEMA",
    "INFO_SCHEMA_DIGEST",
    "INFO_SCHEMA_VERSION",
    "INSERT_SQL",
    "LAKE_ALIAS",
    "LAKE_HASH_SQL",
    "LAKE_INFO_COLUMNS",
    "LAKE_INFO_TABLE",
    "LAKE_METADATA_ALIAS",
    "LAKE_SCHEMA_VERSION",
    "LAKE_TABLES",
    "LAKE_TABLE_NAMES",
    "PARTITION_SQL",
    "READER_SELECT_SQL",
    "SESSION_KEY",
    "LakeColumn",
    "LakeTable",
    "expected_lake_info",
    "lake_info_mismatches",
    "lake_schema_digest",
]
