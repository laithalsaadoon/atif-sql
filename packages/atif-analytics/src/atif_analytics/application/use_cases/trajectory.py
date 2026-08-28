# SPDX-License-Identifier: Apache-2.0

"""Per-session windowed trajectory pipeline (CONTRACT-V2 turn_window analogue).

Each session contributes one *window* per text step — pairing the prior
text step (``prev_uuid``) with the current one (``curr_uuid``). The first
window in a session has ``prev_uuid IS NULL`` plus a synthetic
``prev_sentiment`` of ``neutral`` so every text step gets exactly one row
in the output parquet. Windows come from the corpus reader's
``text_windows`` (main chain only, sidechain + compact-summary excluded;
uuid = first source_uuid — the turn_window analogue per CONTRACT-V2).

The model receives all windows for a session in one request body, batched
into chunks of ≤16 windows; consecutive chunks share an *anchor turn*.
The model returns :class:`TrajectoryArrayResult`; the host pipeline
verifies completeness by matching the returned ``(prev_uuid, curr_uuid)``
tuples against the requested ones. Missing windows trigger ONE bounded
retry of just the missing windows; persistent misses are stamped with
neutral placeholder rows so a single refusing chunk never wedges the
pipeline. A refused chunk becomes placeholders outright; any other error
enqueues the session for a later run.

Each session writes its own shard as it finishes, so a crash costs only
the sessions still in flight. ``replace_sessions`` scans the whole shard
set, so it is called ONCE per run over every session that flushed rather
than once per flushing session, with ``skip_parts`` exempting the shards
those sessions just wrote. Both call shapes preserve the same two
invariants — a session that produced nothing keeps the rows it already
serves, and a session that did produce rows never duplicates its window
pairs — so the batching buys scan cost, not correctness.

Transcript text is sent to a third-party model provider on this path: the
window bodies, clipped per :func:`format_chunk_xml`, leave this machine.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
import polars as pl
from loguru import logger

from atif_analytics.application.prompts import TRAJECTORY_SYSTEM_PROMPT
from atif_analytics.application.use_cases._shared import (
    BUDGET_CHECK_BATCH,
    RunBudget,
    build_cache,
    build_checkpoint,
    build_provider,
    build_retry_queue,
    pipeline_usage,
)
from atif_analytics.domain.costs import estimate_cost_tokens, tokens_for_chars
from atif_analytics.domain.models import TrajectoryArrayResult
from atif_analytics.domain.trajectory import (
    MAX_WINDOWS_PER_CHUNK,
    WindowRow,
    build_row,
    chunk_windows,
    format_chunk_xml,
    missing_keys,
    placeholder_row,
)
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.sqlite_state import checkpointer

if TYPE_CHECKING:
    from pathlib import Path

    from atif_analytics.infrastructure.parquet_cache import ParquetCache
    from atif_analytics.infrastructure.settings import AnalyticsSettings
    from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint
    from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
    from atif_models.domain.ports import LlmStructuredProvider
    from atif_models.domain.registry import ModelSpec

# Polars schema for the parquet shape — column types must match across
# reruns or the analytics view fails to bind.
_PARQUET_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "prev_uuid": pl.Utf8,
    "curr_uuid": pl.Utf8,
    "prev_sentiment": pl.Utf8,
    "curr_sentiment": pl.Utf8,
    "delta": pl.Float64,
    "is_transition": pl.Boolean,
    "transition_kind": pl.Utf8,
    "confidence": pl.Float32,
    "classified_at": pl.Datetime("us", "UTC"),
}

#: Dry-run OUTPUT budget per 16-window chunk (~800 tokens).
#: Input is MEASURED from the windows' actual clipped text lengths
#: (window count × chunk prompt size) — see :func:`_estimate_window_chars`.
_AVG_OUT_TOKENS = 800

#: Per-window XML envelope overhead in chars (<window>/<prev>/<curr> tags,
#: role/uuid attributes) plus the per-chunk system-prompt + schema-reminder
#: share — matches :func:`format_chunk_xml`'s rendering within noise.
_WINDOW_XML_OVERHEAD_CHARS = 220


def _estimate_window_chars(windows: list[WindowRow]) -> int:
    """Estimated prompt chars for a session's windows (clip parity with format_chunk_xml)."""
    total = 0
    for _sid, _prev_uuid, _curr_uuid, _prev_role, _curr_role, prev_text, curr_text in windows:
        total += min(len(prev_text or ""), 2000) + min(len(curr_text), 2000)
        total += _WINDOW_XML_OVERHEAD_CHARS
    return total


