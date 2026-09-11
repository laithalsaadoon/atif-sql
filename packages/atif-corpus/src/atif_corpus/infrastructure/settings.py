# SPDX-License-Identifier: Apache-2.0

"""Runtime configuration for atif-corpus.

Pydantic v2 ``BaseSettings`` populated from env vars prefixed with
``ATIF_SQL_`` (workspace convention). Reading process env is I/O, so this
lives in ``infrastructure`` and never in ``domain``. Every ``_default_*()``
factory reads env at CALL time — not import time — so tests and long-lived
processes that re-point ``CLAUDE_CONFIG_DIR`` observe the new value without
a module reload.

TWO AGENTS, TWO DEFAULT ROOTS. ``agent`` selects which pair of defaults the
unset roots take: ``<CLAUDE_CONFIG_DIR>/projects`` for Claude Code,
``<CODEX_HOME>/sessions`` for Codex, each with its own slugged corpus root so
one agent's corpus can never overwrite the other's. An EXPLICIT root — passed
in or set in the env — always wins over the agent default, which is what keeps
``ATIF_SQL_SOURCE_ROOT`` meaning exactly what it meant before Codex existed.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_corpus.domain.agents import DEFAULT_AGENT, AgentSource
from atif_corpus.domain.slug import corpus_slug


def _claude_config_root() -> str:
    """Return Claude Code's config-dir root, honoring ``CLAUDE_CONFIG_DIR``."""
    return str(Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser())


def _default_source_root() -> Path:
    """Root directory holding per-project session transcript directories."""
    return Path(_claude_config_root()) / "projects"


def _codex_config_root() -> str:
    """Return Codex CLI's config-dir root, honoring ``CODEX_HOME``.

    ``CODEX_HOME`` is Codex's own variable, not one this workspace invents, so
    a user who already re-points Codex gets their rollouts discovered with no
    extra configuration.
    """
    return str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())


def _default_codex_source_root() -> Path:
    """Root directory holding Codex's per-day rollout directories."""
    return Path(_codex_config_root()) / "sessions"


def _corpus_root_for(source_root: Path) -> Path:
    """Materialized corpus root: ``~/.atif-sql/corpus/<slug of source root>``.

    Slugging the SOURCE root (not a fixed name) keeps corpora for different
    inputs separate: re-pointing ``CLAUDE_CONFIG_DIR``, or switching agents,
    must never overwrite another corpus's artifacts.
    """
    return Path.home() / ".atif-sql" / "corpus" / corpus_slug(source_root)


def _default_corpus_root() -> Path:
    """The Claude Code corpus root — the default when no agent is named."""
    return _corpus_root_for(_default_source_root())


class CorpusSettings(BaseSettings):
    """Env-driven settings for corpus materialization."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Which agent's transcripts this corpus holds (``ATIF_SQL_AGENT``).
    agent: AgentSource = DEFAULT_AGENT
    #: Where raw transcripts live: ``<config>/projects`` for Claude Code,
    #: ``<CODEX_HOME>/sessions`` for Codex.
    source_root: Path = Field(default_factory=_default_source_root)
    #: Where materialized artifacts are written (CONTRACT.md layout root).
    corpus_root: Path = Field(default_factory=_default_corpus_root)
    #: Seconds of source silence before a session may (re)materialize.
    quiesce_seconds: int = 300

    @model_validator(mode="after")
    def _apply_agent_defaults(self) -> CorpusSettings:
        """Re-derive the roots the caller left unset from ``agent``.

        ``model_fields_set`` is the test, and it is the right one: a field
        filled by its ``default_factory`` is absent from that set while a field
        populated from the environment or from a keyword is present. So an
        explicit ``ATIF_SQL_SOURCE_ROOT`` (or an explicit ``corpus_root=``)
        still wins over the agent default, and only a genuinely unset root
        moves. With ``agent`` at its default this method changes nothing, which
        is why every pre-Codex caller observes exactly the previous behavior.
        """
        if self.agent is DEFAULT_AGENT:
            return self
        source_root = self.source_root
        if "source_root" not in self.model_fields_set:
            source_root = _default_codex_source_root()
            self.source_root = source_root
        if "corpus_root" not in self.model_fields_set:
            self.corpus_root = _corpus_root_for(source_root)
        return self
