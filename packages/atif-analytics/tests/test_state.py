# SPDX-License-Identifier: Apache-2.0

"""Checkpoint skip logic, retry drain/backoff, and the parquet cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from atif_analytics.application.use_cases import _shared
from atif_analytics.infrastructure.parquet_cache import (
    MIN_PARQUET_BYTES,
    PART_GLOB,
    ParquetCache,
    count_rows,
    iter_part_files,
    replace_sessions,
)
from atif_analytics.infrastructure.sqlite_state import checkpointer, retry_queue

T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_skip_unchanged(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, T0)])
    pending, skipped = checkpointer.filter_unchanged(
        [("s1", T0, T0), ("s2", T0, T0)],
        pipeline="classify",
        checkpoint_db_path=db,
    )
    assert pending == ["s2"]
    assert skipped == 1


def test_checkpoint_readmits_on_advanced_ts(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, T0)])
    pending, skipped = checkpointer.filter_unchanged(
        [("s1", T1, T0)], pipeline="classify", checkpoint_db_path=db
    )
    assert pending == ["s1"]
    assert skipped == 0


def test_checkpoint_readmits_on_advanced_mtime(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, T0)])
    pending, _ = checkpointer.filter_unchanged(
        [("s1", T0, T1)], pipeline="classify", checkpoint_db_path=db
    )
    assert pending == ["s1"]


def test_checkpoint_none_bound_means_pending(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, None)])
    pending, _ = checkpointer.filter_unchanged(
        [("s1", T0, T0)], pipeline="classify", checkpoint_db_path=db
    )
    assert pending == ["s1"]


def test_checkpoint_is_per_pipeline(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, T0)])
    pending, _ = checkpointer.filter_unchanged(
        [("s1", T0, T0)], pipeline="trajectory", checkpoint_db_path=db
    )
    assert pending == ["s1"]


def test_checkpoint_upsert_overwrites(db: Path) -> None:
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T0, T0)])
    checkpointer.mark_completed(db, pipeline="classify", rows=[("s1", T1, T1)])
    assert checkpointer.count_rows(db) == 1
    loaded = checkpointer.load_as_map(db, "classify")
    assert loaded["s1"] == (T1, T1)


# ---------------------------------------------------------------------------
# Retry queue
# ---------------------------------------------------------------------------


def test_retry_enqueue_and_drain_after_backoff(db: Path) -> None:
    attempts = retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="boom", now=T0)
    assert attempts == 1
    # Not yet due (backoff = 2 min).
    assert retry_queue.drain(db, pipeline="classify", now=T0) == []
    assert retry_queue.drain(db, pipeline="classify", now=T0 + timedelta(minutes=3)) == ["s1"]


def test_retry_backoff_escalates(db: Path) -> None:
    retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="e1", now=T0)
    attempts = retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="e2", now=T0)
    assert attempts == 2
    # Second failure → backoff 4 min.
    assert retry_queue.drain(db, pipeline="classify", now=T0 + timedelta(minutes=3)) == []
    assert retry_queue.drain(db, pipeline="classify", now=T0 + timedelta(minutes=5)) == ["s1"]


def test_retry_mark_done_stops_drain(db: Path) -> None:
    retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="boom", now=T0)
    retry_queue.mark_done(db, pipeline="classify", unit_ids=["s1"])
    assert retry_queue.drain(db, pipeline="classify", now=T0 + timedelta(hours=2)) == []
    assert retry_queue.pending_count(db, pipeline="classify") == 0


def test_retry_max_attempts_caps_drain(db: Path) -> None:
    for i in range(5):
        retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error=f"e{i}", now=T0)
    assert retry_queue.drain(db, pipeline="classify", now=T0 + timedelta(days=1)) == []


def test_blocked_units_covers_exhausted_and_backing_off(db: Path) -> None:
    # s1: attempts exhausted — blocked forever (drain never re-admits it).
    for i in range(5):
        retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error=f"e{i}", now=T0)
    # s2: one failure — blocked while the 2-min backoff runs, then drainable.
    retry_queue.enqueue(db, pipeline="classify", unit_id="s2", error="e", now=T0)
    # s3: completed — never blocked.
    retry_queue.enqueue(db, pipeline="classify", unit_id="s3", error="e", now=T0)
    retry_queue.mark_done(db, pipeline="classify", unit_ids=["s3"])

    assert retry_queue.blocked_units(db, pipeline="classify", now=T0) == {"s1", "s2"}
    # After the backoff elapses s2 becomes drain's business, not blocked.
    later = T0 + timedelta(minutes=3)
    assert retry_queue.blocked_units(db, pipeline="classify", now=later) == {"s1"}
    assert retry_queue.drain(db, pipeline="classify", now=later) == ["s2"]


def test_blocked_units_missing_db_is_empty(tmp_path: Path) -> None:
    assert retry_queue.blocked_units(tmp_path / "absent.db", pipeline="classify") == set()


def test_retry_unknown_pipeline_rejected(db: Path) -> None:
    with pytest.raises(ValueError, match="unknown pipeline"):
        retry_queue.enqueue(db, pipeline="nope", unit_id="s1", error="e")


def test_pipeline_names_are_the_contract_five() -> None:
    """The five pipelines the checkpoint and retry tables accept."""
    assert checkpointer.PIPELINE_NAMES == (
        "classify",
        "trajectory",
        "conflicts",
        "user_friction",
        "perceived",
    )


#: Number words the ``_shared`` docstring may use for its pipeline total, so the
#: prose count follows ``PIPELINE_NAMES`` with no second place to edit.
_PIPELINE_COUNT_WORDS: dict[int, str] = {4: "four", 5: "five", 6: "six", 7: "seven"}


def test_shared_docstring_describes_every_llm_pipeline() -> None:
    """``_shared``'s docstring must name each LLM pipeline and state the real count.

    ``_shared`` is imported by exactly the LLM pipelines, so its docstring is
    where a reader learns how many there are and which ones spend money at
    Bedrock. A count written in prose drifts silently as pipelines are added;
    ``PIPELINE_NAMES`` is the source of truth and this pins the prose to it.
    """
    doc = _shared.__doc__
    assert doc is not None
    missing = [name for name in checkpointer.PIPELINE_NAMES if name not in doc]
    assert not missing, f"the _shared docstring does not name these pipelines: {missing}"
    word = _PIPELINE_COUNT_WORDS.get(len(checkpointer.PIPELINE_NAMES))
    assert word is not None, f"add {len(checkpointer.PIPELINE_NAMES)} to _PIPELINE_COUNT_WORDS"
    assert f"the {word} LLM pipelines" in doc, (
        f"the _shared docstring must say 'the {word} LLM pipelines'"
    )


# ---------------------------------------------------------------------------
# Parquet cache
# ---------------------------------------------------------------------------


def _df(sids: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"session_id": sids, "v": list(range(len(sids)))})


def test_sharded_write_and_read(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path / "cache_dir")
    cache.write_part(_df(["a", "b"]))
    cache.write_part(_df(["c"]))
    assert len(iter_part_files(tmp_path / "cache_dir")) == 2
    out = cache.read_all()
    assert out is not None
    assert set(out["session_id"].to_list()) == {"a", "b", "c"}
    assert cache.count_rows() == 3
    # Column pushdown.
    proj = cache.read_all(columns=["session_id"])
    assert proj is not None
    assert proj.columns == ["session_id"]


def test_replace_sessions_drops_and_unlinks_empty_shards(tmp_path: Path) -> None:
    target = tmp_path / "cache_dir"
    cache = ParquetCache(target)
    cache.write_part(_df(["a", "b"]))
    cache.write_part(_df(["a"]))
    removed = replace_sessions(target, key_column="session_id", session_ids=["a"])
    assert removed == 2
    out = cache.read_all()
    assert out is not None
    assert out["session_id"].to_list() == ["b"]
    # The all-"a" shard was unlinked.
    assert len(iter_part_files(target)) == 1


def test_read_all_empty_returns_none(tmp_path: Path) -> None:
    assert ParquetCache(tmp_path / "nothing").read_all() is None


def test_torn_shard_is_skipped_by_every_reader(tmp_path: Path) -> None:
    """A half-flushed shard must not fail a read that could skip it.

    `write_part` drops new shards straight into the directory readers glob, so
    a crashed writer's stub is live the moment it appears. Reading one raises
    inside polars, which would take down a whole pipeline over a file carrying
    no rows.
    """
    target = tmp_path / "cache_dir"
    cache = ParquetCache(target)
    cache.write_part(_df(["a", "b"]))
    torn = target / "part-99999999999999999.parquet"
    torn.write_bytes(b"PAR1")
    assert torn.stat().st_size <= MIN_PARQUET_BYTES
    assert len(sorted(target.glob(PART_GLOB))) == 2, "the torn shard is in the live glob"

    assert torn not in iter_part_files(target)
    out = cache.read_all()
    assert out is not None
    assert out["session_id"].to_list() == ["a", "b"]
    assert count_rows(target) == 2
    assert replace_sessions(target, key_column="session_id", session_ids=["a"]) == 1


def test_retry_attempts_survive_concurrent_enqueues(db: Path) -> None:
    """The attempt counter is a spend cap, so a lost increment raises the ceiling.

    Two writers hammering ONE (pipeline, unit_id) must land 2 * N increments.
    A read-modify-write under a DEFERRED transaction lets both read the same
    value and both write N + 1, and every lost increment buys the failing unit
    another Bedrock call before `max_attempts` stops it.
    """
    import threading

    rounds = 40
    workers = 2
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def hammer() -> None:
        barrier.wait()
        for _ in range(rounds):
            try:
                retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="boom")
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)
                return

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"enqueue raised under contention: {errors!r}"
    final = retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="boom")
    assert final == rounds * workers + 1


def test_retry_enqueue_rolls_back_a_failed_increment(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise between BEGIN and COMMIT must leave the counter where it was.

    `enqueue` restores `isolation_level` in a `finally` before closing. Setting
    that attribute to a STRING stores it; only setting it to None commits a
    pending transaction — so the restore cannot silently commit a half-applied
    increment, and `close()` discards the open transaction instead.
    """
    retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="first")
    assert retry_queue.pending_count(db, pipeline="classify") == 1

    def _boom(_attempts: int) -> timedelta:
        msg = "backoff exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(retry_queue, "_backoff_delta", _boom)
    with pytest.raises(RuntimeError, match="backoff exploded"):
        retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="second")
    monkeypatch.undo()

    # The failed call neither incremented nor overwrote the stored error.
    assert retry_queue.enqueue(db, pipeline="classify", unit_id="s1", error="third") == 2
