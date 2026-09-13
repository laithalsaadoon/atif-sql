# SPDX-License-Identifier: Apache-2.0

"""DuckDB view and macro registry over the materialized ATIF corpus.

Wires a DuckDB connection to a ``<corpus_root>/sessions/<id>/`` tree (per
docs/CONTRACT.md: ``trajectory.json``, ``edges.jsonl``, ``loss_report.json``,
``meta.json``) and exposes it as a stable set of SQL views and analytical
macros. Every view reads ATIF-v1.7, never raw Claude Code JSONL, so a
column's meaning comes from the ATIF schema rather than from a transcript
field that happens to share its name.

Design notes
------------
* Raw readers are ``CREATE TEMP TABLE`` over ``read_json`` with an explicit
  ``columns={...}`` projection. In DuckDB 1.5+ the dict is a *strict
  projection filter*: every field any downstream view touches must appear in
  the dict or it silently disappears (the strict-filter lesson). Listing the
  fields also skips JSON schema inference — the dominant cost on a large
  corpus, otherwise paid on every ``DESCRIBE``/view bind.
* ``filename=true`` + a path regexp derive ``session_id_path`` from the
  ``sessions/<id>/`` directory, which is the canonical session key (the
  in-document ``session_id`` is advisory; the directory name is what
  atif-corpus keyed the materialization on).
* ``trajectory.json`` is ONE document per file (``format='auto'``, compact
  JSON), NOT newline-delimited; ``steps`` is projected as a ``JSON[]``
  column so views unnest it lazily at query time. ``edges.jsonl`` is
  ``format='newline_delimited'``.
* Two sources per session, chosen per session. A session whose
  ``meta.json`` names the current ``columnar_schema`` and whose four parquet
  artifacts are present (:mod:`atif_duck.domain.columnar`) is read through
  lazy views over ``read_parquet``, so no JSON is parsed for it at query
  time; every other session is read from ``trajectory.json`` exactly as
  before. When a corpus holds both kinds the two branches ``UNION ALL`` into
  one relation per raw reader (``v_raw_trajectories``, ``v_raw_steps``,
  ``v_raw_tool_calls``, ``v_raw_tool_results``), and the business views
  never learn which branch a row came from. The parquet branch is lazy on
  purpose: loading the tool-result text eagerly measured at 2.1 s per
  process on a 300-session corpus, which is what the JSON path already
  costs, whereas a lazy view answers the panel queries in about 20 ms.
  :func:`register_raw` returns which sessions took which path and every
  parquet the views will open, so the query sandbox can grant exactly those
  files.
* All views use ``CREATE OR REPLACE`` so callers may safely re-register.
* No corpus path is ever part of a statement's text. The TEMP TABLE readers
  take their glob or file list as a bound parameter (``read_json(?)``); the
  parquet readers are relations built through the connection's
  ``read_parquet`` API (the file list is a Python value) and registered as
  views, because ``CREATE VIEW`` itself cannot be prepared. Every remaining interpolation is
  a module constant, a catalog constant, or a projection expression, and the
  AST test in ``tests/test_sql_text_boundaries.py`` fails on anything else.
* A session directory whose name fails the boundary in
  :mod:`atif_duck.domain.session_id` registers nothing: the name is the one
  piece of outside text that becomes a path, so it is checked before any path
  is built, once per registration, and reported in
  :attr:`RawSources.rejected_session_ids`.
* register-or-fail-loud: every registration function logs via
  ``logger.exception`` and re-raises on any DDL failure.

Semantics a transcript-shaped reader gets wrong (documented per-view below)
--------------------------------------------------------------------------
* ``steps``, not ``messages``, is the primary per-turn surface: one row
  per ATIF step (harbor bundles all events sharing an assistant
  ``message.id`` into one step, so step count < raw message count by design).
* ``messages`` is a uuid-keyed view reconstructed from ``edges.jsonl``,
  which carries the raw-record identity the trajectory itself cannot
  provide (fidelity gap 7: UUID_NOT_PRESERVED).
* Token columns follow ATIF ``Metrics`` semantics: ``prompt_tokens`` is the
  TOTAL input (non-cached + cache_read + cache_creation), ``cached_tokens``
  is the cache-read subset, and ``cache_creation`` is recovered from
  ``metrics.extra`` (fidelity gap 6: CACHE_SPLIT_PARTIAL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from atif_duck.domain.catalog import DEFAULT_PRICING, VIEW_SCHEMA
from atif_duck.domain.columnar import (
    META_COLUMNAR_KEY,
    SESSION_COLUMNS,
    SESSION_PARQUET,
    STEPS_PARQUET,
    TOOL_CALLS_PARQUET,
    TOOL_RESULTS_PARQUET,
)
from atif_duck.domain.embedding_guard import ensure_store_matches
from atif_duck.domain.session_id import session_id_rejection
from atif_duck.domain.sql_literal import SqlFragment, sql_literal
from atif_duck.infrastructure.columnar import ColumnarCoverage, session_has_columnar
from atif_duck.infrastructure.projections import (
    CALL_COLUMNS,
    RESULT_COLUMNS,
    WHOLE_STEP,
    render,
    step_columns,
    step_key_columns,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    import duckdb

# ---------------------------------------------------------------------------
# Raw-reader object names
# ---------------------------------------------------------------------------
#: The materialized raw readers. Named constants rather than inline literals
#: so a DDL interpolation site cannot typo a second table into existence.
#: Deliberately absent from :data:`atif_duck.domain.catalog.VIEW_NAMES`:
#: they describe corpus files rather than being queryable business surface.
#:
#: ``v_raw_trajectories`` carries the trajectory's top-level members minus
#: ``steps``; the step-level surface is ``v_raw_steps`` /
#: ``v_raw_tool_calls`` / ``v_raw_tool_results``, each already in its view's
#: column shape whichever source it came from. The JSON-path TEMP TABLE that
#: still holds ``steps JSON[]`` is ``v_raw_trajectories_json``.
_RAW_TRAJECTORIES_TABLE: str = "v_raw_trajectories"
_RAW_TRAJECTORIES_JSON_TABLE: str = "v_raw_trajectories_json"
_RAW_STEPS_TABLE: str = "v_raw_steps"
_RAW_TOOL_CALLS_TABLE: str = "v_raw_tool_calls"
_RAW_TOOL_RESULTS_TABLE: str = "v_raw_tool_results"
_RAW_EDGES_TABLE: str = "v_raw_edges"
_RAW_LOSS_REPORTS_TABLE: str = "v_raw_loss_reports"
_RAW_META_TABLE: str = "v_raw_meta"

#: The columnar branch's parquet readers: one parameterized ``read_parquet``
#: relation per artifact kind, registered as a view under these names and
#: selected from by the union views above. They exist only while at least one
#: session is read from parquet.
_RAW_SESSIONS_PARQUET_TABLE: str = "v_raw_sessions_parquet"
_RAW_STEPS_PARQUET_TABLE: str = "v_raw_steps_parquet"
_RAW_TOOL_CALLS_PARQUET_TABLE: str = "v_raw_tool_calls_parquet"
_RAW_TOOL_RESULTS_PARQUET_TABLE: str = "v_raw_tool_results_parquet"


@dataclass(frozen=True, slots=True)
class RawSources:
    """Which source each complete session was registered from.

    Returned by :func:`register_raw` (and :func:`register`) so the
    composition root can grant the sandbox exactly the files the views read
    lazily, and so a test can prove the columnar path was taken rather than
    silently falling back to JSON.
    """

    #: Sessions read through lazy views over their parquet artifacts.
    columnar_session_ids: tuple[str, ...]
    #: Sessions read from ``trajectory.json`` (no or stale columnar artifacts).
    json_session_ids: tuple[str, ...]
    #: Every parquet a registered raw reader opens at caller-query time.
    lazy_read_paths: tuple[Path, ...]
    #: Session directory names that failed the session id boundary and so
    #: contribute to no view (sorted). Logged once each at registration.
    rejected_session_ids: tuple[str, ...] = ()

    @property
    def coverage(self) -> ColumnarCoverage:
        """The same counts ``atif-sql status`` reports."""
        return ColumnarCoverage(
            columnar_sessions=len(self.columnar_session_ids),
            json_sessions=len(self.json_session_ids),
        )


#: Ceiling for a ``read_json`` ``maximum_object_size``. Live trajectory.json
#: files reach 436 MB because harbor inlines subagent sidechains and tool
#: outputs; 1 GiB is about 2.3x headroom over the largest observed document.
_OBJECT_SIZE_CAP: int = 1_073_741_824

#: Floor for the same bound: DuckDB's own default.
_OBJECT_SIZE_FLOOR: int = 16_777_216

#: Headroom over the largest file when sizing the bound: a quarter of the file
#: plus one MiB, so a file that grows a little between the stat and the read
#: still parses.
_OBJECT_SIZE_HEADROOM_DIVISOR: int = 4
_OBJECT_SIZE_HEADROOM_BYTES: int = 1_048_576

#: The DuckDB extension that reads the Lance embeddings store.
LANCE_EXTENSION: str = "lance"


def _object_size_bound(paths: Iterable[Path]) -> int:
    """``maximum_object_size`` for a ``read_json`` over ``paths``, sized from the files.

    The bound is not free. DuckDB's eager JSON reader reserves about twice
    this many bytes PER THREAD before it parses anything, so the 1 GiB
    constant this replaced cost 2 GiB a thread: over 300 ``edges.jsonl``
    files of under 8 MB each it exhausted a 6 GB ``memory_limit`` at four
    threads and a 25 GB one at sixteen, while 16 MiB read the same rows in
    0.10 s (measured on the 300-session panel corpus). An earlier note here
    claimed lowering it changed nothing; that measurement held the thread
    count at one.

    A newline-delimited file's objects are its lines and a one-document file
    IS its object, so the largest file present bounds every object either
    reader meets. That size plus headroom, floored at DuckDB's default and
    capped at :data:`_OBJECT_SIZE_CAP`, is the bound. A path that vanished
    between the listing and the stat counts as zero.
    """
    largest = 0
    for path in paths:
        try:
            largest = max(largest, path.stat().st_size)
        except OSError:
            continue
    with_headroom = largest + largest // _OBJECT_SIZE_HEADROOM_DIVISOR + _OBJECT_SIZE_HEADROOM_BYTES
    return max(_OBJECT_SIZE_FLOOR, min(_OBJECT_SIZE_CAP, with_headroom))


# Explicit projection for ``v_raw_trajectories``.
#
# ATIF-v1.7 ``Trajectory`` is ``extra='forbid'``, so the top level is a
# closed set; we project exactly what the views touch. ``steps`` stays
# ``JSON[]`` (not a deep STRUCT) because Step.message is a union
# (str | ContentPart[]) and Step.extra / metrics.extra are free-form —
# a STRUCT projection would force one shape and silently null the other.
_TRAJECTORY_COLUMNS: dict[str, str] = {
    "schema_version": "VARCHAR",
    "session_id": "VARCHAR",
    "trajectory_id": "VARCHAR",
    "agent": "STRUCT(name VARCHAR, version VARCHAR, model_name VARCHAR, extra JSON)",
    "steps": "JSON[]",
    "final_metrics": (
        "STRUCT(total_prompt_tokens BIGINT, total_completion_tokens BIGINT, "
        "total_cached_tokens BIGINT, total_cost_usd DOUBLE, total_steps BIGINT, "
        "extra JSON)"
    ),
    "extra": "JSON",
}

# Explicit projection for ``v_raw_edges``: one line per RAW transcript record
# per docs/CONTRACT.md. ``parent_uuid`` is declared VARCHAR outright: a root
# record leaves it null, so inferred typing resolves it as a NULL-vs-string
# JSON union and every downstream view then needs its own CAST. Declaring the
# type in the one explicit-columns reader pays that cost once.
_EDGE_COLUMNS: dict[str, str] = {
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
_LOSS_REPORT_COLUMNS: dict[str, str] = {
    "record_counts": "JSON",
    "records_total": "BIGINT",
    "records_converted": "BIGINT",
    "records_dropped": "BIGINT",
    "gaps_observed": "JSON",
    "subagent_files_found": "BIGINT",
    "subagent_files_convertible": "BIGINT",
    "workflow_subagent_files_found": "BIGINT",
}

# Explicit projection for ``v_raw_meta`` per docs/CONTRACT.md.
_META_COLUMNS: dict[str, str] = {
    "session_id": "VARCHAR",
    "source_mtime_ns": "BIGINT",
    "source_files": "JSON",
    "harbor_version": "VARCHAR",
    "converter_version": "VARCHAR",
    "materialized_at": "VARCHAR",
    # Which agent wrote the transcript. Added 2026-09-11 with Codex support, so
    # a session materialized before then has no such key and reads as NULL —
    # an explicit ``columns=`` projection nulls a missing key rather than
    # failing, which is what lets a corpus predating the change still register.
    # Queries read ``sessions.agent`` (from the trajectory) instead; this
    # column is provenance for an operator reading meta.json directly.
    "agent": "VARCHAR",
    # Which columnar schema the session's parquet artifacts were written
    # against (:data:`atif_duck.domain.columnar.COLUMNAR_SCHEMA_VERSION`).
    # Absent (NULL) on a session materialized before the artifacts existed;
    # that session is read from trajectory.json, which is always correct.
    META_COLUMNAR_KEY: "BIGINT",
}


def _typed_empty(columns: Sequence[tuple[str, str]]) -> SqlFragment:
    """A zero-row SELECT carrying the declared column names and types."""
    projected = ", ".join(f"CAST(NULL AS {sql_type}) AS {name}" for name, sql_type in columns)
    return SqlFragment(f"SELECT {projected} WHERE false")


def _typed_parquet_select(reader: str, columns: Sequence[tuple[str, str]]) -> SqlFragment:
    """SELECT the declared columns, cast to their declared types, from a parquet reader.

    ``reader`` is one of the ``_RAW_*_PARQUET_TABLE`` names bound by
    :func:`_bind_parquet_reader`; the paths themselves never appear here. The
    casts are belt and braces: the producer wrote these exact types, and a
    ``UNION ALL`` against the JSON branch needs both sides to agree on every
    column, so the declared type is asserted here rather than inferred from
    the first file.
    """
    projected = ", ".join(f"CAST({name} AS {sql_type}) AS {name}" for name, sql_type in columns)
    return SqlFragment(f"SELECT {projected} FROM {reader}")  # noqa: S608  # nosec B608 - reader is a module constant; columns are catalog constants


def _union_or_empty(
    branches: Sequence[SqlFragment], columns: Sequence[tuple[str, str]]
) -> SqlFragment:
    """Join the present source branches, or an empty typed relation if none is."""
    return SqlFragment("\nUNION ALL\n".join(branches)) if branches else _typed_empty(columns)


def _json_steps_select() -> SqlFragment:
    """The JSON path's ``steps`` projection over ``v_raw_trajectories_json``."""
    return SqlFragment(
        f"SELECT t.session_id_path AS session_id,\n    {render(step_columns(WHOLE_STEP))}\n"  # nosec B608 - projection constants over a module-constant table
        f"FROM {_RAW_TRAJECTORIES_JSON_TABLE} t, UNNEST(t.steps) AS s(step)"
    )


