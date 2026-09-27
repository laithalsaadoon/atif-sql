# SPDX-License-Identifier: Apache-2.0

"""The analytics artifact layout under one corpus root, as a value object.

CONTRACT-V2 §Ports & state fixes the storage shape: sharded parquet dirs
under ``<corpus_root>/analytics/<name>/`` for the LLM pipelines and one
sqlite WAL ``state.db`` per corpus. The trajectory dir and the structural
single-file parquets (clusters, cluster_terms, session_communities,
community_profile) were removed with their pipelines on 2026-09-27; an older
corpus may still hold them, and nothing reads them.
This module is the only place those paths are computed (the atif-corpus
``CorpusLayout`` precedent) — atif-duck reads the same shape without
importing us, and atif-cli threads ``corpus_root`` through.

Pure path arithmetic; nothing here touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Sharded-cache directory names for the LLM pipelines. The names match
#: the DuckDB view each backs.
CLASSIFICATIONS_DIRNAME = "session_classifications"
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
    def state_db_path(self) -> Path:
        """The sqlite WAL checkpoint + retry-queue file (one per corpus)."""
        return self.analytics_dir / STATE_DB_FILENAME


__all__ = [
    "CLASSIFICATIONS_DIRNAME",
    "CONFLICTS_DIRNAME",
    "PERCEIVED_ERRORS_DIRNAME",
    "REFUSALS_DIRNAME",
    "STATE_DB_FILENAME",
    "USER_FRICTION_DIRNAME",
    "AnalyticsLayout",
]
