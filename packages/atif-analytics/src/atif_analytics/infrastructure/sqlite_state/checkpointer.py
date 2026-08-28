# SPDX-License-Identifier: Apache-2.0

"""Per-(session_id, pipeline) checkpoint backed by a SQLite WAL file.

Tracks when each LLM pipeline last processed each session so re-runs skip
sessions whose transcripts have not advanced. One row per
``(session_id, pipeline)``; UPSERT via ``INSERT ... ON CONFLICT DO UPDATE``.

Schema::

    CREATE TABLE session_checkpoint (
        session_id            TEXT NOT NULL,
        pipeline              TEXT NOT NULL,
        last_ts_processed     TEXT,
        last_mtime_processed  TEXT,
        completed_at          TEXT NOT NULL,
        PRIMARY KEY (session_id, pipeline)
    );

All timestamps are stored as ISO-8601 UTC strings; they sort
lexicographically. WAL journal mode lets multiple readers run concurrently
with one writer; ``busy_timeout`` absorbs transient writer contention.
The file lives at ``<corpus_root>/analytics/state.db`` — corpus-scoped, so
one corpus's completions can never skip another's re-scoring.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

PIPELINE_NAMES: tuple[str, ...] = (
    "classify",
    "trajectory",
    "conflicts",
    "user_friction",
    "perceived",
)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS session_checkpoint (
    session_id            TEXT NOT NULL,
    pipeline              TEXT NOT NULL,
    last_ts_processed     TEXT,
    last_mtime_processed  TEXT,
    completed_at          TEXT NOT NULL,
    PRIMARY KEY (session_id, pipeline)
);
"""

# Starvation counter: one row per budget-skipped stage per run. A stage
# that actually runs clears its rows, so the row count IS the consecutive
# budget-skip streak (run_analyze escalates to ERROR at >= 3).
_CREATE_BUDGET_SKIPS_SQL = """
CREATE TABLE IF NOT EXISTS budget_skips (
    pipeline TEXT NOT NULL,
    run_at   TEXT NOT NULL
);
"""

# Process-local set of paths whose schema has already been bootstrapped this
# process. Skipping the redundant ``CREATE TABLE IF NOT EXISTS`` on every
# ``connect`` call avoids racing the writer lock when concurrent threads
# open the same file: on a cold start the WAL transition and the CREATE
# both take the writer lock, and racing them raises "database is locked".
_SCHEMA_BOOTSTRAPPED: set[str] = set()
_SCHEMA_BOOTSTRAP_LOCK = threading.Lock()