def _json_tool_calls_select() -> SqlFragment:
    """The JSON path's ``tool_calls`` projection.

    The inner query narrows each step to (ids, ts, calls list) BEFORE the
    second UNNEST: a step's JSON string is megabytes on harbor trajectories
    that inline subagent sidechains, and unnesting it directly replicates
    that string once per tool call.
    """
    return SqlFragment(
        f"SELECT session_id, step_id, ts, {render(CALL_COLUMNS)}\n"  # noqa: S608  # nosec B608 - constants only
        "FROM (\n"
        f"    SELECT t.session_id_path AS session_id, {render(step_key_columns(WHOLE_STEP))},\n"
        f"           {WHOLE_STEP.json('tool_calls', '[*]')} AS calls\n"
        f"    FROM {_RAW_TRAJECTORIES_JSON_TABLE} t, UNNEST(t.steps) AS s(step)\n"
        f"    WHERE {WHOLE_STEP.json('tool_calls')} IS NOT NULL\n"
        ") step_calls, UNNEST(calls) AS c(call)"
    )


def _json_tool_results_select() -> SqlFragment:
    """The JSON path's ``tool_results`` projection (same early narrowing)."""
    return SqlFragment(
        f"SELECT session_id, step_id, ts, {render(RESULT_COLUMNS)}\n"  # noqa: S608  # nosec B608 - constants only
        "FROM (\n"
        f"    SELECT t.session_id_path AS session_id, {render(step_key_columns(WHOLE_STEP))},\n"
        f"           {WHOLE_STEP.json('observation', '.results[*]')} AS results\n"
        f"    FROM {_RAW_TRAJECTORIES_JSON_TABLE} t, UNNEST(t.steps) AS s(step)\n"
        f"    WHERE {WHOLE_STEP.json('observation', '.results')} IS NOT NULL\n"
        ") step_results, UNNEST(results) AS r(res)"
    )


#: ``v_raw_trajectories`` columns, in order: the trajectory's top-level
#: members minus ``steps``, plus the two path-derived keys.
_TRAJECTORY_RELATION_COLUMNS: tuple[tuple[str, str], ...] = (
    *(column for column in SESSION_COLUMNS if column[0] != "session_id_path"),
    ("trajectory_path", "VARCHAR"),
    ("session_id_path", "VARCHAR"),
)


def _json_trajectories_select() -> SqlFragment:
    names = ", ".join(name for name, _ in _TRAJECTORY_RELATION_COLUMNS)
    return SqlFragment(f"SELECT {names} FROM {_RAW_TRAJECTORIES_JSON_TABLE}")  # noqa: S608  # nosec B608 - constants only