_SCHEMA_REMINDER = (
    "You will receive one or more <window> blocks. For EACH <window> block, "
    "emit one TrajectoryWindow object inside the top-level "
    '"windows" array. Echo the exact (prev_uuid, curr_uuid) from each '
    "window in your response — the host pipeline verifies completeness "
    "by uuid-pair match. Output JSON only, schema-conformant, no prose."
)


async def _classify_chunk(
    provider: LlmStructuredProvider,
    *,
    chunk: list[WindowRow],
) -> dict[tuple[str | None, str], dict[str, Any]] | BaseException:
    """Send one chunk through the provider; return the per-(prev,curr) map or the exception.

    Terminal failures are RETURNED (not raised) so the caller's dispatch
    loop routes them: a :class:`RefusalError` becomes neutral placeholders;
    anything else is enqueued for a later run.
    """
    payload_xml = format_chunk_xml(chunk)
    user_text = f"{_SCHEMA_REMINDER}\n\n{payload_xml}"
    try:
        result = await provider.classify_structured(
            system=TRAJECTORY_SYSTEM_PROMPT,
            prompt=user_text,
            schema=TrajectoryArrayResult,
        )
    except Exception as exc:  # noqa: BLE001 — routed by the caller; CancelledError still cancels
        return exc
    indexed: dict[tuple[str | None, str], dict[str, Any]] = {}
    for win in result.windows:
        # Keyed off the MODEL fields, not off `win_dict.get(...)`: the map's key type
        # is `tuple[str | None, str]` and `missing_keys` looks rows up with exactly
        # that shape, so a `.get` returning None for `curr_uuid` would file the
        # window under a key no lookup can hit and the turn would be stamped as
        # unanswered forever. `TrajectoryWindow.curr_uuid` is a required
        # `min_length=1` str, and reading the attribute is what says so.
        indexed[(win.prev_uuid, win.curr_uuid)] = win.model_dump()
    return indexed


