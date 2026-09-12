# SPDX-License-Identifier: Apache-2.0

r"""Write and detect the typed columnar artifacts (see :mod:`atif_duck.domain.columnar`).

Two jobs, both keyed on the contract in the domain module:

* :class:`ColumnarArtifactProducer` writes the four parquet files for one
  session from the trajectory dict the corpus writer already holds. It is the
  adapter atif-cli plugs into atif-corpus's ``ArtifactProducer`` port.
* :func:`session_has_columnar` and :func:`columnar_coverage` answer "may this
  session be read from parquet?" for the registry and for ``atif-sql status``,
  with one predicate so the two can never disagree.

Why the producer walks the dict instead of ``read_json`` over the staged
``trajectory.json``: DuckDB's JSON reader holds the whole document plus its
typed conversion in memory, which measured at 515 MB just to parse a 204 MB
session, against a materialize pass that otherwise peaks at 667 MB on the same
corpus. The dict is already in memory, so the producer streams it into DuckDB
as byte-bounded Arrow batches (one JSON text per step member, per tool call,
per result) and lets DuckDB do the typing.

Why one DuckDB statement per batch and a pyarrow writer, rather than one
``COPY ... TO`` per file: everything DuckDB allocates outside its buffer
manager (yyjson documents, string heaps, exported Arrow buffers) stays
resident in its bundled allocator until the statement ends, so a single COPY
over the 204 MB session measured +416 MB over the dict, and exporting the
typed rows as Arrow tables measured +230 MB, both roughly the size of the
text they had processed. Fetching each batch's typed rows as Python objects
(glibc-allocated, freed on the spot) and appending them to the parquet file
as one row group each measured +108 MB with 1 MiB batches. Row order is the
trajectory's array order by construction.

Why DuckDB still does the typing: the JSON columns (``tool_input``,
``content``, ``source_uuids``) must be byte-identical to what the JSON-path
views return, and those are yyjson's re-serialization of the value. Python's
``json.dumps`` output differs (``\u`` escapes, number spelling), but DuckDB
re-parses whatever text it is handed and re-serializes it, so passing
``json.dumps(value)`` in and reading a JSON column out lands on exactly the
bytes ``json_extract`` would have produced from ``trajectory.json``. The
column expressions themselves are shared with the views through
:mod:`atif_duck.infrastructure.projections`. TIMESTAMP columns cross into
Python as ``epoch_us`` integers and are cast back on the Arrow side, so the
value never passes through ``datetime`` and its range.

File mode: every parquet is written ``0o444``. The query sandbox grants each
one by path so a view can read it lazily, and a DuckDB path grant also permits
``COPY ... TO <path> (USE_TMP_FILE false)``. A read-only file turns that write
into an ``IO Error`` for any non-root user, which is what keeps injected SQL
from rewriting the transcript-derived surface it is reading.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_duck.domain.columnar import (
    COLUMNAR_FILENAMES,
    COLUMNAR_SCHEMA_VERSION,
    COLUMNAR_SCHEMAS,
    META_COLUMNAR_KEY,
    MIN_PARQUET_BYTES,
    SESSION_COLUMNS,
    SESSION_PARQUET,
    STEPS_PARQUET,
    TOOL_CALLS_PARQUET,
    TOOL_RESULTS_PARQUET,
    columnar_paths,
    is_current_columnar_schema,
)
from atif_duck.domain.session_id import session_id_rejection
from atif_duck.domain.sql_literal import SqlFragment, sql_literal
from atif_duck.infrastructure.projections import (
    CALL_COLUMNS,
    MEMBER_COLUMNS,
    RESULT_COLUMNS,
    STEP_MEMBERS,
    render,
    step_columns,
    step_key_columns,
)

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

# pyarrow ships no type information, so its schema and batch objects are
# annotated ``Any`` here (the same treatment atif-analytics gives them); the
# checker would otherwise report every one of them as Unknown.

# ---------------------------------------------------------------------------
# Detection (shared by the registry and `atif-sql status`)
# ---------------------------------------------------------------------------


def _readable_parquet(path: Path) -> bool:
    try:
        return path.stat().st_size > MIN_PARQUET_BYTES
    except OSError:
        return False


def session_has_columnar(session_dir: Path, meta_columnar_schema: object) -> bool:
    """True when ``session_dir`` may be read from its columnar artifacts.

    Two conditions, both required: ``meta.json``'s ``columnar_schema`` names
    this build's schema version, and all four parquet files are present and
    longer than a parquet header. A session failing either is read from
    ``trajectory.json``, which is always correct and merely slower, so this
    predicate can only ever cost speed.
    """
    return is_current_columnar_schema(meta_columnar_schema) and all(
        _readable_parquet(path) for path in columnar_paths(session_dir)
    )


@dataclass(frozen=True, slots=True)
class ColumnarCoverage:
    """How many complete sessions a corpus can serve from parquet versus JSON."""

    #: Sessions the registry reads from their columnar artifacts.
    columnar_sessions: int
    #: Sessions the registry reads from ``trajectory.json``.
    json_sessions: int

    @property
    def query_path(self) -> str:
        """``columnar``, ``json``, ``mixed``, or ``empty`` (no complete session)."""
        if self.columnar_sessions and not self.json_sessions:
            return "columnar"
        if self.json_sessions and not self.columnar_sessions:
            return "json"
        if self.columnar_sessions and self.json_sessions:
            return "mixed"
        return "empty"

    @property
    def total_sessions(self) -> int:
        """Complete sessions of either kind."""
        return self.columnar_sessions + self.json_sessions


def columnar_coverage(corpus_root: Path) -> ColumnarCoverage:
    """Count columnar-readable and JSON-only sessions under ``corpus_root``.

    Reads each session's ``meta.json`` (a torn dir without one is not a
    session at all, exactly as the registry's meta gate sees it) and applies
    :func:`session_has_columnar`. Used by ``atif-sql status`` so an operator
    can see which path ``query`` will take without opening DuckDB.
    """
    sessions_dir = corpus_root / "sessions"
    if not sessions_dir.is_dir():
        return ColumnarCoverage(columnar_sessions=0, json_sessions=0)
    columnar = json_only = 0
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir() or session_id_rejection(session_dir.name) is not None:
            # The registry registers nothing from a dir whose name fails the
            # session id boundary, so the coverage count must not see it either.
            continue
        try:
            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        schema = meta.get(META_COLUMNAR_KEY) if isinstance(meta, dict) else None
        if session_has_columnar(session_dir, schema):
            columnar += 1
        else:
            json_only += 1
    return ColumnarCoverage(columnar_sessions=columnar, json_sessions=json_only)


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------

#: Rows are batched into Arrow record batches by JSON text size, not row
#: count, so a session of a few huge tool results and a session of thousands
#: of tiny ones both stay inside a few MB of transient memory. One batch is
#: one DuckDB statement and one parquet row group. Measured on the 204 MB
#: session (201 MB of tool-result text): 1 MiB batches hold the producer's
#: peak at +108 MB over the trajectory dict, 4 MiB batches cost +184 MB, and
#: 512 KiB batches saved nothing further.
_BATCH_BYTES: int = 1024 * 1024

#: The relation every projection reads. One Arrow batch is registered under
#: this name per statement and unregistered right after.
_SOURCE: str = "batch_src"

#: Read-only for owner, group, and world: see the module docstring.
_ARTIFACT_MODE: int = 0o444

#: Parquet compression for every artifact.
_COMPRESSION: str = "zstd"

_COMPACT: tuple[str, str] = (",", ":")

#: DuckDB types that map to an Arrow type one to one. JSON, TIMESTAMP, and
#: STRUCT are handled by :func:`_arrow_type` itself.
_FLAT_ARROW_TYPES: tuple[str, ...] = ("VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN")


def _dumps(value: Any) -> str:
    """Serialize exactly as the corpus writer serializes ``trajectory.json``.

    Not required for equivalence (DuckDB re-serializes on the way in) but
    keeps the producer's input the same text the JSON path parses.
    """
    return json.dumps(value, separators=_COMPACT)


def _member(obj: Mapping[str, Any], key: str) -> str | None:
    """JSON text of ``obj[key]``, or SQL NULL when the key is absent.

    Mirrors ``json_extract(step, '$.key')``: a missing key is NULL, a present
    ``null`` is the JSON value ``null``. The two behave the same under every
    downstream cast except ``json_type``, and the message flattener reads
    both as "not an ARRAY", so the distinction is preserved rather than
    reasoned away.
    """
    return _dumps(obj[key]) if key in obj else None


def _step_rows(session_id: str, steps: Iterable[Any]) -> Iterator[tuple[str | None, ...]]:
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        yield (session_id, *(_member(step, key) for key in STEP_MEMBERS))


def _call_rows(session_id: str, steps: Iterable[Any]) -> Iterator[tuple[str | None, ...]]:
    # Mirrors ``WHERE json_extract(step, '$.tool_calls') IS NOT NULL`` then
    # ``UNNEST(json_extract(step, '$.tool_calls[*]'))``: only a list yields rows.
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        calls = step.get("tool_calls")
        if not isinstance(calls, list):
            continue
        step_id, ts = _member(step, "step_id"), _member(step, "timestamp")
        for call in calls:
            yield (session_id, step_id, ts, _dumps(call))


def _result_rows(session_id: str, steps: Iterable[Any]) -> Iterator[tuple[str | None, ...]]:
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, Mapping) else None
        if not isinstance(results, list):
            continue
        step_id, ts = _member(step, "step_id"), _member(step, "timestamp")
        for result in results:
            yield (session_id, step_id, ts, _dumps(result))


def _batches(rows: Iterable[tuple[str | None, ...]], schema: Any) -> Iterator[Any]:
    """Group rows into ``pyarrow.RecordBatch``es of about :data:`_BATCH_BYTES` of text."""
    import pyarrow as pa

    width = len(schema)
    columns: list[list[str | None]] = [[] for _ in range(width)]
    size = 0
    for row in rows:
        for index, value in enumerate(row):
            columns[index].append(value)
            if value is not None:
                size += len(value)
        if size >= _BATCH_BYTES:
            yield pa.record_batch(columns, schema=schema)
            columns = [[] for _ in range(width)]
            size = 0
    # Always emit the tail, even when empty: an empty parquet with the right
    # columns is the correct artifact for a session with no tool calls.
    yield pa.record_batch(columns, schema=schema)


def _string_schema(names: Iterable[str]) -> Any:
    import pyarrow as pa

    return pa.schema([pa.field(name, pa.string()) for name in names])


def _struct_members(struct_type: str) -> tuple[tuple[str, str], ...]:
    """``(name, type)`` members of a flat DuckDB ``STRUCT(a T, b U)`` type string.

    Only the two flat struct shapes in :data:`SESSION_COLUMNS` reach here, so
    a comma-split over the member list is sufficient.
    """
    inner = struct_type[len("STRUCT(") : -1]
    members: list[tuple[str, str]] = []
    for part in inner.split(","):
        name, sql_type = part.strip().split(" ", 1)
        members.append((name, sql_type))
    return tuple(members)


def _struct_spec(struct_type: str) -> str:
    """Render a DuckDB ``STRUCT`` type as ``json_transform``'s JSON spec."""
    return json.dumps(dict(_struct_members(struct_type)))