def _columnar_trajectories_select() -> SqlFragment:
    """``session.parquet`` rows in ``v_raw_trajectories`` shape.

    ``trajectory_path`` is derived the way ``read_json(filename=true)``
    reports it (``<sessions_dir>/<id>/trajectory.json``), so the ``sessions``
    view's path column reads the same whichever branch produced the row. The
    parquet reader carries each file's own ``filename``
    (``<sessions_dir>/<id>/session.parquet``), so the trajectory path is that
    string with its last component replaced: the corpus root never has to be
    spliced into the statement.
    """
    casts = ", ".join(
        f"CAST({name} AS {sql_type}) AS {name}"
        for name, sql_type in SESSION_COLUMNS
        if name != "session_id_path"
    )
    path_expr = "regexp_replace(filename, '/[^/]+$', '/trajectory.json')"
    return SqlFragment(
        f"SELECT {casts}, {path_expr} AS trajectory_path, session_id_path "  # noqa: S608  # nosec B608 - catalog constants over a module-constant reader
        f"FROM {_RAW_SESSIONS_PARQUET_TABLE}"
    )


def _render_columns_clause(columns: dict[str, str]) -> SqlFragment:
    """Render a ``columns={...}`` clause body for ``read_json``.

    Keys are bare DuckDB identifiers (no quoting); values are SQL type
    strings wrapped in single quotes. Both halves come from code-side
    constants — never user input — so escaping is defensive only.
    """
    return SqlFragment(", ".join(f"{name}: {sql_literal(typ)}" for name, typ in columns.items()))


def _catalog_columns(view_name: str) -> SqlFragment:
    """The catalog's column names for ``view_name``, comma-joined in catalog order."""
    return SqlFragment(", ".join(name for name, _ in VIEW_SCHEMA[view_name]))


def _bind_parquet_reader(
    con: duckdb.DuckDBPyConnection,
    reader: str,
    paths: Sequence[Path],
    *,
    with_filename: bool = False,
) -> None:
    """Register ``reader`` as a lazy ``read_parquet`` relation over ``paths``.

    ``CREATE VIEW ... read_parquet(?)`` is refused ("this type of statement
    can't be prepared"), so the file list goes through the connection's own
    ``read_parquet`` relation API, which takes it as a Python value, and that
    relation is registered as the view. It plans as the same ``PARQUET_SCAN``
    a hand-written view would (projection and filter pushdown included) and
    opens the files per query, not at registration. Measured on the 300
    session panel corpus it matches the inlined-literal view on both wall
    time and peak memory; a relation built from ``sql("... read_parquet(?)",
    params=...)`` instead did NOT (about 3x the memory), so that shape is
    deliberately not used here.
    """
    files = [str(path) for path in paths]
    con.read_parquet(files, filename=with_filename).create_view(reader, replace=True)


# ---------------------------------------------------------------------------
# Raw readers
# ---------------------------------------------------------------------------


def _gate_session_dirs(con: duckdb.DuckDBPyConnection, sessions_dir: Path) -> tuple[str, ...]:
    """Apply the two per-directory gates; return the names the boundary rejected.

    Walks ``sessions_dir`` once, right after ``v_raw_meta`` is read and
    before any per-session path is built:

    * A directory whose name fails the session id boundary
      (:mod:`atif_duck.domain.session_id`) is REJECTED: its row is deleted
      from ``v_raw_meta`` (by bound parameter, so the offending name is data
      even here), which keeps it out of every other reader through the meta
      gate and out of :func:`_split_sources`, and it is logged once. A corpus
      an older version wrote may hold such a dir; it is reported in
      :attr:`RawSources.rejected_session_ids` and never read.
    * A well-named directory without a ``meta.json`` is INCOMPLETE (torn
      writer, partial cleanup). The meta gate already excludes it; this makes
      the exclusion OBSERVABLE, because a session silently missing from
      ``sessions`` is much harder to diagnose than a logged skip.
    """
    if not sessions_dir.is_dir():
        return ()
    # The one interpolation is the module constant _RAW_META_TABLE.
    rows = con.execute(f"SELECT session_id_path FROM {_RAW_META_TABLE}").fetchall()  # noqa: S608  # nosec B608 - module constant
    with_meta = {row[0] for row in rows}
    rejected: list[str] = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        name = session_dir.name
        rejection = session_id_rejection(name)
        if rejection is not None:
            logger.warning(
                "Rejecting session dir {!r} ({}); nothing from it is registered", name, rejection
            )
            con.execute(f"DELETE FROM {_RAW_META_TABLE} WHERE session_id_path = ?", [name])  # noqa: S608  # nosec B608 - module constant; the name is bound
            rejected.append(name)
            continue
        if name not in with_meta:
            logger.warning(
                "Skipping incomplete session dir {} (no meta.json — "
                "crashed writer or partial cleanup); excluded from all views",
                session_dir,
            )
    return tuple(sorted(rejected))


def _split_sources(
    con: duckdb.DuckDBPyConnection, sessions_dir: Path
) -> tuple[list[str], list[str]]:
    """Partition the meta-bearing sessions into (columnar ids, JSON ids).

    A session is columnar when :func:`session_has_columnar` says so; every
    other session with a ``trajectory.json`` is JSON. A meta-bearing dir
    with neither readable source is skipped with a warning rather than
    failing the whole registration: ``read_json`` over an explicit path list
    errors on one missing file, and one broken session must not take the
    corpus offline.

    Runs after :func:`_gate_session_dirs`, so every id read here has passed
    the session id boundary and may become a path component.
    """
    # The one interpolation is the module constant _RAW_META_TABLE plus the
    # contract's meta key.
    rows = con.execute(
        f"SELECT session_id_path, {META_COLUMNAR_KEY} FROM {_RAW_META_TABLE} ORDER BY 1"  # noqa: S608  # nosec B608 - module constants
    ).fetchall()
    columnar_ids: list[str] = []
    json_ids: list[str] = []
    for session_id, columnar_schema in rows:
        session_dir = sessions_dir / str(session_id)
        if session_has_columnar(session_dir, columnar_schema):
            columnar_ids.append(str(session_id))
        elif (session_dir / "trajectory.json").is_file():
            json_ids.append(str(session_id))
        else:
            logger.warning(
                "Skipping session dir {} (meta.json present but no readable "
                "trajectory.json or columnar artifacts); excluded from all views",
                session_dir,
            )
    return columnar_ids, json_ids


