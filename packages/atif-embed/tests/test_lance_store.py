# SPDX-License-Identifier: Apache-2.0

"""Lance store roundtrip + identity guard over a real lancedb tmp dir.

lancedb is a local library (no network), so these tests run it for real —
the roundtrip through pyarrow fixed-size lists is exactly the surface that
breaks silently under mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from atif_embed.domain.embedding_guard import RECOVERY_HINT, ensure_store_matches
from atif_embed.domain.errors import EmbeddingProviderMismatch
from atif_embed.infrastructure import lance_store
from atif_embed.infrastructure.lance_store import LanceVectorStore

DIM = 8

#: Column set of a store written before the ``text_hash`` staleness stamp.
_PRE_TEXT_HASH_COLUMNS = ["uuid", "model", "dim", "embedding", "embedded_at"]


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

    def test_get_embedded_hashes_beyond_default_query_cap(self, tmp_path: Path) -> None:
        """The explicit limit(count_rows()) must defeat LanceDB's 10-row default."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        uuids = [f"u-{i}" for i in range(25)]
        store.add_chunk(_chunk(uuids))
        assert set(store.get_embedded_hashes()) == set(uuids)

    def test_truncated_flag_roundtrips(self, tmp_path: Path) -> None:
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2"], truncated=[True, False]))
        db = lance_store.connect_db(uri)
        tbl = db.open_table(lance_store.TABLE_NAME)
        arrow = tbl.search().select(["uuid", "truncated"]).limit(10).to_arrow()
        flags = dict(
            zip(
                arrow.column("uuid").to_pylist(),
                arrow.column("truncated").to_pylist(),
                strict=True,
            )
        )
        assert flags == {"u-1": True, "u-2": False}

    def test_optimize_and_ensure_index_are_safe_on_tiny_tables(self, tmp_path: Path) -> None:
        """Index creation on a tiny table falls back to brute-force, not an error."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1", "u-2"]))
        store.optimize()
        store.ensure_index(metric="cosine")  # must not raise

    def test_ensure_index_rejects_bad_metric(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        with pytest.raises(ValueError, match="Unsupported Lance metric"):
            store.ensure_index(metric="manhattan")


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

    def test_delete_then_readd_leaves_exactly_one_row_per_uuid(self, tmp_path: Path) -> None:
        """Replace, not accumulate: two rows for one uuid fans out the kNN join."""
        uri = tmp_path / "lance"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"], hashes=["old-hash"]))
        store.delete_uuids(["u-1"])
        store.add_chunk(_chunk(["u-1"], hashes=["new-hash"]))
        assert lance_store.count_rows(uri) == 1
        assert store.get_embedded_hashes() == {"u-1": "new-hash"}

    def test_uuid_with_a_quote_is_escaped(self, tmp_path: Path) -> None:
        store = LanceVectorStore(tmp_path / "lance", dim=DIM)
        store.add_chunk(_chunk(["u'1", "u-2"]))
        assert store.delete_uuids(["u'1"]) == 1
        assert set(store.get_embedded_hashes()) == {"u-2"}


def _write_pre_text_hash_store(uri: Path, *, uuids: list[str], dim: int = DIM) -> None:
    """Create a 5-column Lance store: the shape that shipped before ``text_hash``."""
    import lancedb
    import pyarrow as pa

    full = lance_store.lance_schema(dim)
    legacy = pa.schema([full.field(name) for name in _PRE_TEXT_HASH_COLUMNS])
    uri.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(uri))
    tbl = db.create_table(lance_store.TABLE_NAME, schema=legacy, mode="create")
    now = datetime.now(UTC)
    tbl.add(
        pa.table(
            {
                "uuid": uuids,
                "model": ["fake-model:1"] * len(uuids),
                "dim": [dim] * len(uuids),
                "embedding": [[float(i + 1)] * dim for i in range(len(uuids))],
                "embedded_at": [now] * len(uuids),
            },
            schema=legacy,
        )
    )


class TestPreTextHashStore:
    """A store predating ``text_hash`` must migrate ONLINE, never via rebuild.

    ``get_embedded_hashes`` maps every pre-stamp row to a sentinel that can
    never equal a real blake2b digest, so the discovery anti-join re-picks
    every row as stale and the store heals itself incrementally — the
    2026-08-24 destroy-and-rebuild (~3.4M vectors of Cohere spend and a
    search outage across both fleet corpora) is the behavior this class
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

    def test_a_current_store_is_untouched_by_the_fallback(self, tmp_path: Path) -> None:
        """The fallback must be narrow: a 7-column store returns real hashes."""
        uri = tmp_path / "current"
        store = LanceVectorStore(uri, dim=DIM)
        store.add_chunk(_chunk(["u-1"]))
        assert store.get_embedded_hashes() == {"u-1": "h-u-1"}

    def test_delete_by_uuid_works_on_the_pre_stamp_schema(self, tmp_path: Path) -> None:
        """The replace path's delete predicate is uuid-only, so it must not
        depend on the columns the legacy schema lacks."""
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        db = lance_store.connect_db(uri)
        tbl = db.open_table(lance_store.TABLE_NAME)
        assert lance_store.delete_uuids(tbl, ["u-1"]) == 1
        assert set(lance_store.get_embedded_hashes(uri)) == {"u-2"}


class TestPreStampMigration:
    """Opening a pre-stamp table for WRITING evolves it in place.

    Lance rejects a 7-column append into a 5-column table, so
    ``open_or_create_table`` adds the missing columns first — a
    metadata-only ``add_columns`` backfilled with the sentinel/default. No
    rows are dropped and no vectors change.
    """

    def _open(self, uri: Path) -> Any:
        db = lance_store.connect_db(uri)
        return lance_store.open_or_create_table(db, dim=DIM)

    def test_migration_adds_the_columns_without_dropping_rows(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1", "u-2"])
        tbl = self._open(uri)
        assert set(tbl.schema.names) == set(lance_store.lance_schema(DIM).names)
        assert lance_store.count_rows(uri) == 2

    def test_migrated_rows_carry_the_sentinel_and_default(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        tbl = self._open(uri)
        arrow = tbl.search().select(["uuid", "text_hash", "truncated"]).limit(10).to_arrow()
        assert arrow.column("text_hash").to_pylist() == [lance_store._PRE_STAMP_SENTINEL]
        assert arrow.column("truncated").to_pylist() == [False]

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
        self._open(uri)
        self._open(uri)  # second open must be a no-op, not a duplicate column
        assert lance_store.count_rows(uri) == 1

    def test_migration_stamps_the_schema_version_sidecar(self, tmp_path: Path) -> None:
        uri = tmp_path / "legacy"
        _write_pre_text_hash_store(uri, uuids=["u-1"])
        assert lance_store.read_schema_version(uri) is None
        self._open(uri)
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
        db = lance_store.connect_db(uri)
        lance_store.open_or_create_table(db, dim=DIM)
        assert lance_store.read_schema_version(uri) == lance_store.SCHEMA_VERSION


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
