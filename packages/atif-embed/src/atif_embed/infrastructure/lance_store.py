# SPDX-License-Identifier: Apache-2.0

"""Local LanceDB embeddings store.

Lance is exactly the right shape for "AI artifact store with versioning +
native vector search":

* writes are append-only fragments via ``tbl.add()``
* stale rows are removed by predicate via ``tbl.delete()``
* compaction is explicit via ``tbl.optimize()``
* the vector index lives next to the data
* DuckDB reads it back via the lance core extension
  (``INSTALL lance; LOAD lance; ATTACH (TYPE LANCE)`` — that read path lives
  in atif-duck's ``register_vss``; this module never touches DuckDB)

Schema evolution policy (v2, 2026-08-27)
----------------------------------------
ADDITIVE columns migrate ONLINE, never via rebuild: opening a store that
lacks a newer column evolves the table in place (``Table.add_columns``, a
metadata-only operation — no re-embedding, no data loss) and backfills the
new column with a sentinel/default. Rows carrying the
:data:`_PRE_STAMP_SENTINEL` stamp then mismatch their corpus hash in the
discovery anti-join and re-embed incrementally through the ordinary
staleness path (delete-before-append), so the store heals itself across
successive runs while search stays online. A BREAKING change — a provider
or dimension switch — stays fail-loud via ``embedding_guard``: those
vectors live in an incompatible space and must be rebuilt deliberately.

The store records its schema version in a ``schema_version.json`` sidecar
next to the table (written on create, checked on open). Bump
:data:`SCHEMA_VERSION` with the next evolution and key the migration on the
stored number rather than probing by column name.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import lancedb
import pyarrow as pa
from lancedb.index import IvfHnswSq
from loguru import logger

from atif_embed.domain.sql_literal import sql_literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    import polars as pl

#: Lance table name inside the namespace.
TABLE_NAME = "embeddings"

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

#: ``text_hash`` stamped on rows that predate the stamp. Angle brackets make
#: it impossible to equal a real blake2b hex digest, so every pre-stamp row
#: mismatches its current corpus hash in the discovery anti-join and is
#: re-embedded incrementally through the ordinary staleness path.
_PRE_STAMP_SENTINEL = "<pre-stamp>"

#: Distance metrics accepted by the LanceDB vector index (``IvfHnswSq``'s
#: ``distance_type``). Mirrors ``EmbedSettings.hnsw_metric``'s domain.
DistanceMetric = Literal["cosine", "l2", "dot"]

# Process-local connection cache. lancedb.connect() is cheap (no I/O until a
# table is opened) but caching avoids re-running the consistency-interval setup.
_DB_CACHE: dict[str, lancedb.DBConnection] = {}


def _has_table(db: lancedb.DBConnection, name: str) -> bool:
    """True iff ``name`` exists in the LanceDB root namespace.

    ``db.list_tables()`` returns a ``ListTablesResponse`` (pydantic model)
    with a ``tables: list[str]`` field plus an opaque ``page_token``. The
    local namespace is small enough that pagination never kicks in, so we
    read ``.tables`` directly. ``db.table_names()`` is deprecated as of
    lancedb 0.30 (emits ``DeprecationWarning``) — don't reach for it.
    """
    return name in db.list_tables().tables


# pyarrow ships no py.typed and no bundled stubs, so `pa.Schema` is itself an
# unknown type and any signature naming it reports as partially unknown. The
# annotation is still the most precise one available and is what a reader needs.
def lance_schema(dim: int) -> pa.Schema:  # pyright: ignore[reportUnknownParameterType]
    """Pyarrow schema for the embeddings table.

    The ``embedding`` column is a FIXED-SIZE list of float32. Lance treats
    this as a vector column for indexing — a regular ``pa.list_(pa.float32())``
    won't work for ``create_index``.

    ``text_hash`` is the content stamp the backfill's staleness check reads:
    a re-converted session can change a step's flattened text under the same
    uuid, and without the stamp the uuid-only anti-join would treat that row
    as already embedded forever. ``truncated`` marks rows whose source text
    was clipped before embedding, so a search miss past the clip point is
    attributable instead of invisible.
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


