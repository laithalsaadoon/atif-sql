# SPDX-License-Identifier: Apache-2.0

"""Local Lance embeddings store, written through DuckDB's lance extension.

The store is one Lance dataset, ``<lance_uri>/embeddings.lance``. This module
reads and writes it with DuckDB's ``lance`` core extension: the store
directory is ATTACHed as a Lance namespace (``ATTACH ... (TYPE LANCE)``) and
every operation is a SQL statement.

* an append is an ``INSERT`` from the chunk's Arrow table
* a replace deletes the stale rows first, with ``MERGE ... WHEN MATCHED THEN
  DELETE`` against an Arrow table of uuids, in one Lance transaction
* compaction is ``OPTIMIZE``, followed by ``VACUUM LANCE``, which removes the
  versions older than :data:`VERSION_RETENTION_SECONDS` so they stop piling up
* reads are plain scans

atif-duck reads the same dataset through the same extension (``register_vss``).
Up to atif-sql 0.5.0 the writer was the ``lancedb`` package instead. The
on-disk format is the same Lance format, so a store either one wrote opens in
the other with no re-embedding, and a new store is created at the data storage
version lancedb 0.39 and 0.40 write (:data:`DATA_STORAGE_VERSION`), so going
back to 0.5.0 stays safe.

Schema evolution policy (v2, 2026-08-27)
----------------------------------------
ADDITIVE columns migrate ONLINE, never via rebuild: opening a store that
lacks a newer column evolves the table in place (``ALTER TABLE ... ADD
COLUMN ... DEFAULT``, which back-fills every existing row without touching
the vectors) and backfills the new column with a sentinel or default. Rows
carrying the :data:`_PRE_STAMP_SENTINEL` stamp then mismatch their corpus hash
in the discovery anti-join and re-embed incrementally through the ordinary
staleness path (delete-before-append), so the store heals itself across
successive runs while search stays online. A BREAKING change (a provider or
dimension switch) stays fail-loud via ``embedding_guard``: those vectors live
in an incompatible space and must be rebuilt deliberately.

The store records its schema version in a ``schema_version.json`` sidecar
next to the table (written on create, checked on open). Bump
:data:`SCHEMA_VERSION` with the next evolution and key the migration on the
stored number rather than probing by column name.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pyarrow as pa
from loguru import logger

from atif_embed.domain.errors import EmbeddingStoreUnreadable
from atif_embed.domain.sql_literal import sql_literal
from atif_embed.infrastructure.lance_extension import load_lance_extension

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

    import duckdb
    import polars as pl

#: Lance table name inside the namespace (the dataset is ``<lance_uri>/embeddings.lance``).
TABLE_NAME = "embeddings"

#: The Lance dataset's directory inside the store directory.
DATASET_DIR = f"{TABLE_NAME}.lance"

#: Catalog name the store directory is ATTACHed under. Distinct from
#: atif-duck's ``lance_store`` so the two never meet in one connection.
STORE_ALIAS = "atif_embed_store"

#: Fully qualified name of the embeddings table in an open connection.
_TABLE = f"{STORE_ALIAS}.main.{TABLE_NAME}"

#: Current store schema version. v1 was the pre-stamp 5-column shape; v2
#: added ``text_hash`` + ``truncated``. Bump with the next ADDITIVE evolution
#: and key its migration on the stored number (``read_schema_version``), not
#: on probing for a column by name.
SCHEMA_VERSION = 2

#: Sidecar filename recording :data:`SCHEMA_VERSION`, written next to the
#: Lance table directory contents (inside ``lance_uri``). A sidecar rather
#: than a table column: the version describes the SCHEMA, and reading it must
#: not require the schema to be readable.
SCHEMA_VERSION_FILE = "schema_version.json"

#: Lance file format version of a NEW store. lancedb 0.39.0 (Lance 12.0.0,
#: what atif-sql 0.5.0 locked) and 0.40.0 (Lance 13.0.0) both create tables at
#: 2.2, so a store this module creates is one 0.5.0 could have created itself.
#: Pinned rather than left to the extension's default, so a later extension
#: release with a newer default can't create stores an older atif-sql can't
#: open. An existing store keeps whatever version it was created at: appends
#: never change it.
DATA_STORAGE_VERSION = "2.2"

#: DuckDB memory cap for the store's connection. A bare ``duckdb.connect()``
#: takes 80% of the host's RAM; on the 124 GB fleet host that's about 99 GB,
#: and its default spill directory is ``.tmp`` under the working directory. The
#: store's own work is small (one chunk of a few hundred vectors per INSERT, a
#: two-column scan of every uuid and hash), and Lance's compaction and cleanup
#: run in the extension's own memory, outside this cap.
WRITER_MEMORY_BYTES: int = 1024**3

#: DuckDB threads for the store's connection.
WRITER_THREADS: int = 2

#: ``VACUUM LANCE`` removes the versions created longer ago than this, except
#: the current one. It ages a version by when it was created, not by when a
#: newer one replaced it: a head replaced a moment earlier, created 4 s before,
#: went under a 2-second window (checked 2026-11-03). So the window promises a
#: reader nothing on its own. After an idle hour, the first tick's cleanup can
#: delete the files of the version a running ``search`` opened a moment ago.
#: Lance appears to keep the files it has already opened readable: the
#: 2026-11-03 review ran 16 scans overlapping cleanups with no error. The gap
#: left is a reader that has read the manifest but not yet opened a fragment it
#: names when the cleanup runs. That read fails, and running it again reads the
#: current version. The hour matches the lake's cleanup. lancedb's
#: ``optimize()`` kept a week of versions, which is how one fleet store reached
#: 1,568 versions and 2.9 GB of old data.
VERSION_RETENTION_SECONDS: int = 3600

#: ``text_hash`` stamped on rows that predate the stamp. Angle brackets make
#: it impossible to equal a real blake2b hex digest, so every pre-stamp row
#: mismatches its current corpus hash in the discovery anti-join and is
#: re-embedded incrementally through the ordinary staleness path.
_PRE_STAMP_SENTINEL = "<pre-stamp>"

#: The table's columns as DuckDB DDL, in :func:`lance_schema` order. ``{dim}``
#: is the only placeholder, filled through ``int()``.
_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("uuid", "VARCHAR"),
    ("model", "VARCHAR"),
    ("dim", "INTEGER"),
    ("embedding", "FLOAT[{dim}]"),
    ("embedded_at", "TIMESTAMPTZ"),
    ("text_hash", "VARCHAR"),
    ("truncated", "BOOLEAN"),
)

#: Column names, in table order.
_COLUMNS: tuple[str, ...] = tuple(name for name, _ in _COLUMN_DDL)

#: Columns a v1 (pre-stamp) store lacks, with the DDL type and the literal
#: default each one is back-filled with.
_PRE_STAMP_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("text_hash", "VARCHAR", sql_literal(_PRE_STAMP_SENTINEL)),
    ("truncated", "BOOLEAN", "false"),
)

#: Names the Arrow tables of one write are registered under, for one statement.
_CHUNK_VIEW = "atif_embed_chunk"
_DELETE_VIEW = "atif_embed_delete"


# pyarrow ships no py.typed and no bundled stubs, so `pa.Schema` is itself an
# unknown type and any signature naming it reports as partially unknown. The
# annotation is still the most precise one available and is what a reader needs.
def lance_schema(dim: int) -> pa.Schema:  # pyright: ignore[reportUnknownParameterType]
    """Arrow schema of the embeddings table, as Lance stores it.

    The ``embedding`` column is a FIXED-SIZE list of float32 (``FLOAT[dim]``
    in DuckDB). Lance treats only that shape as a vector column; a
    variable-size list won't index.

    ``text_hash`` is the content stamp the backfill's staleness check reads:
    a re-converted session can change a step's flattened text under the same
    uuid, and without the stamp the uuid-only anti-join would treat that row
    as already embedded forever. ``truncated`` marks rows whose source text
    was clipped before embedding, so a search miss past the clip point is
    attributable instead of invisible.

    The table is created from :data:`_COLUMN_DDL`, and every column is then
    marked NOT NULL, which yields exactly this schema; a test reads a created
    store back against it.
    """
    return pa.schema(
        [
            pa.field("uuid", pa.string(), nullable=False),
            pa.field("model", pa.string(), nullable=False),
            pa.field("dim", pa.int32(), nullable=False),
            pa.field("embedding", pa.list_(pa.float32(), dim), nullable=False),
            pa.field("embedded_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("text_hash", pa.string(), nullable=False),
            pa.field("truncated", pa.bool_(), nullable=False),
        ]
    )


class StoreConnection:
    """One DuckDB connection with the lance extension loaded and one store ATTACHed.

    Opened by :func:`connect_db`. :meth:`close` (or leaving a ``with`` block)
    releases the connection and removes its private spill directory.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, uri: Path, spill: Path) -> None:
        self._con = con
        self._uri = uri
        self._spill = spill
        self._closed = False

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """The DuckDB connection; the store's table is :data:`STORE_ALIAS` ``.main.embeddings``."""
        return self._con

    @property
    def uri(self) -> Path:
        """The store directory this connection has ATTACHed."""
        return self._uri

    def close(self) -> None:
        """Close the connection and remove its spill directory. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            self._con.close()
        finally:
            shutil.rmtree(self._spill, ignore_errors=True)

    def __enter__(self) -> Self:
        """Open for a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close at the end of the ``with`` block."""
        self.close()


def connect_db(uri: Path | str, *, install_extension: bool = False) -> StoreConnection:
    """Open a DuckDB connection on the store at ``uri``, creating the directory if missing.

    The connection is bounded: :data:`WRITER_MEMORY_BYTES`,
    :data:`WRITER_THREADS`, and a private spill directory that
    :meth:`StoreConnection.close` removes. Extension autoloading is off, and
    the lance extension is loaded explicitly through
    :func:`~atif_embed.infrastructure.lance_extension.load_lance_extension`,
    which downloads it only when ``install_extension`` is true. ``TimeZone``
    is pinned to UTC, because a ``TIMESTAMPTZ`` column takes the session's
    zone into the Lance schema it creates: under ``TZ=America/Chicago`` a new
    table's ``embedded_at`` would be ``timestamp[us, tz=America/Chicago]``
    instead of the ``UTC`` every existing store carries.

    Every statement runs in autocommit, so each write is its own Lance commit
    and is visible to every other connection (another process's ``search``
    included) as soon as it returns.
    """
    import duckdb

    root = Path(uri).resolve()
    root.mkdir(parents=True, exist_ok=True)
    spill = Path(tempfile.mkdtemp(prefix="atif-sql-embed-"))
    con = duckdb.connect(
        config={
            "memory_limit": f"{int(WRITER_MEMORY_BYTES)}B",
            "threads": int(WRITER_THREADS),
            "temp_directory": str(spill),
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
    )
    try:
        con.execute("SET TimeZone = 'UTC';")
        load_lance_extension(con, install=install_extension)
        # ATTACH is one of the statement kinds DuckDB will not prepare, so the
        # path enters the statement text, through sql_literal only.
        con.execute(f"ATTACH {sql_literal(str(root))} AS {STORE_ALIAS} (TYPE LANCE);")
    except BaseException:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
        raise
    return StoreConnection(con, root, spill)


def _first_row(
    conn: StoreConnection, sql: str, params: list[object] | None = None
) -> tuple[Any, ...] | None:
    """The first row of ``sql``, reading the result to its end.

    ``fetchall`` rather than ``fetchone``: DuckDB 2.0 counts a result read
    with ``fetchone`` as unfinished until a later fetch returns nothing, and
    ``register`` refuses to run while one is pending.
    """
    rows = conn.con.execute(sql, params).fetchall() if params else conn.con.execute(sql).fetchall()
    return tuple(rows[0]) if rows else None


def has_table(conn: StoreConnection) -> bool:
    """True iff the ATTACHed store holds the embeddings table.

    Raises
    ------
    EmbeddingStoreUnreadable
        :data:`DATASET_DIR` is on disk but the attached catalog doesn't list
        the table. The lance extension leaves out a dataset it can't open (a
        truncated manifest, for one) instead of failing the ATTACH, so
        without this check an unreadable store reads as an empty one: every
        corpus text looks unembedded, all of it goes to the embedder, and
        the ``INSERT`` after it fails because the dataset already exists, on
        every run. Every reader and writer here asks this first, so the
        error comes before any embedding call.
    """
    row = _first_row(
        conn,
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
        [STORE_ALIAS, TABLE_NAME],
    )
    if row is not None and int(row[0]) > 0:
        return True
    dataset = conn.uri / DATASET_DIR
    if dataset.exists():
        msg = (
            f"the embeddings store at {conn.uri} holds a Lance dataset the lance extension "
            f"can't open ({dataset}); its newest manifest may be damaged or partly written. "
            "Restore the store from a backup, or move it aside and re-embed with "
            "`atif-sql embed --all --no-dry-run`"
        )
        raise EmbeddingStoreUnreadable(msg)
    return False


def column_names(conn: StoreConnection) -> list[str]:
    """The embeddings table's column names, in table order (empty when there's no table)."""
    rows = conn.con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name = ? AND table_name = ? ORDER BY column_index",
        [STORE_ALIAS, TABLE_NAME],
    ).fetchall()
    return [str(row[0]) for row in rows]


def read_schema_version(lance_uri: Path) -> int | None:
    """Return the store's recorded schema version, or ``None`` when unrecorded.

    ``None`` covers both a store that predates the sidecar (v1/v2 written
    before 2026-08-27) and a directory with no store at all; callers that
    need to distinguish those already know whether the table exists.
    """
    sidecar = Path(lance_uri) / SCHEMA_VERSION_FILE
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("schema_version")
    return int(version) if isinstance(version, int) else None


def write_schema_version(lance_uri: Path, version: int = SCHEMA_VERSION) -> None:
    """Record ``version`` in the store's sidecar (create or overwrite)."""
    sidecar = Path(lance_uri) / SCHEMA_VERSION_FILE
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({"schema_version": version}) + "\n", encoding="utf-8")