def _session_row_sql(session_id_literal: SqlFragment) -> SqlFragment:
    """The one-row ``session.parquet`` projection over the registered batch.

    Each trajectory member arrives as JSON text; ``json_transform`` types the
    two structs with the same shape the JSON reader projects them to, so the
    ``sessions`` view reads identical struct fields either way.
    """
    typed: list[str] = [f"{session_id_literal} AS session_id_path"]
    for name, sql_type in SESSION_COLUMNS[1:]:
        if sql_type.startswith("STRUCT"):
            typed.append(
                f"CAST(json_transform({name}, {sql_literal(_struct_spec(sql_type))}) AS {sql_type}) AS {name}"
            )
        elif sql_type == "JSON":
            typed.append(f"json_extract({name}, '$') AS {name}")
        else:
            typed.append(f"json_extract_string({name}, '$')::{sql_type} AS {name}")
    # Every name and type comes from SESSION_COLUMNS; the only literal is the
    # sql_literal-escaped session id.
    return SqlFragment("SELECT " + ", ".join(typed) + f" FROM {_SOURCE}")  # noqa: S608  # nosec B608 - SESSION_COLUMNS constants; the id came through sql_literal


def _arrow_type(sql_type: str) -> Any:
    """The Arrow type DuckDB's ``read_parquet`` maps back to exactly ``sql_type``.

    JSON is the canonical ``arrow.json`` extension (parquet's JSON logical
    type, which DuckDB reads as JSON), TIMESTAMP is microseconds without a
    zone (DuckDB's own TIMESTAMP), and a STRUCT is built member by member.
    Anything else the catalog might grow is a hard error here rather than a
    silently wrong file.
    """
    import pyarrow as pa

    if sql_type == "JSON":
        return pa.json_(pa.string())
    if sql_type == "TIMESTAMP":
        return pa.timestamp("us")
    if sql_type.startswith("STRUCT("):
        return pa.struct([pa.field(name, _arrow_type(t)) for name, t in _struct_members(sql_type)])
    if sql_type in _FLAT_ARROW_TYPES:
        flat = {
            "VARCHAR": pa.string(),
            "BIGINT": pa.int64(),
            "DOUBLE": pa.float64(),
            "BOOLEAN": pa.bool_(),
        }
        return flat[sql_type]
    msg = f"columnar producer has no Arrow mapping for DuckDB type {sql_type!r}"
    raise ValueError(msg)