def to_iso(dt: datetime | None) -> str | None:
    """ISO-8601 UTC string, or None."""
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _from_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string back to a tz-aware UTC datetime."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def connect(path: Path) -> sqlite3.Connection:
    """Open the SQLite checkpoint DB and ensure tables exist.

    Per-connection PRAGMAs run every open; the file-level WAL/synchronous
    setup + DDL run once per (process, path) under a lock, because the WAL
    transition and ``CREATE TABLE`` both grab the writer lock and racing
    them on cold start raises ``database is locked``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    key = str(path.resolve())
    with _SCHEMA_BOOTSTRAP_LOCK:
        if key not in _SCHEMA_BOOTSTRAPPED:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute(_CREATE_TABLES_SQL)
            con.execute(_CREATE_BUDGET_SKIPS_SQL)
            _SCHEMA_BOOTSTRAPPED.add(key)
    # Writes wrap in implicit BEGIN/COMMIT from here on.
    con.isolation_level = "DEFERRED"
    return con


def load_as_map(db_path: Path, pipeline: str) -> dict[str, tuple[datetime | None, datetime | None]]:
    """Return ``{session_id: (last_ts, last_mtime)}`` for one pipeline.

    Empty dict when the DB doesn't exist yet or the pipeline has no rows.
    """
    if not db_path.exists():
        return {}
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT session_id, last_ts_processed, last_mtime_processed "
            "FROM session_checkpoint WHERE pipeline = ?",
            [pipeline],
        ).fetchall()
    finally:
        con.close()
    return {
        str(sid): (_from_iso(last_ts), _from_iso(last_mtime)) for sid, last_ts, last_mtime in rows
    }


def filter_unchanged(
    candidates: Iterable[tuple[str, datetime | None, datetime | None]],
    *,
    pipeline: str,
    checkpoint_db_path: Path,
) -> tuple[list[str], int]:
    """Drop sessions whose ``(last_ts, last_mtime)`` has not advanced.

    ``candidates`` is an iterable of ``(session_id, current_last_ts,
    current_last_mtime)``. Returns ``(pending_session_ids, skipped_count)``.

    A session is skipped iff a checkpoint row exists for ``pipeline`` AND
    both ``current_last_ts <= ckpt.last_ts`` AND ``current_last_mtime <=
    ckpt.last_mtime``. Either bound moving forward invalidates the skip.
    """
    ckpt = load_as_map(checkpoint_db_path, pipeline)
    pending: list[str] = []
    skipped = 0
    for sid, cur_ts, cur_mtime in candidates:
        prev = ckpt.get(sid)
        if prev is None:
            pending.append(sid)
            continue
        prev_ts, prev_mtime = prev
        if _stale_or_equal(cur_ts, prev_ts) and _stale_or_equal(cur_mtime, prev_mtime):
            skipped += 1
            continue
        pending.append(sid)
    return pending, skipped


def _stale_or_equal(cur: datetime | None, prev: datetime | None) -> bool:
    """True iff both are present and ``cur`` has not advanced past ``prev``.

    None on either side returns False (treat as advanced).
    """
    if cur is None or prev is None:
        return False
    cur_aware = cur.astimezone(UTC) if cur.tzinfo else cur.replace(tzinfo=UTC)
    prev_aware = prev.astimezone(UTC) if prev.tzinfo else prev.replace(tzinfo=UTC)
    return cur_aware <= prev_aware


def mark_completed(
    db_path: Path,
    *,
    pipeline: str,
    rows: Iterable[tuple[str, datetime | None, datetime | None]],
) -> int:
    """Upsert checkpoint rows for ``(session_id, pipeline)``.

    Each row is ``(session_id, last_ts_processed, last_mtime_processed)``.
    ``completed_at`` is stamped with ``datetime.now(UTC)``. Returns the
    number of upserted rows; an empty ``rows`` leaves the DB untouched.
    """
    incoming = list(rows)
    if not incoming:
        return 0
    now_iso = to_iso(datetime.now(UTC))
    payload = [
        (sid, pipeline, to_iso(last_ts), to_iso(last_mtime), now_iso)
        for sid, last_ts, last_mtime in incoming
    ]
    con = connect(db_path)
    try:
        con.executemany(
            "INSERT INTO session_checkpoint "
            "(session_id, pipeline, last_ts_processed, last_mtime_processed, completed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, pipeline) DO UPDATE SET "
            "last_ts_processed = excluded.last_ts_processed, "
            "last_mtime_processed = excluded.last_mtime_processed, "
            "completed_at = excluded.completed_at",
            payload,
        )
        con.commit()
    finally:
        con.close()
    return len(incoming)


def record_budget_skip(db_path: Path, pipeline: str, *, now: datetime | None = None) -> int:
    """Append one budget-skip row for ``pipeline``; return the streak length.

    The streak is the number of consecutive runs this stage was skipped
    with ``budget_exhausted`` — a successful run clears the rows via
    :func:`clear_budget_skips`, so a plain count is the streak.
    """
    con = connect(db_path)
    try:
        con.execute(
            "INSERT INTO budget_skips (pipeline, run_at) VALUES (?, ?)",
            [pipeline, to_iso(now or datetime.now(UTC))],
        )
        con.commit()
        row = con.execute(
            "SELECT count(*) FROM budget_skips WHERE pipeline = ?", [pipeline]
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 1


def clear_budget_skips(db_path: Path, pipeline: str) -> None:
    """Reset ``pipeline``'s budget-skip streak (called when the stage runs)."""
    con = connect(db_path)
    try:
        con.execute("DELETE FROM budget_skips WHERE pipeline = ?", [pipeline])
        con.commit()
    finally:
        con.close()


def budget_skip_count(db_path: Path, pipeline: str) -> int:
    """Current consecutive budget-skip streak for ``pipeline`` (0 when absent)."""
    if not db_path.exists():
        return 0
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT count(*) FROM budget_skips WHERE pipeline = ?", [pipeline]
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def count_rows(db_path: Path) -> int:
    """Total number of checkpoint rows, or 0 when the DB is missing."""
    if not db_path.exists():
        return 0
    con = connect(db_path)
    try:
        row = con.execute("SELECT count(*) FROM session_checkpoint").fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


class SqliteCheckpoint:
    """The checkpoint port over one ``state.db``."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """The bound sqlite file."""
        return self._db_path

    def load_as_map(self, pipeline: str) -> dict[str, tuple[datetime | None, datetime | None]]:
        """``{session_id: (last_ts, last_mtime)}`` for one pipeline."""
        return load_as_map(self._db_path, pipeline)

    def filter_unchanged(
        self,
        candidates: Iterable[tuple[str, datetime | None, datetime | None]],
        *,
        pipeline: str,
    ) -> tuple[list[str], int]:
        """``(pending_session_ids, skipped_count)`` per the staleness rule."""
        return filter_unchanged(candidates, pipeline=pipeline, checkpoint_db_path=self._db_path)

    def mark_completed(
        self,
        *,
        pipeline: str,
        rows: Iterable[tuple[str, datetime | None, datetime | None]],
    ) -> int:
        """Upsert checkpoint rows; return the number upserted."""
        return mark_completed(self._db_path, pipeline=pipeline, rows=rows)

    def count_rows(self) -> int:
        """Total checkpoint rows (``0`` when absent)."""
        return count_rows(self._db_path)


__all__ = [
    "PIPELINE_NAMES",
    "SqliteCheckpoint",
    "budget_skip_count",
    "clear_budget_skips",
    "connect",
    "count_rows",
    "filter_unchanged",
    "load_as_map",
    "mark_completed",
    "record_budget_skip",
    "to_iso",
]