def migrate_pre_stamp_table(conn: StoreConnection) -> None:
    """Evolve a pre-stamp table to the v2 schema, in place and online.

    Adds ``text_hash`` (backfilled with :data:`_PRE_STAMP_SENTINEL`) and
    ``truncated`` (backfilled false) with ``ALTER TABLE ... ADD COLUMN ...
    DEFAULT``, then marks each NOT NULL, the schema lancedb's
    ``add_columns`` left behind. No rows are dropped, no vectors are
    re-embedded, and readers keep working throughout. The sentinel then
    drives incremental re-embedding through the ordinary staleness path (see
    the module docstring).

    Each default is a plain literal on purpose. The extension rejects
    ``ALTER COLUMN ... SET DEFAULT``, and DuckDB rewrites any other ``ADD
    COLUMN ... DEFAULT`` form into an ``ADD COLUMN`` followed by one, which
    could fail after the column exists. Each column is added on its own, so a
    run cut short between the two leaves a table the next open finishes (a
    column added but not yet marked NOT NULL stays nullable, which every
    writer still fills).
    """
    present = set(column_names(conn))
    added: list[str] = []
    for name, ddl_type, default in _PRE_STAMP_ADDITIONS:
        if name in present:
            continue
        conn.con.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl_type} DEFAULT {default};")
        conn.con.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {name} SET NOT NULL;")
        added.append(name)
    if not added:
        return
    write_schema_version(conn.uri)
    logger.info(
        "Migrated pre-stamp Lance store at {} to schema v{}: added {} online "
        "(no vectors rewritten; rows re-embed incrementally via the sentinel staleness path)",
        conn.uri,
        SCHEMA_VERSION,
        sorted(added),
    )


