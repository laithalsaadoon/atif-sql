# SPDX-License-Identifier: Apache-2.0

"""Lance store roundtrip + identity guard over a real store in a tmp dir.

The store is written and read through DuckDB's lance extension, which the
suite installs once (``lance_extension_present``), so these tests run it for
real: the roundtrip through fixed-size float lists, nullability and Lance
versions is exactly the surface that breaks silently under mocks.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest
from embed_fixtures import truncate_newest_manifest

from atif_embed.domain.embedding_guard import RECOVERY_HINT, ensure_store_matches
from atif_embed.domain.errors import (
    EmbeddingProviderMismatch,
    EmbeddingStoreUnreadable,
    LanceExtensionInstallFailed,
    LanceExtensionMissing,
)
from atif_embed.infrastructure import lance_extension, lance_store
from atif_embed.infrastructure.lance_store import LanceVectorStore, StoreConnection

DIM = 8

#: The embeddings table inside a :func:`lance_store.connect_db` connection.
TABLE = f"{lance_store.STORE_ALIAS}.main.{lance_store.TABLE_NAME}"

#: Column set of a store written before the ``text_hash`` staleness stamp.
_PRE_TEXT_HASH_COLUMNS = ["uuid", "model", "dim", "embedding", "embedded_at"]

#: The manifest's ``data_format`` (protobuf field 15, tag byte 0x7a): a
#: ``DataStorageFormat`` whose field 1 (``file_format``) is "lance" and whose
#: field 2 (``version``) is a short string. The tag keeps it apart from the
#: ``writer_version`` message, which also starts with "lance".
_STORAGE_FORMAT = re.compile(rb"\x7a.\x0a\x05lance\x12([\x01-\x10])", re.DOTALL)


def _chunk(
    uuids: list[str],
    *,
    model: str = "fake-model:1",
    dim: int = DIM,
    hashes: list[str] | None = None,
    truncated: list[bool] | None = None,
) -> pl.DataFrame:
    now = datetime.now(UTC)
    return pl.DataFrame(
        {
            "uuid": uuids,
            "model": [model] * len(uuids),
            "dim": [dim] * len(uuids),
            "embedding": [[float(i + 1)] * dim for i in range(len(uuids))],
            "embedded_at": [now] * len(uuids),
            "text_hash": hashes if hashes is not None else [f"h-{u}" for u in uuids],
            "truncated": truncated if truncated is not None else [False] * len(uuids),
        },
        schema={
            "uuid": pl.Utf8,
            "model": pl.Utf8,
            "dim": pl.Int32,
            "embedding": pl.Array(pl.Float32, dim),
            "embedded_at": pl.Datetime("us", "UTC"),
            "text_hash": pl.Utf8,
            "truncated": pl.Boolean,
        },
    )


def _select(uri: Path, sql: str) -> list[tuple[Any, ...]]:
    """Run ``sql`` (naming the table as ``{table}``) on a fresh connection to the store."""
    with lance_store.connect_db(uri) as conn:
        return conn.con.execute(sql.format(table=TABLE)).fetchall()


def _manifests(uri: Path) -> list[Path]:
    return sorted((uri / f"{lance_store.TABLE_NAME}.lance" / "_versions").glob("*.manifest"))


def _storage_versions(uri: Path) -> set[str]:
    """Every ``data_storage_version`` the store's manifests record."""
    found: set[str] = set()
    for manifest in _manifests(uri):
        blob = manifest.read_bytes()
        for match in _STORAGE_FORMAT.finditer(blob):
            start = match.end()
            found.add(blob[start : start + match.group(1)[0]].decode())
    return found