def _arrow_schema(columns: Sequence[tuple[str, str]]) -> Any:
    import pyarrow as pa

    return pa.schema([pa.field(name, _arrow_type(sql_type)) for name, sql_type in columns])


def _fetch_select(columns: Sequence[tuple[str, str]], inner_sql: SqlFragment) -> SqlFragment:
    """Wrap a projection so its rows cross into Python losslessly.

    Columns are picked by name in catalog order, so the file's column order is
    the catalog's whatever order the projection spells them in. TIMESTAMP
    columns travel as ``epoch_us`` integers (see the module docstring); a
    STRUCT member typed TIMESTAMP would need the same treatment and has no
    caller today, so it is refused rather than mis-typed.
    """
    picks: list[str] = []
    for name, sql_type in columns:
        if sql_type == "TIMESTAMP":
            picks.append(f'epoch_us("{name}") AS "{name}"')
            continue
        if sql_type.startswith("STRUCT(") and any(
            member_type == "TIMESTAMP" for _, member_type in _struct_members(sql_type)
        ):
            msg = f"columnar producer cannot fetch a TIMESTAMP struct member: {name} {sql_type}"
            raise ValueError(msg)
        picks.append(f'"{name}"')
    # Column names are catalog constants; inner_sql is built from constants.
    return SqlFragment(f"SELECT {', '.join(picks)} FROM ({inner_sql})")  # noqa: S608  # nosec B608 - catalog column names over a constant-built inner SELECT


