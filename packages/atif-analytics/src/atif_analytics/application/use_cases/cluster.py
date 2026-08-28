# SPDX-License-Identifier: Apache-2.0

"""Cluster message embeddings via UMAP + HDBSCAN.

Reads the lance embeddings store (atif-embed's output; SKIPS gracefully
when absent), fits UMAP 50d + HDBSCAN with the verbatim hyperparameters,
and writes ``clusters.parquet`` with ``(uuid, cluster_id, x, y, is_noise)``.
The mtime sidecar keyed on the lance store's newest fragment mtime skips
the refit when the embeddings have not moved.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
import polars as pl
from loguru import logger

from atif_analytics.domain.structure.cluster import cluster_embeddings
from atif_analytics.infrastructure.freshness import (
    is_output_fresh,
    newest_mtime_ns,
    sidecar_for,
    stamp_output,
)
from atif_analytics.infrastructure.lance_reader import load_embeddings

if TYPE_CHECKING:
    from atif_analytics.infrastructure.settings import AnalyticsSettings


def _stats_from_parquet(df: pl.DataFrame) -> dict[str, int]:
    """Derive the stats dict from an existing clusters parquet.

    ``clusters`` counts DISTINCT non-noise ids, NOT non-noise rows: a
    cache hit must report the same quantity a fresh run reports, and
    counting rows makes the same key mean two different things.
    ``noise`` counts rows labelled ``-1``.
    """
    real = df.filter(pl.col("cluster_id") >= 0)
    return {
        "total": len(df),
        "clusters": int(real["cluster_id"].n_unique()),
        "noise": int((df["cluster_id"] < 0).sum()),
    }


def run_clustering(settings: AnalyticsSettings, *, force: bool = False) -> dict[str, int]:
    """Run UMAP + HDBSCAN over the lance embeddings; write ``clusters.parquet``.

    Returns ``{"total": N, "clusters": K, "noise": M, "skipped": 0|1}``
    where K excludes the noise cluster. ``skipped=1`` marks the graceful
    no-embeddings path (the store is the VSS branch's output and may not
    exist yet — that is a normal state, not an error).
    """
    layout = settings.layout()
    out_path = layout.clusters_parquet
    lance_uri = settings.resolve_lance_uri()

    loaded = load_embeddings(lance_uri)
    if loaded is None:
        logger.info("cluster: no embeddings at {} — skipping", lance_uri)
        return {"total": 0, "clusters": 0, "noise": 0, "skipped": 1}

    # UMAP's spectral init needs N strictly greater than the target
    # dimensionality (scipy eigsh requires k < N); a store this small is a
    # smoke-test corpus, not a clusterable one — skip like the absent case.
    cfg_probe = settings.clustering_config()
    if len(loaded[0]) <= cfg_probe.umap_n_components_50:
        logger.info(
            "cluster: only {} embeddings (need > {} for the {}d UMAP) — skipping",
            len(loaded[0]),
            cfg_probe.umap_n_components_50,
            cfg_probe.umap_n_components_50,
        )
        return {"total": len(loaded[0]), "clusters": 0, "noise": 0, "skipped": 1}

    # Mtime-sidecar fast path: if the lance dataset hasn't moved since the
    # last successful clustering, skip the UMAP+HDBSCAN refit.
    sidecar = sidecar_for(out_path, input_name="embeddings")
    in_mtime_ns = newest_mtime_ns(lance_uri)
    if not force and is_output_fresh(out_path, sidecar=sidecar, input_mtime_ns=in_mtime_ns):
        logger.info("Embeddings unchanged since last cluster run; reusing {}.", out_path)
        return {**_stats_from_parquet(pl.read_parquet(out_path)), "skipped": 0}

    cfg = settings.clustering_config()
    t0 = time.monotonic()
    uuids, matrix = loaded
    logger.info("Loaded {} embeddings, shape={}, dtype={}", len(uuids), matrix.shape, matrix.dtype)

    labels, coords = cluster_embeddings(matrix, cfg)
    k = int(labels.max()) + 1 if labels.max() >= 0 else 0
    noise = int((labels < 0).sum())

    viz_x = np.ascontiguousarray(coords[:, 0], dtype=np.float32) if coords is not None else None
    viz_y = np.ascontiguousarray(coords[:, 1], dtype=np.float32) if coords is not None else None
    df = pl.DataFrame(
        {
            "uuid": uuids,
            "cluster_id": labels.astype(np.int32),
            "x": viz_x if viz_x is not None else [None] * len(uuids),
            "y": viz_y if viz_y is not None else [None] * len(uuids),
            "is_noise": labels < 0,
        },
        schema={
            "uuid": pl.Utf8,
            "cluster_id": pl.Int32,
            "x": pl.Float32,
            "y": pl.Float32,
            "is_noise": pl.Boolean,
        },
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_path)
    stamp_output(sidecar, in_mtime_ns)
    logger.info(
        "Wrote {} rows to {} (total elapsed: {:.1f}s)", len(df), out_path, time.monotonic() - t0
    )
    return {"total": len(uuids), "clusters": k, "noise": noise, "skipped": 0}


__all__ = ["run_clustering"]