def connect_db(uri: Path | str) -> lancedb.DBConnection:
    """Open (or reuse) a LanceDB connection rooted at ``uri``.

    ``read_consistency_interval=timedelta(0)`` makes every read see writes
    from other processes immediately. Without this, readers get a stale
    manifest snapshot and miss recently appended fragments.
    """
    key = str(Path(uri).resolve())
    cached = _DB_CACHE.get(key)
    if cached is not None:
        return cached
    Path(uri).mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(key, read_consistency_interval=timedelta(0))
    _DB_CACHE[key] = db
    return db


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


def migrate_pre_stamp_table(tbl: Any, lance_uri: Path) -> None:
    """Evolve a pre-stamp table to the v2 schema, in place and online.

    Adds ``text_hash`` (backfilled with :data:`_PRE_STAMP_SENTINEL`) and
    ``truncated`` (backfilled false) via Lance schema evolution
    (``Table.add_columns`` with SQL default expressions) — a metadata-only
    operation: no rows are dropped, no vectors re-embedded, and readers keep
    working throughout. The sentinel then drives incremental re-embedding
    through the ordinary staleness path (see the module docstring).
    """
    missing = [name for name in ("text_hash", "truncated") if name not in tbl.schema.names]
    additions: dict[str, str] = {}
    if "text_hash" in missing:
        additions["text_hash"] = f"'{_PRE_STAMP_SENTINEL}'"
    if "truncated" in missing:
        additions["truncated"] = "false"
    if not additions:
        return
    tbl.add_columns(additions)
    write_schema_version(lance_uri)
    logger.info(
        "Migrated pre-stamp Lance store at {} to schema v{}: added {} online "
        "(metadata-only; rows re-embed incrementally via the sentinel staleness path)",
        lance_uri,
        SCHEMA_VERSION,
        sorted(additions),
    )


def open_or_create_table(db: lancedb.DBConnection, *, dim: int) -> Any:
    """Open the embeddings table, creating it with the right schema if missing.

    An existing table that predates a current ADDITIVE column is evolved in
    place (see :func:`migrate_pre_stamp_table`) before it is handed to the
    write path, so ``add_chunk`` never appends a 7-column frame into a
    5-column table (Lance rejects the schema mismatch).
    """
    if _has_table(db, TABLE_NAME):
        tbl = db.open_table(TABLE_NAME)
        migrate_pre_stamp_table(tbl, Path(db.uri))
        if read_schema_version(Path(db.uri)) is None:
            # A store from before the sidecar existed whose schema is already
            # current: stamp it so the NEXT evolution keys on the number.
            write_schema_version(Path(db.uri))
        return tbl
    tbl = db.create_table(TABLE_NAME, schema=lance_schema(dim), mode="create")
    write_schema_version(Path(db.uri))
    return tbl


def _is_missing_column(exc: Exception, column: str) -> bool:
    """True when ``exc`` is LanceDB's untyped "no such column" ValueError.

    LanceDB raises a plain ``ValueError`` whose text carries the schema
    complaint, so matching the message is the only available discriminator.
    """
    message = str(exc)
    return "Schema error" in message and f"No field named {column}" in message


def delete_uuids(tbl: Any, uuids: Iterable[str]) -> int:
    """Delete every row whose uuid is in ``uuids``; return how many were named.

    Re-embedding a changed text must REPLACE the stale vector, not sit
    beside it: two rows under one uuid would make the kNN join fan out and
    rank against pre-change content. Lance has no upsert on this table, so
    delete-then-append is the replace.
    """
    names = sorted(set(uuids))
    if not names:
        return 0
    predicate = f"uuid IN ({', '.join(sql_literal(u) for u in names)})"
    tbl.delete(predicate)
    return len(names)