def _arrow_array(values: list[Any], sql_type: str) -> Any:
    """Type one fetched column (Python objects from DuckDB) as :func:`_arrow_type` says."""
    import pyarrow as pa

    if sql_type == "JSON":
        # DuckDB hands JSON to Python as its normalized text.
        return pa.ExtensionArray.from_storage(pa.json_(pa.string()), pa.array(values, pa.string()))
    if sql_type == "TIMESTAMP":
        return pa.array(values, pa.int64()).cast(pa.timestamp("us"))
    if sql_type.startswith("STRUCT("):
        # DuckDB hands a STRUCT to Python as a dict (or None for a NULL struct).
        members = _struct_members(sql_type)
        arrays = [
            _arrow_array([None if value is None else value[name] for value in values], member_type)
            for name, member_type in members
        ]
        fields = [pa.field(name, _arrow_type(member_type)) for name, member_type in members]
        mask = pa.array([value is None for value in values], pa.bool_())
        return pa.StructArray.from_arrays(arrays, fields=fields, mask=mask)
    return pa.array(values, _arrow_type(sql_type))


def _arrow_table(
    rows: list[tuple[Any, ...]], columns: Sequence[tuple[str, str]], schema: Any
) -> Any:
    import pyarrow as pa

    fetched: list[list[Any]] = [[] for _ in columns]
    for row in rows:
        for index, value in enumerate(row):
            fetched[index].append(value)
    arrays = [
        _arrow_array(values, sql_type)
        for values, (_, sql_type) in zip(fetched, columns, strict=True)
    ]
    return pa.table(arrays, schema=schema)


