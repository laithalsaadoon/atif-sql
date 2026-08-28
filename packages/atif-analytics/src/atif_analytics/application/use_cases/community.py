# SPDX-License-Identifier: Apache-2.0

"""Session-level community detection via mutual-kNN + Leiden+CPM.

Session centroids are the L2-normalized means of each session's step
embeddings (read from the lance store, uuids
mapped to sessions via the corpus steps' first source_uuids), the graph is
mutual-kNN cosine (k=15, floor 0.3), γ comes from the resolution profile
(or an explicit setting), and the partition is Leiden CPM with seed 42.
Writes ``session_communities.parquet``
``(session_id, community_id, size, is_medoid, coherence, gamma_used)`` plus
the ``community_profile.parquet`` sidecar when auto-γ ran.

Freshness keys on the EMBEDDINGS store, not clusters.parquet and not this
output's existence: communities are derived straight from the embeddings,
so the store's mtime is the input identity. Skips gracefully when the store
is absent.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
import polars as pl
from loguru import logger

from atif_analytics.domain.structure.community import (
    NOISE_COMMUNITY_ID,
    ResolutionLevel,
    _build_graph,
    _build_mutual_knn,
    _compute_medoid_and_coherence,
    _compute_resolution_profile,
    _pick_zoom,
    _relabel_and_collapse,
    _run_leiden_cpm,
    _warn_disconnected,
)
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.freshness import (
    is_output_fresh,
    newest_mtime_ns,
    sidecar_for,
    stamp_output,
)
from atif_analytics.infrastructure.lance_reader import load_embeddings

if TYPE_CHECKING:
    from atif_analytics.infrastructure.settings import AnalyticsSettings


def _uuid_to_session(reader: CorpusReader) -> dict[str, str]:
    """``{step_uuid: session_id}`` over every complete session."""
    out: dict[str, str] = {}
    for sid in reader.session_ids():
        for step in reader.load_steps(sid):
            if step.uuid:
                out[step.uuid] = sid
    return out


def _session_centroids(
    uuids: list[str],
    matrix: np.ndarray,
    uuid_to_session: dict[str, str],
) -> tuple[list[str], np.ndarray]:
    """Per-session L2-normalized mean embeddings.

    Embeddings whose uuid maps to no session (stale store rows) are
    dropped with a count log.
    """
    sessions = np.array([uuid_to_session.get(u, "") for u in uuids])
    keep = sessions != ""
    dropped = int((~keep).sum())
    if dropped:
        logger.info("community: {} embeddings had no session mapping — dropped", dropped)
    matrix = matrix[keep]
    sessions = sessions[keep]
    if matrix.shape[0] == 0:
        return [], np.zeros((0, 0), dtype=np.float32)
    order = np.argsort(sessions, kind="stable")
    matrix = matrix[order]
    sessions = sessions[order]
    sids_arr, starts, counts = np.unique(sessions, return_index=True, return_counts=True)
    summed = np.add.reduceat(matrix, starts, axis=0)
    centroids = (summed / counts[:, None]).astype(np.float32)
    norms = np.linalg.norm(centroids, axis=1, keepdims=True)
    centroids = centroids / np.where(norms == 0, 1.0, norms)
    return [str(s) for s in sids_arr.tolist()], centroids


def run_communities(
    settings: AnalyticsSettings,
    *,
    force: bool = False,
    gamma: float | None = None,
    resolution: ResolutionLevel = "medium",
    reader: CorpusReader | None = None,
) -> dict[str, int | float | str]:
    """Run Leiden+CPM on session centroids; write primary parquet (+ sidecar).

    Returns ``{"sessions", "communities", "noise", "gamma_used", "quality",
    "algorithm", "skipped"}``. ``communities`` counts only real (≥ min-size)
    communities; singletons aggregate into ``noise``. ``skipped=1`` marks
    the graceful no-embeddings path.
    """
    layout = settings.layout()
    out_path = layout.communities_parquet
    profile_path = layout.community_profile_parquet
    lance_uri = settings.resolve_lance_uri()

    loaded = load_embeddings(lance_uri)
    if loaded is None:
        logger.info("community: no embeddings at {} — skipping", lance_uri)
        return {
            "sessions": 0,
            "communities": 0,
            "noise": 0,
            "gamma_used": 0.0,
            "quality": float("nan"),
            "algorithm": "leiden_cpm",
            "skipped": 1,
        }

    sidecar = sidecar_for(out_path, input_name="embeddings")
    embeddings_mtime_ns = newest_mtime_ns(lance_uri)
    if not force and is_output_fresh(out_path, sidecar=sidecar, input_mtime_ns=embeddings_mtime_ns):
        logger.info("Embeddings unchanged since last community run; reusing {}.", out_path)
        df = pl.read_parquet(out_path)
        real = df.filter(pl.col("community_id") != NOISE_COMMUNITY_ID)
        noise_n = df.height - real.height
        n_comm = int(real["community_id"].n_unique()) if real.height else 0
        cached_gamma = (
            float(df["gamma_used"][0]) if "gamma_used" in df.columns and df.height else 0.0
        )
        return {
            "sessions": int(df.height),
            "communities": n_comm,
            "noise": noise_n,
            "gamma_used": cached_gamma,
            "quality": float("nan"),
            "algorithm": "leiden_cpm",
            "skipped": 0,
        }

    cfg = settings.community_config()
    reader = reader if reader is not None else CorpusReader(settings.corpus_root)

    uuids, matrix = loaded
    sids, centroids = _session_centroids(uuids, matrix, _uuid_to_session(reader))
    if not sids:
        logger.info("community: no session centroids derivable — skipping")
        return {
            "sessions": 0,
            "communities": 0,
            "noise": 0,
            "gamma_used": 0.0,
            "quality": float("nan"),
            "algorithm": "leiden_cpm",
            "skipped": 1,
        }
    logger.info("Computed {} session centroids (dim={})", len(sids), centroids.shape[1])

    t0 = time.monotonic()
    sim = centroids @ centroids.T
    np.fill_diagonal(sim, 0.0)

    edges, weights = _build_mutual_knn(sim, k=cfg.leiden_knn_k, floor=cfg.leiden_edge_floor)
    logger.info(
        "Mutual-kNN graph: {} nodes, {} edges (k={}, floor={:.2f}) in {:.1f}s",
        len(sids),
        len(edges),
        cfg.leiden_knn_k,
        cfg.leiden_edge_floor,
        time.monotonic() - t0,
    )

    graph = _build_graph(len(sids), edges, weights)

    profile_rows: list[tuple[float, int, float, int]] | None = None
    if gamma is None:
        if cfg.leiden_resolution is not None:
            gamma_used = float(cfg.leiden_resolution)
        else:
            t1 = time.monotonic()
            profile_rows = _compute_resolution_profile(
                graph,
                range_lo=cfg.leiden_resolution_range_lo,
                range_hi=cfg.leiden_resolution_range_hi,
                seed=cfg.seed,
                n_iterations=cfg.leiden_n_iterations,
            )
            logger.info(
                "Resolution profile: {} γ change-points in {:.1f}s",
                len(profile_rows),
                time.monotonic() - t1,
            )
            if not profile_rows:
                gamma_used = (cfg.leiden_resolution_range_lo + cfg.leiden_resolution_range_hi) / 2.0
            else:
                gamma_used = _pick_zoom(profile_rows, resolution, n_nodes=graph.n_nodes)
    else:
        gamma_used = float(gamma)

    t2 = time.monotonic()
    membership, quality = _run_leiden_cpm(
        graph, gamma=gamma_used, seed=cfg.seed, n_iterations=cfg.leiden_n_iterations
    )
    n_communities = len({m for m in membership if m >= 0})
    logger.info(
        "Leiden+CPM γ={:.4f}: {} raw communities (quality={:.4f}) in {:.1f}s",
        gamma_used,
        n_communities,
        quality,
        time.monotonic() - t2,
    )

    labels = membership
    _warn_disconnected(graph, labels)

    medoid_indices, coherence = _compute_medoid_and_coherence(sim, labels)

    rows, n_real, n_noise = _relabel_and_collapse(
        labels,
        sids,
        min_size=cfg.leiden_min_community_size,
        medoid_indices=medoid_indices,
        coherence=coherence,
        gamma_used=gamma_used,
    )

    df = pl.DataFrame(
        rows,
        schema={
            "session_id": pl.Utf8,
            "community_id": pl.Int32,
            "size": pl.Int32,
            "is_medoid": pl.Boolean,
            "coherence": pl.Float32,
            "gamma_used": pl.Float32,
        },
        orient="row",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_path)
    stamp_output(sidecar, embeddings_mtime_ns)
    logger.info(
        "Leiden+CPM: {} kept >= {} sessions, {} singletons -> noise. Wrote {} rows to {}",
        n_real,
        cfg.leiden_min_community_size,
        n_noise,
        len(df),
        out_path,
    )

    if profile_rows is not None:
        prof_df = pl.DataFrame(
            profile_rows,
            schema={
                "gamma": pl.Float64,
                "n_communities": pl.Int32,
                "quality": pl.Float64,
                "plateau_length": pl.Int32,
            },
            orient="row",
        )
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        prof_df.write_parquet(profile_path)
        logger.info("Wrote {} γ-points to {}", prof_df.height, profile_path)

    return {
        "sessions": int(df.height),
        "communities": n_real,
        "noise": n_noise,
        "gamma_used": float(gamma_used),
        "quality": quality,
        "algorithm": "leiden_cpm",
        "skipped": 0,
    }


__all__ = ["run_communities"]
