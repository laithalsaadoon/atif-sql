# SPDX-License-Identifier: Apache-2.0

"""Env-driven settings for the analytics pipelines.

Pydantic v2 ``BaseSettings`` under the workspace ``ATIF_SQL_`` prefix
(atif-corpus / atif-models precedent). Carries the corpus root and the
per-pipeline knobs (friction char cutoff, batch size, budget ceilings,
transcript caps).

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

from atif_analytics.domain.config import TranscriptCaps
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

    # ------------------------------------------------------------------
    # Derivations
    # ------------------------------------------------------------------

    def layout(self) -> AnalyticsLayout:
        """The analytics artifact layout under :attr:`corpus_root`."""
        return AnalyticsLayout(corpus_root=self.corpus_root)

    def llm(self) -> LlmSettings:
        """The atif-models LLM settings (family/sizes/region/concurrency)."""
        from atif_models.infrastructure.settings import LlmSettings

        return LlmSettings()

    def transcript_caps(self) -> TranscriptCaps:
        """Project the transcript char caps."""
        return TranscriptCaps(
            session_text_total_max_chars=self.session_text_total_max_chars,
            session_text_tool_result_max_chars=self.session_text_tool_result_max_chars,
        )


__all__ = ["AnalyticsSettings"]
