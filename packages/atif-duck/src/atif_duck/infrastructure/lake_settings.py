# SPDX-License-Identifier: Apache-2.0

"""Where the lake lives and how it is written, from ``ATIF_SQL_*`` env vars.

Reading process env is I/O, so this lives in ``infrastructure``. Every default
factory reads the environment at call time, so a test that re-points ``HOME``
sees the new value without a module reload.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_duck.infrastructure.lake import (
    DEFAULT_LOAD_BATCH_SIZE,
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DEFAULT_STAGE_WORKERS,
    DEFAULT_SYNC_BATCH_SIZE,
)


def _default_lake_root() -> Path:
    """``~/.atif-sql/lake``: one lake for every corpus."""
    return Path.home() / ".atif-sql" / "lake"


def _default_corpus_base() -> Path:
    """``~/.atif-sql/corpus``: where the default corpus roots live (``lake rebuild`` scans it)."""
    return Path.home() / ".atif-sql" / "corpus"


class LakeSettings(BaseSettings):
    """Env-driven settings for the DuckLake every corpus is queried through."""

    model_config = SettingsConfigDict(env_prefix="ATIF_SQL_", env_file=".env", extra="ignore")

    #: The lake root (``ATIF_SQL_LAKE_ROOT``): ``catalog.duckdb``, its
    #: published reader copy, and ``data/``.
    lake_root: Path = Field(default_factory=_default_lake_root)
    #: The directory ``lake rebuild`` looks in for corpora when none is named
    #: (``ATIF_SQL_CORPUS_BASE``); every child holding ``sessions/`` is one.
    corpus_base: Path = Field(default_factory=_default_corpus_base)
    #: Sessions per lake transaction when materialize syncs what it published
    #: (``ATIF_SQL_LAKE_SYNC_BATCH_SIZE``).
    lake_sync_batch_size: int = Field(default=DEFAULT_SYNC_BATCH_SIZE, ge=1)
    #: Sessions per lake transaction when a whole corpus is loaded
    #: (``ATIF_SQL_LAKE_LOAD_BATCH_SIZE``).
    lake_load_batch_size: int = Field(default=DEFAULT_LOAD_BATCH_SIZE, ge=1)
    #: Processes that stage a batch's parquet from its compressed trajectories
    #: while the lake loads it (``ATIF_SQL_LAKE_STAGE_WORKERS``); 1 stages in
    #: the loading process.
    lake_stage_workers: int = Field(default=DEFAULT_STAGE_WORKERS, ge=1)
    #: Seconds a writer waits for another writer (``ATIF_SQL_LAKE_LOCK_TIMEOUT_SECONDS``).
    lake_lock_timeout_seconds: float = Field(default=DEFAULT_LOCK_TIMEOUT_SECONDS, gt=0)
    #: ``lake compact`` expires snapshots older than this many days
    #: (``ATIF_SQL_LAKE_EXPIRE_DAYS``).
    lake_expire_days: int = Field(default=30, ge=0)


__all__ = ["LakeSettings"]
