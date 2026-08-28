# SPDX-License-Identifier: Apache-2.0

"""Embed use case over a FakeEmbedder + real Lance store in a tmp dir."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from embed_fixtures import (
    EXPECTED_EMBEDDABLE_UUIDS,
    LONG_USER_TEXT,
    FakeEmbedder,
    rewrite_step_text,
)

from atif_embed.application.embed import discover_unembedded, run_backfill
from atif_embed.domain.errors import EmbeddingProviderMismatch
from atif_embed.domain.text_stamp import MAX_EMBEDDABLE_CHARS, PendingText, text_hash
from atif_embed.infrastructure import lance_store
from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows
from atif_embed.infrastructure.lance_store import LanceVectorStore
from atif_embed.infrastructure.settings import EmbedSettings

if TYPE_CHECKING:
    from collections.abc import Iterator

DIM = 8


@pytest.fixture
def settings(tmp_path: Path) -> EmbedSettings:
    return EmbedSettings(lance_uri=tmp_path / "lance", batch_size=2, embed_concurrency=2)


@pytest.fixture
def store(settings: EmbedSettings, corpus_root: Path) -> LanceVectorStore:
    return LanceVectorStore(settings.resolve_lance_uri(corpus_root), dim=DIM)


def _stored_flags(uri: Path) -> dict[str, bool]:
    db = lance_store.connect_db(uri)
    tbl = db.open_table(lance_store.TABLE_NAME)
    arrow = tbl.search().select(["uuid", "truncated"]).limit(1000).to_arrow()
    return dict(
        zip(arrow.column("uuid").to_pylist(), arrow.column("truncated").to_pylist(), strict=True)
    )


class TestDiscover:
    def test_anti_join_against_store(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        embedder = FakeEmbedder(dim=DIM)
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=embedder,
                store=store,
                limit=2,
            )
        )
        pending = list(
            discover_unembedded(corpus_root, text_rows=DuckDbTextRows(), store=store, limit=None)
        )
        assert [p.uuid for p in pending] == EXPECTED_EMBEDDABLE_UUIDS[2:]


class TestRunBackfill:
    def test_backfill_writes_all_rows_with_identity_stamp(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        embedder = FakeEmbedder(dim=DIM)
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=embedder,
                store=store,
            )
        )
        assert written == len(EXPECTED_EMBEDDABLE_UUIDS)
        assert set(store.get_embedded_hashes()) == set(EXPECTED_EMBEDDABLE_UUIDS)
        assert store.table_identity() == ("fake-model:1", DIM)

    def test_rows_stamp_the_hash_of_the_text_embedded(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        stored = store.get_embedded_hashes()
        current = {
            p.uuid: p.text_hash for p in DuckDbTextRows().iter_unembedded(corpus_root, embedded={})
        }
        assert stored == current

    def test_backfill_is_idempotent(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        embedder = FakeEmbedder(dim=DIM)
        first = asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        second = asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        assert first == len(EXPECTED_EMBEDDABLE_UUIDS)
        assert second == 0  # everything already embedded

    def test_limit_caps_the_run(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        embedder = FakeEmbedder(dim=DIM)
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=embedder,
                store=store,
                limit=3,
            )
        )
        assert written == 3
        assert len(store.get_embedded_hashes()) == 3

    def test_dry_run_returns_plan_and_writes_nothing(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        plan = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                store=store,
                dry_run=True,
            )
        )
        assert isinstance(plan, dict)
        assert plan["pipeline"] == "embed"
        assert plan["candidates"] == len(EXPECTED_EMBEDDABLE_UUIDS)
        assert plan["dry_run"] is True
        assert plan["model"] == "global.cohere.embed-v4:0"
        assert store.get_embedded_hashes() == {}

    def test_write_guard_fires_on_provider_switch(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        """Guards must fire: seed the store with one provider, append with another."""
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(model_id="fake-model:1", dim=DIM),
                store=store,
                limit=1,
            )
        )
        with pytest.raises(EmbeddingProviderMismatch):
            asyncio.run(
                run_backfill(
                    corpus_root=corpus_root,
                    settings=settings,
                    embedder=FakeEmbedder(model_id="other-model:2", dim=DIM),
                    store=store,
                )
            )


class TestReconversionStaleness:
    """A re-converted step must be re-embedded, and its old row replaced.

    Without the hash stamp the uuid-only anti-join calls the row embedded
    forever and semantic search keeps ranking against pre-fix text.
    """

    def _seed(self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings) -> None:
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )

    def test_changed_text_is_re_embedded(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        self._seed(corpus_root, store, settings)
        new_text = LONG_USER_TEXT + " Now with the corrected tail that the fix recovered."
        rewrite_step_text(corpus_root, "ua-1", new_text)
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert written == 1
        assert store.get_embedded_hashes()["ua-1"] == text_hash(new_text)

    def test_stale_row_is_replaced_not_duplicated(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        """Two rows under one uuid would fan the kNN join out across both."""
        self._seed(corpus_root, store, settings)
        uri = settings.resolve_lance_uri(corpus_root)
        before = lance_store.count_rows(uri)
        rewrite_step_text(corpus_root, "ua-1", LONG_USER_TEXT + " Corrected.")
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert lance_store.count_rows(uri) == before

    def test_the_new_vector_reflects_the_new_text(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        self._seed(corpus_root, store, settings)
        new_text = LONG_USER_TEXT + " Corrected with completely different content."
        rewrite_step_text(corpus_root, "ua-1", new_text)
        embedder = FakeEmbedder(dim=DIM)
        asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        uri = settings.resolve_lance_uri(corpus_root)
        db = lance_store.connect_db(uri)
        tbl = db.open_table(lance_store.TABLE_NAME)
        arrow = tbl.search().select(["uuid", "embedding"]).limit(1000).to_arrow()
        vectors = dict(
            zip(
                arrow.column("uuid").to_pylist(),
                arrow.column("embedding").to_pylist(),
                strict=True,
            )
        )
        expected = embedder._vector(new_text)
        assert vectors["ua-1"] == pytest.approx(expected, abs=1e-6)

    def test_unchanged_siblings_are_not_re_embedded(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        self._seed(corpus_root, store, settings)
        rewrite_step_text(corpus_root, "ua-1", LONG_USER_TEXT + " Corrected.")
        embedder = FakeEmbedder(dim=DIM)
        asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        assert [len(call) for call in embedder.document_calls] == [1]


class TestPreStampOnlineMigration:
    """End-to-end: a pre-stamp store heals itself through ordinary runs.

    Seed the corpus's Lance directory with a 5-column legacy store whose
    uuids ARE current corpus rows, then run the normal backfill: discovery
    must mark every legacy row stale (``replaces_existing=True``), the store
    must evolve additively (no rows dropped, no empty-store window), and a
    second run must find candidates only for genuinely new/changed text.
    """

    def _seed_legacy(self, corpus_root: Path, settings: EmbedSettings) -> Path:
        from test_lance_store import _write_pre_text_hash_store

        uri = settings.resolve_lance_uri(corpus_root)
        _write_pre_text_hash_store(uri, uuids=EXPECTED_EMBEDDABLE_UUIDS, dim=DIM)
        return uri

    def test_discovery_marks_every_pre_stamp_row_stale(
        self, corpus_root: Path, settings: EmbedSettings
    ) -> None:
        self._seed_legacy(corpus_root, settings)
        store = LanceVectorStore(settings.resolve_lance_uri(corpus_root), dim=DIM)
        pending = list(
            discover_unembedded(corpus_root, text_rows=DuckDbTextRows(), store=store, limit=None)
        )
        assert [p.uuid for p in pending] == EXPECTED_EMBEDDABLE_UUIDS
        assert all(p.replaces_existing for p in pending)

    def test_backfill_migrates_and_replaces_without_a_rebuild(
        self, corpus_root: Path, settings: EmbedSettings
    ) -> None:
        uri = self._seed_legacy(corpus_root, settings)
        store = LanceVectorStore(uri, dim=DIM)
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert written == len(EXPECTED_EMBEDDABLE_UUIDS)
        # Replaced, not duplicated: one row per uuid, all with real hashes.
        assert lance_store.count_rows(uri) == len(EXPECTED_EMBEDDABLE_UUIDS)
        current = {
            p.uuid: p.text_hash for p in DuckDbTextRows().iter_unembedded(corpus_root, embedded={})
        }
        assert store.get_embedded_hashes() == current

    def test_the_store_is_never_empty_mid_migration(
        self, corpus_root: Path, settings: EmbedSettings
    ) -> None:
        """A --limit run migrates part of the store; the rest keeps serving."""
        uri = self._seed_legacy(corpus_root, settings)
        store = LanceVectorStore(uri, dim=DIM)
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
                limit=2,
            )
        )
        assert lance_store.count_rows(uri) == len(EXPECTED_EMBEDDABLE_UUIDS)
        hashes = store.get_embedded_hashes()
        migrated = [u for u, h in hashes.items() if h != lance_store._PRE_STAMP_SENTINEL]
        assert len(migrated) == 2

    def test_post_migration_run_finds_only_new_or_changed_text(
        self, corpus_root: Path, settings: EmbedSettings
    ) -> None:
        self._seed_legacy(corpus_root, settings)
        store = LanceVectorStore(settings.resolve_lance_uri(corpus_root), dim=DIM)
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        second = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert second == 0
        rewrite_step_text(corpus_root, "ua-1", LONG_USER_TEXT + " Changed after migration.")
        third = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert third == 1


class TestTruncationStamp:
    def test_long_text_is_flagged_on_the_row(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        """A capped vector must be distinguishable from a full one."""
        rewrite_step_text(corpus_root, "ua-1", "y" * (MAX_EMBEDDABLE_CHARS + 10))
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        flags = _stored_flags(settings.resolve_lance_uri(corpus_root))
        assert flags["ua-1"] is True
        assert flags["aa-1"] is False

    def test_no_row_is_flagged_when_nothing_is_capped(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert not any(_stored_flags(settings.resolve_lance_uri(corpus_root)).values())


class TestBoundedLossInsideAChunk:
    """One failed batch must not discard the sibling batches' billed vectors."""

    def test_successful_rows_are_written_when_one_row_fails(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        from embed_fixtures import LONG_AGENT_TEXT

        embedder = FakeEmbedder(dim=DIM, fail_texts={LONG_AGENT_TEXT})
        written = asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        assert written == len(EXPECTED_EMBEDDABLE_UUIDS) - 1
        stored = set(store.get_embedded_hashes())
        assert "aa-1" not in stored
        assert stored == set(EXPECTED_EMBEDDABLE_UUIDS) - {"aa-1"}

    def test_the_failed_row_is_re_picked_next_run(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        from embed_fixtures import LONG_AGENT_TEXT

        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM, fail_texts={LONG_AGENT_TEXT}),
                store=store,
            )
        )
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert written == 1
        assert set(store.get_embedded_hashes()) == set(EXPECTED_EMBEDDABLE_UUIDS)

    def test_all_rows_failing_writes_nothing_and_does_not_raise(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        texts = {p.text for p in DuckDbTextRows().iter_unembedded(corpus_root, embedded={})}
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=FakeEmbedder(dim=DIM, fail_texts=texts),
                store=store,
            )
        )
        assert written == 0
        assert store.get_embedded_hashes() == {}