class TestRoundtrip:
    def test_add_then_read_back(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2", "u-3"]))

        assert lance_store.count_rows(uri) == 3
        assert store.get_embedded_hashes() == {"u-1": "h-u-1", "u-2": "h-u-2", "u-3": "h-u-3"}
        assert store.table_identity() == ("fake-model:1", DIM)

    def test_empty_store_identity_is_none(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        assert store.table_identity() is None
        assert store.get_embedded_hashes() == {}
        assert lance_store.count_rows(uri) == 0

    def test_reads_on_a_missing_store_create_nothing(self, tmp_path: Path) -> None:
        """A dry run on a fresh corpus must not leave a store directory behind."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        assert store.get_embedded_hashes() == {}
        assert store.table_identity() is None
        assert lance_store.get_embedded_hashes(uri) == {}
        assert lance_store.table_identity(uri) is None
        assert not uri.exists()

    def test_the_table_exists_only_after_the_first_write(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        with lance_store.connect_db(uri) as conn:
            assert not lance_store.has_table(conn)
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1"]))
        with lance_store.connect_db(uri) as conn:
            assert lance_store.has_table(conn)

    def test_get_embedded_hashes_returns_every_row(self, tmp_path: Path) -> None:
        """No implicit row cap: lancedb's query defaulted to 10 rows, a scan has none."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        uuids = [f"u-{i}" for i in range(2500)]
        store.add_chunk(_chunk(uuids))
        assert set(store.get_embedded_hashes()) == set(uuids)

    def test_truncated_flag_roundtrips(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2"], truncated=[True, False]))
        assert dict(_select(uri, "SELECT uuid, truncated FROM {table}")) == {
            "u-1": True,
            "u-2": False,
        }

    def test_the_vector_roundtrips_exactly(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1", "u-2"]))
        vectors = dict(_select(uri, "SELECT uuid, embedding FROM {table}"))
        assert vectors == {"u-1": (1.0,) * DIM, "u-2": (2.0,) * DIM}

    def test_optimize_is_safe_on_tiny_tables(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2"]))
        store.optimize()  # must not raise
        assert lance_store.count_rows(uri) == 2

    def test_no_vector_index_is_built(self, tmp_path: Path) -> None:
        """Search is an exact scan, so embed builds no index (the store used to carry one)."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk([f"u-{i}" for i in range(300)]))
        store.optimize()
        dataset = uri / f"{lance_store.TABLE_NAME}.lance"
        assert _select(uri, f"SHOW INDEXES ON '{dataset}'") == []

    def test_close_is_idempotent_and_the_store_reopens(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"]))
        store.close()
        store.close()
        store.add_chunk(_chunk(["u-2"]))
        assert set(store.get_embedded_hashes()) == {"u-1", "u-2"}


class TestRoundTripThroughTheExtension:
    """Create, append, read back, delete, optimize and vacuum, read again.

    A second connection, opened before the first write and reused for every
    read, must see each committed write: the store's equivalent of
    lancedb's ``read_consistency_interval=timedelta(0)``, without which a
    long-lived reader would answer from a stale version.
    """

    def test_full_cycle_is_visible_to_a_second_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uri = tmp_path / "lance"
        # Vacuum everything but the latest version, so the test can see it act.
        monkeypatch.setattr(lance_store, "VERSION_RETENTION_SECONDS", 0)
        writer = LanceVectorStore(uri, dim=DIM)
        writer.add_chunk(_chunk(["seed"]))
        with lance_store.connect_db(uri) as reader:

            def read() -> dict[str, str]:
                return dict(reader.con.execute(f"SELECT uuid, text_hash FROM {TABLE}").fetchall())

            uuids = [f"u-{i}" for i in range(40)]
            writer.add_chunk(_chunk(uuids))
            assert writer.table_identity() == ("fake-model:1", DIM)
            assert read() == {u: f"h-{u}" for u in ["seed", *uuids]}
            assert writer.get_embedded_hashes() == read()

            gone = uuids[::2]
            assert writer.delete_uuids(gone) == len(gone)
            expected = {u: f"h-{u}" for u in ["seed", *uuids] if u not in gone}
            assert read() == expected

            before = len(_manifests(uri))
            writer.optimize()
            assert len(_manifests(uri)) < before
            assert read() == expected
            assert writer.get_embedded_hashes() == expected
            assert writer.table_identity() == ("fake-model:1", DIM)
        writer.close()


class TestNewStoreFormat:
    """A new store must be one atif-sql 0.5.0 (lancedb 0.39) could have written itself."""

    def test_the_columns_and_nullability_match_lance_schema(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1"]))
        with lance_store.connect_db(uri) as conn:
            described = conn.con.execute(f"DESCRIBE {TABLE}").fetchall()
            exported = conn.con.sql(f"SELECT * FROM {TABLE} LIMIT 0").to_arrow_table().schema
        expected = lance_store.lance_schema(DIM)
        assert [row[0] for row in described] == expected.names
        # DESCRIBE's third column is "null": every column is NOT NULL, as lancedb created them.
        assert [row[2] for row in described] == ["NO"] * len(expected.names)
        assert [exported.field(name).type for name in expected.names] == [
            expected.field(name).type for name in expected.names
        ]

    def test_a_new_store_is_created_at_the_pinned_storage_version(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1"]))
        assert _storage_versions(uri) == {lance_store.DATA_STORAGE_VERSION}

    def test_the_connection_runs_in_utc(self, tmp_path: Path) -> None:
        with lance_store.connect_db(tmp_path / "lance") as conn:
            assert conn.con.execute("SELECT current_setting('TimeZone')").fetchall() == [("UTC",)]

    def test_the_connection_is_bounded(self, tmp_path: Path) -> None:
        """A bare connection takes 80% of the host's RAM and spills into ``./.tmp``."""
        settings = (
            "current_setting('memory_limit'), current_setting('threads'), "
            "current_setting('temp_directory')"
        )
        reference = duckdb.connect(config={"memory_limit": f"{lance_store.WRITER_MEMORY_BYTES}B"})
        try:
            expected_limit = reference.execute(f"SELECT {settings}").fetchall()[0][0]
            bare_limit = duckdb.connect().execute(f"SELECT {settings}").fetchall()[0][0]
        finally:
            reference.close()
        with lance_store.connect_db(tmp_path / "lance") as conn:
            limit, threads, temp_directory = conn.con.execute(f"SELECT {settings}").fetchall()[0]
            spill = Path(str(temp_directory))
            assert spill.is_dir()
            assert spill.name.startswith("atif-sql-embed-")
        assert limit == expected_limit
        assert limit != bare_limit
        assert int(threads) == lance_store.WRITER_THREADS
        assert not spill.exists()

    def test_a_store_created_in_another_time_zone_still_stamps_utc(self, tmp_path: Path) -> None:
        """The session's zone goes into a new TIMESTAMPTZ column's Lance type.

        Run in a fresh interpreter so ``TZ`` is read at start-up, the way an
        operator's shell would set it.
        """
        uri = tmp_path / "lance"
        code = (
            "import sys\n"
            "from datetime import UTC, datetime\n"
            "from pathlib import Path\n"
            "import polars as pl\n"
            "from atif_embed.infrastructure.lance_store import LanceVectorStore\n"
            "row = {'uuid': ['u-1'], 'model': ['m:1'], 'dim': [2], 'embedding': [[1.0, 0.0]],\n"
            "       'embedded_at': [datetime.now(UTC)], 'text_hash': ['h'], 'truncated': [False]}\n"
            "schema = {'uuid': pl.Utf8, 'model': pl.Utf8, 'dim': pl.Int32,\n"
            "          'embedding': pl.Array(pl.Float32, 2), 'embedded_at': pl.Datetime('us', 'UTC'),\n"
            "          'text_hash': pl.Utf8, 'truncated': pl.Boolean}\n"
            "LanceVectorStore(Path(sys.argv[1]), dim=2).add_chunk(pl.DataFrame(row, schema=schema))\n"
        )
        env = {**os.environ, "TZ": "America/Chicago"}
        subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, this interpreter
            [sys.executable, "-c", code, str(uri)], check=True, env=env, capture_output=True
        )
        manifest = _manifests(uri)[-1].read_bytes()
        assert b"timestamp:us:UTC" in manifest
        assert b"America/Chicago" not in manifest


class TestDelete:
    def test_delete_removes_named_uuids_only(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2", "u-3"]))
        assert store.delete_uuids(["u-1", "u-3"]) == 2
        assert set(store.get_embedded_hashes()) == {"u-2"}

    def test_delete_of_empty_iterable_is_a_noop(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        store.add_chunk(_chunk(["u-1"]))
        assert store.delete_uuids([]) == 0
        assert set(store.get_embedded_hashes()) == {"u-1"}

    def test_delete_of_many_uuids_in_one_call(self, tmp_path: Path) -> None:
        """Past four values DuckDB plans ``IN (...)`` as a join, which the extension's DELETE refuses."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        uuids = [f"u-{i}" for i in range(1200)]
        store.add_chunk(_chunk(uuids))
        gone = uuids[:1000]
        assert store.delete_uuids([*gone, "never-stored"]) == len(gone) + 1
        assert set(store.get_embedded_hashes()) == set(uuids[1000:])

    def test_duplicate_names_are_deleted_once(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2"]))
        assert store.delete_uuids(["u-1", "u-1"]) == 1
        assert set(store.get_embedded_hashes()) == {"u-2"}

    def test_delete_then_readd_leaves_exactly_one_row_per_uuid(self, tmp_path: Path) -> None:
        """Replace, not accumulate: two rows for one uuid fans out the kNN join."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"], hashes=["old-hash"]))
        store.delete_uuids(["u-1"])
        store.add_chunk(_chunk(["u-1"], hashes=["new-hash"]))
        assert lance_store.count_rows(uri) == 1
        assert store.get_embedded_hashes() == {"u-1": "new-hash"}

    def test_uuid_with_a_quote_is_deleted(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        store.add_chunk(_chunk(["u'1", "u-2"]))
        assert store.delete_uuids(["u'1"]) == 1
        assert set(store.get_embedded_hashes()) == {"u-2"}


def _write_pre_text_hash_store(uri: Path, *, uuids: list[str], dim: int = DIM) -> None:
    """Create a 5-column Lance store: the shape that shipped before ``text_hash``.

    Every column NOT NULL, the way lancedb created it.
    """
    uri.mkdir(parents=True, exist_ok=True)
    with lance_store.connect_db(uri) as conn:
        conn.con.execute(
            f"CREATE TABLE {TABLE} (uuid VARCHAR, model VARCHAR, dim INTEGER, "
            f"embedding FLOAT[{int(dim)}], embedded_at TIMESTAMPTZ)"
        )
        for name in _PRE_TEXT_HASH_COLUMNS:
            conn.con.execute(f"ALTER TABLE {TABLE} ALTER COLUMN {name} SET NOT NULL")
        legacy = _chunk(uuids, dim=dim).select(_PRE_TEXT_HASH_COLUMNS).to_arrow()
        conn.con.register("legacy_rows", legacy)
        conn.con.execute(f"INSERT INTO {TABLE} SELECT * FROM legacy_rows").fetchall()
        conn.con.unregister("legacy_rows")


class TestPreTextHashStore:
    """A store predating ``text_hash`` must migrate ONLINE, never via rebuild.

    ``get_embedded_hashes`` maps every pre-stamp row to a sentinel that can
    never equal a real blake2b digest, so the discovery anti-join re-picks
    every row as stale and the store heals itself incrementally — the
    2026-08-24 destroy-and-rebuild (~3.4M vectors of Cohere spend and a
    search outage across both production corpora) is the behavior this class
    forbids.
    """

    def test_get_embedded_hashes_returns_the_sentinel_map_without_raising(
        self, tmp_path: Path
    ) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        assert lance_store.get_embedded_hashes(uri) == {
            "u-1": lance_store._PRE_STAMP_SENTINEL,
            "u-2": lance_store._PRE_STAMP_SENTINEL,
        }

    def test_the_sentinel_can_never_equal_a_real_hash(self) -> None:
        """blake2b hex is [0-9a-f]+; the sentinel's angle brackets are not."""
        from atif_embed.domain.text_stamp import text_hash

        sentinel = lance_store._PRE_STAMP_SENTINEL
        assert not all(c in "0123456789abcdef" for c in sentinel)
        assert len(sentinel) != len(text_hash("anything"))

    def test_the_store_port_returns_the_sentinel_map_too(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        hashes = LanceVectorStore(uri, dim=DIM).get_embedded_hashes()
        assert hashes == {"u-1": lance_store._PRE_STAMP_SENTINEL}

    def test_reading_does_not_migrate(self, tmp_path: Path) -> None:
        """Only a write evolves the table; a dry run leaves the schema alone."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        LanceVectorStore(uri, dim=DIM).get_embedded_hashes()
        with lance_store.connect_db(uri) as conn:
            assert lance_store.column_names(conn) == _PRE_TEXT_HASH_COLUMNS

    def test_a_current_store_is_untouched_by_the_fallback(self, tmp_path: Path) -> None:
        """The fallback must be narrow: a 7-column store returns real hashes."""
        uri = tmp_path / "current"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"]))
        assert store.get_embedded_hashes() == {"u-1": "h-u-1"}

    def test_delete_by_uuid_works_on_the_pre_stamp_schema(self, tmp_path: Path) -> None:
        """The replace path's delete is uuid-only, so it must not
        depend on the columns the legacy schema lacks."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        with lance_store.connect_db(uri) as conn:
            assert lance_store.delete_uuids(conn, ["u-1"]) == 1
        assert set(lance_store.get_embedded_hashes(uri)) == {"u-2"}


class TestPreStampMigration:
    """Opening a pre-stamp table for WRITING evolves it in place.

    The INSERT names all seven columns, so ``open_or_create_table`` adds the
    missing ones first, with ``ADD COLUMN ... DEFAULT`` back-filling the
    sentinel/default. No rows are dropped and no vectors change.
    """

    def _open(self, uri: Path) -> StoreConnection:
        conn = lance_store.connect_db(uri)
        lance_store.open_or_create_table(conn, dim=DIM)
        return conn

    def test_migration_adds_the_columns_without_dropping_rows(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        with self._open(uri) as conn:
            assert lance_store.column_names(conn) == lance_store.lance_schema(DIM).names
        assert lance_store.count_rows(uri) == 2

    def test_migrated_columns_are_not_null_like_lancedbs(self, tmp_path: Path) -> None:
        """lancedb's ``add_columns`` left both columns NOT NULL; so must this migration."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        with self._open(uri) as conn:
            described = conn.con.execute(f"DESCRIBE {TABLE}").fetchall()
        assert [row[2] for row in described] == ["NO"] * len(described)

    def test_migrated_rows_carry_the_sentinel_and_default(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        self._open(uri).close()
        assert _select(uri, "SELECT uuid, text_hash, truncated FROM {table}") == [
            ("u-1", lance_store._PRE_STAMP_SENTINEL, False)
        ]

    def test_migration_leaves_the_vectors_alone(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        before = sorted(_select(uri, "SELECT uuid, embedding FROM {table}"))
        self._open(uri).close()
        assert sorted(_select(uri, "SELECT uuid, embedding FROM {table}")) == before

    def test_append_with_the_full_schema_works_after_migration(self, tmp_path: Path) -> None:
        """The exact incident shape: evolve, then append current-schema rows."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-2"]))
        assert store.get_embedded_hashes() == {
            "u-1": lance_store._PRE_STAMP_SENTINEL,
            "u-2": "h-u-2",
        }

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        self._open(uri).close()
        self._open(uri).close()  # second open must be a no-op, not a duplicate column
        assert lance_store.count_rows(uri) == 1

    def test_a_half_finished_migration_is_finished_on_the_next_open(self, tmp_path: Path) -> None:
        """A run stopped between the two ADD COLUMNs leaves a table the next open completes."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        with lance_store.connect_db(uri) as conn:
            conn.con.execute(f"ALTER TABLE {TABLE} ADD COLUMN text_hash VARCHAR DEFAULT 'x'")
        with self._open(uri) as conn:
            assert lance_store.column_names(conn) == lance_store.lance_schema(DIM).names
        assert _select(uri, "SELECT text_hash, truncated FROM {table}") == [("x", False)]

    def test_migration_stamps_the_schema_version_sidecar(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        assert lance_store.read_schema_version(uri) is None
        self._open(uri).close()
        assert lance_store.read_schema_version(uri) == lance_store.SCHEMA_VERSION


class TestSchemaVersionSidecar:
    def test_create_writes_the_sidecar(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1"]))
        assert lance_store.read_schema_version(uri) == lance_store.SCHEMA_VERSION
        assert (uri / lance_store.SCHEMA_VERSION_FILE).is_file()

    def test_missing_sidecar_reads_as_none(self, tmp_path: Path) -> None:
        assert lance_store.read_schema_version(tmp_path / "nowhere") is None

    def test_garbage_sidecar_reads_as_none(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        uri.mkdir(parents=True)
        (uri / lance_store.SCHEMA_VERSION_FILE).write_text("not json")
        assert lance_store.read_schema_version(uri) is None

    def test_current_schema_without_sidecar_gets_stamped_on_open(self, tmp_path: Path) -> None:
        """A v2-schema store written before the sidecar existed self-stamps."""
        uri = tmp_path / "lance"
        LanceVectorStore(uri, dim=DIM).add_chunk(_chunk(["u-1"]))
        (uri / lance_store.SCHEMA_VERSION_FILE).unlink()
        with lance_store.connect_db(uri) as conn:
            lance_store.open_or_create_table(conn, dim=DIM)
        assert lance_store.read_schema_version(uri) == lance_store.SCHEMA_VERSION


class _RefusingCon:
    """A DuckDB connection that records statements and refuses those starting with a prefix."""

    def __init__(self, con: Any, statements: list[str], refuse: str) -> None:
        self._con = con
        self._statements = statements
        self._refuse = refuse

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(sql)
        if sql.startswith(self._refuse):
            problem = "Lance error: Retryable commit conflict for version 7"
            raise duckdb.TransactionException(problem)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


class TestOptimize:
    def test_vacuum_still_runs_when_optimize_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A conflict that costs OPTIMIZE its commit mustn't also cost the cleanup."""
        monkeypatch.setattr(lance_store, "VERSION_RETENTION_SECONDS", 0)
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        for name in ("a", "b", "c", "d"):
            store.add_chunk(_chunk([name]))
        store.close()
        versions = uri / lance_store.DATASET_DIR / "_versions"
        before = len(list(versions.glob("*.manifest")))
        statements: list[str] = []
        with lance_store.connect_db(uri) as conn:
            conn._con = _RefusingCon(conn._con, statements, "OPTIMIZE")  # pyright: ignore[reportAttributeAccessIssue]
            lance_store.optimize_if_needed(conn)
        assert [sql.split()[0] for sql in statements] == ["OPTIMIZE", "VACUUM"]
        assert len(list(versions.glob("*.manifest"))) < before
        assert lance_store.count_rows(uri) == 4


class TestUnreadableStore:
    """A dataset on disk that the extension can't open is an error, never an empty store."""

    def _damaged(self, tmp_path: Path) -> Path:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        for name in ("a", "b", "c"):
            store.add_chunk(_chunk([name]))
        store.close()
        truncate_newest_manifest(uri)
        return uri

    def test_has_table_raises_instead_of_reading_as_empty(self, tmp_path: Path) -> None:
        uri = self._damaged(tmp_path)
        with lance_store.connect_db(uri) as conn, pytest.raises(EmbeddingStoreUnreadable) as info:
            lance_store.has_table(conn)
        assert info.value.terminal is True
        assert str(uri / lance_store.DATASET_DIR) in str(info.value)

    def test_every_reader_raises(self, tmp_path: Path) -> None:
        uri = self._damaged(tmp_path)
        for read in (
            lance_store.count_rows,
            lance_store.table_identity,
            lance_store.get_embedded_hashes,
        ):
            with pytest.raises(EmbeddingStoreUnreadable):
                read(uri)

    def test_the_writer_raises_before_any_write(self, tmp_path: Path) -> None:
        uri = self._damaged(tmp_path)
        store = LanceVectorStore(uri, dim=DIM)
        with pytest.raises(EmbeddingStoreUnreadable):
            store.add_chunk(_chunk(["d"]))
        store.close()

    def test_a_store_holding_only_its_sidecars_still_reads_as_empty(self, tmp_path: Path) -> None:
        """No dataset directory, so nothing is damaged: the first write creates the table."""
        uri = tmp_path / "lance"
        uri.mkdir()
        (uri / lance_store.SCHEMA_VERSION_FILE).write_text('{"schema_version": 2}')
        (uri / "lake_watermark.json").write_text("{}")
        assert lance_store.count_rows(uri) == 0
        assert lance_store.table_identity(uri) is None
        assert lance_store.get_embedded_hashes(uri) == {}


class _RecordingConnection:
    """A DuckDB connection whose statements are recorded, and optionally refused."""

    def __init__(self, con: Any, statements: list[str], refuse: str | None) -> None:
        self._con = con
        self._statements = statements
        self._refuse = refuse

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(sql)
        if self._refuse is not None and sql.startswith(self._refuse):
            problem = "Failed to download extension"
            raise duckdb.IOException(problem)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def _reads_as_absent(con: object) -> bool:
    """Stands in for ``lance_extension_installed``: the extension is never there."""
    del con
    return False


def _record_connections(
    monkeypatch: pytest.MonkeyPatch, statements: list[str], *, refuse: str | None
) -> None:
    """Make every ``duckdb.connect()`` a :class:`_RecordingConnection`."""
    real_connect = duckdb.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        return _RecordingConnection(real_connect(*args, **kwargs), statements, refuse)

    monkeypatch.setattr(duckdb, "connect", connect)


class TestExtensionLoading:
    """The store opens only through the extension, and only a real run downloads it."""

    @pytest.fixture
    def absent(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """The extension reads as absent; every statement the store runs is recorded."""
        statements: list[str] = []
        monkeypatch.setattr(lance_extension, "lance_extension_installed", _reads_as_absent)
        _record_connections(monkeypatch, statements, refuse=None)
        return statements

    def test_a_read_only_open_refuses_without_installing(
        self, tmp_path: Path, absent: list[str]
    ) -> None:
        uri = tmp_path / "lance"
        uri.mkdir()
        with pytest.raises(LanceExtensionMissing, match="--install-extension") as excinfo:
            LanceVectorStore(uri, dim=DIM).get_embedded_hashes()
        assert excinfo.value.terminal is True
        assert not any(s.startswith("INSTALL") for s in absent)

    def test_a_missing_store_needs_no_extension(self, tmp_path: Path, absent: list[str]) -> None:
        assert LanceVectorStore(tmp_path / "lance", dim=DIM).get_embedded_hashes() == {}
        assert absent == []

    def test_a_real_run_installs_then_loads(self, tmp_path: Path, absent: list[str]) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM, install_extension=True)
        store.add_chunk(_chunk(["u-1"]))
        name = lance_extension.LANCE_EXTENSION
        assert [s for s in absent if s.split()[0] in {"INSTALL", "LOAD"}] == [
            f"INSTALL {name};",
            f"LOAD {name};",
        ]
        store.close()

    def test_a_failed_download_is_transient(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lance_extension, "lance_extension_installed", _reads_as_absent)
        _record_connections(monkeypatch, [], refuse="INSTALL")
        store = LanceVectorStore(tmp_path / "lance", dim=DIM, install_extension=True)
        with pytest.raises(LanceExtensionInstallFailed, match="Failed to download") as excinfo:
            store.add_chunk(_chunk(["u-1"]))
        assert excinfo.value.terminal is False


class TestIdentityGuard:
    def test_empty_store_is_claimable(self) -> None:
        ensure_store_matches(
            stored_model=None,
            stored_dim=None,
            expected_model="anything",
            expected_dim=1024,
        )

    def test_matching_identity_passes(self) -> None:
        ensure_store_matches(
            stored_model="fake-model:1",
            stored_dim=DIM,
            expected_model="fake-model:1",
            expected_dim=DIM,
        )

    def test_model_drift_raises(self) -> None:
        with pytest.raises(EmbeddingProviderMismatch, match="different provider/model"):
            ensure_store_matches(
                stored_model="fake-model:1",
                stored_dim=DIM,
                expected_model="other-model:2",
                expected_dim=DIM,
            )

    def test_dim_drift_raises(self) -> None:
        with pytest.raises(EmbeddingProviderMismatch, match="dim=8"):
            ensure_store_matches(
                stored_model="fake-model:1",
                stored_dim=DIM,
                expected_model="fake-model:1",
                expected_dim=1024,
            )

    def test_none_expected_dim_trusts_model_alone(self) -> None:
        ensure_store_matches(
            stored_model="fake-model:1",
            stored_dim=DIM,
            expected_model="fake-model:1",
            expected_dim=None,
        )

    def test_guard_fires_from_real_store_stamp(self, tmp_path: Path) -> None:
        """End-to-end: a real store's stamp must trip the guard on drift."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"]))
        identity = store.table_identity()
        assert identity is not None
        stored_model, stored_dim = identity
        with pytest.raises(EmbeddingProviderMismatch):
            ensure_store_matches(
                stored_model=stored_model,
                stored_dim=stored_dim,
                expected_model="global.cohere.embed-v4:0",
                expected_dim=1024,
            )


class TestRecoveryHint:
    """The hint must name the path the store is ACTUALLY at.

    The default is ``<corpus_root>/embeddings_lance`` (see
    ``EmbedSettings.resolve_lance_uri``); naming a home directory instead
    would have the operator delete nothing and stay broken.
    """

    def test_hint_names_the_corpus_relative_default(self) -> None:
        assert "<corpus-root>/embeddings_lance" in RECOVERY_HINT

    def test_hint_does_not_name_a_home_directory_store(self) -> None:
        assert "~/.atif-sql" not in RECOVERY_HINT

    def test_hint_names_the_env_override(self) -> None:
        assert "ATIF_SQL_LANCE_URI" in RECOVERY_HINT

    def test_mismatch_message_carries_the_hint(self) -> None:
        with pytest.raises(EmbeddingProviderMismatch) as excinfo:
            ensure_store_matches(
                stored_model="a",
                stored_dim=DIM,
                expected_model="b",
                expected_dim=DIM,
            )
        assert RECOVERY_HINT in str(excinfo.value)

    def test_hint_matches_the_resolved_default_directory_name(self, tmp_path: Path) -> None:
        """Pin the hint to the settings that actually pick the directory."""
        from atif_embed.infrastructure.settings import EmbedSettings

        # `_env_file` keeps a developer's real .env out of this assertion.
        # pydantic-settings 2.15.0 declares it on `BaseSettings.__init__`, but
        # pydantic's @dataclass_transform makes pyright synthesize a fresh
        # __init__ from the model fields for every subclass, which shadows it.
        resolved = EmbedSettings(_env_file=None, lance_uri=None).resolve_lance_uri(  # pyright: ignore[reportCallIssue]
            tmp_path
        )
        assert resolved == tmp_path / "embeddings_lance"
        assert resolved.name in RECOVERY_HINT

    def test_a_leftover_hnsw_metric_variable_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``hnsw_metric`` is gone; a user who still exports it must not crash."""
        from atif_embed.infrastructure.settings import EmbedSettings

        monkeypatch.setenv("ATIF_SQL_HNSW_METRIC", "cosine")
        settings = EmbedSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
        assert not hasattr(settings, "hnsw_metric")
