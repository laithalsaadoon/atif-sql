# SPDX-License-Identifier: Apache-2.0

"""Perceived-error detection pipeline (LangSmith's Perceived Error, ours).

Implements LangSmith's published "Perceived Error" definition on the
conflicts-pipeline chassis.

Transcript text is sent to a third-party model provider on this path: a
session's full rendered body, tool arguments and tool results included,
leaves this machine.

Reads complete session transcripts WITH uuid headers (``include_uuids=True``
puts each text step's uuid — the FIRST source_uuid, per CONTRACT-V2 — in
the header as ``[uuid=... role ts]``) and emits one row per user-perceived
agent error, anchored on the USER turn where the perception surfaces.
Clean sessions produce ZERO rows.

Eligibility (LangSmith parity): a session needs >= 2 human-AI message
pairs before it can be judged — perceiving an error requires the human
RESPONDING to AI output. Ineligible sessions are checkpointed as skipped
(zero LLM calls) so they never re-render; if the session later grows, the
advancing (last_ts, mtime) bounds re-admit it through the normal
checkpoint path. Two more LangSmith runtime traits carry over through
existing machinery: idle-gated evaluation (our corpus quiescence — the
materializer only finalizes settled sessions) and "skipped/failed units
are never billed" (checkpoint + blocked-units retry gate).

The uuid-validity guard (conflicts precedent) validates the model's
returned ``turn_uuid`` values against the session's USER-role main-chain
text-step header uuids before they land in the parquet — the documented
invariant is USER turns only, so an assistant uuid (or a hallucinated one)
is dropped at the source (logged, reason 'non-user turn_uuid'). The guard
FAILS CLOSED: a session that rendered no user-turn header uuids at all has
no universe to validate against, so its rows are dropped rather than
admitted unverified. Refusals land in the ``analytics/refusals`` sidecar (a
sentinel row would poison the uuid-keyed joins).

Output schema (per parquet shard)::

    session_id           VARCHAR
    turn_uuid            VARCHAR  (NOT NULL — validated: user-turn header uuid)
    signal               VARCHAR  (enum: correction|repeated_request|
                                   rejected_action|contradictory_response|
                                   acknowledged_mistake|
                                   persistent_misunderstanding|
                                   unresolved_outcome)
    severity             VARCHAR  (enum: minor|moderate|major)
    evidence             VARCHAR
    agent_error_summary  VARCHAR
    confidence           DOUBLE
    detected_at          TIMESTAMP
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import PERCEIVED_SYSTEM_PROMPT
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
from atif_analytics.domain.models import PerceivedErrorsResult
from atif_analytics.domain.transcript import human_ai_pair_count
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
    "turn_uuid": pl.Utf8,
    "signal": pl.Utf8,
    "severity": pl.Utf8,
    "evidence": pl.Utf8,
    "agent_error_summary": pl.Utf8,
    "confidence": pl.Float64,
    "detected_at": pl.Datetime("us", "UTC"),
}

#: LangSmith eligibility floor: judging a perceived error requires the human
#: responding to AI output at least twice.
_MIN_HUMAN_AI_PAIRS = 2

#: Dry-run OUTPUT budget per session (structured array, conflicts-sized).
#: Input is MEASURED from the rendered uuid-headed transcript (chars/4).
_AVG_OUT_TOKENS = 400


def _already_done(cache: ParquetCache) -> set[str]:
    """Session ids already present in the cache."""
    done_df = cache.read_all(columns=["session_id"])
    if done_df is not None and done_df.height > 0:
        return set(done_df["session_id"].to_list())
    return set()


def _eligible(reader: CorpusReader, session_id: str) -> bool:
    """LangSmith parity gate: >= 2 completed human-AI message pairs."""
    return human_ai_pair_count(reader.load_steps(session_id)) >= _MIN_HUMAN_AI_PAIRS


def _user_turn_uuids(reader: CorpusReader, session_id: str) -> set[str]:
    """The USER-role main-chain text-step header uuids the model actually saw.

    Exactly the ``[uuid=... user ...]`` headers :func:`render_session_text`
    emits under ``include_uuids``, so this is the validity universe for a
    returned ``turn_uuid``.
    """
    return {
        s.uuid
        for s in reader.load_steps(session_id)
        if s.role == "user" and s.text and not s.is_sidechain and s.uuid
    }


async def _perceived_async(
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
    """Async implementation behind :func:`detect_perceived_errors`."""
    already = _already_done(cache)

    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline="perceived",
        checkpoint_db_path=checkpoint.db_path,
    )
    keep = set(unchanged_pending)

    retry_ids = set(retry.drain(pipeline="perceived"))
    if retry_ids:
        logger.info("perceived: draining {} retry-queue entries", len(retry_ids))
        keep |= retry_ids

    # Retry queue is the single re-admission gate for failed units (see
    # classify): a live entry blocks dispatch until drain says it's due.
    blocked = retry.blocked_units(pipeline="perceived")
    if blocked & keep:
        logger.info(
            "perceived: skipping {} sessions with live retry entries (backoff/attempts cap)",
            len(blocked & keep),
        )
        keep -= blocked

    # Session ceiling applied DURING the walk, not after: bounds is
    # newest-first and the cap keeps the freshest sessions. The walk stops
    # admitting at the cap, so it costs O(cap) eligibility probes and
    # renders rather than O(corpus) — a session that cannot run this tick
    # never has its steps parsed or its transcript (up to 800K chars) built.
    max_sessions = settings.llm_max_sessions_per_run
    pending: list[tuple[str, str]] = []
    # Captured while this session's steps are still hot in the reader's
    # bounded memo — a later re-walk would re-parse the trajectory.
    valid_uuids: dict[str, set[str]] = {}
    ineligible: list[str] = []
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
        # LangSmith eligibility gate: < 2 human-AI pairs -> checkpoint as
        # skipped (never billed, never re-rendered until the session grows
        # and its bounds advance past the checkpoint).
        if not _eligible(reader, sid):
            ineligible.append(sid)
            continue
        text = reader.session_text(sid, include_uuids=True)
        if not text:
            empty_sids.append(sid)
            continue
        pending.append((sid, text))
        valid_uuids[sid] = _user_turn_uuids(reader, sid)

    if ineligible or empty_sids:
        checkpoint.mark_completed(
            pipeline="perceived",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in (*ineligible, *empty_sids)],
        )
        logger.info(
            "perceived: checkpointed {} ineligible (<{} human-AI pairs) and "
            "{} empty-transcript sessions as skipped",
            len(ineligible),
            _MIN_HUMAN_AI_PAIRS,
            len(empty_sids),
        )

    if deferred:
        logger.warning(
            "perceived: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            deferred,
        )

    if not pending:
        logger.info("perceived: no pending sessions (skipped={} via checkpoint)", skipped)
        return 0
    if skipped:
        logger.info("perceived: skipped {} sessions via checkpoint", skipped)

    chunk_size = write_chunk_size(settings.batch_size)
    logger.info("perceived: {} pending sessions", len(pending))

    written_errors = 0
    processed_sessions = 0
    i = 0
    chunk_index = 0
    total_chunks = (len(pending) + chunk_size - 1) // chunk_size
    while i < len(pending):
        chunk_index += 1
        t0 = time.monotonic()
        # Budget-checked sub-batches; ``i`` advances by what was actually
        # SENT so a mid-chunk stop leaves the remainder unstamped.
        chunk, results = await gather_under_budget(
            pending[i : i + chunk_size],
            make_coro=lambda unit: provider.classify_structured(
                system=PERCEIVED_SYSTEM_PROMPT,
                prompt=unit[1],
                schema=PerceivedErrorsResult,
            ),
            budget=budget,
        )
        if not chunk:
            logger.warning(
                "perceived: cost ceiling hit (${:.2f} >= ${:.2f}) — "
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
                    # Terminal: zero error rows (a sentinel row would poison
                    # the uuid-keyed conversation-time joins), but the
                    # refusal lands as a durable audit row in the sidecar.
                    logger.info("perceived: {} refused (terminal) — audit sidecar row", sid)
                    ok_sids.add(sid)
                    refused.append((sid, str(res)))
                    continue
                errors += 1
                logger.warning("perceived: {} failed (queued for retry): {}", sid, res)
                retry.enqueue(pipeline="perceived", unit_id=sid, error=str(res))
                continue
            ok_sids.add(sid)
            for e in res.errors:
                turn_uuid = e.turn_uuid
                if not turn_uuid:
                    logger.warning("perceived: {} returned an empty turn_uuid — skipping", sid)
                    continue
                # uuid-validity guard (conflicts precedent): the schema's
                # invariant is a USER-turn anchor, so anything outside the
                # user-role text-step header uuids — an assistant turn's
                # uuid or a hallucinated one — can never be recovered on
                # the conversation-time axis this pipeline joins on. FAIL
                # CLOSED: an empty universe means no returned uuid can be
                # confirmed, and an unverified uuid in perceived_errors is
                # indistinguishable from a verified one downstream.
                session_uuids = valid_uuids.get(sid) or set()
                if turn_uuid not in session_uuids:
                    logger.warning(
                        "perceived: {} returned a non-user turn_uuid {!r} — "
                        "dropping row (signal={})",
                        sid,
                        turn_uuid,
                        e.signal,
                    )
                    continue
                rows.append(
                    {
                        "session_id": sid,
                        "turn_uuid": turn_uuid,
                        "signal": e.signal,
                        "severity": e.severity,
                        "evidence": e.evidence,
                        "agent_error_summary": e.agent_error_summary,
                        "confidence": float(e.confidence),
                        "detected_at": now,
                    }
                )
        if rows:
            cache.write_part(pl.DataFrame(rows, schema=_PARQUET_SCHEMA))
        if refused:
            write_refusal_audit(
                settings.layout().refusals_dir, pipeline="perceived", refusals=refused
            )
        if ok_sids:
            checkpoint.mark_completed(
                pipeline="perceived",
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in ok_sids],
            )
            retry.mark_done(pipeline="perceived", unit_ids=list(ok_sids))
        written_errors += len(rows)
        processed_sessions += len(ok_sids)
        logger.info(
            "perceived chunk {}/{}: {} sessions, {} error rows, {} failures, {:.1f}s",
            chunk_index,
            total_chunks,
            len(ok_sids),
            len(rows),
            errors,
            time.monotonic() - t0,
        )

    logger.info(
        "perceived: processed {} sessions, wrote {} error rows",
        processed_sessions,
        written_errors,
    )
    return processed_sessions


def detect_perceived_errors(
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
    """Detect user-perceived errors per session; return sessions processed.

    The returned int counts SESSIONS, not error rows: a clean session and
    one with three perceived errors both count as one (and both are
    checkpointed so a rerun is a no-op). Ineligible sessions (< 2 human-AI
    pairs) are checkpointed as skipped without an LLM call and do not
    count. ``dry_run`` (the default) returns a plan dict instead.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.perceived_errors_dir)

    if provider is None or spec is None:
        provider, spec = build_provider(settings, "perceived")

    if dry_run:
        # Measured estimation: input tokens from the actual rendered
        # uuid-headed transcript (chars/4), eligibility-gated and capped
        # like the real run.
        already = _already_done(cache)
        bounds = reader.session_bounds(since_days=since_days, limit=limit)
        pending = [sid for sid in bounds if sid not in already]
        eligible = [sid for sid in pending if _eligible(reader, sid)]
        max_sessions = settings.llm_max_sessions_per_run
        capped = eligible[:max_sessions]
        in_tokens = sum(
            tokens_for_chars(len(reader.session_text(sid, include_uuids=True))) for sid in capped
        )
        out_tokens = len(capped) * _AVG_OUT_TOKENS
        pricing = (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
        cost = estimate_cost_tokens(in_tokens, out_tokens, pricing)
        logger.info(
            "perceived --dry-run: {} sessions pending, {} eligible "
            "({} would run under the cap), estimated cost ~${:.2f}",
            len(pending),
            len(eligible),
            len(capped),
            cost,
        )
        return {
            "pipeline": "perceived",
            "candidates": len(pending),
            "eligible_candidates": len(eligible),
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
    with pipeline_usage(provider, spec, "perceived"):
        return asyncio.run(
            _perceived_async(
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


__all__ = ["detect_perceived_errors"]