def _create_table(conn: StoreConnection, *, dim: int) -> None:
    """Create the embeddings table at :data:`DATA_STORAGE_VERSION`, every column NOT NULL.

    The extension's ``CREATE TABLE`` takes no column constraints, so the
    columns are marked NOT NULL one ``ALTER`` at a time afterwards. That
    costs a handful of tiny versions once per store, which the next
    ``VACUUM LANCE`` removes.
    """
    columns = ", ".join(f"{name} {ddl_type.format(dim=int(dim))}" for name, ddl_type in _COLUMN_DDL)
    conn.con.execute(
        f"CREATE TABLE {_TABLE} ({columns}) "
        f"WITH (data_storage_version = {sql_literal(DATA_STORAGE_VERSION)});"
    )
    for name in _COLUMNS:
        conn.con.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {name} SET NOT NULL;")


def open_or_create_table(conn: StoreConnection, *, dim: int) -> None:
    """Make sure the embeddings table exists with the current schema.

    An existing table that predates a current ADDITIVE column is evolved in
    place (see :func:`migrate_pre_stamp_table`) before the write path uses
    it, so ``add_chunk`` never appends a 7-column chunk into a 5-column table.
    A missing table is created with :func:`lance_schema`'s schema.
    """
    if has_table(conn):
        migrate_pre_stamp_table(conn)
        if read_schema_version(conn.uri) is None:
            # A store from before the sidecar existed whose schema is already
            # current: stamp it so the NEXT evolution keys on the number.
            write_schema_version(conn.uri)
        return
    _create_table(conn, dim=dim)
    write_schema_version(conn.uri)


