# SPDX-License-Identifier: Apache-2.0

"""Runtime configuration for atif-corpus.

Pydantic v2 ``BaseSettings`` populated from env vars prefixed with
``ATIF_SQL_`` (workspace convention). Reading process env is I/O, so this
lives in ``infrastructure`` and never in ``domain``. Every ``_default_*()``
factory reads env at CALL time — not import time — so tests and long-lived
processes that re-point ``CLAUDE_CONFIG_DIR`` observe the new value without
a module reload.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_corpus.domain.slug import corpus_slug


def _claude_config_root() -> str:
    """Return Claude Code's config-dir root, honoring ``CLAUDE_CONFIG_DIR``."""
    return str(Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser())


def _default_source_root() -> Path:
    """Root directory holding per-project session transcript directories."""
    return Path(_claude_config_root()) / "projects"


def _default_corpus_root() -> Path:
    """Materialized corpus root: ``~/.atif-sql/corpus/<slug of source root>``.

    Slugging the SOURCE root (not a fixed name) keeps corpora for different
    inputs separate: re-pointing ``CLAUDE_CONFIG_DIR`` must never overwrite
    another corpus's artifacts.
    """
    return Path.home() / ".atif-sql" / "corpus" / corpus_slug(_default_source_root())


class CorpusSettings(BaseSettings):
    """Env-driven settings for corpus materialization."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Where raw Claude Code transcripts live (``<config>/projects``).
    source_root: Path = Field(default_factory=_default_source_root)
    #: Where materialized artifacts are written (CONTRACT.md layout root).
    corpus_root: Path = Field(default_factory=_default_corpus_root)
    #: Seconds of source silence before a session may (re)materialize.
    quiesce_seconds: int = 300