async def _trajectory_async(
    settings: AnalyticsSettings,
    *,
    reader: CorpusReader,
    provider: LlmStructuredProvider,
    since_days: int | None,
    limit: int | None,
    cache: ParquetCache,
    checkpoint: SqliteCheckpoint,
    retry: SqliteRetryQueue,
    budget: RunBudget | None,
) -> int:
    """Async implementation behind :func:`trajectory_messages`."""
    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped_sessions = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline="trajectory",
        checkpoint_db_path=checkpoint.db_path,
    )
    active_sessions: set[str] = set(unchanged_pending)

    retry_sids = set(retry.drain(pipeline="trajectory"))
    if retry_sids:
        logger.info("trajectory: draining {} retry-queue entries", len(retry_sids))
        active_sessions |= retry_sids

    # Retry queue is the single re-admission gate for failed units (see
    # classify): a live entry blocks dispatch until drain says it's due.
    blocked = retry.blocked_units(pipeline="trajectory")
    if blocked & active_sessions:
        logger.info(
            "trajectory: skipping {} sessions with live retry entries (backoff/attempts cap)",
            len(blocked & active_sessions),
        )
        active_sessions -= blocked

    if skipped_sessions:
        logger.info("trajectory: skipped {} sessions via checkpoint", skipped_sessions)

    if not active_sessions:
        logger.info("trajectory: no sessions in window")
        return 0

    # Session ceiling: walk bounds (newest-first) so the cap keeps the
    # freshest sessions and defers the backlog to later runs (budget guard).
    max_sessions = settings.llm_max_sessions_per_run
    by_session: dict[str, list[WindowRow]] = defaultdict(list)
    total_windows = 0
    capped_out = 0
    windowless: list[str] = []
    for sid in bounds:
        if sid not in active_sessions:
            # (Retry-queue survivors whose session vanished from the corpus
            # drop out here too — they're not in bounds at all.)
            continue
        if len(by_session) >= max_sessions:
            capped_out += 1
            continue
        windows = reader.text_windows(sid)
        if windows:
            by_session[sid] = windows
            total_windows += len(windows)
        else:
            # A session with no windowable text steps yields no row and no
            # LLM call, so without a checkpoint it re-parses on every tick
            # forever. Stamp it at current bounds; growth re-admits it.
            windowless.append(sid)
    if capped_out:
        logger.warning(
            "trajectory: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            capped_out,
        )
    if windowless:
        checkpoint.mark_completed(
            pipeline="trajectory",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in windowless],
        )
        logger.info(
            "trajectory: checkpointed {} sessions with zero windows as skipped", len(windowless)
        )

    if not by_session:
        logger.info("trajectory: 0 windows pending after filtering")
        return 0

    logger.info("trajectory: {} sessions, {} total windows pending", len(by_session), total_windows)

    write_lock = anyio.Lock()
    written_box = [0]
    processed_sessions: set[str] = set()
    budget_hit = [False]
    # Sessions that landed rows this run, and the shards those rows went to.
    # A session absent from ``flushed_sessions`` produced nothing this run,
    # so the replace below leaves its prior rows alone — the retry queue only
    # re-admits it for a LATER run, and until then those rows are all the
    # parquet has for it.
    flushed_sessions: set[str] = set()
    fresh_parts: set[Path] = set()

    # Every session runs concurrently, so an unguarded per-chunk budget check
    # is a race: N sessions all read "not exhausted" and all dispatch, and
    # the ceiling is only visible once those calls return. This semaphore
    # caps calls in flight, and the budget is read while holding it, so the
    # overshoot is bounded by BUDGET_CHECK_BATCH chunks rather than by the
    # session count.
    dispatch_gate = anyio.Semaphore(BUDGET_CHECK_BATCH)

    async def _dispatch_chunk(chunk: list[WindowRow]) -> dict[Any, Any] | BaseException | None:
        """Send one chunk unless the budget is already spent (``None`` then)."""
        async with dispatch_gate:
            if budget is not None and budget.exhausted():
                return None
            return await _classify_chunk(provider, chunk=chunk)

    async def _process_session(sid: str, session_windows: list[WindowRow]) -> None:
        chunks = chunk_windows(session_windows)
        all_rows: list[dict[str, Any]] = []
        session_failed = False

        try:
            for chunk_idx, chunk in enumerate(chunks):
                t0 = time.monotonic()
                res = await _dispatch_chunk(chunk)
                if res is None:
                    # Cost ceiling: abandon THIS session's remaining chunks
                    # without stamping it — but flush the paid-for rows below.
                    # The replace on re-admission drops that partial flush, so
                    # the parquet stays duplicate-free.
                    if not budget_hit[0]:
                        budget_hit[0] = True
                        logger.warning(
                            "trajectory: cost ceiling hit (${:.2f} >= ${:.2f}) — "
                            "aborting remaining chunks (nothing stamped)",
                            budget.spent_usd() if budget is not None else 0.0,
                            budget.max_cost_usd if budget is not None else 0.0,
                        )
                    session_failed = True
                    break
                now = datetime.now(UTC)

                from atif_models.domain.ports import RefusalError

                if isinstance(res, RefusalError):
                    logger.info(
                        "trajectory: chunk {}/{} of session {} refused — neutral placeholders",
                        chunk_idx + 1,
                        len(chunks),
                        sid,
                    )
                    all_rows.extend(placeholder_row(sid, row[1], row[2], now) for row in chunk)
                    continue
                if isinstance(res, BaseException):
                    logger.warning(
                        "trajectory: chunk {}/{} of session {} failed ({}); enqueuing for retry",
                        chunk_idx + 1,
                        len(chunks),
                        sid,
                        res,
                    )
                    retry.enqueue(pipeline="trajectory", unit_id=sid, error=str(res))
                    session_failed = True
                    break

                indexed = res
                missing = missing_keys(chunk, indexed)

                # Bounded retry: re-request only the missing windows once.
                if missing:
                    logger.info(
                        "trajectory: chunk {}/{} of session {} missing {}/{} windows — retrying",
                        chunk_idx + 1,
                        len(chunks),
                        sid,
                        len(missing),
                        len(chunk),
                    )
                    retry_res = await _dispatch_chunk(missing)
                    now = datetime.now(UTC)
                    if isinstance(retry_res, dict):
                        for key, win in retry_res.items():
                            indexed[key] = win
                    still_missing = missing_keys(chunk, indexed)
                    all_rows.extend(
                        placeholder_row(sid, row[1], row[2], now) for row in still_missing
                    )
                    if still_missing:
                        logger.warning(
                            "trajectory: session {} chunk {}: {} window(s) "
                            "persistently missing — stamped neutral placeholders",
                            sid,
                            chunk_idx + 1,
                            len(still_missing),
                        )

                for row in chunk:
                    key = (row[1], row[2])
                    win = indexed.get(key)
                    if win is None:
                        continue
                    all_rows.append(build_row(sid, win, now))

                logger.info(
                    "trajectory: session {} chunk {}/{} done in {:.1f}s ({} windows)",
                    sid,
                    chunk_idx + 1,
                    len(chunks),
                    time.monotonic() - t0,
                    len(chunk),
                )
        except Exception as exc:  # noqa: BLE001 — non-cancel exceptions go to retry
            logger.warning("trajectory: session {} aborted ({}); enqueuing for retry", sid, exc)
            retry.enqueue(pipeline="trajectory", unit_id=sid, error=str(exc))
            session_failed = True

        if all_rows:
            # Flush even on failure: successful chunks are PAID work and must
            # never be discarded when a later chunk fails or the budget trips.
            # Each session writes its own shard so a crash loses at most the
            # sessions still in flight. The write + counters mutate under one
            # lock so the on-disk and in-memory state move together. A failed
            # session is flushed but NOT checkpointed/marked done: the retry
            # queue (or the checkpoint path, for a budget abort) re-admits it
            # whole next run.
            df = pl.DataFrame(all_rows, schema=_PARQUET_SCHEMA)
            async with write_lock:
                fresh_parts.add(cache.write_part(df))
                flushed_sessions.add(sid)
                written_box[0] += len(all_rows)
                if not session_failed:
                    processed_sessions.add(sid)

    async with anyio.create_task_group() as tg:
        for sid, session_windows in by_session.items():
            tg.start_soon(_process_session, sid, session_windows)

    written = written_box[0]

    if flushed_sessions:
        # ONE replace over exactly the sessions that produced fresh rows,
        # skipping the shards those rows just went to. Prior rows for a
        # re-flushed session must go or its window pairs duplicate; a session
        # that flushed nothing is excluded, so its prior rows stay served.
        removed = cache.replace_sessions(
            key_column="session_id",
            session_ids=list(flushed_sessions),
            skip_parts=fresh_parts,
        )
        if removed:
            logger.info("trajectory: replaced {} prior row(s) for re-admitted sessions", removed)

    if processed_sessions:
        checkpoint.mark_completed(
            pipeline="trajectory",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in processed_sessions],
        )
        retry.mark_done(pipeline="trajectory", unit_ids=list(processed_sessions))

    logger.info("trajectory: wrote {} windows across {} sessions", written, len(processed_sessions))
    return written