def delete_uuids(conn: StoreConnection, uuids: Iterable[str]) -> int:
    """Delete every row whose uuid is in ``uuids``; return how many were named.

    Re-embedding a changed text must REPLACE the stale vector, not sit
    beside it: two rows under one uuid would make the kNN join fan out and
    rank against pre-change content. The table has no upsert key, so
    delete-then-append is the replace.

    The delete is a ``MERGE`` against an Arrow table of the uuids rather than
    ``DELETE ... WHERE uuid IN (...)``: DuckDB plans an ``IN`` list longer
    than four values as a join, which the extension's ``DELETE`` refuses
    ("unsupported DELETE plan"). The uuids never enter the statement text,
    and the whole delete is one Lance commit.
    """
    names = sorted(set(uuids))
    if not names:
        return 0
    conn.con.register(_DELETE_VIEW, pa.table({"uuid": pa.array(names, pa.string())}))
    try:
        conn.con.execute(
            f"MERGE INTO {_TABLE} AS target USING {_DELETE_VIEW} AS gone "
            "ON target.uuid = gone.uuid WHEN MATCHED THEN DELETE;"
        ).fetchall()
    finally:
        conn.con.unregister(_DELETE_VIEW)
    return len(names)


def add_chunk(conn: StoreConnection, df: pl.DataFrame) -> None:
    """Append one chunk of embeddings.

    The polars DataFrame must have ``embedding: pl.Array(pl.Float32, dim)``,
    the fixed-size list shape the table's ``FLOAT[dim]`` column holds.
    ``to_arrow()`` keeps that shape, and the INSERT names every column, so a
    frame with columns in another order still lands in the right ones.
    """
    columns = ", ".join(_COLUMNS)
    conn.con.register(_CHUNK_VIEW, df.to_arrow())
    try:
        conn.con.execute(
            f"INSERT INTO {_TABLE} ({columns}) SELECT {columns} FROM {_CHUNK_VIEW};"  # noqa: S608  # nosec B608 - module-constant names only
        ).fetchall()
    finally:
        conn.con.unregister(_CHUNK_VIEW)


