# SPDX-License-Identifier: Apache-2.0

"""Per-pipeline config value-objects (pure, frozen dataclasses).

The pure-math hyperparameter slices for clustering / community / terms
plus the transcript caps, carved into small frozen dataclasses so the math
never sees a model id or a corpus path. Stdlib-only; the env-driven
``AnalyticsSettings`` in ``infrastructure.settings`` projects down into these.

Hyperparameter DEFAULTS are pinned by CONTRACT-V2 §Structural pipelines —
UMAP 50d / HDBSCAN 20,5 / Leiden k15 floor .3 min 3 seed 42 / c-TF-IDF
2,.95,1-2,top10 — so a rerun reproduces the same clustering.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClusteringConfig:
    """UMAP + HDBSCAN hyperparameters for the ``cluster`` stage.

    ``seed`` threads into both UMAP ``random_state`` calls so same-seed
    reruns produce byte-identical cluster IDs.
    """

    umap_n_components_50: int = 50
    umap_n_components_2: int = 2
    umap_n_neighbors: int = 30
    umap_min_dist_cluster: float = 0.0
    umap_min_dist_viz: float = 0.1
    umap_metric: str = "cosine"
    hdbscan_min_cluster_size: int = 20
    hdbscan_min_samples: int = 5
    seed: int = 42
    #: Whether to fit the 2-d viz projection alongside the 50-d clustering
    #: one. Off by default: it measured 66% of the stage's wall clock and
    #: nothing consumes the coordinates.
    compute_viz_coords: bool = False


@dataclass(frozen=True, slots=True)
class CommunityConfig:
    """Leiden+CPM + mutual-kNN hyperparameters for the ``community`` stage.

    ``seed`` keys every Leiden call, including the ones the resolution-profile
    bisection makes. ``leiden_resolution`` is ``None`` when auto-γ should run.
    ``leiden_n_iterations = -1`` means "iterate to stability" and the solver
    seam maps it to a fixed positive cycle count.
    """

    leiden_knn_k: int = 15
    leiden_edge_floor: float = 0.3
    leiden_min_community_size: int = 3
    leiden_resolution: float | None = None
    leiden_resolution_range_lo: float = 0.05
    leiden_resolution_range_hi: float = 0.95
    leiden_n_iterations: int = -1
    seed: int = 42


@dataclass(frozen=True, slots=True)
class TermsConfig:
    """c-TF-IDF hyperparameters for the ``terms`` stage.

    ``CountVectorizer`` ``min_df`` / ``max_df`` / ngram bounds plus the
    per-cluster top-N cutoff, pinned at 2 / .95 / 1-2 / top 10.
    """

    tfidf_min_df: int = 2
    tfidf_max_df: float = 0.95
    tfidf_ngram_min: int = 1
    tfidf_ngram_max: int = 2
    tfidf_top_n_terms: int = 10


@dataclass(frozen=True, slots=True)
class TranscriptCaps:
    """Character caps for session-transcript assembly.

    The per-tool-result clip bounds arbitrarily large Bash / file-read
    outputs; the total cap keeps an assembled session under the model
    context window.
    """

    session_text_total_max_chars: int = 800_000
    session_text_tool_result_max_chars: int = 50_000


__all__ = [
    "ClusteringConfig",
    "CommunityConfig",
    "TermsConfig",
    "TranscriptCaps",
]
