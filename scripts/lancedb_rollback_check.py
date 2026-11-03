#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Recheck that lancedb 0.39.0 (what atif-sql 0.5.0 locked) can still use a store this tree writes.

    mise run release:lancedb-rollback

Run by hand, never by a gate: the check step downloads lancedb. Run it whenever
``duckdb`` moves in ``uv.lock`` or DuckDB's lance extension moves to a new build,
because rolling back to 0.5.0 is safe only while lancedb can open what the
extension writes. RELEASING.md records the build it last passed against.

Three steps, each its own process, so each runs in its own environment:

``write STORE``
    In the workspace venv: create a store through atif-embed's writer (the
    lance extension), delete a row and compact it, then print the DuckDB
    version and the extension build that wrote it.
``check STORE``
    Under ``uv run --isolated --no-project --with lancedb==0.39.0``: open the
    store with lancedb and read it, search it by cosine, append a row and
    compact it.
``reread STORE``
    In the workspace venv again: read the store lancedb just wrote to,
    append, compact and read again, which is rolling forward after a rollback.

Each step prints one JSON line and exits nonzero on the first thing that
doesn't hold.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DIM = 8
ROWS = 20
DELETED = ("u-000", "u-001")
MODEL = "rollback-check:1"


def _vector(i: int) -> list[float]:
    return [float((i * (j + 3)) % 11) + 1.0 for j in range(DIM)]


def _frame(uuids: list[str], start: int) -> Any:
    import polars as pl

    now = datetime.now(UTC)
    return pl.DataFrame(
        {
            "uuid": uuids,
            "model": [MODEL] * len(uuids),
            "dim": [DIM] * len(uuids),
            "embedding": [_vector(start + i) for i in range(len(uuids))],
            "embedded_at": [now] * len(uuids),
            "text_hash": [f"h-{uuid}" for uuid in uuids],
            "truncated": [False] * len(uuids),
        },
        schema={
            "uuid": pl.Utf8,
            "model": pl.Utf8,
            "dim": pl.Int32,
            "embedding": pl.Array(pl.Float32, DIM),
            "embedded_at": pl.Datetime("us", "UTC"),
            "text_hash": pl.Utf8,
            "truncated": pl.Boolean,
        },
    )


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")


def _fail(step: str, problem: str) -> None:
    _emit({"step": step, "ok": False, "problem": problem})
    raise SystemExit(1)


def write(store: Path) -> dict[str, Any]:
    """Create the store through the lance extension, as ``atif-sql embed`` would."""
    import duckdb

    from atif_embed.infrastructure import lance_store

    if store.exists():
        _fail("write", f"{store} already exists; give the check an empty path")
    writer = lance_store.LanceVectorStore(store, dim=DIM, install_extension=True)
    writer.add_chunk(_frame([f"u-{i:03d}" for i in range(ROWS)], 0))
    writer.delete_uuids(list(DELETED))
    writer.optimize()
    writer.close()
    con = duckdb.connect()
    try:
        build = con.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'lance'"
        ).fetchall()[0][0]
    finally:
        con.close()
    return {
        "duckdb": duckdb.__version__,
        "lance_extension": str(build),
        "data_storage_version": lance_store.DATA_STORAGE_VERSION,
        "rows": lance_store.count_rows(store),
    }


def check(store: Path) -> dict[str, Any]:
    """Read, search, append to and compact the store with lancedb, as atif-sql 0.5.0 would."""
    lancedb = importlib.import_module("lancedb")
    pa = importlib.import_module("pyarrow")

    table = lancedb.connect(str(store)).open_table("embeddings")
    before = int(table.count_rows())
    if before != ROWS - len(DELETED):
        _fail("check", f"lancedb counts {before} rows, the writer left {ROWS - len(DELETED)}")
    stored = table.to_arrow()
    uuids = {str(uuid) for uuid in stored.column("uuid").to_pylist()}
    if uuids & set(DELETED):
        _fail("check", f"lancedb still sees deleted rows {sorted(uuids & set(DELETED))}")
    hits = table.search(_vector(5)).metric("cosine").limit(3).to_list()
    if not hits or hits[0]["uuid"] != "u-005":
        _fail("check", f"a cosine search for u-005's own vector returned {hits[:1]}")
    row = pa.table(
        {
            "uuid": ["u-lancedb"],
            "model": [MODEL],
            "dim": pa.array([DIM], type=pa.int32()),
            "embedding": pa.array([_vector(99)], type=pa.list_(pa.float32(), DIM)),
            "embedded_at": pa.array([datetime.now(UTC)], type=pa.timestamp("us", tz="UTC")),
            "text_hash": ["h-u-lancedb"],
            "truncated": [False],
        },
        schema=table.schema,
    )
    table.add(row)
    table.optimize()
    after = int(table.count_rows())
    if after != before + 1:
        _fail("check", f"after lancedb's append and optimize the store holds {after} rows")
    return {
        "lancedb": str(lancedb.__version__),
        "rows_before": before,
        "rows_after": after,
        "top_hit": hits[0]["uuid"],
    }


def reread(store: Path) -> dict[str, Any]:
    """Read and extend the store again through the extension, after lancedb wrote to it."""
    from atif_embed.infrastructure import lance_store

    expected = ROWS - len(DELETED) + 1
    rows = lance_store.count_rows(store)
    if rows != expected:
        _fail("reread", f"the extension counts {rows} rows, lancedb left {expected}")
    if "u-lancedb" not in lance_store.get_embedded_hashes(store):
        _fail("reread", "the row lancedb appended is missing")
    writer = lance_store.LanceVectorStore(store, dim=DIM)
    writer.add_chunk(_frame(["u-forward"], 50))
    writer.optimize()
    writer.close()
    after = lance_store.count_rows(store)
    if after != expected + 1:
        _fail("reread", f"after the extension's append the store holds {after} rows")
    return {"rows_before": rows, "rows_after": after, "identity": lance_store.table_identity(store)}


_STEPS = {"write": write, "check": check, "reread": reread}


def main(argv: list[str]) -> None:
    """Run one step on one store: ``write|check|reread STORE``. Exit 64 on bad arguments."""
    match argv:
        case [step, store] if step in _STEPS:
            _emit({"step": step, "ok": True, **_STEPS[step](Path(store))})
        case _:
            sys.stderr.write(f"usage: {Path(__file__).name} {{write|check|reread}} STORE\n")
            raise SystemExit(64)


if __name__ == "__main__":
    main(sys.argv[1:])