def optimize_if_needed(conn: StoreConnection) -> None:
    """Compact accumulated fragments, then remove versions past the retention window.

    Every append, delete and compaction commits a new Lance version, and
    every append adds a fragment. ``OPTIMIZE`` merges small fragments and
    rewrites the ones carrying many deletions; ``VACUUM LANCE`` then deletes
    the files only versions older than :data:`VERSION_RETENTION_SECONDS`
    still name. Both are best-effort, as lancedb's ``optimize()`` was: a
    failure is logged and the next run tries again, because a store that
    isn't compacted is slower, not wrong. They're also independent: VACUUM
    runs whether or not OPTIMIZE committed.
    """
    import duckdb

    try:
        conn.con.execute(f"OPTIMIZE {_TABLE};").fetchall()
    except duckdb.Error as exc:
        # Under a steady delete-and-insert loop OPTIMIZE loses commits to
        # retryable conflicts (30 of 244 in the 2026-11-03 review). Returning
        # here would let old versions pile up for as long as that lasted.
        logger.warning("Lance OPTIMIZE failed: {}; running VACUUM anyway", exc)
    try:
        conn.con.execute(
            f"VACUUM LANCE {_TABLE} WITH (older_than_seconds = {int(VERSION_RETENTION_SECONDS)});"
        ).fetchall()
    except duckdb.Error as exc:
        logger.warning("Lance VACUUM failed: {}", exc)


