# SPDX-License-Identifier: Apache-2.0

"""Stance-conflict detection pipeline over the materialized corpus.

Transcript text is sent to a third-party model provider on this path: a
session's full rendered body, tool arguments and tool results included,
leaves this machine.

Reads complete session transcripts WITH uuid headers (``include_uuids=True``
puts each text step's uuid — the FIRST source_uuid, per CONTRACT-V2 — in
the header as ``[uuid=... role ts]``) and emits one row per detected
conflict pair, keyed on ``(turn_a_uuid, turn_b_uuid)``. Sessions with no
conflicts produce ZERO rows. Refused sessions ALSO produce zero pair rows
(a sentinel pair would poison the uuid-keyed conversation-time joins);
the refusal instead lands as a durable audit row in the
``analytics/refusals`` sidecar (``{pipeline, unit_id, reason,
refused_at}``) so refusal-skips are queryable, not just log lines.

The uuid-validity guard validates the model's returned ``turn_*_uuid``
values against the session's ``edges.jsonl`` uuid set before they land in
the parquet — a pair naming a uuid that is not a real raw-record uuid can
never be recovered on a conversation-time axis, so it is dropped at the
source. The guard FAILS CLOSED: when the uuid universe cannot be read at
all, every pair from that session is dropped rather than admitted
unverified, because a wrong uuid in the parquet is indistinguishable from
a right one to every downstream view.

Output schema (per parquet shard)::

    session_id      VARCHAR
    turn_a_uuid     VARCHAR  (NOT NULL)
    turn_b_uuid     VARCHAR  (NOT NULL)
    conflict_kind   VARCHAR  (enum: disagreement|correction|reversal|impasse)
    severity        VARCHAR  (enum: low|medium|high)
    agent_position  VARCHAR
    user_position   VARCHAR
    confidence      DOUBLE
    detected_at     TIMESTAMP
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import CONFLICTS_SYSTEM_PROMPT
from atif_analytics.application.use_cases._shared import (
    RunBudget,
    build_cache,
    build_checkpoint,
    build_provider,
    build_retry_queue,
    gather_under_budget,
    pipeline_usage,
    write_chunk_size,
    write_refusal_audit,
)
from atif_analytics.domain.costs import estimate_cost_tokens, tokens_for_chars
from atif_analytics.domain.models import ConflictsResult
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
    "turn_a_uuid": pl.Utf8,
    "turn_b_uuid": pl.Utf8,
    "conflict_kind": pl.Utf8,
    "severity": pl.Utf8,
    "agent_position": pl.Utf8,
    "user_position": pl.Utf8,
    "confidence": pl.Float64,
    "detected_at": pl.Datetime("us", "UTC"),
}

#: Dry-run OUTPUT budget per session (~400 structured-output tokens). Input
#: is MEASURED from the rendered uuid-headed transcript (chars/4), never a
#: static prior: a real transcript runs an order of magnitude past any
#: fixed guess.
_AVG_OUT_TOKENS = 400


def _already_done(cache: ParquetCache) -> set[str]:
    """Session ids already present in the cache."""
    done_df = cache.read_all(columns=["session_id"])
    if done_df is not None and done_df.height > 0:
        return set(done_df["session_id"].to_list())
    return set()


async def _conflicts_async(
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
    """Async implementation behind :func:`detect_conflicts`."""
    already = _already_done(cache)

    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline="conflicts",
        checkpoint_db_path=checkpoint.db_path,
    )
    keep = set(unchanged_pending)

    retry_ids = set(retry.drain(pipeline="conflicts"))
    if retry_ids:
        logger.info("conflicts: draining {} retry-queue entries", len(retry_ids))
        keep |= retry_ids

    # Retry queue is the single re-admission gate for failed units (see
    # classify): a live entry blocks dispatch until drain says it's due.
    blocked = retry.blocked_units(pipeline="conflicts")
    if blocked & keep:
        logger.info(
            "conflicts: skipping {} sessions with live retry entries (backoff/attempts cap)",
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
        text = reader.session_text(sid, include_uuids=True)
        if not text:
            # A session that renders to nothing yields no row and no LLM
            # call, so without a checkpoint it re-renders every tick
            # forever. Stamp it at current bounds; growth re-admits it.
            empty_sids.append(sid)
            continue
        pending.append((sid, text))

    if empty_sids:
        checkpoint.mark_completed(
            pipeline="conflicts",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in empty_sids],
        )
        logger.info(
            "conflicts: checkpointed {} sessions with empty transcripts as skipped",
            len(empty_sids),
        )

    if deferred:
        logger.warning(
            "conflicts: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            deferred,
        )

    if not pending:
        logger.info("conflicts: no pending sessions (skipped={} via checkpoint)", skipped)
        return 0
    if skipped:
        logger.info("conflicts: skipped {} sessions via checkpoint", skipped)

    # Per-session set of real raw-record uuids for the returned-uuid guard.
    # ``None`` means the edges file was unreadable — the guard then has no
    # universe to check against and drops the session's pairs.
    valid_uuids = {sid: reader.edges_uuids(sid) for sid, _ in pending}

    chunk_size = write_chunk_size(settings.batch_size)
    logger.info("conflicts: {} pending sessions", len(pending))

    written_pairs = 0
    processed_sessions = 0
    i = 0
    chunk_index = 0
    total_chunks = (len(pending) + chunk_size - 1) // chunk_size
    while i < len(pending):
        chunk_index += 1
        t0 = time.monotonic()
        # Budget-checked sub-batches; ``i`` advances by what was actually
        # SENT so a mid-chunk stop leaves the remainder unstamped. Conflicts
        # runs on the LARGE model over full transcripts, so this is the
        # stage where one unchecked gather overshoots hardest.
        chunk, results = await gather_under_budget(
            pending[i : i + chunk_size],
            make_coro=lambda unit: provider.classify_structured(
                system=CONFLICTS_SYSTEM_PROMPT,
                prompt=unit[1],
                schema=ConflictsResult,
            ),
            budget=budget,
        )
        if not chunk:
            logger.warning(
                "conflicts: cost ceiling hit (${:.2f} >= ${:.2f}) — "
                "aborting with {} sessions unstarted (nothing stamped)",
                budget.spent_usd() if budget is not None else 0.0,
                budget.max_cost_usd if budget is not None else 0.0,
                len(pending) - i,
            )
            break
        i += len(chunk)
        now = datetime.now(UTC)

        rows: list[dict[str, Any]] = []
        errors = 0
        ok_sids: set[str] = set()
        refused: list[tuple[str, str]] = []
        for (sid, _), res in zip(chunk, results, strict=True):
            if isinstance(res, BaseException):
                from atif_models.domain.ports import RefusalError

                if isinstance(res, RefusalError):
                    # Terminal: zero PAIR rows (a sentinel pair would poison
                    # the uuid-keyed conversation-time joins — see the
                    # refusals sidecar decision in _shared.write_refusal_audit),
                    # but the refusal itself lands as a durable audit row.
                    logger.info("conflicts: {} refused (terminal) — audit sidecar row", sid)
                    ok_sids.add(sid)
                    refused.append((sid, str(res)))
                    continue
                errors += 1
                logger.warning("conflicts: {} failed (queued for retry): {}", sid, res)
                retry.enqueue(pipeline="conflicts", unit_id=sid, error=str(res))
                continue
            ok_sids.add(sid)
            for c in res.conflicts:
                turn_a = c.turn_a_uuid
                turn_b = c.turn_b_uuid
                if not turn_a or not turn_b or turn_a == turn_b:
                    logger.warning(
                        "conflicts: {} returned a degenerate pair "
                        "(turn_a={!r}, turn_b={!r}) — skipping",
                        sid,
                        turn_a,
                        turn_b,
                    )
                    continue
                # uuid-validity guard: a pair whose turn uuids are not BOTH
                # real raw-record uuids for this session can never be
                # recovered on a conversation-time axis. FAIL CLOSED — an
                # unreadable edges file (None) yields no universe to check
                # against, and admitting the pair anyway would put an
                # unverified uuid into session_conflicts, where nothing
                # downstream can tell it from a verified one.
                session_uuids = valid_uuids.get(sid)
                if session_uuids is None:
                    logger.warning(
                        "conflicts: {} has no readable uuid universe — dropping pair "
                        "(turn_a={!r}, turn_b={!r}); the validity guard cannot run",
                        sid,
                        turn_a,
                        turn_b,
                    )
                    continue
                if turn_a not in session_uuids or turn_b not in session_uuids:
                    logger.warning(
                        "conflicts: {} returned a pair with non-record uuids "
                        "(turn_a={!r} valid={}, turn_b={!r} valid={}) — skipping",
                        sid,
                        turn_a,
                        turn_a in session_uuids,
                        turn_b,
                        turn_b in session_uuids,
                    )
                    continue
                rows.append(
                    {
                        "session_id": sid,
                        "turn_a_uuid": turn_a,
                        "turn_b_uuid": turn_b,
                        "conflict_kind": c.conflict_kind,
                        "severity": c.severity,
                        "agent_position": c.agent_position,
                        "user_position": c.user_position,
                        "confidence": float(c.confidence),
                        "detected_at": now,
                    }
                )
        if rows:
            cache.write_part(pl.DataFrame(rows, schema=_PARQUET_SCHEMA))
        if refused:
            write_refusal_audit(
                settings.layout().refusals_dir, pipeline="conflicts", refusals=refused
            )
        if ok_sids:
            checkpoint.mark_completed(
                pipeline="conflicts",
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in ok_sids],
            )
            retry.mark_done(pipeline="conflicts", unit_ids=list(ok_sids))
        written_pairs += len(rows)
        processed_sessions += len(ok_sids)
        logger.info(
            "conflicts chunk {}/{}: {} sessions, {} pairs, {} errors, {:.1f}s",
            chunk_index,
            total_chunks,
            len(ok_sids),
            len(rows),
            errors,
            time.monotonic() - t0,
        )

    logger.info(
        "conflicts: processed {} sessions, wrote {} pair rows", processed_sessions, written_pairs
    )
    return processed_sessions


def detect_conflicts(
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
    """Detect stance conflicts per session; return the count of sessions processed.

    The returned int counts SESSIONS, not pair rows: a session that produced
    two conflict pairs and one that produced zero both count as one (and
    both are checkpointed so a rerun is a no-op). ``dry_run`` (the default)
    returns a plan dict instead.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.conflicts_dir)

    if provider is None or spec is None:
        provider, spec = build_provider(settings, "conflicts")

    if dry_run:
        # Measured estimation: input tokens from the actual rendered
        # uuid-headed transcript (chars/4), capped like the real run.
        already = _already_done(cache)
        bounds = reader.session_bounds(since_days=since_days, limit=limit)
        pending = [sid for sid in bounds if sid not in already]
        max_sessions = settings.llm_max_sessions_per_run
        capped = pending[:max_sessions]
        in_tokens = sum(
            tokens_for_chars(len(reader.session_text(sid, include_uuids=True))) for sid in capped
        )
        out_tokens = len(capped) * _AVG_OUT_TOKENS
        pricing = (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
        cost = estimate_cost_tokens(in_tokens, out_tokens, pricing)
        logger.info(
            "conflicts --dry-run: {} sessions ({} would run under the cap), "
            "estimated cost ~${:.2f}",
            len(pending),
            len(capped),
            cost,
        )
        return {
            "pipeline": "conflicts",
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

    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)
    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)
    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, "conflicts"):
        return asyncio.run(
            _conflicts_async(
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


__all__ = ["detect_conflicts"]
