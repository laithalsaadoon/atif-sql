# SPDX-License-Identifier: Apache-2.0

"""The analytics artifact layout under one corpus root, as a value object.

CONTRACT-V2 §Ports & state fixes the storage shape: sharded parquet dirs
under ``<corpus_root>/analytics/<name>/`` for the five LLM pipelines, the
single-file structural parquets, and one sqlite WAL ``state.db`` per corpus.
This module is the only place those paths are computed (the atif-corpus
``CorpusLayout`` precedent) — atif-duck reads the same shape without
importing us, and atif-cli threads ``corpus_root`` through.

Pure path arithmetic; nothing here touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Sharded-cache directory names for the five LLM pipelines. The names match
#: the DuckDB view each backs.
CLASSIFICATIONS_DIRNAME = "session_classifications"
TRAJECTORY_DIRNAME = "message_trajectory"
CONFLICTS_DIRNAME = "session_conflicts"
USER_FRICTION_DIRNAME = "user_friction"
PERCEIVED_ERRORS_DIRNAME = "perceived_errors"

#: Refusal audit sidecar: a durable parquet record of a refusal-skip rather
#: than a log line that ages out. Pipelines whose in-band schema can't hold a
#: sentinel row (conflicts: rows are uuid-keyed PAIRS) append here instead:
#: ``{pipeline, unit_id, reason, refused_at}``.
#:
#: The atif-duck catalog binds NO view over this directory, so it has no name
#: in ``atif-sql schema`` and no name to SELECT from. A reader has to point
#: ``read_parquet`` at the shard files under
#: ``<corpus_root>/analytics/refusals/`` itself.
REFUSALS_DIRNAME = "refusals"

#: Single-file structural outputs (one parquet each;
#: the writers replace the whole file per run, so sharding buys nothing).
CLUSTERS_FILENAME = "clusters.parquet"
CLUSTER_TERMS_FILENAME = "cluster_terms.parquet"
COMMUNITIES_FILENAME = "session_communities.parquet"
COMMUNITY_PROFILE_FILENAME = "community_profile.parquet"

STATE_DB_FILENAME = "state.db"


@dataclass(frozen=True, slots=True)
class AnalyticsLayout:
    """Computes every analytics artifact path under one ``corpus_root``."""

    corpus_root: Path

    @property
    def analytics_dir(self) -> Path:
        """``<corpus_root>/analytics/`` — parent of every artifact below."""
        return self.corpus_root / "analytics"

    @property
    def classifications_dir(self) -> Path:
        """Sharded cache for the classify pipeline."""
        return self.analytics_dir / CLASSIFICATIONS_DIRNAME

    @property
    def trajectory_dir(self) -> Path:
        """Sharded cache for the windowed trajectory pipeline."""
        return self.analytics_dir / TRAJECTORY_DIRNAME

    @property
    def conflicts_dir(self) -> Path:
        """Sharded cache for the conflicts pipeline."""
        return self.analytics_dir / CONFLICTS_DIRNAME

    @property
    def user_friction_dir(self) -> Path:
        """Sharded cache for the friction pipeline."""
        return self.analytics_dir / USER_FRICTION_DIRNAME

    @property
    def perceived_errors_dir(self) -> Path:
        """Sharded cache for the perceived-error pipeline."""
        return self.analytics_dir / PERCEIVED_ERRORS_DIRNAME

    @property
    def refusals_dir(self) -> Path:
        """Sharded refusal-audit sidecar (see :data:`REFUSALS_DIRNAME`)."""
        return self.analytics_dir / REFUSALS_DIRNAME

    @property
    def clusters_parquet(self) -> Path:
        """UMAP+HDBSCAN output: ``(uuid, cluster_id, x, y, is_noise)``."""
        return self.analytics_dir / CLUSTERS_FILENAME

    @property
    def cluster_terms_parquet(self) -> Path:
        """c-TF-IDF output: ``(cluster_id, term, weight, rank)``."""
        return self.analytics_dir / CLUSTER_TERMS_FILENAME

    @property
    def communities_parquet(self) -> Path:
        """Leiden+CPM output: one row per session with a community id."""
        return self.analytics_dir / COMMUNITIES_FILENAME

    @property
    def community_profile_parquet(self) -> Path:
        """Optional resolution-profile sidecar written on auto-γ runs."""
        return self.analytics_dir / COMMUNITY_PROFILE_FILENAME

    @property
    def state_db_path(self) -> Path:
        """The sqlite WAL checkpoint + retry-queue file (one per corpus)."""
        return self.analytics_dir / STATE_DB_FILENAME


__all__ = [
    "CLASSIFICATIONS_DIRNAME",
    "CLUSTERS_FILENAME",
    "CLUSTER_TERMS_FILENAME",
    "COMMUNITIES_FILENAME",
    "COMMUNITY_PROFILE_FILENAME",
    "CONFLICTS_DIRNAME",
    "PERCEIVED_ERRORS_DIRNAME",
    "REFUSALS_DIRNAME",
    "STATE_DB_FILENAME",
    "TRAJECTORY_DIRNAME",
    "USER_FRICTION_DIRNAME",
    "AnalyticsLayout",
]