def _count_rows(conn: StoreConnection) -> int:
    if not has_table(conn):
        return 0
    row = _first_row(conn, f"SELECT count(*) FROM {_TABLE};")  # noqa: S608  # nosec B608 - module-constant names only
    return int(row[0]) if row else 0


def _table_identity(conn: StoreConnection) -> tuple[str, int] | None:
    if not has_table(conn):
        return None
    row = _first_row(conn, f"SELECT model, dim FROM {_TABLE} LIMIT 1;")  # noqa: S608  # nosec B608 - module-constant names only
    if row is None:
        return None
    return (str(row[0]), int(row[1]))


def _embedded_hashes(conn: StoreConnection) -> dict[str, str]:
    if not has_table(conn):
        return {}
    if "text_hash" not in column_names(conn):
        rows = conn.con.execute(f"SELECT uuid FROM {_TABLE};").fetchall()  # noqa: S608  # nosec B608 - module-constant names only
        logger.info(
            "Embedding store at {} predates the text_hash stamp; treating all "
            "{} rows as stale for incremental online re-embedding",
            conn.uri,
            len(rows),
        )
        return {str(row[0]): _PRE_STAMP_SENTINEL for row in rows}
    rows = conn.con.execute(f"SELECT uuid, text_hash FROM {_TABLE};").fetchall()  # noqa: S608  # nosec B608 - module-constant names only
    return {str(row[0]): str(row[1]) for row in rows}


def count_rows(lance_uri: Path, *, install_extension: bool = False) -> int:
    """Return the row count of the embeddings table, or 0 when missing.

    A directory that doesn't exist is answered without opening DuckDB, so no
    directory is created and the extension isn't needed.
    """
    if not Path(lance_uri).is_dir():
        return 0
    with connect_db(lance_uri, install_extension=install_extension) as conn:
        return _count_rows(conn)


def table_identity(lance_uri: Path, *, install_extension: bool = False) -> tuple[str, int] | None:
    """Return the store's stamped ``(model, dim)`` from its first row, or ``None``.

    ``None`` means the store is empty or absent (fresh install), so any
    provider may claim it. Every row stamps the same ``model`` / ``dim`` (see
    ``run_backfill``), so reading the first row is sufficient. Used by the
    fail-loud provider/dimension guard
    (:func:`atif_embed.domain.embedding_guard.ensure_store_matches`).
    """
    if not Path(lance_uri).is_dir():
        return None
    with connect_db(lance_uri, install_extension=install_extension) as conn:
        return _table_identity(conn)


def get_embedded_hashes(lance_uri: Path, *, install_extension: bool = False) -> dict[str, str]:
    """Return ``{uuid: text_hash}`` for every embedded row.

    Used by the backfill's anti-join: a uuid whose stored hash differs from
    the corpus's current text is STALE, not embedded, so it must be re-picked
    and its old row deleted. The scan projects the two string columns, so
    Lance never decodes the ``FLOAT[dim]`` vector column.

    A store written before ``text_hash`` existed has no such column. That is
    NOT terminal: every row gets :data:`_PRE_STAMP_SENTINEL` instead (a value
    no real blake2b digest can equal), so the discovery anti-join marks every
    row stale and the store migrates itself incrementally through the
    ordinary delete-before-append path, with no rebuild and no empty-store
    window. The 2026-08-24 incident (both production corpora rebuilt, ~3.4M
    vectors of Cohere spend) came from raising a destroy-and-rebuild error
    here instead.
    """
    if not Path(lance_uri).is_dir():
        return {}
    with connect_db(lance_uri, install_extension=install_extension) as conn:
        return _embedded_hashes(conn)