def register_raw(con: duckdb.DuckDBPyConnection, corpus_root: Path) -> RawSources:
    """Create the raw readers over ``corpus_root``.

    ``v_raw_meta`` / ``v_raw_edges`` / ``v_raw_loss_reports`` are TEMP TABLEs
    over ``read_json`` of the CONTRACT corpus layout
    ``<corpus_root>/sessions/<session_id>/...``, with ``session_id_path``
    derived from the directory component via regexp over ``filename``. Each
    glob is ONE bound parameter, never statement text. (A glob rather than an
    explicit file list because DuckDB reads 300 small files about twice as
    fast through a glob; the cost is that a corpus root containing a glob
    metacharacter such as ``[`` or a backslash is not readable, which was already so.)

    ``v_raw_trajectories`` / ``v_raw_steps`` / ``v_raw_tool_calls`` /
    ``v_raw_tool_results`` are VIEWs that union up to two sources, chosen
    per session by :func:`_split_sources`: the session's parquet artifacts
    (read lazily through the ``_RAW_*_PARQUET_TABLE`` readers, each a
    ``read_parquet`` relation over a Python file list, no JSON touched at
    query time) or its ``trajectory.json`` (parsed once into the TEMP TABLE
    ``v_raw_trajectories_json`` from a bound file list and unnested per
    query, as every version before the columnar artifacts did). A corpus
    with no columnar session registers exactly as before; a corpus with no
    JSON session never opens a ``trajectory.json``.

    TORN-SET GUARD: ``meta.json`` is written last by the corpus writer, so
    its presence marks a session dir as complete. Every other reader is
    restricted to meta-bearing session dirs (the JSON path list is built
    from ``v_raw_meta``; edges and loss use a semi-join), so a session dir
    missing its meta (crashed writer) contributes nothing to any view
    instead of a partial artifact set.

    SESSION ID BOUNDARY: a session dir whose name fails
    :mod:`atif_duck.domain.session_id` is removed from ``v_raw_meta`` by
    :func:`_gate_session_dirs` before any per-session path is built, so the
    meta gate excludes it everywhere. A corpus an older version wrote may
    hold such a dir; it is logged once and reported, never read.

    Parameters
    ----------
    con
        Open DuckDB connection.
    corpus_root
        Materialized corpus root (the directory containing ``sessions/``).

    Returns
    -------
    RawSources
        Which sessions took which path, every parquet the views open at
        query time (for the sandbox's file allowlist), and the rejected names.

    Raises
    ------
    duckdb.Error
        If any DDL fails (including an empty/absent corpus — ``read_json``
        errors on a glob with zero matches, which is the honest failure
        mode for "nothing materialized yet"). Logged via
        ``logger.exception`` before re-raise.
    """
    sessions_dir = corpus_root / "sessions"
    edges_glob = str(sessions_dir / "*" / "edges.jsonl")
    loss_glob = str(sessions_dir / "*" / "loss_report.json")
    meta_glob = str(sessions_dir / "*" / "meta.json")

    # meta.json is written LAST by atif-corpus — its presence marks a
    # session dir as COMPLETE. The edges and loss readers are restricted to
    # meta-bearing session dirs via this semi-join predicate, so a torn
    # session dir (crashed writer, partial cleanup) is invisible to every
    # view rather than surfacing a partial artifact set.
    # The one interpolation is the module constant _RAW_META_TABLE.
    meta_gate = f"session_id_path IN (SELECT session_id_path FROM {_RAW_META_TABLE})"  # noqa: S608  # nosec B608 - module constant

    try:
        # meta FIRST: every other reader derives from it. The glob is the
        # statement's one parameter.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_META_TABLE} AS
            SELECT *,
                   filename AS meta_path,
                   regexp_extract(filename, '/sessions/([^/]+)/meta\\.json$', 1)
                       AS session_id_path
            FROM read_json(
                ?,
                format='auto',
                filename=true,
                columns={{{_render_columns_clause(_META_COLUMNS)}}}
            );
            """,  # noqa: S608  # nosec B608 - glob is a bound parameter; table and columns are constants
            [meta_glob],
        )
        logger.debug("Registered {} from glob {}", _RAW_META_TABLE, meta_glob)
        rejected = _gate_session_dirs(con, sessions_dir)

        columnar_ids, json_ids = _split_sources(con, sessions_dir)
        logger.info(
            "register_raw: {} session(s) read from columnar artifacts, {} from trajectory.json",
            len(columnar_ids),
            len(json_ids),
        )

        trajectory_branches: list[SqlFragment] = []
        steps_branches: list[SqlFragment] = []
        calls_branches: list[SqlFragment] = []
        results_branches: list[SqlFragment] = []
        lazy_paths: list[Path] = []

        parquet_readers: tuple[tuple[str, str, bool], ...] = (
            (_RAW_SESSIONS_PARQUET_TABLE, SESSION_PARQUET, True),
            (_RAW_STEPS_PARQUET_TABLE, STEPS_PARQUET, False),
            (_RAW_TOOL_CALLS_PARQUET_TABLE, TOOL_CALLS_PARQUET, False),
            (_RAW_TOOL_RESULTS_PARQUET_TABLE, TOOL_RESULTS_PARQUET, False),
        )
        if columnar_ids:
            for reader, artifact, with_filename in parquet_readers:
                paths = [sessions_dir / sid / artifact for sid in columnar_ids]
                _bind_parquet_reader(con, reader, paths, with_filename=with_filename)
                lazy_paths.extend(paths)
            trajectory_branches.append(_columnar_trajectories_select())
            steps_branches.append(
                _typed_parquet_select(_RAW_STEPS_PARQUET_TABLE, VIEW_SCHEMA["steps"])
            )
            calls_branches.append(
                _typed_parquet_select(_RAW_TOOL_CALLS_PARQUET_TABLE, VIEW_SCHEMA["tool_calls"])
            )
            results_branches.append(
                _typed_parquet_select(_RAW_TOOL_RESULTS_PARQUET_TABLE, VIEW_SCHEMA["tool_results"])
            )
        else:
            # A connection re-registered after every columnar session lost
            # its artifacts must not keep the previous generation's readers.
            for reader, _, _ in parquet_readers:
                con.execute(f"DROP VIEW IF EXISTS {reader};")

        if json_ids:
            # One trajectory document per file -> format='auto' (NOT NDJSON).
            # The explicit projection keeps `steps` as a lazy JSON[] column.
            # An explicit path list rather than a glob: the glob would parse
            # the columnar sessions' trajectory.json too, which is the cost
            # this whole arrangement exists to avoid. The list is the
            # statement's one parameter.
            trajectory_paths = [sessions_dir / sid / "trajectory.json" for sid in json_ids]
            json_files = [str(path) for path in trajectory_paths]
            trajectory_bound = _object_size_bound(trajectory_paths)
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE {_RAW_TRAJECTORIES_JSON_TABLE} AS
                SELECT *,
                       filename AS trajectory_path,
                       regexp_extract(filename, '/sessions/([^/]+)/trajectory\\.json$', 1)
                           AS session_id_path
                FROM read_json(
                    ?,
                    format='auto',
                    filename=true,
                    columns={{{_render_columns_clause(_TRAJECTORY_COLUMNS)}}},
                    maximum_object_size={int(trajectory_bound)}
                );
                """,  # noqa: S608  # nosec B608 - file list is a bound parameter; table/columns are constants; the bound is an int
                [json_files],
            )
            logger.debug(
                "Registered {} over {} trajectory.json file(s)",
                _RAW_TRAJECTORIES_JSON_TABLE,
                len(json_ids),
            )
            trajectory_branches.append(_json_trajectories_select())
            steps_branches.append(_json_steps_select())
            calls_branches.append(_json_tool_calls_select())
            results_branches.append(_json_tool_results_select())
        else:
            # A connection re-registered after every session gained its
            # artifacts must not keep the previous generation's parsed JSON.
            con.execute(f"DROP TABLE IF EXISTS {_RAW_TRAJECTORIES_JSON_TABLE};")

        for table, branches, columns in (
            (_RAW_TRAJECTORIES_TABLE, trajectory_branches, _TRAJECTORY_RELATION_COLUMNS),
            (_RAW_STEPS_TABLE, steps_branches, VIEW_SCHEMA["steps"]),
            (_RAW_TOOL_CALLS_TABLE, calls_branches, VIEW_SCHEMA["tool_calls"]),
            (_RAW_TOOL_RESULTS_TABLE, results_branches, VIEW_SCHEMA["tool_results"]),
        ):
            # Table names are module constants; every branch is built above
            # from catalog constants over module-constant readers.
            con.execute(f"CREATE OR REPLACE VIEW {table} AS\n{_union_or_empty(branches, columns)};")  # nosec B608 - constants only
            logger.debug("Registered {} ({} source branch(es))", table, len(branches))

        # edges.jsonl is one line per RAW record -> newline_delimited.
        # The record's own `source_file` (the raw transcript path) is kept;
        # the edges.jsonl path itself is aliased to `edges_path`. The glob
        # reads every matching file (the meta gate filters rows afterwards),
        # so the bound is sized over every file the glob can reach.
        edges_bound = _object_size_bound(sessions_dir.glob("*/edges.jsonl"))
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_EDGES_TABLE} AS
            SELECT * FROM (
                SELECT *,
                       filename AS edges_path,
                       regexp_extract(filename, '/sessions/([^/]+)/edges\\.jsonl$', 1)
                           AS session_id_path
                FROM read_json(
                    ?,
                    format='newline_delimited',
                    filename=true,
                    columns={{{_render_columns_clause(_EDGE_COLUMNS)}}},
                    maximum_object_size={int(edges_bound)}
                )
            ) WHERE {meta_gate};
            """,  # noqa: S608  # nosec B608 - glob is a bound parameter; table/columns/gate are constants; the bound is an int
            [edges_glob],
        )
        logger.debug("Registered {} from glob {}", _RAW_EDGES_TABLE, edges_glob)

        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_LOSS_REPORTS_TABLE} AS
            SELECT * FROM (
                SELECT *,
                       filename AS report_path,
                       regexp_extract(filename, '/sessions/([^/]+)/loss_report\\.json$', 1)
                           AS session_id_path
                FROM read_json(
                    ?,
                    format='auto',
                    filename=true,
                    columns={{{_render_columns_clause(_LOSS_REPORT_COLUMNS)}}}
                )
            ) WHERE {meta_gate};
            """,  # noqa: S608  # nosec B608 - glob is a bound parameter; table/columns/gate are constants
            [loss_glob],
        )
        logger.debug("Registered {} from glob {}", _RAW_LOSS_REPORTS_TABLE, loss_glob)
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register raw readers over {}", corpus_root)
        raise
    return RawSources(
        columnar_session_ids=tuple(columnar_ids),
        json_session_ids=tuple(json_ids),
        lazy_read_paths=tuple(lazy_paths),
        rejected_session_ids=rejected,
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def register_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the business-level views on top of the raw readers.

    Must be called after :func:`register_raw`. Creates, in dependency order:
    ``steps``, ``sessions``, ``messages``, ``tool_calls``, ``tool_results``,
    ``todo_events``, ``todo_state_current``, ``subagent_spawns``,
    ``task_creations``, ``task_updates``, ``tasks_state_current``,
    ``skill_invocations``, ``skill_usage``, ``subagent_steps``,
    ``loss_reports``.

    Parameters
    ----------
    con
        Open DuckDB connection with raw readers already registered.

    Raises
    ------
    duckdb.Error
        If any view DDL fails. Logged via ``logger.exception`` before
        re-raise.
    """
    try:
        # One row per ATIF step, and the per-turn surface the contract names
        # ("messages-parity view name: steps"). The column expressions live
        # in :mod:`atif_duck.infrastructure.projections` (shared with the
        # columnar producer, so both sources agree byte for byte) and are
        # applied by register_raw when it builds ``v_raw_steps``; this view
        # pins the catalog's column order over that relation.
        con.execute(
            f"CREATE OR REPLACE VIEW steps AS SELECT {_catalog_columns('steps')} "  # noqa: S608  # nosec B608 - catalog columns over a module-constant table
            f"FROM {_RAW_STEPS_TABLE};"
        )
        logger.debug("Registered view: steps")

        # One row per materialized session. ``agent`` / ``agent_version`` come
        # from the trajectory's own ``agent`` struct, which harbor stamps per
        # adapter — ``claude-code`` with the Claude Code version, ``codex`` with
        # the codex-cli version. That is the ONE place the producing agent is
        # recorded inside an artifact, so every agent-aware query reads this
        # column rather than inferring the agent from a corpus path.
        #
        # cwd / git_branch come from the harbor adapter's ``agent.extra``; the
        # key differs per agent and both are read here because ``agent.extra``
        # is free-form: Claude Code writes SETS (``cwds`` / ``git_branches``,
        # first element representative) while Codex writes one ``cwd`` string
        # and a ``git`` struct. A Claude Code session spanning two cwds reports
        # one of them silently, so treat the column as indicative, not
        # exhaustive.
        con.execute(
            """
            CREATE OR REPLACE VIEW sessions AS
            SELECT
                t.session_id_path                                     AS session_id,
                t.agent.name                                          AS agent,
                t.agent.version                                       AS agent_version,
                coalesce(
                    json_extract_string(t.agent.extra, '$.cwds[0]'),
                    json_extract_string(t.agent.extra, '$.cwd')
                )                                                     AS cwd,
                coalesce(
                    json_extract_string(t.agent.extra, '$.git_branches[0]'),
                    json_extract_string(t.agent.extra, '$.git.branch')
                )                                                     AS git_branch,
                s.started_at,
                s.ended_at,
                s.agent_steps,
                s.step_count,
                t.agent.model_name                                    AS model_name,
                t.final_metrics.total_cost_usd                        AS total_cost_usd,
                t.trajectory_path
            FROM v_raw_trajectories t
            LEFT JOIN (
                SELECT
                    session_id,
                    min(ts)                                    AS started_at,
                    max(ts)                                    AS ended_at,
                    count(*) FILTER (WHERE source = 'agent')   AS agent_steps,
                    count(*)                                   AS step_count
                FROM steps
                GROUP BY session_id
            ) s ON s.session_id = t.session_id_path;
            """
        )
        logger.debug("Registered view: sessions")

        # COMPAT view: uuid-keyed raw-record identity reconstructed from
        # edges.jsonl. The trajectory cannot provide this (fidelity gap 7:
        # harbor never lands the event uuid in Step.extra), so parent-chain
        # walks and per-record counts run against the census-derived edges.
        # ``type`` carries the raw record role (user/assistant/system/...);
        # a separate role column would be redundant with it.
        con.execute(
            """
            CREATE OR REPLACE VIEW messages AS
            SELECT
                uuid,
                parent_uuid,
                session_id_path AS session_id,
                ts,
                type,
                is_sidechain,
                coalesce(is_compact_summary, false) AS is_compact_summary,
                message_id,
                source_file
            FROM v_raw_edges;
            """
        )
        logger.debug("Registered view: messages")

        # One row per ToolCall in any step. The columns are renamed off the
        # ATIF field names: function_name -> tool_name, tool_call_id ->
        # tool_use_id, arguments -> tool_input. ``tool_input`` is an already
        # parsed JSON value, NOT a JSON string, so a query must not
        # json_extract it twice. Expressions: projections.CALL_COLUMNS.
        con.execute(
            f"CREATE OR REPLACE VIEW tool_calls AS SELECT {_catalog_columns('tool_calls')} "  # noqa: S608  # nosec B608 - catalog columns over a module-constant table
            f"FROM {_RAW_TOOL_CALLS_TABLE};"
        )
        logger.debug("Registered view: tool_calls")

        # One row per ObservationResult. ``source_call_id`` is ATIF's join
        # key back to the tool_calls array, surfaced under the SAME name
        # ``tool_use_id`` that ``tool_calls`` exposes, so the two views join
        # with `USING (tool_use_id)` and neither side needs a rename.
        # Expressions: projections.RESULT_COLUMNS.
        con.execute(
            f"CREATE OR REPLACE VIEW tool_results AS SELECT {_catalog_columns('tool_results')} "  # noqa: S608  # nosec B608 - catalog columns over a module-constant table
            f"FROM {_RAW_TOOL_RESULTS_TABLE};"
        )
        logger.debug("Registered view: tool_results")

        # DuckDB's UNNEST requires a LIST, and the ``$.todos[*]`` wildcard
        # path yields JSON[] that UNNEST accepts natively — a bare
        # ``$.todos`` yields a scalar JSON value that it rejects.
        # ``step_id`` is the snapshot tie-break: steps are unique and ordered
        # per session, so no uuid column is needed to sequence the snapshots.
        con.execute(
            """
            CREATE OR REPLACE VIEW todo_events AS
            SELECT
                tc.session_id,
                tc.ts                                      AS written_at,
                tc.step_id,
                json_extract_string(todo, '$.content')     AS subject,
                json_extract_string(todo, '$.status')      AS status,
                json_extract_string(todo, '$.activeForm')  AS active_form,
                row_number() OVER (
                    PARTITION BY tc.session_id
                    ORDER BY tc.ts, tc.step_id
                ) AS snapshot_ix
            FROM tool_calls tc,
                 UNNEST(json_extract(tc.tool_input, '$.todos[*]')) AS t(todo)
            WHERE tc.tool_name = 'TodoWrite';
            """
        )
        logger.debug("Registered view: todo_events")

        # Latest snapshot wins per (session_id, subject) — TodoWrite always
        # rewrites the full list, so the newest row is the current state.
        con.execute(
            """
            CREATE OR REPLACE VIEW todo_state_current AS
            SELECT session_id, subject, status, active_form, written_at
            FROM (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY session_id, subject
                           ORDER BY snapshot_ix DESC
                       ) AS rn
                FROM todo_events
            )
            WHERE rn = 1;
            """
        )
        logger.debug("Registered view: todo_state_current")

        # Subagent launchers: ``Task`` (pre-v2.1.63) and ``Agent`` (v2.1.63+).
        # Input shape: {subagent_type, description, prompt, run_in_background?}.
        con.execute(
            """
            CREATE OR REPLACE VIEW subagent_spawns AS
            SELECT
                session_id,
                ts AS spawned_at,
                step_id,
                tool_use_id,
                tool_name AS spawn_tool,
                json_extract_string(tool_input, '$.subagent_type')      AS subagent_type,
                json_extract_string(tool_input, '$.description')        AS description,
                json_extract_string(tool_input, '$.prompt')             AS prompt,
                json_extract_string(tool_input, '$.run_in_background')  AS run_in_background
            FROM tool_calls
            WHERE tool_name IN ('Task', 'Agent');
            """
        )
        logger.debug("Registered view: subagent_spawns")

        # Persistent task creation: ``TaskCreate`` (Claude Code v2.1.16+) and
        # the SDK-py mirror ``mcp__tasks__task_create``. Distinct from
        # subagent_spawns — no subagent_type / prompt fields.
        con.execute(
            """
            CREATE OR REPLACE VIEW task_creations AS
            SELECT
                session_id,
                ts AS created_at,
                step_id,
                tool_use_id,
                tool_name                                              AS create_tool,
                json_extract_string(tool_input, '$.subject')           AS subject,
                json_extract_string(tool_input, '$.description')       AS description,
                json_extract_string(tool_input, '$.activeForm')        AS active_form,
                json_extract(tool_input, '$.metadata')                 AS metadata
            FROM tool_calls
            WHERE tool_name IN ('TaskCreate', 'mcp__tasks__task_create');
            """
        )
        logger.debug("Registered view: task_creations")

        # Task lifecycle updates. The native tool spells the key ``taskId``
        # (camel) and the mcp variant spells it ``id``, so both are extracted
        # and COALESCEd: reading only one silently drops half the updates.
        con.execute(
            """
            CREATE OR REPLACE VIEW task_updates AS
            SELECT
                session_id,
                ts AS updated_at,
                step_id,
                tool_use_id,
                tool_name AS update_tool,
                COALESCE(
                    json_extract_string(tool_input, '$.taskId'),
                    json_extract_string(tool_input, '$.id')
                )                                                       AS task_id,
                json_extract_string(tool_input, '$.status')             AS status,
                json_extract(tool_input, '$.addBlockedBy')              AS add_blocked_by,
                json_extract_string(tool_input, '$.owner')              AS owner
            FROM tool_calls
            WHERE tool_name IN ('TaskUpdate', 'mcp__tasks__task_update');
            """
        )
        logger.debug("Registered view: task_updates")

        # Latest status per (session_id, task_id). The runtime assigns the
        # task id and returns it only in the tool RESULT, so we recover it
        # from ``tool_results`` via the source_call_id join, parsing the
        # "Task #N" / {taskId} shapes the result text carries, and falling
        # back to per-session creation order when neither shape matches.
        #
        # NULLIF is required because DuckDB's regexp_extract returns '' (not
        # NULL) on no-match, and '' satisfies COALESCE — without it every later
        # fallback branch is unreachable and non-"Task #N" results all collapse
        # onto the same empty task_id, colliding in the latest_status join.
        #
        # The creation-order fallback orders by ``created_at`` and then by
        # ``step_id, tool_use_id``: several TaskCreate calls in one step share a
        # timestamp, and a window with a tied ORDER BY numbers them in physical
        # scan order, which differs between the JSON reader (a hash join
        # reorders the unnested calls) and the parquet reader. The tiebreaker
        # makes the number a function of the data, not of the source format.
        con.execute(
            """
            CREATE OR REPLACE VIEW tasks_state_current AS
            WITH creates AS (
                SELECT
                    tc.session_id,
                    tc.created_at,
                    tc.subject,
                    tc.active_form,
                    tc.tool_use_id,
                    COALESCE(
                        NULLIF(
                            regexp_extract(
                                CAST(tr.content AS VARCHAR), 'Task #(\\d+)', 1
                            ),
                            ''
                        ),
                        json_extract_string(tr.content, '$.taskId'),
                        CAST(row_number() OVER (
                            PARTITION BY tc.session_id
                            ORDER BY tc.created_at, tc.step_id, tc.tool_use_id
                        ) AS VARCHAR)
                    ) AS task_id
                FROM task_creations tc
                LEFT JOIN tool_results tr USING (tool_use_id)
            ),
            latest_status AS (
                SELECT session_id, task_id, status, updated_at,
                       row_number() OVER (
                           PARTITION BY session_id, task_id
                           ORDER BY updated_at DESC
                       ) AS rn
                FROM task_updates
                WHERE task_id IS NOT NULL
            )
            SELECT
                c.session_id,
                c.task_id,
                c.subject,
                c.active_form,
                COALESCE(ls.status, 'pending') AS status,
                c.created_at,
                ls.updated_at AS last_updated_at
            FROM creates c
            LEFT JOIN latest_status ls
              ON ls.session_id = c.session_id
             AND ls.task_id = c.task_id
             AND ls.rn = 1;
            """
        )
        logger.debug("Registered view: tasks_state_current")

        # Every Skill / slash-command invocation, unioned across both shapes:
        # * ``tool`` — the assistant invokes the built-in ``Skill`` tool with
        #   ``arguments.skill = '<name>'``. Lives in ``tool_calls`` already.
        # * ``slash_command`` — the user types ``/<name>``, which Claude Code
        #   serializes into the message text as
        #   ``<command-name>/<name></command-name>`` (sometimes with
        #   ``<command-args>``). ATIF flattens user content to one text
        #   message per step, so ONE regex over ``steps.message`` covers both
        #   raw serializations (list-typed content blocks and bare VARCHAR
        #   content) without branching on the content type.
        # ``skill_id`` is the raw identifier, NOT a normalized name:
        # ``erpaval`` and ``personal-plugins:erpaval`` are distinct rows, so a
        # per-skill aggregate has to decide for itself whether to fold them.
        cmd_name_re = SqlFragment("<command-name>/([A-Za-z0-9_:.-]+)</command-name>")
        args_re = SqlFragment("<command-args>([^<]*)</command-args>")
        con.execute(
            f"""
            CREATE OR REPLACE VIEW skill_invocations AS
            SELECT
                tc.session_id,
                tc.ts,
                tc.step_id,
                'tool'                                         AS source,
                json_extract_string(tc.tool_input, '$.skill')  AS skill_id,
                json_extract_string(tc.tool_input, '$.args')   AS args,
                tc.tool_use_id
            FROM tool_calls tc
            WHERE tc.tool_name = 'Skill'
              AND json_extract_string(tc.tool_input, '$.skill') IS NOT NULL
            UNION ALL
            SELECT
                s.session_id,
                s.ts,
                s.step_id,
                'slash_command'                                     AS source,
                regexp_extract(s.message, '{cmd_name_re}', 1)       AS skill_id,
                NULLIF(regexp_extract(s.message, '{args_re}', 1), '') AS args,
                NULL                                                AS tool_use_id
            FROM steps s
            WHERE s.source = 'user'
              AND s.message LIKE '%<command-name>/%'
              AND regexp_extract(s.message, '{cmd_name_re}', 1) != '';
            """  # noqa: S608  # nosec B608 - the two interpolated regexes are local literals defined above
        )
        logger.debug("Registered view: skill_invocations")

        # Labels derive from the skill_id string alone — there is no skills
        # catalog in the corpus to join against. The heuristic: a skill_id
        # WITHOUT a ':' has no plugin namespace and is labelled builtin
        # (`erpaval`, `review`, ...); a `plugin:skill` id splits into plugin +
        # skill_name and is not builtin. ``is_builtin`` therefore OVER-marks a
        # user-local non-plugin skill as builtin. That is tolerable only
        # because the rank/mix macros use the flag to damp slash-only noise;
        # a query that treats it as ground truth about provenance is wrong.
        con.execute(
            """
            CREATE OR REPLACE VIEW skill_usage AS
            SELECT
                si.session_id,
                si.ts,
                si.step_id,
                si.source,
                si.skill_id,
                si.args,
                si.tool_use_id,
                CASE WHEN strpos(si.skill_id, ':') > 0
                     THEN substr(si.skill_id, strpos(si.skill_id, ':') + 1)
                     ELSE si.skill_id
                END                                            AS skill_name,
                CASE WHEN strpos(si.skill_id, ':') > 0
                     THEN split_part(si.skill_id, ':', 1)
                     ELSE NULL
                END                                            AS plugin,
                strpos(si.skill_id, ':') = 0                   AS is_builtin
            FROM skill_invocations si;
            """
        )
        logger.debug("Registered view: skill_usage")

        # Harbor INLINES subagent transcripts into the flat step list
        # (fidelity gap 4), marking them only via ``extra.is_sidechain`` — so
        # the subagent surface is a filter over ``steps`` and there is no
        # separate subagent raw reader to read. A consequence worth knowing:
        # a session-level aggregate over ``steps`` already includes subagent
        # work unless it filters ``is_sidechain`` out.
        con.execute(
            """
            CREATE OR REPLACE VIEW subagent_steps AS
            SELECT *
            FROM steps
            WHERE is_sidechain;
            """
        )
        logger.debug("Registered view: subagent_steps")

        # Per-session conversion loss accounting from atif-converter's
        # LossReport: record counts by raw type plus the fidelity gaps this
        # session actually exhibits. Any count taken from the other views is
        # only as complete as this view says the conversion was, so a
        # discrepancy between a raw record count and a step count is explained
        # here rather than being a defect in the view that reports it.
        con.execute(
            """
            CREATE OR REPLACE VIEW loss_reports AS
            SELECT
                session_id_path AS session_id,
                records_total,
                records_converted,
                records_dropped,
                gaps_observed,
                record_counts,
                subagent_files_found,
                subagent_files_convertible,
                workflow_subagent_files_found,
                report_path
            FROM v_raw_loss_reports;
            """
        )
        logger.debug("Registered view: loss_reports")
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register derived views")
        raise


# ---------------------------------------------------------------------------
# VSS
# ---------------------------------------------------------------------------


def _lance_table_present(con: duckdb.DuckDBPyConnection) -> bool:
    """True iff the ATTACHed ``lance_store`` catalog exposes ``embeddings``.

    Probed via ``duckdb_tables()`` rather than a speculative SELECT: a Lance
    directory that exists with metadata but no embeddings table (legitimate
    intermediate state after a ``lancedb.connect`` that never created a
    table) ATTACHes cleanly but then blows up at view-bind time with a
    catalog error. The right gate is "is the table actually there?".
    """
    row = con.execute(
        """
        SELECT count(*)
        FROM duckdb_tables()
        WHERE database_name = 'lance_store' AND table_name = 'embeddings'
        """
    ).fetchone()
    return row is not None and int(row[0]) > 0


def lance_extension_installed(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the lance extension is present in this connection's extension directory.

    Read from ``duckdb_extensions()``, which lists the local directory and
    never reaches the network, so the answer costs a few milliseconds and no
    download.
    """
    row = con.execute(
        "SELECT installed FROM duckdb_extensions() WHERE extension_name = ?", [LANCE_EXTENSION]
    ).fetchone()
    return row is not None and bool(row[0])


