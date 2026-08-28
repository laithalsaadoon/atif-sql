# SPDX-License-Identifier: Apache-2.0

"""Sharded parquet I/O helpers for the pipeline-append caches.

Each pipeline chunk writes ``<dir>/part-<ts_ns>.parquet`` and readers glob
the directory, so append cost is proportional to the chunk size rather than
the cache size. The legacy single-file branch is kept for tool-side
inspection of ad-hoc ``*.parquet`` targets (tests use it).

Public API: :func:`is_sharded_dir`, :func:`write_part`, :func:`read_all`,
:func:`iter_part_files`, :func:`count_rows`, :func:`replace_sessions`.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections.abc import Iterable
from pathlib import Path

import polars as pl
from loguru import logger

#: Glob pattern for shard part files within a sharded cache directory.
PART_GLOB: str = "part-*.parquet"

#: Smallest byte count a readable parquet file can have. The format's own
#: framing is ``PAR1`` + footer + 4-byte footer length + ``PAR1``, so anything
#: below this is a file a crashed or half-flushed writer left behind, not a
#: parquet with zero rows. Reading one raises inside polars, so the size check
#: is what keeps a torn artifact from failing a pipeline that could skip it.
MIN_PARQUET_BYTES: int = 16


def is_sharded_dir(path: Path) -> bool:
    """Return True iff ``path`` is (or should be) treated as a sharded cache directory.

    Two cases qualify: ``path`` exists and is a directory, or ``path`` does
    not exist yet and has no ``.parquet`` suffix (new caches default to the
    directory layout; a missing ``x.parquet`` stays legacy single-file).
    """
    if path.exists():
        return path.is_dir()
    return path.suffix != ".parquet"


def _is_whole_parquet(part: Path) -> bool:
    """False for a file too small to carry parquet framing; warns when it drops one.

    A shard at or below :data:`MIN_PARQUET_BYTES` is what a crashed or
    half-flushed writer leaves behind, and :func:`write_part` drops new
    shards straight into the directory readers glob, so the torn file is
    live from the moment it appears.
    """
    try:
        size = part.stat().st_size
    except OSError as exc:
        logger.warning("parquet cache: cannot stat {} ({}); skipping", part, exc)
        return False
    if size <= MIN_PARQUET_BYTES:
        logger.warning(
            "parquet cache: skipping torn shard {} ({} bytes, minimum {})",
            part,
            size,
            MIN_PARQUET_BYTES,
        )
        return False
    return True


def iter_part_files(target: Path) -> list[Path]:
    """Return a sorted list of whole parquet files backing ``target``.

    For a sharded directory: every ``part-*.parquet`` under it, sorted by
    name (timestamp-keyed, so the order is also chronological). For a
    legacy single-file path: ``[target]`` if it exists, else ``[]``.

    Torn files are dropped here rather than at each call site, because this
    is the one choke point every reader shares — :func:`read_all`,
    :func:`count_rows`, and :func:`replace_sessions` — so
    :data:`MIN_PARQUET_BYTES` governs the sharded read path and not only the
    legacy rewrite in :func:`write_part`.
    """
    if not target.exists():
        return []
    candidates = sorted(target.glob(PART_GLOB)) if target.is_dir() else [target]
    return [part for part in candidates if _is_whole_parquet(part)]


def write_part(target: Path, df: pl.DataFrame) -> Path:
    """Write ``df`` as a new shard (or rewrite the legacy single file).

    Sharded branch: drop a brand-new ``part-<ns>.parquet`` into the
    directory — no read-then-rewrite. Legacy branch: concat onto the
    existing file and rewrite (historical behavior).
    """
    if is_sharded_dir(target):
        target.mkdir(parents=True, exist_ok=True)
        # Nanosecond timestamps are sortable, monotonic on Linux, and avoid
        # filename collisions when two part-writes land in the same
        # millisecond under concurrency.
        part_path = target / f"part-{time.time_ns()}.parquet"
        df.write_parquet(part_path)
        return part_path

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > MIN_PARQUET_BYTES:
        existing = pl.read_parquet(target)
        df = pl.concat([existing, df], how="diagonal_relaxed")
    df.write_parquet(target)
    return target


def read_all(
    target: Path,
    *,
    columns: list[str] | None = None,
) -> pl.DataFrame | None:
    """Return the union of all part files (or the legacy single file).

    Returns ``None`` when the cache is empty or missing. ``columns``
    projects the read down via parquet column pushdown — callers that only
    need a key column (the anti-join sets) never decode the wide text
    columns.
    """
    parts = iter_part_files(target)
    if not parts:
        return None
    return pl.read_parquet([str(p) for p in parts], columns=columns)


def count_rows(target: Path) -> int:
    """Total row count across every part file (parquet footers only)."""
    parts = iter_part_files(target)
    if not parts:
        return 0
    import pyarrow.parquet as pq

    total = 0
    for p in parts:
        total += int(pq.ParquetFile(str(p)).metadata.num_rows)
    return total


def _shard_may_hold(part: Path, key_column: str, sorted_ids: list[str]) -> bool:
    """True when ``part``'s footer statistics do not rule out every wanted id.

    Reads each row group's ``[min, max]`` for ``key_column`` and asks whether
    ANY wanted id falls inside it, via a binary search over the sorted id
    list. That is a membership test, not a range-overlap test, and the
    distinction is what makes it pay on the shape the pipelines actually
    write: one random uuid session per shard. A ``[min(ids), max(ids)]``
    comparison spans nearly the whole uuid space there, so it rules out
    almost nothing; the membership test rules each shard out individually.
    (Only when keys happen to be near-sorted do the two converge — and that
    is not this cache's shape.)

    Anything unclear — no statistics, an unreadable footer — answers True,
    because a pruning check that guesses wrong drops rows.
    """
    import pyarrow.parquet as pq

    try:
        meta = pq.ParquetFile(str(part)).metadata
        names = list(meta.schema.names)
        if key_column not in names:
            # No key column, so no row here can match — the full-read path
            # would reach the same conclusion after decoding the shard.
            return False
        col_index = names.index(key_column)
        for group in range(meta.num_row_groups):
            stats = meta.row_group(group).column(col_index).statistics
            if stats is None or not stats.has_min_max:
                return True
            lo = str(stats.min)
            hi = str(stats.max)
            at = bisect_left(sorted_ids, lo)
            if at < len(sorted_ids) and sorted_ids[at] <= hi:
                return True
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(
            "replace_sessions: unreadable footer for {} ({}); reading in full", part, exc
        )
        return True
    return False


def replace_sessions(
    target: Path,
    *,
    key_column: str,
    session_ids: Iterable[str],
    skip_parts: Iterable[Path] = (),
) -> int:
    """Drop rows whose ``key_column`` is in ``session_ids`` across every shard.

    A re-admitted session may already have rows under an earlier shard;
    without this, those rows accumulate and every
    ``(session_id, prev_uuid, curr_uuid)`` pair duplicates on rerun.

    Call this ONCE per run with every id whose rows are being replaced: the
    scan walks the whole shard set, so a per-session call in a loop pays it
    once per session. ``skip_parts`` exempts shards the caller has just
    written, letting fresh rows be written before the replace runs. Shards
    with matches are rewritten in place; shards that become empty are
    unlinked. Returns the total rows removed.
    """
    ids = set(session_ids)
    if not ids:
        return 0
    parts = iter_part_files(target)
    if not parts:
        return 0
    exempt = {p.resolve() for p in skip_parts}
    sorted_ids = sorted(ids)
    removed_total = 0
    for part in parts:
        if part.resolve() in exempt:
            continue
        if not _shard_may_hold(part, key_column, sorted_ids):
            continue
        try:
            df = pl.read_parquet(part)
        except (OSError, pl.exceptions.ComputeError) as exc:
            # A truncated or unreadable shard must not block the replace.
            logger.warning("replace_sessions: unreadable shard {} ({}); skipping", part, exc)
            continue
        if key_column not in df.columns or df.height == 0:
            continue
        mask = df[key_column].is_in(list(ids))
        hit_count = int(mask.sum())
        if hit_count == 0:
            continue
        removed_total += hit_count
        kept = df.filter(~mask)
        if kept.height == 0:
            try:
                part.unlink()
            except OSError as exc:
                logger.warning("replace_sessions: failed to unlink empty shard {}: {}", part, exc)
            continue
        kept.write_parquet(part)
    if removed_total:
        logger.info(
            "replace_sessions: dropped {} row(s) for {} session(s) under {}",
            removed_total,
            len(ids),
            target,
        )
    return removed_total


class ParquetCache:
    """The cache port over one sharded-parquet target.

    Binds the target in the constructor; methods delegate to the
    module-level functions so tests can monkeypatch them.
    """

    def __init__(self, target: Path) -> None:
        self._target = target

    @property
    def target(self) -> Path:
        """The bound cache directory / legacy file path."""
        return self._target

    def write_part(self, df: pl.DataFrame) -> Path:
        """Append ``df`` as a new shard; return the written path."""
        return write_part(self._target, df)

    def read_all(self, *, columns: list[str] | None = None) -> pl.DataFrame | None:
        """Union of all parts (``None`` when empty)."""
        return read_all(self._target, columns=columns)

    def count_rows(self) -> int:
        """Total row count (footer reads only)."""
        return count_rows(self._target)

    def iter_part_files(self) -> list[Path]:
        """Sorted list of backing parquet files."""
        return iter_part_files(self._target)

    def replace_sessions(
        self,
        *,
        key_column: str,
        session_ids: Iterable[str],
        skip_parts: Iterable[Path] = (),
    ) -> int:
        """Drop rows keyed to ``session_ids``; return the removed count."""
        return replace_sessions(
            self._target,
            key_column=key_column,
            session_ids=session_ids,
            skip_parts=skip_parts,
        )


__all__ = [
    "MIN_PARQUET_BYTES",
    "PART_GLOB",
    "ParquetCache",
    "count_rows",
    "is_sharded_dir",
    "iter_part_files",
    "read_all",
    "replace_sessions",
    "write_part",
]