def add_chunk(tbl: Any, df: pl.DataFrame) -> None:
    """Append one chunk of embeddings.

    The polars DataFrame must have ``embedding: pl.Array(pl.Float32, dim)``
    — the fixed-size list shape Lance expects. ``to_arrow()`` preserves the
    distinction; a regular ``pl.List`` becomes a variable-size list and Lance
    will reject it as a vector column for indexing.
    """
    arrow_table = df.to_arrow()
    tbl.add(arrow_table)


def ensure_index(tbl: Any, *, metric: DistanceMetric = "cosine") -> None:
    """Create the IVF_HNSW_SQ vector index on the ``embedding`` column.

    No-op if an index already exists. Index name is exactly ``IVF_HNSW_SQ``
    (scalar-quantized HNSW) — there is no plain ``IVF_HNSW`` literal in
    LanceDB. SQ gives a good size/recall balance at small-corpus scale.
    """
    try:
        existing = tbl.list_indices()
    except (AttributeError, RuntimeError):
        existing = []
    for idx in existing:
        if getattr(idx, "column", None) == "embedding":
            return
        if "embedding" in getattr(idx, "columns", []):
            return
    try:
        # lancedb >=0.30 unified index API: pass the vector column as the first
        # positional arg and an index-config object via ``config=``. The legacy
        # ``metric=/vector_column_name=/index_type=`` kwargs are deprecated
        # (they emit a DeprecationWarning as of 0.34). ``IvfHnswSq`` is the
        # config class for the scalar-quantized HNSW index this store uses;
        # ``distance_type`` carries the former ``metric``.
        tbl.create_index("embedding", config=IvfHnswSq(distance_type=metric))
    except (RuntimeError, ValueError) as exc:
        # Index creation can fail on tiny tables (no vectors yet, or k-means
        # seeding hits a degenerate split). Log and continue — cosine search
        # still works without an index, just slower (brute-force scan).
        logger.warning("LanceDB create_index failed ({}); falling back to brute-force scan", exc)


def optimize_if_needed(tbl: Any) -> None:
    """Compact accumulated fragments + clean up old versions.

    Lance accumulates one fragment per ``tbl.add()`` call. Past ~100
    fragments read latency starts to degrade; the backfill triggers
    compaction much earlier (every 8 chunks) because the embeddings table is
    small. ``tbl.optimize()`` returns ``None`` and mutates in place.
    """
    try:
        tbl.optimize()
    except (RuntimeError, AttributeError) as exc:
        logger.warning("Lance optimize failed: {}", exc)


def count_rows(lance_uri: Path) -> int:
    """Return the row count of the embeddings Lance table, or 0 when missing."""
    if not lance_uri.exists():
        return 0
    db = connect_db(lance_uri)
    if not _has_table(db, TABLE_NAME):
        return 0
    return int(db.open_table(TABLE_NAME).count_rows())


def table_identity(lance_uri: Path) -> tuple[str, int] | None:
    """Return the store's stamped ``(model, dim)`` from its first row, or ``None``.

    ``None`` means the store is empty or absent (fresh install), so any
    provider may claim it. Every row stamps the same ``model`` / ``dim`` (see
    ``run_backfill``), so reading the first row is sufficient. Used by the
    fail-loud provider/dimension guard
    (:func:`atif_embed.domain.embedding_guard.ensure_store_matches`).
    """
    n = count_rows(lance_uri)
    if n == 0:
        return None
    db = connect_db(lance_uri)
    tbl = db.open_table(TABLE_NAME)
    arrow = tbl.search().select(["model", "dim"]).limit(1).to_arrow()
    if arrow.num_rows == 0:
        return None
    model = str(arrow.column("model").to_pylist()[0])
    dim = int(arrow.column("dim").to_pylist()[0])
    return (model, dim)