class _CountingTextRows:
    """TextRowsPort recording how many rows it has yielded so far.

    ``discover_unembedded`` must not drain this before the caller asks: the
    count observed after pulling k rows is the property under test.
    """

    def __init__(self, uuids: list[str]) -> None:
        self._uuids = uuids
        self.yielded = 0

    def iter_unembedded(
        self,
        corpus_root: Path,
        *,
        embedded: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[PendingText]:
        for uuid in self._uuids:
            if limit is not None and self.yielded >= limit:
                return
            self.yielded += 1
            text = f"text for {uuid} padded past the thirty-two character floor"
            yield PendingText(
                uuid=uuid,
                text=text,
                text_hash=text_hash(text),
                replaces_existing=False,
            )


class TestStreamingDiscovery:
    def test_discovery_yields_no_rows_before_the_caller_pulls(
        self, corpus_root: Path, store: LanceVectorStore
    ) -> None:
        """Building the iterator must cost ZERO rows, not merely return non-list."""
        rows = _CountingTextRows([f"u-{i}" for i in range(50)])
        pending = discover_unembedded(corpus_root, text_rows=rows, store=store)
        assert rows.yielded == 0
        iterator = iter(pending)
        next(iterator)
        assert rows.yielded == 1
        next(iterator)
        assert rows.yielded == 2

    def test_backfill_never_drains_discovery_ahead_of_a_chunk(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        """Rows pulled must track chunk progress, not jump to the full corpus.

        The chunk size is ``max(batch_size * 4, 256)``, so a corpus larger than
        one chunk proves the point: an adapter drained up front reports every
        row on the FIRST embed call.
        """
        n_rows = 700
        rows = _CountingTextRows([f"u-{i}" for i in range(n_rows)])
        observed: list[int] = []

        class _ObservingEmbedder(FakeEmbedder):
            @override
            async def embed_documents(self, texts: list[str]) -> list[list[float] | None]:
                observed.append(rows.yielded)
                return await super().embed_documents(texts)

        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=settings,
                embedder=_ObservingEmbedder(dim=DIM),
                text_rows=rows,
                store=store,
            )
        )

        assert written == n_rows
        chunk_size = max(settings.batch_size * 4, 256)
        assert observed, "the embedder was never called"
        assert observed[0] <= chunk_size
        assert observed[0] < n_rows
        assert observed == sorted(observed)
        assert len(observed) > 1, "corpus must span more than one chunk"

    def test_chunk_boundaries_do_not_lose_rows(
        self, corpus_root: Path, store: LanceVectorStore, settings: EmbedSettings
    ) -> None:
        """chunk_size below the corpus size must still write every row."""
        small = settings.model_copy(update={"batch_size": 1})
        written = asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=small,
                embedder=FakeEmbedder(dim=DIM),
                store=store,
            )
        )
        assert written == len(EXPECTED_EMBEDDABLE_UUIDS)