class LanceVectorStore:
    """:class:`~atif_embed.domain.ports.VectorStorePort` over this module.

    Binds the module functions to one ``lance_uri`` + ``dim`` pair and keeps
    ONE connection open for its lifetime, so a run pays for loading the
    extension and attaching the store once. Reads on a store directory that
    doesn't exist answer empty without opening anything, so a dry run on a
    fresh corpus creates nothing. The first write creates the directory and
    the table (or migrates an older one).

    ``install_extension`` lets the connection download the lance extension
    when it's absent. Only a caller that already reaches the network passes
    it (a real embed run); otherwise an absent extension raises
    :class:`~atif_embed.domain.errors.LanceExtensionMissing`.

    :meth:`close` releases the connection; the store also closes itself when
    it's garbage-collected.
    """

    def __init__(self, lance_uri: Path, *, dim: int, install_extension: bool = False) -> None:
        self._lance_uri = lance_uri
        self._dim = dim
        self._install_extension = install_extension
        self._conn: StoreConnection | None = None
        self._finalizer: weakref.finalize[[], Self] | None = None
        self._writable = False

    def _connection(self) -> StoreConnection:
        if self._conn is None:
            conn = connect_db(self._lance_uri, install_extension=self._install_extension)
            self._conn = conn
            self._finalizer = weakref.finalize(self, conn.close)
        return self._conn

    def _write_connection(self) -> StoreConnection:
        conn = self._connection()
        if not self._writable:
            open_or_create_table(conn, dim=self._dim)
            self._writable = True
        return conn

    def _readable(self) -> bool:
        return self._conn is not None or Path(self._lance_uri).is_dir()

    def table_identity(self) -> tuple[str, int] | None:
        """The store's stamped ``(model, dim)``, or None when it has none."""
        if not self._readable():
            return None
        return _table_identity(self._connection())

    def get_embedded_hashes(self) -> dict[str, str]:
        """``{uuid: text_hash}`` for every row already in the store."""
        if not self._readable():
            return {}
        return _embedded_hashes(self._connection())

    def count_rows(self) -> int:
        """The number of rows in the store (0 when it has no table)."""
        if not self._readable():
            return 0
        return _count_rows(self._connection())

    def delete_uuids(self, uuids: Iterable[str]) -> int:
        """Delete the rows carrying ``uuids``; return how many went."""
        return delete_uuids(self._write_connection(), uuids)

    def add_chunk(self, df: pl.DataFrame) -> None:
        """Append one embedded chunk to the table."""
        add_chunk(self._write_connection(), df)

    def optimize(self) -> None:
        """Compact the table's fragments and remove versions past the retention window."""
        optimize_if_needed(self._write_connection())

    def close(self) -> None:
        """Close the store's connection, if one is open. Safe to call twice."""
        if self._finalizer is not None:
            self._finalizer()
        self._conn = None
        self._finalizer = None
        self._writable = False


__all__ = [
    "DATASET_DIR",
    "DATA_STORAGE_VERSION",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_FILE",
    "STORE_ALIAS",
    "TABLE_NAME",
    "VERSION_RETENTION_SECONDS",
    "WRITER_MEMORY_BYTES",
    "WRITER_THREADS",
    "LanceVectorStore",
    "StoreConnection",
    "add_chunk",
    "column_names",
    "connect_db",
    "count_rows",
    "delete_uuids",
    "get_embedded_hashes",
    "has_table",
    "lance_schema",
    "migrate_pre_stamp_table",
    "open_or_create_table",
    "optimize_if_needed",
    "read_schema_version",
    "table_identity",
    "write_schema_version",
]