class ColumnarArtifactProducer:
    """Write a session's typed columnar artifacts into its staged directory.

    Satisfies atif-corpus's ``ArtifactProducer`` port structurally (the two
    packages may not import each other). Each call opens its own in-memory
    DuckDB connection and closes it before returning: DuckDB's allocator
    keeps a connection's freed memory resident, and over a pass of hundreds
    of sessions a shared connection measured 50 MB higher at peak and 140 MB
    higher between sessions than a fresh one, against 9 ms to open one.
    ``duckdb`` and ``pyarrow`` are imported on first use so importing this
    module stays cheap for the commands that only detect artifacts.
    """

    def __init__(self, *, threads: int = 2) -> None:
        #: DuckDB worker threads for the per-batch projections. Two is enough
        #: to overlap Python's batch production with DuckDB's parsing; more
        #: only adds in-flight allocations to the peak.
        self.threads = threads

    def _connection(self) -> duckdb.DuckDBPyConnection:
        import duckdb

        con = duckdb.connect()
        con.execute(f"SET threads={int(self.threads)}")
        return con

    def produce(
        self,
        session_dir: Path,
        *,
        session_id: str,
        trajectory: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Write the four parquet files into ``session_dir``; return the meta extras.

        Parameters
        ----------
        session_dir
            The STAGED session directory (the corpus writer swaps it into
            ``sessions/<session_id>/`` afterwards, so no path inside the
            files depends on where the directory ends up).
        session_id
            The canonical session key (the directory name the corpus writer
            will publish under), stamped into every row as ``session_id``.
        trajectory
            The ATIF trajectory dict the corpus writer just serialized.

        Returns
        -------
        Mapping[str, Any]
            ``{"columnar_schema": COLUMNAR_SCHEMA_VERSION}`` for ``meta.json``.

        Raises
        ------
        duckdb.Error, OSError, pyarrow.ArrowException
            Propagated: the corpus writer records the session as failed and
            the staged directory never publishes.
        """
        con = self._connection()
        try:
            self._produce(con, session_dir, session_id, trajectory)
        finally:
            con.close()
        logger.debug("columnar: wrote {} for session {}", COLUMNAR_FILENAMES, session_id)
        return {META_COLUMNAR_KEY: COLUMNAR_SCHEMA_VERSION}

    @staticmethod
    def _produce(
        con: duckdb.DuckDBPyConnection,
        session_dir: Path,
        session_id: str,
        trajectory: Mapping[str, Any],
    ) -> None:
        steps = trajectory.get("steps")
        step_list: list[Any] = steps if isinstance(steps, list) else []
        session_literal = sql_literal(session_id)

        session_schema = _string_schema(name for name, _ in SESSION_COLUMNS[1:])
        session_row = tuple(_member(trajectory, name) for name, _ in SESSION_COLUMNS[1:])
        ColumnarArtifactProducer._write(
            con,
            _batches([session_row], session_schema),
            _session_row_sql(session_literal),
            COLUMNAR_SCHEMAS[SESSION_PARQUET],
            session_dir / SESSION_PARQUET,
        )

        steps_schema = _string_schema(("session_id", *STEP_MEMBERS))
        ColumnarArtifactProducer._write(
            con,
            _batches(_step_rows(session_id, step_list), steps_schema),
            SqlFragment(
                f"SELECT session_id, {render(step_columns(MEMBER_COLUMNS))} FROM {_SOURCE}"  # noqa: S608  # nosec B608 - projections constants
            ),
            COLUMNAR_SCHEMAS[STEPS_PARQUET],
            session_dir / STEPS_PARQUET,
        )

        calls_schema = _string_schema(("session_id", "step_id", "timestamp", "call"))
        ColumnarArtifactProducer._write(
            con,
            _batches(_call_rows(session_id, step_list), calls_schema),
            SqlFragment(
                "SELECT session_id, "  # noqa: S608  # nosec B608 - projections constants only
                f"{render(step_key_columns(MEMBER_COLUMNS))}, {render(CALL_COLUMNS)} FROM {_SOURCE}"
            ),
            COLUMNAR_SCHEMAS[TOOL_CALLS_PARQUET],
            session_dir / TOOL_CALLS_PARQUET,
        )

        results_schema = _string_schema(("session_id", "step_id", "timestamp", "res"))
        ColumnarArtifactProducer._write(
            con,
            _batches(_result_rows(session_id, step_list), results_schema),
            SqlFragment(
                "SELECT session_id, "  # noqa: S608  # nosec B608 - projections constants only
                f"{render(step_key_columns(MEMBER_COLUMNS))}, {render(RESULT_COLUMNS)} FROM {_SOURCE}"
            ),
            COLUMNAR_SCHEMAS[TOOL_RESULTS_PARQUET],
            session_dir / TOOL_RESULTS_PARQUET,
        )

    @staticmethod
    def _write(
        con: duckdb.DuckDBPyConnection,
        batches: Iterator[Any],
        select_sql: SqlFragment,
        columns: Sequence[tuple[str, str]],
        target: Path,
    ) -> None:
        """Project each batch through DuckDB and append it to ``target`` as one row group."""
        import pyarrow.parquet as pq

        schema = _arrow_schema(columns)
        fetch_sql = _fetch_select(columns, select_sql)
        writer = pq.ParquetWriter(str(target), schema, compression=_COMPRESSION)
        try:
            for batch in batches:
                con.register(_SOURCE, batch)
                try:
                    rows = con.execute(fetch_sql).fetchall()
                finally:
                    con.unregister(_SOURCE)
                writer.write_table(_arrow_table(rows, columns, schema))
        finally:
            writer.close()
        # Durability matches the JSON artifacts: bytes on stable storage before
        # meta.json publishes them. Then read-only, per the module docstring.
        fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        target.chmod(_ARTIFACT_MODE)


__all__ = [
    "ColumnarArtifactProducer",
    "ColumnarCoverage",
    "columnar_coverage",
    "session_has_columnar",
]
