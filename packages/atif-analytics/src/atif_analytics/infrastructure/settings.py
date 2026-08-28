# SPDX-License-Identifier: Apache-2.0

"""Env-driven settings for the analytics pipelines.

Pydantic v2 ``BaseSettings`` under the workspace ``ATIF_SQL_`` prefix
(atif-corpus / atif-models precedent). Carries the corpus root, the
per-pipeline knobs (friction char cutoff, batch size, transcript caps), the
structural hyperparameters (defaults pinned by the frozen domain configs),
and the lance-store location the structural stages read (the VSS branch owns the writer;
CONTRACT-V2 documents the table schema ``{uuid, model, dim, embedding,
embedded_at}``).

Composes :class:`atif_models.infrastructure.settings.LlmSettings` for the
LLM family/size/region/concurrency selection rather than duplicating those
fields.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_analytics.domain.config import (
    ClusteringConfig,
    CommunityConfig,
    TermsConfig,
    TranscriptCaps,
)
from atif_analytics.domain.layout import AnalyticsLayout

if TYPE_CHECKING:
    from atif_models.infrastructure.settings import LlmSettings


def _default_corpus_root() -> Path:
    """Mirror atif-corpus's default: ``~/.atif-sql/corpus/<slug of source root>``.

    Re-implements the slug locally (sha256 of the resolved source root,
    ``default`` for ``~/.claude``) because atif-analytics may not import
    atif-corpus (independence contract). The parity test pins both against
    the same expected strings.
    """
    import hashlib
    import re

    env_source = os.environ.get("ATIF_SQL_SOURCE_ROOT")
    if env_source is not None:
        source_root = Path(env_source)
    else:
        config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        source_root = config_root / "projects"
    resolved = source_root.expanduser().resolve()
    # corpus_slug parity: a bare ~/.claude source maps to the reserved
    # "default" key; everything else is <sanitized-dirname>-<8-hex sha256>.
    if resolved == Path("~/.claude").expanduser().resolve():
        slug = "default"
    else:
        name = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-")[:32]
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
        slug = f"{name}-{digest}" if name else digest
    return Path.home() / ".atif-sql" / "corpus" / slug


class AnalyticsSettings(BaseSettings):
    """Env-driven configuration for the analytics pipelines."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Materialized corpus root (the directory containing ``sessions/``).
    corpus_root: Path = Field(default_factory=_default_corpus_root)

    #: Lance embeddings store the structural stages read. ``None`` resolves
    #: to ``<corpus_root>/embeddings_lance`` (the atif-embed convention).
    lance_uri: Path | None = None

    # --- LLM pipeline knobs ---
    #: Char cutoff for friction candidates: longer user turns are almost
    #: always genuine instructions.
    friction_max_chars: int = 300
    #: Hard per-run session ceiling for each LLM pipeline's candidate list
    #: (newest-first, so fresh sessions win). The unattended nightly cron
    #: relies on this: a backlogged corpus must never turn one tick into an
    #: unbounded spend. Env: ``ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN``.
    llm_max_sessions_per_run: int = 50
    #: Hard per-run dollar ceiling across ALL LLM pipelines, checked against
    #: the UsageAccumulator's running actuals between stages — when crossed,
    #: the remaining LLM stages are aborted cleanly (nothing stamped for
    #: unstarted sessions). Env: ``ATIF_SQL_LLM_MAX_COST_USD_PER_RUN``.
    llm_max_cost_usd_per_run: float = 25.0
    #: Write chunking: rows land every ``max(batch_size * 4, 256)`` units.
    batch_size: int = 96
    #: Transcript caps: 800K per session, 50K per tool_result.
    session_text_total_max_chars: int = 800_000
    session_text_tool_result_max_chars: int = 50_000

    # --- Structural hyperparameters (pinned by CONTRACT-V2) ---
    umap_n_components_50: int = 50
    umap_n_components_2: int = 2
    umap_n_neighbors: int = 30
    umap_min_dist_cluster: float = 0.0
    umap_min_dist_viz: float = 0.1
    umap_metric: str = "cosine"
    umap_compute_viz: bool = False
    hdbscan_min_cluster_size: int = 20
    hdbscan_min_samples: int = 5
    leiden_knn_k: int = 15
    leiden_edge_floor: float = 0.3
    leiden_min_community_size: int = 3
    leiden_resolution: float | None = None
    leiden_resolution_range_lo: float = 0.05
    leiden_resolution_range_hi: float = 0.95
    leiden_n_iterations: int = -1
    seed: int = 42
    tfidf_min_df: int = 2
    tfidf_max_df: float = 0.95
    tfidf_ngram_min: int = 1
    tfidf_ngram_max: int = 2
    tfidf_top_n_terms: int = 10

    # ------------------------------------------------------------------
    # Derivations
    # ------------------------------------------------------------------

    def layout(self) -> AnalyticsLayout:
        """The analytics artifact layout under :attr:`corpus_root`."""
        return AnalyticsLayout(corpus_root=self.corpus_root)

    def resolve_lance_uri(self) -> Path:
        """Effective lance dataset directory (atif-embed's convention)."""
        if self.lance_uri is not None:
            return self.lance_uri
        return self.corpus_root / "embeddings_lance"

    def llm(self) -> LlmSettings:
        """The atif-models LLM settings (family/sizes/region/concurrency)."""
        from atif_models.infrastructure.settings import LlmSettings

        return LlmSettings()

    def clustering_config(self) -> ClusteringConfig:
        """Project the UMAP + HDBSCAN hyperparameters (+ seed)."""
        return ClusteringConfig(
            umap_n_components_50=self.umap_n_components_50,
            umap_n_components_2=self.umap_n_components_2,
            umap_n_neighbors=self.umap_n_neighbors,
            umap_min_dist_cluster=self.umap_min_dist_cluster,
            umap_min_dist_viz=self.umap_min_dist_viz,
            umap_metric=self.umap_metric,
            compute_viz_coords=self.umap_compute_viz,
            hdbscan_min_cluster_size=self.hdbscan_min_cluster_size,
            hdbscan_min_samples=self.hdbscan_min_samples,
            seed=self.seed,
        )

    def community_config(self) -> CommunityConfig:
        """Project the Leiden+CPM + mutual-kNN hyperparameters (+ seed)."""
        return CommunityConfig(
            leiden_knn_k=self.leiden_knn_k,
            leiden_edge_floor=self.leiden_edge_floor,
            leiden_min_community_size=self.leiden_min_community_size,
            leiden_resolution=self.leiden_resolution,
            leiden_resolution_range_lo=self.leiden_resolution_range_lo,
            leiden_resolution_range_hi=self.leiden_resolution_range_hi,
            leiden_n_iterations=self.leiden_n_iterations,
            seed=self.seed,
        )

    def terms_config(self) -> TermsConfig:
        """Project the c-TF-IDF hyperparameters."""
        return TermsConfig(
            tfidf_min_df=self.tfidf_min_df,
            tfidf_max_df=self.tfidf_max_df,
            tfidf_ngram_min=self.tfidf_ngram_min,
            tfidf_ngram_max=self.tfidf_ngram_max,
            tfidf_top_n_terms=self.tfidf_top_n_terms,
        )

    def transcript_caps(self) -> TranscriptCaps:
        """Project the transcript char caps."""
        return TranscriptCaps(
            session_text_total_max_chars=self.session_text_total_max_chars,
            session_text_tool_result_max_chars=self.session_text_tool_result_max_chars,
        )


__all__ = ["AnalyticsSettings"]
