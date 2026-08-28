# SPDX-License-Identifier: Apache-2.0

"""Durable retry queue backed by SQLite WAL.

When an LLM call fails in a way that's worth retrying (parse failure,
throttle that outlived tenacity's budget, transient model error), the unit
of work gets enqueued here. A later run drains the queue before starting
fresh work, so a mid-run crash never costs the rows already paid for.

One row per ``(pipeline, unit_id)``. ``unit_id`` is ``session_id`` for
``classify`` / ``conflicts`` / ``trajectory`` and the message ``uuid`` for
``user_friction``. Semantics are "upsert with attempt counter":

- First failure  → insert with attempts=1, next_attempt_at = now + 2 min.
- Retry failure  → attempts += 1, next_attempt_at = now + 2^attempts min (cap 60).
- Retry success  → ``completed_at`` stamped; row stays as audit trail.

Lives in the same ``state.db`` as the checkpoint table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The two bootstrap sentinels stay module-private in `checkpointer` and are
# reached for here on purpose: both modules open the SAME SQLite file, so the
# lock has to be the SAME object in both or the WAL transition and the two
# CREATEs race the writer lock again ("database is locked" on a cold start).
# The registry is keyed per-table (`retry::<path>` here), so sharing it does
# not let one module's bootstrap satisfy the other's.
from atif_analytics.infrastructure.sqlite_state.checkpointer import (
    _SCHEMA_BOOTSTRAP_LOCK,  # pyright: ignore[reportPrivateUsage]
    _SCHEMA_BOOTSTRAPPED,  # pyright: ignore[reportPrivateUsage]
    PIPELINE_NAMES,
    connect as _checkpoint_connect,
    to_iso,
)

MAX_ATTEMPTS_DEFAULT: int = 5
_BACKOFF_CAP_MIN: int = 60

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS retry_queue (
    pipeline        TEXT    NOT NULL,
    unit_id         TEXT    NOT NULL,
    error           TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    completed_at    TEXT,
    PRIMARY KEY (pipeline, unit_id)
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    """Open the queue DB, reusing the checkpointer connection helper.

    Both tables live in the same SQLite file; a distinct bootstrap
    sentinel keeps ``retry_queue``'s CREATE from re-running (and racing the
    writer lock) on every open.
    """
    con = _checkpoint_connect(path)
    key = f"retry::{path.resolve()}"
    with _SCHEMA_BOOTSTRAP_LOCK:
        if key not in _SCHEMA_BOOTSTRAPPED:
            prior = con.isolation_level
            con.isolation_level = None  # autocommit for DDL
            con.execute(_CREATE_TABLE_SQL)
            con.isolation_level = prior
            _SCHEMA_BOOTSTRAPPED.add(key)
    return con


def _backoff_delta(attempts: int) -> timedelta:
    """Exponential backoff in minutes: 2, 4, 8, 16, 32, capped at 60."""
    minutes = min(2**attempts, _BACKOFF_CAP_MIN)
    return timedelta(minutes=minutes)


def enqueue(
    db_path: Path,
    *,
    pipeline: str,
    unit_id: str,
    error: str,
    now: datetime | None = None,
) -> int:
    """Record a failure. Increments ``attempts`` on repeat calls.

    Returns the resulting ``attempts`` value for logging.
    """
    if pipeline not in PIPELINE_NAMES:
        msg = f"unknown pipeline: {pipeline!r}"
        raise ValueError(msg)
    cur = now or datetime.now(UTC)
    cur_iso = to_iso(cur)
    con = _connect(db_path)
    prior_isolation = con.isolation_level
    # Explicit transaction control below; the implicit DEFERRED wrapper cannot
    # express "take the writer lock before the read".
    con.isolation_level = None
    try:
        # BEGIN IMMEDIATE, not DEFERRED: the counter is a read-modify-write, so
        # two concurrent enqueues of the SAME (pipeline, unit_id) must not both
        # read `attempts` = N and both write N + 1. Under-counting here does not
        # lose a row, it raises the ceiling `max_attempts` puts on Bedrock
        # spend for a unit that keeps failing. DEFERRED would start read-only
        # and upgrade mid-transaction, which is exactly the window that lets
        # both sides read the pre-increment value.
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT attempts FROM retry_queue WHERE pipeline = ? AND unit_id = ?",
            [pipeline, unit_id],
        ).fetchone()
        prev = int(row[0]) if row else 0
        attempts = prev + 1
        # The backoff stays in Python (`_backoff_delta`) rather than becoming
        # SQLite date arithmetic, so the schedule has one definition.
        next_at_iso = to_iso(cur + _backoff_delta(attempts))
        con.execute(
            "INSERT INTO retry_queue "
            "(pipeline, unit_id, error, attempts, next_attempt_at, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(pipeline, unit_id) DO UPDATE SET "
            "error = excluded.error, "
            "attempts = excluded.attempts, "
            "next_attempt_at = excluded.next_attempt_at, "
            "created_at = excluded.created_at, "
            "completed_at = NULL",
            [pipeline, unit_id, error[:2000], attempts, next_at_iso, cur_iso],
        )
        con.execute("COMMIT")
    finally:
        # Closing an open transaction rolls it back, so a raise between BEGIN
        # and COMMIT leaves the counter untouched rather than half-applied.
        con.isolation_level = prior_isolation
        con.close()
    return attempts


def drain(
    db_path: Path,
    *,
    pipeline: str,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    limit: int | None = None,
) -> list[str]:
    """Return unit_ids eligible for retry (not completed, attempts<max, due now)."""
    if not db_path.exists():
        return []
    cur = now or datetime.now(UTC)
    cur_iso = to_iso(cur)
    con = _connect(db_path)
    sql = (
        "SELECT unit_id FROM retry_queue "
        "WHERE pipeline = ? AND completed_at IS NULL "
        "  AND attempts < ? AND next_attempt_at <= ? "
        "ORDER BY next_attempt_at"
    )
    params: list[object] = [pipeline, max_attempts, cur_iso]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def blocked_units(
    db_path: Path,
    *,
    pipeline: str,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
) -> set[str]:
    """Unit_ids with a LIVE retry entry that must NOT be dispatched this run.

    Live = not completed, and either the attempts budget is exhausted
    (``attempts >= max_attempts``) or the backoff window has not elapsed
    (``next_attempt_at`` in the future). This is the complement of
    :func:`drain` over live rows — together they make the retry queue the
    single re-admission gate: a failed-and-live unit re-enters ONLY through
    ``drain``, never through the checkpoint path (a failed unit is not
    checkpointed, so ``filter_unchanged`` would otherwise re-admit it fresh
    on every run, forever — the uncapped-retry money leak).
    """
    if not db_path.exists():
        return set()
    cur_iso = to_iso(now or datetime.now(UTC))
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT unit_id FROM retry_queue "
            "WHERE pipeline = ? AND completed_at IS NULL "
            "  AND (attempts >= ? OR next_attempt_at > ?)",
            [pipeline, max_attempts, cur_iso],
        ).fetchall()
    finally:
        con.close()
    return {str(r[0]) for r in rows}


def mark_done(
    db_path: Path,
    *,
    pipeline: str,
    unit_ids: Iterable[str],
    now: datetime | None = None,
) -> int:
    """Mark the given unit_ids as completed. No-op if unknown."""
    ids = list(unit_ids)
    if not ids:
        return 0
    cur_iso = to_iso(now or datetime.now(UTC))
    con = _connect(db_path)
    try:
        con.executemany(
            "UPDATE retry_queue SET completed_at = ? "
            "WHERE pipeline = ? AND unit_id = ? AND completed_at IS NULL",
            [(cur_iso, pipeline, uid) for uid in ids],
        )
        con.commit()
    finally:
        con.close()
    return len(ids)


def pending_count(db_path: Path, *, pipeline: str) -> int:
    """Count not-yet-completed rows for one pipeline."""
    if not db_path.exists():
        return 0
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM retry_queue WHERE pipeline = ? AND completed_at IS NULL",
            [pipeline],
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


class SqliteRetryQueue:
    """The retry-queue port over one ``state.db``."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """The bound sqlite file."""
        return self._db_path

    def enqueue(self, *, pipeline: str, unit_id: str, error: str) -> int:
        """Record a failure; return the attempt counter."""
        return enqueue(self._db_path, pipeline=pipeline, unit_id=unit_id, error=error)

    def drain(
        self,
        *,
        pipeline: str,
        max_attempts: int = MAX_ATTEMPTS_DEFAULT,
        limit: int | None = None,
    ) -> list[str]:
        """Unit_ids eligible for retry (not done, attempts<max, due)."""
        return drain(self._db_path, pipeline=pipeline, max_attempts=max_attempts, limit=limit)

    def blocked_units(self, *, pipeline: str, max_attempts: int = MAX_ATTEMPTS_DEFAULT) -> set[str]:
        """Unit_ids with a live entry that must not be dispatched this run."""
        return blocked_units(self._db_path, pipeline=pipeline, max_attempts=max_attempts)

    def mark_done(self, *, pipeline: str, unit_ids: Iterable[str]) -> int:
        """Mark unit_ids completed; return the count marked."""
        return mark_done(self._db_path, pipeline=pipeline, unit_ids=unit_ids)

    def pending_count(self, *, pipeline: str) -> int:
        """Count not-yet-completed rows for one pipeline."""
        return pending_count(self._db_path, pipeline=pipeline)


__all__ = [
    "MAX_ATTEMPTS_DEFAULT",
    "SqliteRetryQueue",
    "blocked_units",
    "drain",
    "enqueue",
    "mark_done",
    "pending_count",
]
