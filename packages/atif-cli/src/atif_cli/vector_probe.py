# SPDX-License-Identifier: Apache-2.0

"""Time the exact kNN scan ``search`` runs, with no Bedrock call and no network.

``atif-sql status --vector-probe`` runs this, and the nightly compact lane in
``scripts/atif-sql-refresh.sh`` logs the result for each corpus. ``search``
reads no vector index: it ranks every stored vector, so what it pays grows
with the store, and this number is how the lane notices when it's grown too
far.

The probe runs :data:`PROBE_SQL`, which ranks the store's rows the way
``search`` ranks them (the same ``qv`` CTE, cosine similarity, and ``ORDER BY
array_cosine_distance ... ASC`` with a ``LIMIT``), minus ``search``'s join back
to ``steps``. Its query vector is the stored embedding of the smallest uuid,
so a store gives the same statement every night and nothing is embedded.
Only the scan is timed. Attaching the store, counting its rows and reading
the probe vector all happen before the clock starts.

The connection is the store's own, from
:func:`atif_embed.infrastructure.lance_store.connect_db`: 1 GiB of DuckDB
memory, 2 threads, a private spill directory, no extension autoload, and no
download of the lance extension. The compact lane runs inside a 4G memory
scope, and an unbounded connection would size itself from the whole host.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How many neighbors the probe asks for: ``search``'s default ``-k``.
PROBE_K = 10

#: The probe's kNN. Every line from the ``qv`` CTE to ``LIMIT ?`` except the
#: ``FROM`` matches a line of ``atif_cli.app._SEARCH_SQL``, and
#: ``tests/test_vector_probe.py`` checks that, so a change to how ``search``
#: ranks has to change this too. ``{dim}`` is filled with ``int()`` and
#: ``{table}`` with the store's table name, never with caller text.
PROBE_SQL = """
    WITH qv AS (SELECT CAST(? AS FLOAT[{dim}]) AS v)
    SELECT me.uuid                                                     AS uuid,
           array_cosine_similarity(me.embedding, (SELECT v FROM qv))   AS sim
    FROM {table} me
    ORDER BY array_cosine_distance(me.embedding, (SELECT v FROM qv)) ASC
    LIMIT ?
"""


@dataclass(frozen=True)
class VectorProbe:
    """One timed scan of one store.

    ``hits`` holds the ``(uuid, sim)`` rows the scan returned, best first.
    It's for tests and isn't part of :meth:`as_dict`: the lane logs the
    timing, not the neighbors.
    """

    corpus: str
    store: Path
    vector_rows: int
    scan_s: float
    hits: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict[str, Any]:
        """The probe's JSON payload: ``{corpus, store, vector_rows, scan_s}``."""
        return {
            "corpus": self.corpus,
            "store": str(self.store),
            "vector_rows": self.vector_rows,
            "scan_s": self.scan_s,
        }


def probe_sql(*, dim: int, table: str) -> str:
    """:data:`PROBE_SQL` for a ``dim``-wide store whose table is ``table``."""
    return PROBE_SQL.format(dim=int(dim), table=table)


def probe_store(store: Path, *, corpus: str, k: int = PROBE_K) -> VectorProbe | None:
    """Time one exact top-``k`` scan of the store at ``store``.

    Returns ``None`` when there's nothing to scan: no store directory, no
    embeddings table in it, or no rows. The directory is checked before
    connecting, because opening a store creates its directory.

    Raises
    ------
    LanceExtensionMissing
        The lance extension isn't installed. The probe never downloads it.
    duckdb.Error
        The store couldn't be read.
    """
    from atif_embed.infrastructure.lance_store import (
        STORE_ALIAS,
        TABLE_NAME,
        connect_db,
        has_table,
    )

    if not store.is_dir():
        return None
    table = f"{STORE_ALIAS}.main.{TABLE_NAME}"
    with connect_db(store) as conn:
        if not has_table(conn):
            return None
        con = conn.con
        vector_rows = int(con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0])  # noqa: S608  # nosec B608 - table is the store's constant name
        if vector_rows == 0:
            return None
        smallest = con.execute(f"SELECT min(uuid) FROM {table}").fetchall()[0][0]  # noqa: S608  # nosec B608 - table is the store's constant name
        vector = con.execute(
            f"SELECT embedding FROM {table} WHERE uuid = ? LIMIT 1",  # noqa: S608  # nosec B608 - table is the store's constant name
            [smallest],
        ).fetchall()[0][0]
        sql = probe_sql(dim=len(vector), table=table)
        started = time.perf_counter()
        rows = con.execute(sql, [list(vector), int(k)]).fetchall()
        scan_s = time.perf_counter() - started
    return VectorProbe(
        corpus=corpus,
        store=store,
        vector_rows=vector_rows,
        scan_s=round(scan_s, 3),
        hits=tuple((str(uuid), float(sim)) for uuid, sim in rows),
    )


__all__ = ["PROBE_K", "PROBE_SQL", "VectorProbe", "probe_sql", "probe_store"]
