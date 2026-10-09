# SPDX-License-Identifier: Apache-2.0

"""Embedding backfill use case.

Discovers steps with no CURRENT embedding (through :class:`TextRowsPort`,
against the Lance store's ``{uuid: text_hash}`` map), embeds them through the
:class:`EmbeddingProvider` port, and appends the vectors to the LanceDB store
keyed by the step's primary raw-record uuid.

The provider's ``model_id`` / ``dimension`` are stamped on every row and
enforced against the store's prior stamp (fail-loud on a provider switch:
mixing vector spaces silently corrupts kNN search). Loss is bounded at three
levels: discovery is consumed one chunk at a time, checkpoints of
``max(batch_size * 4, 256)`` rows commit progress mid-run, and a batch that
fails terminally inside a chunk does not discard the batches around it. A
Lance ``optimize()`` runs every 8 chunks and a final optimize +
``ensure_index`` on the way out.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from itertools import islice
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_embed.domain.embedding_guard import ensure_store_matches

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from atif_embed.domain.ports import EmbeddingProvider, TextRowsPort, VectorStorePort
    from atif_embed.domain.text_stamp import PendingText
    from atif_embed.infrastructure.settings import EmbedSettings

#: Optimize the Lance store after this many appended chunks.
_OPTIMIZE_EVERY_CHUNKS = 8


def discover_unembedded(
    corpus_root: Path,
    *,
    text_rows: TextRowsPort,
    store: VectorStorePort,
    limit: int | None = None,
) -> Iterator[PendingText]:
    """Yield the texts that have no CURRENT embedding.

    The store's ``{uuid: text_hash}`` map is read once (column-projected
    scan) and the staleness comparison happens inside the text-rows adapter,
    before the limit cap, so ``--limit N`` always makes N rows of forward
    progress. Lazy: the caller consumes one write-chunk at a time.
    """
    embedded = store.get_embedded_hashes()
    return text_rows.iter_unembedded(corpus_root, embedded=embedded, limit=limit)


async def run_backfill(
    *,
    corpus_root: Path,
    settings: EmbedSettings,
    embedder: EmbeddingProvider | None = None,
    text_rows: TextRowsPort | None = None,
    store: VectorStorePort | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> int | dict[str, Any]:
    """Discover unembedded steps, embed them, and append to the Lance store.

    A run that embedded every row its discovery yielded, and whose discovery
    ran to its end, calls ``text_rows.commit`` with the store's row count, so
    a reader with a watermark (the lake reader) can start the next run from
    there. A dry run never commits.

    Parameters
    ----------
    corpus_root
        Materialized corpus root (the directory containing ``sessions/``).
    settings
        Embedding settings (model, batch size, concurrency, lance uri).
    embedder
        Optional :class:`EmbeddingProvider`; defaults to the provider
        ``settings.embed_provider`` selects (imported lazily so dry runs never
        load boto3 or torch).
    text_rows
        Optional :class:`TextRowsPort`; defaults to the contract-layout
        DuckDB reader.
    store
        Optional :class:`VectorStorePort`; defaults to the Lance-backed store
        over ``settings.lance_uri``.
    limit
        Optional cap on the number of steps embedded this run.
    dry_run
        If true, log the plan and return a plan dict without embedding calls.

    Returns
    -------
    int | dict
        Under ``dry_run=True``, a plan dict with ``{pipeline, discovery,
        candidates, batches, batch_size, concurrency, provider, model, dim,
        store, limit, dry_run}``.
        Otherwise, count of newly written rows (0 when nothing is pending).
    """
    import polars as pl

    lance_uri = settings.resolve_lance_uri(corpus_root)
    if text_rows is None:
        from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows

        text_rows = DuckDbTextRows()
    if store is None:
        from atif_embed.infrastructure.lance_store import LanceVectorStore

        store = LanceVectorStore(lance_uri, dim=settings.embedding_dim)

    plan_model = settings.expected_embedding_identity()[0]
    batch_size = settings.active_batch_size
    stored = store.get_embedded_hashes()
    pending = text_rows.iter_unembedded(corpus_root, embedded=stored, limit=limit)

    if dry_run:
        candidates = sum(1 for _ in pending)
        n_batches = (candidates + batch_size - 1) // batch_size
        if candidates == 0:
            logger.info("No unembedded steps found - nothing to do")
        else:
            logger.info(
                "Backfill plan: {} steps, {} batches, concurrency={}, model={}; "
                "dry_run=True - skipping embedding calls",
                candidates,
                n_batches,
                settings.active_concurrency,
                plan_model,
            )
        return {
            "pipeline": "embed",
            "discovery": text_rows.discovery,
            "candidates": candidates,
            "batches": n_batches,
            "batch_size": batch_size,
            "concurrency": settings.active_concurrency,
            "provider": settings.embed_provider,
            "model": plan_model,
            "dim": settings.embedding_dim,
            "store": str(lance_uri),
            "limit": limit,
            "dry_run": True,
        }

    # Chunk must be a multiple of batch_size so a checkpoint boundary never
    # splits a provider batch.
    chunk_size = max(batch_size * 4, 256)
    rows = iter(pending)
    first_chunk = list(islice(rows, chunk_size))
    if not first_chunk:
        logger.info("No unembedded steps found - nothing to do")
        text_rows.commit(stored_rows=len(stored))
        return 0

    # Build the provider once for the whole run. dimension / model_id become
    # the single contract source. Deferred import keeps boto3 and torch off
    # the dry-run / nothing-pending paths above, which return before this point.
    if embedder is None:
        from atif_embed.infrastructure.providers import build_embedder

        embedder = build_embedder(settings)
    model_id = embedder.model_id
    dim = embedder.dimension

    # Fail-loud guard: if the store was written by a different provider/model,
    # refuse to append into it (mixing vector spaces silently corrupts kNN).
    identity = store.table_identity()
    if identity is not None:
        stored_model, stored_dim = identity
        ensure_store_matches(
            stored_model=stored_model,
            stored_dim=stored_dim,
            expected_model=model_id,
            expected_dim=dim,
        )

    total_t0 = time.monotonic()
    written = 0
    skipped = 0
    chunk_index = 0
    chunks_since_optimize = 0
    chunk: list[PendingText] | None = first_chunk
    while chunk:
        chunk_index += 1
        logger.info("Chunk {}: embedding {} steps", chunk_index, len(chunk))
        t0 = time.monotonic()
        vectors = await embedder.embed_documents([p.text for p in chunk])
        elapsed = time.monotonic() - t0

        embedded = [
            (pending_text, vector)
            for pending_text, vector in zip(chunk, vectors, strict=True)
            if vector is not None
        ]
        failed = len(chunk) - len(embedded)
        skipped += failed
        logger.info(
            "Chunk done in {:.1f}s ({:.1f} vec/s){}",
            elapsed,
            len(embedded) / elapsed if elapsed > 0 else 0.0,
            f"; {failed} rows failed and stay unembedded for the next run" if failed else "",
        )

        if embedded:
            # Replace before append: a stale row under the same uuid would
            # otherwise sit beside the new vector and fan the kNN join out
            # across both.
            stale = [p.uuid for p, _ in embedded if p.replaces_existing]
            if stale:
                deleted = store.delete_uuids(stale)
                logger.info("Replacing {} stale rows whose source text changed", deleted)
            now = datetime.now(UTC)
            # Force a fixed-size Array so to_arrow() produces
            # pa.list_(pa.float32, dim) — what Lance requires for vector
            # columns. A regular pl.List becomes a variable-size list and
            # Lance rejects it for indexing.
            df = pl.DataFrame(
                {
                    "uuid": [p.uuid for p, _ in embedded],
                    "model": [model_id] * len(embedded),
                    "dim": [dim] * len(embedded),
                    "embedding": [v for _, v in embedded],
                    "embedded_at": [now] * len(embedded),
                    "text_hash": [p.text_hash for p, _ in embedded],
                    "truncated": [p.truncated for p, _ in embedded],
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
            store.add_chunk(df)
            written += len(embedded)
            chunks_since_optimize += 1
            logger.info("Checkpoint: {} rows -> {}", len(df), lance_uri)

            # Compact periodically so fragment count stays bounded during long
            # backfills.
            if chunks_since_optimize >= _OPTIMIZE_EVERY_CHUNKS:
                store.optimize()
                chunks_since_optimize = 0

        chunk = list(islice(rows, chunk_size))

    # Final compaction + index ensure on the way out so the search command
    # sees an up-to-date index without paying brute-force scan latency.
    store.optimize()
    store.ensure_index(metric=settings.hnsw_metric)

    # A watermark may only move past rows that are all stored: a failed row
    # has to be found again next run, and a run cut short by --limit never
    # read the rest (the reader itself checks that second half).
    if skipped == 0:
        text_rows.commit(stored_rows=len(store.get_embedded_hashes()))

    total_elapsed = time.monotonic() - total_t0
    logger.info(
        "Backfill complete: {} embeddings in {:.1f}s ({:.1f} vec/s overall){}",
        written,
        total_elapsed,
        written / total_elapsed if total_elapsed > 0 else 0.0,
        f"; {skipped} rows failed and remain unembedded" if skipped else "",
    )
    return written


def embed_query(text: str, *, settings: EmbedSettings) -> list[float]:
    """Embed a single query string for nearest-neighbor search.

    Thin shim over the selected provider (imported lazily): returns a float
    query vector of length ``settings.embedding_dim``. Cohere forces ``float``
    for queries even when documents were stored int8; EmbeddingGemma 2 embeds
    under its ``SearchQuery`` prompt.
    """
    from atif_embed.infrastructure.providers import build_embedder

    return build_embedder(settings).embed_query(text)


__all__ = ["discover_unembedded", "embed_query", "run_backfill"]