def trajectory_messages(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    reader: CorpusReader | None = None,
    provider: LlmStructuredProvider | None = None,
    spec: ModelSpec | None = None,
    cache: ParquetCache | None = None,
    checkpoint: SqliteCheckpoint | None = None,
    retry: SqliteRetryQueue | None = None,
    budget: RunBudget | None = None,
) -> int | dict[str, Any]:
    """Per-session windowed sentiment + transition classification.

    In ``--dry-run`` mode (the default) returns a plan dict; in real-run
    mode returns the count of windows written.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.trajectory_dir)

    if provider is None or spec is None:
        provider, spec = build_provider(settings, "trajectory")

    if dry_run:
        # Measured estimation: input tokens from window count × the windows'
        # actual clipped chunk-prompt sizes, capped like the real run.
        bounds = reader.session_bounds(since_days=since_days, limit=limit)
        max_sessions = settings.llm_max_sessions_per_run
        capped_sids = list(bounds)[:max_sessions]
        # Each call resends the system prompt + schema reminder, so the
        # envelope is charged once per chunk rather than once per session.
        per_call_envelope_chars = len(TRAJECTORY_SYSTEM_PROMPT) + len(_SCHEMA_REMINDER)
        turns = 0
        llm_calls = 0
        in_tokens = 0
        for sid in capped_sids:
            windows = reader.text_windows(sid)
            if not windows:
                continue
            n_chunks = (len(windows) + MAX_WINDOWS_PER_CHUNK - 1) // MAX_WINDOWS_PER_CHUNK
            turns += len(windows)
            llm_calls += n_chunks
            in_tokens += tokens_for_chars(
                _estimate_window_chars(windows) + n_chunks * per_call_envelope_chars
            )
        out_tokens = llm_calls * _AVG_OUT_TOKENS
        pricing = (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
        cost = estimate_cost_tokens(in_tokens, out_tokens, pricing)
        logger.info(
            "trajectory --dry-run: {} sessions ({} would run under the cap), "
            "{} text turns, ~{} LLM calls, est ${:.2f}",
            len(bounds),
            len(capped_sids),
            turns,
            llm_calls,
            cost,
        )
        return {
            "pipeline": "trajectory",
            "candidates": len(bounds),
            "capped_candidates": len(capped_sids),
            "turns": turns,
            "llm_calls": llm_calls,
            "estimated_input_tokens": in_tokens,
            "estimated_output_tokens": out_tokens,
            "estimated_cost_usd": round(cost, 4),
            "max_sessions_per_run": max_sessions,
            "max_cost_usd_per_run": settings.llm_max_cost_usd_per_run,
            "model": spec.model_id,
            "since_days": since_days,
            "limit": limit,
            "dry_run": True,
        }

    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)
    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)

    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, "trajectory"):
        return asyncio.run(
            _trajectory_async(
                settings,
                reader=reader,
                provider=provider,
                since_days=since_days,
                limit=limit,
                cache=cache,
                checkpoint=checkpoint,
                retry=retry,
                budget=budget,
            )
        )


__all__ = ["trajectory_messages"]
