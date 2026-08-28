# SPDX-License-Identifier: Apache-2.0

"""Cross-use-case plumbing shared by the five LLM pipelines.

The five are ``PIPELINE_NAMES`` in
:mod:`atif_analytics.infrastructure.sqlite_state.checkpointer`: classify,
trajectory, conflicts, user_friction, perceived. The three structural
pipelines (cluster, terms, community) call no model and do not use this
module.

The provider factory + per-pipeline usage/cost logging (one provider
instance per pipeline run, usage accumulated in the provider's
:class:`~atif_models.domain.ports.UsageAccumulator` and logged once at the
end), and the storage-port default builders.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_analytics.infrastructure.parquet_cache import ParquetCache
from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint
from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator, Sequence
    from pathlib import Path

    from atif_analytics.infrastructure.settings import AnalyticsSettings
    from atif_models.domain.ports import LlmStructuredProvider
    from atif_models.domain.registry import ModelSpec


def build_provider(
    settings: AnalyticsSettings, pipeline: str
) -> tuple[LlmStructuredProvider, ModelSpec]:
    """One provider per pipeline run, sized per atif-models settings.

    CONTRACT-V2 §Pipeline size assignments: classify/trajectory=medium,
    conflicts=large, friction=small — all resolved through the atif-models
    registry (no model id is written down here).
    """
    from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider

    llm = settings.llm()
    spec = llm.spec_for(pipeline)
    provider = OpenAiBedrockProvider(
        spec,
        region=llm.llm_region,
        concurrency=llm.llm_concurrency,
    )
    logger.info(
        "{}: provider={} model={} concurrency={}",
        pipeline,
        provider.provider,
        spec.model_id,
        llm.llm_concurrency,
    )
    return provider, spec


@contextmanager
def pipeline_usage(
    provider: LlmStructuredProvider, spec: ModelSpec, pipeline: str
) -> Generator[None]:
    """Log the accumulated usage + estimated cost once per pipeline run."""
    from atif_models.domain.registry import estimate_cost as spec_cost

    try:
        yield
    finally:
        usage = getattr(provider, "usage", None)
        if usage is not None:
            summary = usage.summary()
            cost = spec_cost(
                spec,
                input_tokens=summary["input_tokens"],
                output_tokens=summary["output_tokens"],
            )
            logger.info(
                "{}: usage calls={} in={} out={} reasoning={} cached={} est_cost={}",
                pipeline,
                summary["calls"],
                summary["input_tokens"],
                summary["output_tokens"],
                summary["reasoning_tokens"],
                summary["cached_tokens"],
                f"${cost:.4f}" if cost is not None else "n/a",
            )


class RunBudget:
    """Cross-pipeline dollar ceiling over the providers' UsageAccumulators.

    One instance per ``run_analyze`` LLM lane. Each pipeline registers its
    ``(provider, spec)`` via :meth:`watch` before dispatching; :meth:`spent_usd`
    prices every watched accumulator's RUNNING ACTUALS (not estimates), so
    :meth:`exhausted` is checked cheaply between dispatch batches. When it
    trips, pipelines stop dispatching — nothing is stamped (no checkpoint, no
    cache row, no retry entry) for units that never started, so the next run
    picks them up cleanly.

    OVERSHOOT BOUND. Actual spend is only observable after a call returns, so
    the ceiling is a stop-dispatch trigger, not a hard cap. Worst case the run
    ends at ``max_cost_usd`` plus the cost of the units already in flight when
    the crossing became visible — at most :data:`BUDGET_CHECK_BATCH` units on
    the most expensive watched model. Two dispatch shapes deliver that bound:
    :func:`gather_under_budget` sends units in batches of that size and checks
    the budget at each batch boundary, while trajectory (which starts every
    session concurrently) holds a ``BUDGET_CHECK_BATCH``-wide semaphore and
    reads the budget under it. Either way the overshoot does NOT scale with
    the session ceiling or the write chunk size. Pinned by
    ``test_overshoot_never_exceeds_the_documented_budget_batch`` over both
    shapes.
    """

    def __init__(self, max_cost_usd: float) -> None:
        self.max_cost_usd = max_cost_usd
        self._watched: list[tuple[LlmStructuredProvider, ModelSpec]] = []

    def watch(self, provider: LlmStructuredProvider, spec: ModelSpec) -> None:
        """Track one pipeline's provider accumulator for the rest of the run."""
        self._watched.append((provider, spec))

    def spent_usd(self) -> float:
        """Total actual spend so far across every watched accumulator."""
        from atif_models.domain.registry import estimate_cost as spec_cost

        total = 0.0
        for provider, spec in self._watched:
            usage = getattr(provider, "usage", None)
            if usage is None:
                continue
            summary = usage.summary()
            cost = spec_cost(
                spec,
                input_tokens=summary["input_tokens"],
                output_tokens=summary["output_tokens"],
            )
            total += cost or 0.0
        return total

    def exhausted(self) -> bool:
        """True once actual spend has crossed the ceiling."""
        return self.spent_usd() >= self.max_cost_usd


