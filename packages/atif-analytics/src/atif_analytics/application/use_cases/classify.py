# SPDX-License-Identifier: Apache-2.0

"""Session classification pipeline over the materialized corpus.

Reads complete session transcripts from the MATERIALIZED corpus and emits one
row per session into the ``session_classifications`` sharded cache with
autonomy_tier, work_category, success, goal, and confidence fields.
Pull-once / write-many shape: anti-join against the parquet, dispatch
parallel structured-output calls under the provider's concurrency limiter,
write results in chunks of ``max(batch_size * 4, 256)`` for crash-resilience.

Transcript text is sent to a third-party model provider on this path: a
session's full rendered body, tool arguments and tool results included,
leaves this machine.

Error routing (CONTRACT-V2 taxonomy): :class:`RefusalError` is terminal —
the session is checkpointed with a DOCUMENTED sentinel audit row
(``autonomy_tier/work_category/success='unknown'``, ``goal='[refused]'``,
``confidence=0.0``) so the refusal is queryable and never re-billed;
:class:`ProviderUnavailable` (and any other exception) goes to the retry
queue for a later run.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import CLASSIFY_SYSTEM_PROMPT
from atif_analytics.application.use_cases._shared import (
    RunBudget,
    build_cache,
    build_checkpoint,
    build_provider,
    build_retry_queue,
    gather_under_budget,
    pipeline_usage,
    write_chunk_size,
)
from atif_analytics.domain.costs import estimate_cost_tokens, tokens_for_chars
from atif_analytics.domain.models import SessionClassification
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.sqlite_state import checkpointer

if TYPE_CHECKING:
    from atif_analytics.infrastructure.parquet_cache import ParquetCache
    from atif_analytics.infrastructure.settings import AnalyticsSettings
    from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint
    from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
    from atif_models.domain.ports import LlmStructuredProvider
    from atif_models.domain.registry import ModelSpec

_PARQUET_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "autonomy_tier": pl.Utf8,
    "work_category": pl.Utf8,
    "success": pl.Utf8,
    "goal": pl.Utf8,
    "confidence": pl.Float32,
    "classified_at": pl.Datetime("us", "UTC"),
}

#: Dry-run OUTPUT budget per session (~300 structured-output tokens). Input
#: is MEASURED from the rendered transcript (chars/4), never a static prior:
#: a real transcript runs an order of magnitude past any fixed guess.
_AVG_OUT_TOKENS = 300


def _already_done(cache: ParquetCache) -> set[str]:
    """Session ids already present in the cache (anti-join key)."""
    done_df = cache.read_all(columns=["session_id"])
    if done_df is not None and done_df.height > 0:
        return set(done_df["session_id"].to_list())
    return set()


async def _classify_async(
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
    """Async implementation behind :func:`classify_sessions`."""
    already = _already_done(cache)

    # Checkpoint skip: compare current (last_ts, mtime) against the last run.
    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline="classify",
        checkpoint_db_path=checkpoint.db_path,
    )
    keep = set(unchanged_pending)

    # Retry queue: pull pending retries first so they're re-admitted even
    # when the checkpoint would otherwise skip them.
    retry_ids = set(retry.drain(pipeline="classify"))
    if retry_ids:
        logger.info("classify: draining {} retry-queue entries", len(retry_ids))
        keep |= retry_ids

    # Retry queue is the SINGLE re-admission gate for failed units: a session
    # with a live entry (attempts exhausted OR backoff not yet elapsed) is
    # never checkpointed, so the checkpoint path would re-admit — and re-bill —
    # it fresh on every run. Skip it here; drain re-admits when due.
    blocked = retry.blocked_units(pipeline="classify")
    if blocked & keep:
        logger.info(
            "classify: skipping {} sessions with live retry entries (backoff/attempts cap)",
            len(blocked & keep),
        )
        keep -= blocked

    # Session ceiling applied DURING the walk, not after: bounds is
    # newest-first and the cap keeps the freshest sessions, so admitting
    # past it only to slice back would render transcripts (up to 800K chars
    # each) for sessions that can never run this tick. On a backlogged
    # corpus that is the difference between 50 rendered strings and
    # thousands held at once.
    max_sessions = settings.llm_max_sessions_per_run
    pending: list[tuple[str, str]] = []
    empty_sids: list[str] = []
    deferred = 0
    for sid in bounds:
        if sid in already and sid not in retry_ids:
            continue
        if sid not in keep:
            continue
        if len(pending) >= max_sessions:
            deferred += 1
            continue
        text = reader.session_text(sid)
        if not text:
            # A session that renders to nothing yields no row and no LLM
            # call, so without a checkpoint it re-renders every tick
            # forever. Stamp it at current bounds; growth re-admits it.
            empty_sids.append(sid)
            continue
        pending.append((sid, text))

    if empty_sids:
        checkpoint.mark_completed(
            pipeline="classify",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in empty_sids],
        )
        logger.info(
            "classify: checkpointed {} sessions with empty transcripts as skipped",
            len(empty_sids),
        )

    if deferred:
        logger.warning(
            "classify: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            deferred,
        )

    if not pending:
        logger.info("classify: no pending sessions (skipped={} via checkpoint)", skipped)
        return 0
    if skipped:
        logger.info("classify: skipped {} sessions via checkpoint", skipped)

    chunk_size = write_chunk_size(settings.batch_size)
    logger.info("classify: {} pending, chunks of {}", len(pending), chunk_size)

    written = 0
    i = 0
    chunk_index = 0
    total_chunks = (len(pending) + chunk_size - 1) // chunk_size
    while i < len(pending):
        chunk_index += 1
        t0 = time.monotonic()
        # Dispatch in budget-checked sub-batches. One gather over the whole
        # write chunk would buy every remaining call before the ceiling was
        # observable again: chunk_size is 384 by default against a session
        # ceiling of 50, so the chunk loop alone checks exactly once, before
        # any call. ``i`` advances by what was actually SENT, so a budget
        # stop mid-chunk leaves the rest of the chunk unstamped rather than
        # silently skipped.
        chunk, results = await gather_under_budget(
            pending[i : i + chunk_size],
            make_coro=lambda unit: provider.classify_structured(
                system=CLASSIFY_SYSTEM_PROMPT,
                prompt=unit[1],
                schema=SessionClassification,
            ),
            budget=budget,
        )
        elapsed = time.monotonic() - t0
        if not chunk:
            # Abort remaining work cleanly: unstarted sessions get no
            # checkpoint/cache/retry stamp, so the next run picks them up.
            logger.warning(
                "classify: cost ceiling hit (${:.2f} >= ${:.2f}) — "
                "aborting with {} sessions unstarted (nothing stamped)",
                budget.spent_usd() if budget is not None else 0.0,
                budget.max_cost_usd if budget is not None else 0.0,
                len(pending) - i,
            )
            break
        i += len(chunk)

        now = datetime.now(UTC)
        ok_rows: list[dict[str, Any]] = []
        refused_sids: list[str] = []
        errors = 0
        for (sid, _), res in zip(chunk, results, strict=True):
            if isinstance(res, BaseException):
                from atif_models.domain.ports import RefusalError

                if isinstance(res, RefusalError):
                    # Terminal: write the DOCUMENTED refusal sentinel row
                    # (autonomy_tier/work_category/success='unknown',
                    # goal='[refused]', confidence 0.0) so refusal-skips are
                    # queryable, then checkpoint so we never re-bill. The
                    # sentinel is semantically inert for the views: 'unknown'
                    # is already a legal success value, the fake categorical
                    # values sit at confidence 0.0, and goal='[refused]' is
                    # the audit marker. (conflicts, whose rows are uuid-keyed
                    # pairs, uses the refusals sidecar instead.)
                    logger.info("classify: {} refused (terminal) — sentinel row", sid)
                    refused_sids.append(sid)
                    ok_rows.append(
                        {
                            "session_id": sid,
                            "autonomy_tier": "unknown",
                            "work_category": "unknown",
                            "success": "unknown",
                            "goal": "[refused]",
                            "confidence": 0.0,
                            "classified_at": now,
                        }
                    )
                    continue
                errors += 1
                logger.warning("classify: {} failed (queued for retry): {}", sid, res)
                retry.enqueue(pipeline="classify", unit_id=sid, error=str(res))
                continue
            ok_rows.append(
                {
                    "session_id": sid,
                    "autonomy_tier": res.autonomy_tier,
                    "work_category": res.work_category,
                    "success": res.success,
                    "goal": res.goal,
                    "confidence": float(res.confidence),
                    "classified_at": now,
                }
            )

        if ok_rows:
            cache.write_part(pl.DataFrame(ok_rows, schema=_PARQUET_SCHEMA))

        # Checkpoint at CURRENT bounds so a later rerun with no new steps is
        # a no-op; refused sessions checkpoint too (terminal, via their
        # sentinel rows already in ok_rows). Clear the completed sessions
        # from the retry queue.
        done_sids = [row["session_id"] for row in ok_rows]
        if done_sids:
            checkpoint.mark_completed(
                pipeline="classify",
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in done_sids],
            )
            retry.mark_done(pipeline="classify", unit_ids=done_sids)

        written += len(ok_rows)
        logger.info(
            "classify chunk {}/{}: {} ok, {} refused, {} errors, {:.1f}s",
            chunk_index,
            total_chunks,
            len(ok_rows),
            len(refused_sids),
            errors,
            elapsed,
        )

    logger.info("classify: wrote {} total rows", written)
    return written


def _dry_run_plan(
    settings: AnalyticsSettings,
    *,
    reader: CorpusReader,
    cache: ParquetCache,
    spec: ModelSpec,
    since_days: int | None,
    limit: int | None,
) -> dict[str, Any]:
    """The classify ``--dry-run`` plan dict, with measured token counts.

    Input tokens are MEASURED per pending session from the actual rendered
    transcript (chars/4) instead of a static average, and the plan surfaces
    the budget ceilings plus the capped session count — what WOULD run.
    """
    already = _already_done(cache)
    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    pending = [sid for sid in bounds if sid not in already]
    max_sessions = settings.llm_max_sessions_per_run
    capped = pending[:max_sessions]  # newest-first, same cap as the real run
    in_tokens = sum(tokens_for_chars(len(reader.session_text(sid))) for sid in capped)
    out_tokens = len(capped) * _AVG_OUT_TOKENS
    pricing = (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
    cost = estimate_cost_tokens(in_tokens, out_tokens, pricing)
    logger.info(
        "classify --dry-run: {} sessions pending ({} would run under the cap). "
        "Estimated cost ~${:.2f} (model={})",
        len(pending),
        len(capped),
        cost,
        spec.model_id,
    )
    return {
        "pipeline": "classify",
        "candidates": len(pending),
        "capped_candidates": len(capped),
        "llm_calls": len(capped),
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


def classify_sessions(
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
    """Classify pending sessions; return the row count (or a dry-run plan dict).

    ``dry_run`` defaults to True (CONTRACT-V2 cost guard). The storage
    ports and the reader default to the settings-derived adapters;
    ``provider`` defaults to the registry-resolved OpenAI Bedrock adapter
    (one instance per run, usage logged at the end). ``budget`` is the
    cross-pipeline cost ceiling from ``run_analyze``; it is re-checked every
    :data:`~atif_analytics.application.use_cases._shared.BUDGET_CHECK_BATCH`
    sessions, and when it trips the remaining sessions are left unstamped.
    Overshoot is bounded by the batch in flight at the crossing, not by the
    session ceiling.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.classifications_dir)

    if provider is None or spec is None:
        provider, spec = build_provider(settings, "classify")

    if dry_run:
        return _dry_run_plan(
            settings, reader=reader, cache=cache, spec=spec, since_days=since_days, limit=limit
        )

    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)
    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)
    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, "classify"):
        return asyncio.run(
            _classify_async(
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


__all__ = ["classify_sessions"]