def get_embedded_hashes(lance_uri: Path) -> dict[str, str]:
    """Return ``{uuid: text_hash}`` for every embedded row.

    Used by the backfill's anti-join: a uuid whose stored hash differs from
    the corpus's current text is STALE, not embedded, so it must be re-picked
    and its old row deleted. Reads only two string columns via a
    column-projected scan so the ``FLOAT[dim]`` vector column is never
    decoded — ``to_arrow()`` has no projection pushdown and materializes the
    full N×dim float matrix before a ``.select()`` could prune it (~221 MB
    peak at 20k×1024 unprojected vs ~0 MB projected). The explicit
    ``limit(count_rows())`` overrides LanceDB's default 10-row query cap so
    the scan returns every row.

    A store written before ``text_hash`` existed has no such column, and
    LanceDB reports that as a bare ``ValueError``. That is NOT terminal:
    every row gets :data:`_PRE_STAMP_SENTINEL` instead (a value no real
    blake2b digest can equal), so the discovery anti-join marks every row
    stale and the store migrates itself incrementally through the ordinary
    delete-before-append path — no rebuild, no empty-store window. The
    2026-08-24 incident (both fleet corpora rebuilt, ~3.4M vectors of Cohere
    spend) came from raising a destroy-and-rebuild error here instead.
    """
    n = count_rows(lance_uri)
    if n == 0:
        return {}
    db = connect_db(lance_uri)
    tbl = db.open_table(TABLE_NAME)
    try:
        arrow = tbl.search().select(["uuid", "text_hash"]).limit(n).to_arrow()
    except ValueError as exc:
        if not _is_missing_column(exc, "text_hash"):
            raise
        logger.info(
            "Embedding store at {} predates the text_hash stamp; treating all "
            "{} rows as stale for incremental online re-embedding",
            lance_uri,
            n,
        )
        arrow = tbl.search().select(["uuid"]).limit(n).to_arrow()
        return {str(u): _PRE_STAMP_SENTINEL for u in arrow.column("uuid").to_pylist()}
    uuids = arrow.column("uuid").to_pylist()
    hashes = arrow.column("text_hash").to_pylist()
    return {str(u): str(h) for u, h in zip(uuids, hashes, strict=True)}


class LanceVectorStore:
    """:class:`~atif_embed.domain.ports.VectorStorePort` over this module.

    Thin object adapter binding the module functions to one ``lance_uri`` +
    ``dim`` pair so the use case takes a single injectable seam.
    """

    def __init__(self, lance_uri: Path, *, dim: int) -> None:
        self._lance_uri = lance_uri
        self._dim = dim
        self._tbl: Any | None = None

    def _table(self) -> Any:
        if self._tbl is None:
            db = connect_db(self._lance_uri)
            self._tbl = open_or_create_table(db, dim=self._dim)
        return self._tbl

    def table_identity(self) -> tuple[str, int] | None:
        """The store's stamped ``(model, dim)``, or None when it has none."""
        return table_identity(self._lance_uri)

    def get_embedded_hashes(self) -> dict[str, str]:
        """``{uuid: text_hash}`` for every row already in the store."""
        return get_embedded_hashes(self._lance_uri)

    def delete_uuids(self, uuids: Iterable[str]) -> int:
        """Delete the rows carrying ``uuids``; return how many went."""
        return delete_uuids(self._table(), uuids)

    def add_chunk(self, df: pl.DataFrame) -> None:
        """Append one embedded chunk to the table."""
        add_chunk(self._table(), df)

    def optimize(self) -> None:
        """Compact the table's fragments when it has accumulated enough."""
        optimize_if_needed(self._table())

    def ensure_index(self, *, metric: str = "cosine") -> None:
        """Create the vector index under ``metric`` when the table lacks one."""
        if metric not in {"cosine", "l2", "dot"}:
            msg = f"Unsupported Lance metric: {metric!r}"
            raise ValueError(msg)
        ensure_index(self._table(), metric=metric)  # type: ignore[arg-type]


__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_FILE",
    "TABLE_NAME",
    "LanceVectorStore",
    "add_chunk",
    "connect_db",
    "count_rows",
    "delete_uuids",
    "ensure_index",
    "get_embedded_hashes",
    "lance_schema",
    "migrate_pre_stamp_table",
    "open_or_create_table",
    "optimize_if_needed",
    "read_schema_version",
    "table_identity",
    "write_schema_version",
]