def load_lance_extension(con: duckdb.DuckDBPyConnection) -> bool:
    """``LOAD`` the lance extension when it is installed; never install it.

    Returns ``False`` when the extension is absent, and executes no
    ``INSTALL`` in that case whatever ``autoinstall_known_extensions`` says,
    because the installed check runs first. Registration at query time must
    never reach the network: the extension is 242 MB, fetched by name from
    the default repository, and ``query`` runs unattended.
    """
    if not lance_extension_installed(con):
        return False
    con.execute(f"LOAD {LANCE_EXTENSION};")
    return True


def install_lance_extension(con: duckdb.DuckDBPyConnection) -> str:
    """``INSTALL`` then ``LOAD`` the lance extension; return its install path.

    The one place atif-duck reaches the network, so it belongs to explicit
    commands only (``atif-sql embed --install-extension``, or a real embed
    run, which reaches Bedrock anyway). A query-time registration calls
    :func:`load_lance_extension` instead.
    """
    con.execute(f"INSTALL {LANCE_EXTENSION};")
    con.execute(f"LOAD {LANCE_EXTENSION};")
    row = con.execute(
        "SELECT install_path FROM duckdb_extensions() WHERE extension_name = ?", [LANCE_EXTENSION]
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else ""


def register_vss(
    con: duckdb.DuckDBPyConnection,
    *,
    lance_uri: Path,
    expected_model: str | None = None,
    expected_dim: int | None = None,
    dim: int = 1024,
) -> bool:
    """Bind ``message_embeddings`` over a LanceDB local dataset.

    LanceDB stores embeddings + its IVF_HNSW_SQ index in one place (written
    by atif-embed's backfill); reads come back through DuckDB via the lance
    core extension (``LOAD lance; ATTACH (TYPE LANCE)``). The extension is
    only LOADed, never INSTALLed, here: :func:`load_lance_extension` checks
    the local extension directory first and a missing extension degrades to
    the empty fallback table with a warning, so registration never reaches
    the network. Installing is an explicit act
    (:func:`install_lance_extension`, behind ``atif-sql embed``). When no
    store directory exists the extension is not loaded at all, which is the
    state of every corpus that has not run ``embed``.
    The store probe runs through DuckDB itself. atif-duck declares no lancedb
    dependency: lancedb belongs to atif-embed, which writes the store, and the
    independence contract forbids an import edge between the two packages — so
    the lance extension is the only thing atif-duck may read the store with.

    Parameters
    ----------
    con
        Open DuckDB connection.
    lance_uri
        Local LanceDB dataset directory.
    expected_model
        When supplied (by the composition root), the active embedder's
        ``model_id``. Read against the store's stamped ``model`` column via
        the fail-loud provider guard: a mismatch raises
        :class:`atif_duck.domain.embedding_guard.EmbeddingProviderMismatch`
        rather than letting a cross-provider query silently return garbage
        cosine scores (guard-before-bind).
    expected_dim
        The active embedder's dimension, checked alongside
        ``expected_model``. ``None`` trusts ``model_id`` alone.
    dim
        Fixed-length embedding dimension for the empty-fallback table. When a
        populated Lance store is present its own stamped ``dim`` drives the
        ``CAST(embedding AS FLOAT[dim])`` view instead, so a store written by
        a different-width provider binds correctly regardless of this
        argument.

    Returns
    -------
    bool
        ``True`` when the Lance table is reachable through the
        ``message_embeddings`` view; ``False`` when no embeddings exist yet,
        or the store exists but the lance extension is not installed (the
        name is created as an empty TABLE with the right schema so a
        downstream ``CREATE MACRO semantic_search`` can still bind).
    """
    dim_i = int(dim)

    import duckdb as _duckdb

    attached = False
    if lance_uri.is_dir() and not load_lance_extension(con):
        logger.warning(
            "Lance store at {} cannot be read: the {} extension is not installed, so "
            "message_embeddings binds empty. Run `atif-sql embed --install-extension` "
            "(a one-time download) to enable vector search.",
            lance_uri,
            LANCE_EXTENSION,
        )
    elif lance_uri.is_dir():
        try:
            # ATTACH is one of the statement kinds DuckDB will not prepare, so
            # this is the one corpus path that still enters statement text; it
            # does so only through sql_literal, and the boundary test registers
            # a store under a quoted path to keep it that way.
            con.execute(
                f"ATTACH IF NOT EXISTS {sql_literal(str(lance_uri))} AS lance_store (TYPE LANCE);"
            )
            attached = True
        except _duckdb.Error:
            # register-or-fail-loud applies to DDL over a healthy store; a
            # directory the lance extension cannot ATTACH is equivalent to
            # "no store yet" and falls through to the empty-fallback table.
            logger.exception("Lance ATTACH failed for {}; treating as empty store", lance_uri)

    if not attached or not _lance_table_present(con):
        logger.warning(
            "No Lance embeddings table at {}; creating empty message_embeddings "
            "table so semantic_search binds. Run `atif-sql embed --all --no-dry-run` "
            "to backfill.",
            lance_uri,
        )
        con.execute(
            f"""
            CREATE OR REPLACE TABLE message_embeddings (
                uuid        VARCHAR PRIMARY KEY,
                model       VARCHAR,
                dim         INTEGER,
                embedding   FLOAT[{dim_i}],
                embedded_at TIMESTAMPTZ
            );
            """  # nosec B608 - the only interpolation is the width, through int() coercion
        )
        return False

    # Fail-loud provider/dimension guard: the store stamps ``(model, dim)``
    # on every row; read it back and refuse to bind a view over vectors
    # written by a different provider (even at matching width, cross-model
    # vectors live in incompatible spaces and produce numerically valid but
    # garbage cosine scores). The stored width also drives the CAST below.
    row = con.execute("SELECT model, dim FROM lance_store.main.embeddings LIMIT 1;").fetchone()
    if row is not None:
        stored_model, stored_dim = str(row[0]), int(row[1])
        if expected_model is not None:
            ensure_store_matches(
                stored_model=stored_model,
                stored_dim=stored_dim,
                expected_model=expected_model,
                expected_dim=expected_dim,
            )
        dim_i = stored_dim

    # Project the embeddings table as a top-level view named
    # ``message_embeddings``, casting the embedding column to FLOAT[dim] so
    # the fixed-size ARRAY type is what downstream vector functions see.
    con.execute(
        f"""
        CREATE OR REPLACE VIEW message_embeddings AS
        SELECT
            uuid,
            model,
            dim,
            CAST(embedding AS FLOAT[{dim_i}]) AS embedding,
            embedded_at
        FROM lance_store.main.embeddings;
        """  # noqa: S608  # nosec B608 - the only interpolation is the width, through int() coercion
    )
    count_row = con.execute("SELECT count(*) FROM message_embeddings;").fetchone()
    count = int(count_row[0]) if count_row else 0
    logger.debug("Bound message_embeddings over Lance ({} rows, dim={})", count, dim_i)
    return True


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------


def _pricing_values_clause(pricing: dict[str, tuple[float, float]]) -> SqlFragment:
    """Render a pricing dict as an inline SQL ``VALUES`` row list.

    Parameters
    ----------
    pricing
        Mapping of ``model_name -> (input_rate, output_rate)`` per 1M tokens.

    Returns
    -------
    str
        Comma-separated ``('model', in, out)`` rows. Emits a sentinel row
        that matches no real model if ``pricing`` is empty (DuckDB rejects
        empty ``VALUES`` lists).
    """
    if not pricing:
        return SqlFragment(f"({sql_literal('__no_pricing__')}, 0.0, 0.0)")
    # The model NAME is escaped and the two rates are coerced with `float()`
    # rather than interpolated as they arrive. Both are the declared type of the
    # `pricing` parameter, and atif-cli never passes one — but this is a library
    # meant to be embedded in-process, so a caller that reaches DuckDB with a
    # `str` rate past the type checker gets a ValueError here instead of an
    # injected VALUES row.
    rows = [
        f"({sql_literal(model)}, {float(in_rate)}, {float(out_rate)})"
        for model, (in_rate, out_rate) in sorted(pricing.items())
    ]
    return SqlFragment(", ".join(rows))


def register_macros(
    con: duckdb.DuckDBPyConnection,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    skip_vss: bool = False,
) -> None:
    """Create the SQL macros declared in the static catalog.

    Every signature here must match its ``MACRO_SIGNATURES`` entry in
    :mod:`atif_duck.domain.catalog`: the catalog is what ``atif-sql schema``
    prints and what the derived examples are built from, and the drift test
    fails on any divergence.

    Must be called after :func:`register_views` — DuckDB binds macro bodies
    at CREATE time, so every referenced view must already exist. The
    ``semantic_search`` macro additionally requires ``message_embeddings``
    (view or empty fallback table from :func:`register_vss`) to be bound
    first; ``skip_vss=True`` skips it for connections that never call
    :func:`register_vss`.

    Parameters
    ----------
    con
        Open DuckDB connection with views already registered.
    pricing
        Optional pricing override; falls back to
        :data:`atif_duck.domain.catalog.DEFAULT_PRICING`.
    skip_vss
        When ``True``, skip the ``semantic_search`` macro registration.

    Raises
    ------
    duckdb.Error
        If any macro DDL fails. Logged via ``logger.exception`` before
        re-raise.
    """
    pricing_rows = _pricing_values_clause(pricing if pricing is not None else DEFAULT_PRICING)

    try:
        # ``ago('14 days')`` -> ``current_timestamp - INTERVAL 14 DAY``.
        # The CAST handles every interval-unit shape DuckDB recognizes.
        con.execute(
            """
            CREATE OR REPLACE MACRO ago(interval_text) AS (
                current_timestamp - CAST(interval_text AS INTERVAL)
            );
            """
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO model_used(sid) AS (
                SELECT any_value(model_name)
                FROM steps
                WHERE session_id = sid AND model_name IS NOT NULL
            );
            """
        )

        # COORDINATE SPACE: ATIF's ``prompt_tokens`` is the TOTAL input
        # (non-cached + cache_read + cache_creation), so it is NOT the
        # billable uncached base. The charged base here is
        # (prompt_tokens - cached_tokens) = non-cached + cache_creation.
        # Billing cache_creation at the input rate and leaving cache reads
        # uncharged is an approximation: it over-charges a cache write and
        # under-charges a cache read relative to a provider's real rate card.
        # The prefix match strips dated model suffixes
        # (``claude-haiku-4-5-20251001`` -> ``claude-haiku-4-5``).
        #
        # LEFT JOIN plus ``unpriced_steps`` is the honest-accounting shape: an
        # inner join silently drops steps whose model has no pricing row, so a
        # session mixing priced and unpriced models would return a PARTIAL
        # number indistinguishable from a complete one. ``est_cost_usd``
        # covers the priced steps only and is meaningful ONLY when
        # ``unpriced_steps = 0``.
        #
        # Both counters filter on ``model_name IS NOT NULL``: USER steps carry
        # no model and cost nothing, so counting them as unpriced would put
        # every session with a user turn above zero and make a real pricing
        # gap indistinguishable from an ordinary conversation.
        con.execute(
            f"""
            CREATE OR REPLACE MACRO cost_estimate(sid) AS TABLE (
                SELECT sum(
                           (coalesce(s.prompt_tokens, 0) - coalesce(s.cached_tokens, 0))
                               * p.in_rate
                           + coalesce(s.completion_tokens, 0) * p.out_rate
                       ) / 1e6                                   AS est_cost_usd,
                       count(*) FILTER (
                           WHERE s.model_name IS NOT NULL AND p.model IS NOT NULL
                       )                                          AS priced_steps,
                       count(*) FILTER (
                           WHERE s.model_name IS NOT NULL AND p.model IS NULL
                       )                                          AS unpriced_steps
                FROM steps s
                LEFT JOIN (VALUES {pricing_rows}) p(model, in_rate, out_rate)
                  ON regexp_replace(s.model_name, '-\\d{{8}}$', '') = p.model
                WHERE s.session_id = sid
            );
            """  # noqa: S608  # nosec B608 - pricing model names escaped by sql_literal; the rates are floats
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO tool_rank(last_n_days) AS TABLE (
                SELECT tool_name, count(*) AS n
                FROM tool_calls
                WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                  AND tool_name IS NOT NULL
                GROUP BY 1
                ORDER BY n DESC
            );
            """
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO todo_velocity(sid) AS (
                SELECT count(*) FILTER (WHERE status = 'completed')::DOUBLE
                     / NULLIF(count(DISTINCT subject), 0)
                FROM todo_state_current
                WHERE session_id = sid
            );
            """
        )

        # SCOPE: fan-out counts spawn INTENT — the number of Task/Agent
        # launches the session issued — not the number of subagent transcripts
        # that resulted. ATIF inlines sidechains, so there is no per-subagent
        # file to census, and a launch that produced no steps still counts
        # here. Read this as an upper bound on subagents realized.
        con.execute(
            """
            CREATE OR REPLACE MACRO subagent_fanout(sid) AS (
                SELECT count(*)
                FROM subagent_spawns
                WHERE session_id = sid
            );
            """
        )

        # Semantic top-k nearest neighbors over ``message_embeddings``.
        # ``query_vec`` must have the bound view's width.
        #
        # COSINE, not L2, throughout: stored document vectors are int8-cast
        # and un-normalized (observed norms 1206-1419 on the live store) while
        # ``embed_query`` returns unit-norm probes, so L2 ranks partly by
        # MAGNITUDE and puts lower-similarity rows above higher ones. ``sim``
        # and ``distance`` are the two halves of the SAME metric
        # (distance = 1 - sim); mixing cosine ``sim`` with L2 ``distance``
        # would make the two columns disagree about which hit is nearest.
        #
        # ``list_cosine_*`` rather than ``array_cosine_*``: the list variants
        # accept a fixed-size ARRAY and a variable-length LIST on either side,
        # so a caller-computed probe binds without knowing the store's width
        # at DDL time. Neither variant pushes down into the Lance index (both
        # plan a full __LANCE_TABLE_SCAN), so this costs no index lookup.
        if not skip_vss:
            con.execute(
                """
                CREATE OR REPLACE MACRO semantic_search(query_vec, k) AS TABLE (
                    SELECT me.uuid,
                           list_cosine_similarity(me.embedding, query_vec) AS sim,
                           list_cosine_distance(me.embedding, query_vec)   AS distance
                    FROM message_embeddings me
                    ORDER BY list_cosine_distance(me.embedding, query_vec)
                    LIMIT k
                );
                """
            )
        else:
            logger.debug("Skipped semantic_search macro (skip_vss=True)")

        # Skill / slash-command leaderboard over the last N days.
        con.execute(
            """
            CREATE OR REPLACE MACRO skill_rank(last_n_days) AS TABLE (
                SELECT skill_id,
                       skill_name,
                       plugin,
                       is_builtin,
                       count(*)                   AS n,
                       count(DISTINCT session_id) AS sessions
                  FROM skill_usage
                 WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                 GROUP BY 1, 2, 3, 4
                 ORDER BY n DESC
            );
            """
        )

        # How is each skill invoked? Built-ins excluded — they're almost
        # always slash-only and would drown everything else out.
        con.execute(
            """
            CREATE OR REPLACE MACRO skill_source_mix(last_n_days) AS TABLE (
                SELECT skill_id,
                       skill_name,
                       count(*) FILTER (WHERE source = 'tool')          AS n_tool,
                       count(*) FILTER (WHERE source = 'slash_command') AS n_slash,
                       count(*)                                         AS n_total
                  FROM skill_usage
                 WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                   AND NOT is_builtin
                 GROUP BY 1, 2
                 ORDER BY n_total DESC
            );
            """
        )

        _semantic_search_part = "" if skip_vss else "semantic_search, "
        logger.debug(
            "Registered macros: ago, model_used, cost_estimate, tool_rank, "
            f"todo_velocity, subagent_fanout, {_semantic_search_part}skill_rank, "
            "skill_source_mix"
        )
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register macros")
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register(
    con: duckdb.DuckDBPyConnection,
    corpus_root: Path,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    skip_vss: bool = False,
    lance_uri: Path | None = None,
    expected_model: str | None = None,
    expected_dim: int | None = None,
) -> RawSources:
    """Register raw readers, views, VSS, and macros over ``corpus_root``, in order.

    Every call re-scans the corpus, and re-parses every session that has no
    columnar artifacts into TEMP tables, so cost is O(JSON sessions) per
    connection: reuse one connection per process rather than registering per
    query. Returns :func:`register_raw`'s :class:`RawSources` so the caller
    knows which path each session took and which parquet files the views
    will open lazily.

    Order matters: raw TEMP tables first (views bind against them at CREATE
    time), then views, then VSS (``semantic_search``'s body binds against
    ``message_embeddings`` at CREATE time), then macros (macro bodies bind
    against views at CREATE time), then the v2 analytics views + macros
    (which bind against both the analytics parquets AND the base views —
    ``autonomy_trend`` joins ``sessions``, ``sentiment_arc`` joins
    ``messages``).

    Parameters
    ----------
    con
        Open DuckDB connection.
    corpus_root
        Materialized corpus root (the directory containing ``sessions/``).
    pricing
        Optional pricing override for :func:`register_macros`.
    skip_vss
        When ``True``, neither :func:`register_vss` nor the
        ``semantic_search`` macro registers. The embed backfill needs this:
        it WRITES the store that ``message_embeddings`` reads, so a
        connection opened to run the backfill cannot bind that view first.
        Default ``False`` — an empty or absent store degrades to the empty
        fallback table, so registration is otherwise always safe.
    lance_uri
        Local LanceDB dataset directory; defaults to
        ``<corpus_root>/embeddings_lance`` (atif-embed's per-corpus default).
    expected_model, expected_dim
        Active embedder identity for :func:`register_vss`'s fail-loud guard.

    Raises
    ------
    duckdb.Error
        If any registration step fails (register-or-fail-loud).
    """
    from atif_duck.infrastructure.analytics import (
        register_analytics,
        register_analytics_macros,
    )

    sources = register_raw(con, corpus_root)
    register_views(con)
    if not skip_vss:
        register_vss(
            con,
            lance_uri=lance_uri if lance_uri is not None else corpus_root / "embeddings_lance",
            expected_model=expected_model,
            expected_dim=expected_dim,
        )
    register_macros(con, pricing=pricing, skip_vss=skip_vss)
    registered = register_analytics(con, corpus_root)
    register_analytics_macros(con, registered)
    return sources
