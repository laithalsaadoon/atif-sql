# SPDX-License-Identifier: Apache-2.0

"""Structural pipelines: hyperparameter pins, graceful skip, c-TF-IDF math."""

from __future__ import annotations

from pathlib import Path

import pytest

from atif_analytics.application.use_cases.cluster import run_clustering
from atif_analytics.application.use_cases.community import run_communities
from atif_analytics.application.use_cases.terms import run_terms
from atif_analytics.domain.config import ClusteringConfig, CommunityConfig, TermsConfig
from atif_analytics.domain.structure.terms import compute_ctfidf
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.settings import AnalyticsSettings

# ---------------------------------------------------------------------------
# Hyperparameter pins (frozen by CONTRACT-V2 §Structural pipelines)
# ---------------------------------------------------------------------------


def test_clustering_defaults_verbatim() -> None:
    cfg = ClusteringConfig()
    assert cfg.umap_n_components_50 == 50
    assert cfg.umap_n_neighbors == 30
    assert cfg.umap_min_dist_cluster == 0.0
    assert cfg.umap_metric == "cosine"
    assert cfg.hdbscan_min_cluster_size == 20
    assert cfg.hdbscan_min_samples == 5
    assert cfg.seed == 42
    assert cfg.compute_viz_coords is False


def test_community_defaults_verbatim() -> None:
    cfg = CommunityConfig()
    assert cfg.leiden_knn_k == 15
    assert cfg.leiden_edge_floor == 0.3
    assert cfg.leiden_min_community_size == 3
    assert cfg.leiden_resolution is None  # auto-γ
    assert cfg.seed == 42


def test_terms_defaults_verbatim() -> None:
    cfg = TermsConfig()
    assert cfg.tfidf_min_df == 2
    assert cfg.tfidf_max_df == 0.95
    assert cfg.tfidf_ngram_min == 1
    assert cfg.tfidf_ngram_max == 2
    assert cfg.tfidf_top_n_terms == 10


def test_settings_project_the_same_defaults(settings: AnalyticsSettings) -> None:
    assert settings.clustering_config() == ClusteringConfig()
    assert settings.community_config() == CommunityConfig()
    assert settings.terms_config() == TermsConfig()
    caps = settings.transcript_caps()
    assert caps.session_text_total_max_chars == 800_000
    assert caps.session_text_tool_result_max_chars == 50_000


# ---------------------------------------------------------------------------
# Graceful skip when the lance store is absent
# ---------------------------------------------------------------------------


def test_cluster_skips_without_lance_store(settings: AnalyticsSettings) -> None:
    stats = run_clustering(settings)
    assert stats == {"total": 0, "clusters": 0, "noise": 0, "skipped": 1}
    assert not settings.layout().clusters_parquet.exists()


def test_cluster_skips_tiny_store(
    settings: AnalyticsSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N <= 50 embeddings cannot feed the 50d UMAP (scipy eigsh needs k < N)."""
    import numpy as np

    from atif_analytics.application.use_cases import cluster as cluster_mod

    def _tiny_store(_uri: Path) -> tuple[list[str], np.ndarray]:
        return ["u"] * 50, np.zeros((50, 8), dtype=np.float32)

    monkeypatch.setattr(cluster_mod, "load_embeddings", _tiny_store)
    stats = run_clustering(settings)
    assert stats == {"total": 50, "clusters": 0, "noise": 0, "skipped": 1}


def test_community_skips_without_lance_store(settings: AnalyticsSettings) -> None:
    stats = run_communities(settings)
    assert stats["skipped"] == 1
    assert stats["sessions"] == 0


def test_terms_skips_without_clusters(settings: AnalyticsSettings, reader: CorpusReader) -> None:
    stats = run_terms(settings, reader=reader)
    assert stats == {"clusters": 0, "terms": 0, "skipped": 1}


# ---------------------------------------------------------------------------
# c-TF-IDF math (pure)
# ---------------------------------------------------------------------------


def test_compute_ctfidf_ranks_discriminative_terms() -> None:
    cfg = TermsConfig(tfidf_min_df=1, tfidf_top_n_terms=3)
    docs = [
        (0, "alpha alpha alpha shared shared"),
        (1, "beta beta beta shared shared"),
    ]
    rows = compute_ctfidf(docs, cfg)
    top_by_cluster = {cid: term for cid, term, _w, rank in rows if rank == 1}
    assert top_by_cluster[0] == "alpha"
    assert top_by_cluster[1] == "beta"
    # Ranks are 1-based and capped at top_n.
    assert all(1 <= rank <= 3 for _, _, _, rank in rows)


def test_terms_end_to_end_over_synthetic_clusters(
    settings: AnalyticsSettings, reader: CorpusReader, tmp_path: Path
) -> None:
    """With a hand-written clusters.parquet, terms joins step texts by uuid."""
    import polars as pl

    layout = settings.layout()
    layout.analytics_dir.mkdir(parents=True, exist_ok=True)
    # Two clusters over session-one text uuids (min_df=1 via settings tweak).
    pl.DataFrame(
        {
            "uuid": ["u-01", "u-05", "u-04", "u-06"],
            "cluster_id": [0, 0, 1, 1],
            "x": [None] * 4,
            "y": [None] * 4,
            "is_noise": [False] * 4,
        },
        schema={
            "uuid": pl.Utf8,
            "cluster_id": pl.Int32,
            "x": pl.Float32,
            "y": pl.Float32,
            "is_noise": pl.Boolean,
        },
    ).write_parquet(layout.clusters_parquet)

    tweaked = settings.model_copy(update={"tfidf_min_df": 1})
    stats = run_terms(tweaked, reader=reader)
    assert stats["skipped"] == 0
    assert stats["clusters"] == 2
    assert stats["terms"] > 0
    df = pl.read_parquet(layout.cluster_terms_parquet)
    assert set(df.columns) == {"cluster_id", "term", "weight", "rank"}
    # Freshness sidecar: a rerun with the same clusters mtime reuses.
    stats2 = run_terms(tweaked, reader=reader)
    assert stats2["clusters"] == 2