#: Units dispatched between budget re-checks. This is the overshoot unit of
#: the cost ceiling: the ceiling can only be observed after a call returns,
#: so whatever is in flight at the crossing is already bought. Small enough
#: that a stage's overshoot is a handful of calls rather than its whole
#: remaining batch; large enough to keep the provider's concurrency limiter
#: (default 16) fed.
BUDGET_CHECK_BATCH: int = 8


async def gather_under_budget[T, R](
    units: Sequence[T],
    *,
    make_coro: Callable[[T], Coroutine[Any, Any, R]],
    budget: RunBudget | None,
    batch_size: int = BUDGET_CHECK_BATCH,
) -> tuple[list[T], list[R | BaseException]]:
    """Dispatch ``units`` in budget-checked batches; return what actually ran.

    Returns ``(dispatched_units, results)`` — parallel lists, results in
    ``asyncio.gather(return_exceptions=True)`` form. Dispatch stops at the
    first batch boundary where the budget is exhausted, so the caller's
    zip over the pair never stamps a unit that was never sent.

    ``make_coro`` is called lazily per batch: a coroutine built for a unit
    that never gets dispatched would be an un-awaited coroutine warning.
    """
    dispatched: list[T] = []
    results: list[R | BaseException] = []
    for start in range(0, len(units), max(1, batch_size)):
        if budget is not None and budget.exhausted():
            break
        batch = list(units[start : start + max(1, batch_size)])
        batch_results = await asyncio.gather(
            *(make_coro(unit) for unit in batch), return_exceptions=True
        )
        dispatched.extend(batch)
        results.extend(batch_results)
    return dispatched, results


def build_cache(target: Path) -> ParquetCache:
    """Default cache port over one sharded parquet dir."""
    return ParquetCache(target)


def write_refusal_audit(
    refusals_dir: Path,
    *,
    pipeline: str,
    refusals: list[tuple[str, str]],
) -> None:
    """Append ``(unit_id, reason)`` refusal audit rows to ``analytics/refusals``.

    Refusal-skips must be queryable, not just log lines. Used by pipelines
    whose OUTPUT schema cannot hold an in-band sentinel row without bending
    view semantics — conflicts rows are uuid-keyed PAIRS, so a sentinel pair
    would poison the conversation-time joins. (classify writes an in-band
    sentinel instead: its row is session-keyed, so a documented
    ``goal='[refused]'`` row keeps ``session_classifications`` complete
    without touching any view.) Empty ``refusals`` is a no-op.
    """
    if not refusals:
        return
    from datetime import UTC, datetime

    import polars as pl

    now = datetime.now(UTC)
    ParquetCache(refusals_dir).write_part(
        pl.DataFrame(
            {
                "pipeline": [pipeline] * len(refusals),
                "unit_id": [uid for uid, _ in refusals],
                "reason": [reason[:500] for _, reason in refusals],
                "refused_at": [now] * len(refusals),
            },
            schema={
                "pipeline": pl.Utf8,
                "unit_id": pl.Utf8,
                "reason": pl.Utf8,
                "refused_at": pl.Datetime("us", "UTC"),
            },
        )
    )


def build_checkpoint(state_db: Path) -> SqliteCheckpoint:
    """Default checkpoint port over the corpus ``state.db``."""
    return SqliteCheckpoint(state_db)


def build_retry_queue(state_db: Path) -> SqliteRetryQueue:
    """Default retry-queue port over the corpus ``state.db``."""
    return SqliteRetryQueue(state_db)


def write_chunk_size(batch_size: int) -> int:
    """Crash-resilience write chunking: ``max(batch_size*4, 256)`` rows per part.

    This bounds how much completed work a crash can lose, NOT how much the
    run can spend — the budget ceiling is enforced per
    :data:`BUDGET_CHECK_BATCH` units inside each chunk, because the default
    chunk (384) is wider than the whole session ceiling (50).
    """
    return max(batch_size * 4, 256)


__all__ = [
    "BUDGET_CHECK_BATCH",
    "RunBudget",
    "build_cache",
    "build_checkpoint",
    "build_provider",
    "build_retry_queue",
    "gather_under_budget",
    "pipeline_usage",
    "write_chunk_size",
    "write_refusal_audit",
]
