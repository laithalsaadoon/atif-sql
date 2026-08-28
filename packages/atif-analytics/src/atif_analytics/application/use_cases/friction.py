# SPDX-License-Identifier: Apache-2.0

"""Detect user-friction signals in short user-role messages.

A three-tier pipeline over the materialized corpus:

1. Pre-filter to user-role text steps below ``settings.friction_max_chars``
   (default 300), main chain only, system markers excluded.
2. Regex fast-path (:mod:`atif_analytics.domain.friction`) for strong
   unambiguous patterns — confidence 0.9, skips the LLM.
3. Deterministic stamp tier — three rules over steps + error tool_results:

   * Rule 1 — repeated user message body within 10 user turns →
     ``unmet_expectation`` @ 0.85 (re-asking means the first answer fell
     short).
   * Rule 2 — short imperative (≤30 chars, first token ∈ {stop, redo,
     revert, rollback, undo, restart}) → ``correction`` @ 0.9.
   * Rule 3 — trailing ``?`` on the user step immediately after a step
     carrying an error tool_result → ``confusion`` @ 0.85. The error flag
     comes from ``observation.results[].extra.tool_result_metadata.is_error``
     (harbor preserves the raw flag only there).

4. Everything else goes to the LLM per message.

Outputs one row per analysed user message:
``{uuid, session_id, ts, text_snippet, label, rationale, source,
confidence, classified_at}``.

``source`` ∈ {'regex','sql','llm','refused'} — the schema value is the
row's PROVENANCE tier, not the engine that computed it. ``'sql'`` marks the
three deterministic stamp rules; those rules run in Python here, over the
same data the SQL versions read. Downstream atif-duck views and macros bind
to these literal values, so the label is fixed by that contract.

Short user-message text is sent to a third-party model provider on tier 4:
those message bodies leave this machine.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import USER_FRICTION_SYSTEM_PROMPT
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
from atif_analytics.domain.friction import regex_fast_path
from atif_analytics.domain.models import UserFrictionSignal
from atif_analytics.domain.transcript import CLI_BOOKKEEPING_TEXTS
from atif_analytics.infrastructure.corpus_reader import CorpusReader, parse_ts
from atif_analytics.infrastructure.sqlite_state import checkpointer

if TYPE_CHECKING:
    from datetime import datetime as _dt

    from atif_analytics.domain.transcript import StepEvent
    from atif_analytics.infrastructure.parquet_cache import ParquetCache
    from atif_analytics.infrastructure.settings import AnalyticsSettings
    from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint
    from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
    from atif_models.domain.ports import LlmStructuredProvider
    from atif_models.domain.registry import ModelSpec

_SCHEMA: dict[str, Any] = {
    "uuid": pl.Utf8,
    "session_id": pl.Utf8,
    "ts": pl.Datetime("us", "UTC"),
    "text_snippet": pl.Utf8,
    "label": pl.Utf8,
    "rationale": pl.Utf8,
    # Provenance TIER, not the engine: 'sql' is the deterministic stamp tier,
    # whose rules run in Python here. atif-duck views and macros bind to
    # these literal values, so they are contract, not description.
    "source": pl.Utf8,  # 'regex' | 'sql' | 'llm' | 'refused'
    "confidence": pl.Float32,
    "classified_at": pl.Datetime("us", "UTC"),
}

#: Claude Code injects these strings as user-role messages even though
#: they're system-generated bookkeeping, and they dominate the candidate set
#: (~94% of friction LLM calls without this filter). Excluded at the
#: candidate boundary. The set itself lives in the domain
#: (:data:`CLI_BOOKKEEPING_TEXTS`) so the perceived-eligibility pair counter
#: shares the same definition.
_SYSTEM_MARKER_TEXTS = CLI_BOOKKEEPING_TEXTS

#: Rule 2's imperative first-token set.
_IMPERATIVE_TOKENS: frozenset[str] = frozenset(
    {"stop", "redo", "revert", "rollback", "undo", "restart"}
)

_WS_RE = re.compile(r"\s+")

#: Character ceiling for the "short imperative revert" rule. A correction like
#: "no, undo that" is terse by nature; past this length the message is carrying
#: new instructions rather than reverting the last turn, and the imperative
#: first word stops being evidence of friction.
_SHORT_REVERT_MAX_CHARS = 30

#: Dry-run OUTPUT budget per LLM message (~60 tokens of structured output).
#: Input is MEASURED at plan time as system prompt + template + ~250 chars
#: of message (the candidate cutoff is 300): the envelope, not the message,
#: is most of the per-call bill, so estimating from the message alone
#: understates it by more than an order of magnitude.
_AVG_OUT_TOKENS = 60
_AVG_MESSAGE_CHARS = 250

_USER_PROMPT_TEMPLATE = """\
Classify the following SHORT USER MESSAGE from a Claude Code coding session.

You're looking for FRICTION SIGNALS — cues that the human is impatient,
confused, interrupting the agent, correcting it, or asking for something
the agent should have provided proactively but didn't.

Examples of NON-obvious friction:
- "screenshot?" → unmet_expectation (agent should have shared a screenshot)
- "tests?" → unmet_expectation (agent didn't run tests)
- "link?" → unmet_expectation (agent referenced a resource without linking)
- "why?" / "why did you do that?" → confusion
- "wait" / "actually..." → interruption
- "no not that" / "nope" → correction
- "are you there?" / "you alive?" → status_ping

The MAJORITY of short messages are ordinary task instructions and should
get label=none. Only flag a friction signal when the cue is clear.

USER MESSAGE:
```
{text}
```
"""


# ---------------------------------------------------------------------------
# Candidates + deterministic stamp tier
# ---------------------------------------------------------------------------


def candidate_messages(
    steps: list[StepEvent],
    *,
    max_chars: int,
) -> list[tuple[str, str, _dt | None, str]]:
    """The friction candidates for one session: ``(uuid, sid-slot, ts, text)``.

    User-role text steps on the main chain, 1..``max_chars`` chars, system
    markers excluded, uuid present (the row key). The session id slot is
    filled by the caller.
    """
    out: list[tuple[str, str, _dt | None, str]] = []
    for step in steps:
        if step.is_sidechain or step.role != "user" or not step.uuid:
            continue
        text = step.text
        if not text or len(text) > max_chars:
            continue
        if text.strip() in _SYSTEM_MARKER_TEXTS:
            continue
        out.append((step.uuid, "", parse_ts(step.ts), text))
    return out


def deterministic_stamps(
    steps: list[StepEvent],
    candidate_uuids: set[str],
) -> dict[str, tuple[str, float, str]]:
    """Run the three deterministic stamp rules over one session's steps.

    Returns ``{uuid: (label, confidence, 'sql')}``. When two rules match
    the same uuid the higher confidence wins; ties keep the first stamp
    Rules apply in the fixed order 1 → 2 → 3, so a message matching two
    rules always gets the same label.
    """
    out: dict[str, tuple[str, float, str]] = {}
    main = [s for s in steps if not s.is_sidechain]

    def _stamp(uuid: str, label: str, conf: float) -> None:
        existing = out.get(uuid)
        if existing is None or conf > existing[1]:
            out[uuid] = (label, conf, "sql")

    # Rule 1 — repeated user message body within 10 user turns.
    user_turns = [s for s in main if s.role == "user" and s.text and s.uuid]
    norms = [_WS_RE.sub(" ", s.text).lower() for s in user_turns]
    for i, step in enumerate(user_turns):
        if step.uuid not in candidate_uuids:
            continue
        lo = max(0, i - 10)
        if any(norms[j] == norms[i] for j in range(lo, i)):
            _stamp(step.uuid or "", "unmet_expectation", 0.85)

    # Rule 2 — short imperative reverts.
    for step in user_turns:
        if step.uuid not in candidate_uuids:
            continue
        text = step.text
        if len(text) > _SHORT_REVERT_MAX_CHARS:
            continue
        first = text.strip().split(" ", 1)[0].rstrip(".,!?").lower()
        if first in _IMPERATIVE_TOKENS:
            _stamp(step.uuid or "", "correction", 0.9)

    # Rule 3 — trailing '?' on the user step immediately after an error
    # tool_result (no event in between — the step right before it on the
    # main chain carries the error flag).
    for i, step in enumerate(main):
        if (
            step.role == "user"
            and step.uuid
            and step.uuid in candidate_uuids
            and step.text.rstrip().endswith("?")
            and i > 0
            and main[i - 1].has_error_result
        ):
            _stamp(step.uuid, "confusion", 0.85)

    return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def _friction_async(
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
    """Async body behind :func:`detect_user_friction`."""
    already: set[str] = set()
    done_df = cache.read_all(columns=["uuid"])
    if done_df is not None and done_df.height > 0:
        already = set(done_df["uuid"].to_list())

    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped_sessions = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline="user_friction",
        checkpoint_db_path=checkpoint.db_path,
    )
    active_sessions: set[str] = set(unchanged_pending)

    retry_uuids = set(retry.drain(pipeline="user_friction"))
    if retry_uuids:
        logger.info("user_friction: draining {} retry-queue entries", len(retry_uuids))
        already -= retry_uuids

    # Retry queue is the single re-admission gate for failed units (see
    # classify): a uuid with a live entry (backoff pending or attempts
    # exhausted) is treated as done for THIS run — drain re-admits it when due.
    blocked = retry.blocked_units(pipeline="user_friction")
    if blocked:
        logger.info(
            "user_friction: skipping {} uuids with live retry entries (backoff/attempts cap)",
            len(blocked),
        )
        already |= blocked

    # Candidates + per-session deterministic stamps. bounds is newest-first,
    # so the session ceiling keeps the freshest sessions (budget guard) —
    # only the LLM tier costs money, but capping at the candidate boundary
    # keeps the cap semantics uniform across pipelines.
    #
    # ``limit`` is applied at SESSION granularity, like every other stage.
    # Truncating a session's candidate list mid-session instead would leave
    # the cut-away messages in no tier and no retry entry while the session
    # still got checkpointed on its RETAINED rows — filter_unchanged would
    # then skip it forever and those messages would never be classified.
    max_sessions = settings.llm_max_sessions_per_run
    if limit is not None:
        max_sessions = min(max_sessions, int(limit))
    sessions_admitted = 0
    capped_out = 0
    candidates: list[tuple[str, str, _dt | None, str]] = []
    stamps: dict[str, tuple[str, float, str]] = {}
    zero_yield: list[str] = []
    for sid in bounds:
        # A retry uuid may live in a checkpoint-skipped session, so skipped
        # sessions are only walked while the retry set is non-empty.
        if sid not in active_sessions and not retry_uuids:
            continue
        if sessions_admitted >= max_sessions:
            capped_out += 1
            continue
        steps = reader.load_steps(sid)
        session_candidates = [
            (uuid, sid, ts, text)
            for uuid, _, ts, text in candidate_messages(
                steps, max_chars=settings.friction_max_chars
            )
            if uuid not in already
        ]
        if sid not in active_sessions:
            # Only retry-queue uuids re-admit rows from a skipped session.
            session_candidates = [c for c in session_candidates if c[0] in retry_uuids]
        if not session_candidates:
            if sid in active_sessions:
                # No candidates means no rows and no LLM call, so without a
                # checkpoint this session pays a full load_steps + candidate
                # walk on every tick forever (a session whose user turns are
                # all longer than friction_max_chars never yields one).
                zero_yield.append(sid)
            continue
        sessions_admitted += 1
        candidates.extend(session_candidates)
        stamps.update(deterministic_stamps(steps, {c[0] for c in session_candidates}))

    if capped_out:
        logger.warning(
            "user_friction: capped at {} sessions (llm_max_sessions_per_run/limit); {} deferred",
            max_sessions,
            capped_out,
        )
    if zero_yield:
        checkpoint.mark_completed(
            pipeline="user_friction",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in zero_yield],
        )
        logger.info(
            "user_friction: checkpointed {} sessions with zero candidates as skipped",
            len(zero_yield),
        )
    if skipped_sessions:
        logger.info("user_friction: skipped {} sessions via checkpoint", skipped_sessions)
    logger.info("user_friction: {} candidate user messages", len(candidates))

    if not candidates:
        logger.info("user_friction: nothing pending")
        return 0

    # Tier 1: regex fast-path; tier 2: deterministic stamps; rest → LLM.
    now = datetime.now(UTC)
    fast_rows: list[dict[str, Any]] = []
    llm_pending: list[tuple[str, str, _dt | None, str]] = []
    for uuid, session_id, ts, text in candidates:
        hit = regex_fast_path(text or "")
        if hit is not None:
            label, conf = hit
            fast_rows.append(
                {
                    "uuid": uuid,
                    "session_id": session_id,
                    "ts": ts,
                    "text_snippet": (text or "")[:200],
                    "label": label,
                    "rationale": "regex match",
                    "source": "regex",
                    "confidence": conf,
                    "classified_at": now,
                }
            )
            continue
        stamp = stamps.get(uuid)
        if stamp is not None:
            label, conf, source = stamp
            fast_rows.append(
                {
                    "uuid": uuid,
                    "session_id": session_id,
                    "ts": ts,
                    "text_snippet": (text or "")[:200],
                    "label": label,
                    "rationale": "sql stamp",
                    "source": source,
                    "confidence": conf,
                    "classified_at": now,
                }
            )
            continue
        llm_pending.append((uuid, session_id, ts, text))

    logger.info(
        "user_friction: {} regex, {} sql-stamped, {} pending LLM",
        sum(1 for r in fast_rows if r["source"] == "regex"),
        sum(1 for r in fast_rows if r["source"] == "sql"),
        len(llm_pending),
    )

    if fast_rows:
        cache.write_part(pl.DataFrame(fast_rows, schema=_SCHEMA))

    processed_sessions: set[str] = {r["session_id"] for r in fast_rows}

    if not llm_pending:
        if processed_sessions:
            checkpoint.mark_completed(
                pipeline="user_friction",
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in processed_sessions],
            )
        logger.info("user_friction: wrote {} total rows (fast tiers only)", len(fast_rows))
        return len(fast_rows)

    # Tier 3: LLM path, one call per message.
    chunk_size = write_chunk_size(settings.batch_size)
    written = len(fast_rows)

    aborted_sessions: set[str] = set()
    i = 0
    chunk_index = 0
    total_chunks = (len(llm_pending) + chunk_size - 1) // chunk_size
    while i < len(llm_pending):
        chunk_index += 1
        t0 = time.monotonic()
        # Budget-checked sub-batches; ``i`` advances by what was actually
        # SENT so a mid-chunk stop leaves the remainder unstamped.
        chunk, results = await gather_under_budget(
            llm_pending[i : i + chunk_size],
            make_coro=lambda unit: provider.classify_structured(
                system=USER_FRICTION_SYSTEM_PROMPT,
                prompt=_USER_PROMPT_TEMPLATE.format(text=unit[3]),
                schema=UserFrictionSignal,
            ),
            budget=budget,
        )
        if not chunk:
            logger.warning(
                "user_friction: cost ceiling hit (${:.2f} >= ${:.2f}) — "
                "aborting with {} messages unstarted (nothing stamped)",
                budget.spent_usd() if budget is not None else 0.0,
                budget.max_cost_usd if budget is not None else 0.0,
                len(llm_pending) - i,
            )
            # Sessions with unstarted messages must NOT be checkpointed —
            # the next run has to re-admit their remaining candidates.
            aborted_sessions = {session_id for _, session_id, _, _ in llm_pending[i:]}
            break
        i += len(chunk)
        now = datetime.now(UTC)

        ok_rows: list[dict[str, Any]] = []
        done_uuids: list[str] = []
        errors = 0
        for (uuid, session_id, ts, text), res in zip(chunk, results, strict=True):
            if isinstance(res, BaseException):
                from atif_models.domain.ports import RefusalError

                if isinstance(res, RefusalError):
                    logger.info("user_friction: {} refused — marking none", uuid)
                    ok_rows.append(
                        {
                            "uuid": uuid,
                            "session_id": session_id,
                            "ts": ts,
                            "text_snippet": text[:200],
                            "label": "none",
                            "rationale": "refused by provider",
                            "source": "refused",
                            "confidence": 0.0,
                            "classified_at": now,
                        }
                    )
                    done_uuids.append(uuid)
                    processed_sessions.add(session_id)
                    continue
                errors += 1
                logger.warning("user_friction: {} failed (queued for retry): {}", uuid, res)
                retry.enqueue(pipeline="user_friction", unit_id=uuid, error=str(res))
                continue
            ok_rows.append(
                {
                    "uuid": uuid,
                    "session_id": session_id,
                    "ts": ts,
                    "text_snippet": text[:200],
                    "label": res.label,
                    "rationale": (res.rationale or "")[:200],
                    "source": "llm",
                    "confidence": float(res.confidence),
                    "classified_at": now,
                }
            )
            done_uuids.append(uuid)
            processed_sessions.add(session_id)

        if ok_rows:
            cache.write_part(pl.DataFrame(ok_rows, schema=_SCHEMA))
            retry.mark_done(pipeline="user_friction", unit_ids=done_uuids)
            # Checkpoint only sessions with no messages left to dispatch —
            # stamping early would let a budget abort strand their remainder.
            still_pending = {sid for _, sid, _, _ in llm_pending[i:]}
            chunk_sessions = {r["session_id"] for r in ok_rows} - still_pending
            if chunk_sessions:
                checkpoint.mark_completed(
                    pipeline="user_friction",
                    rows=[(sid, *bounds.get(sid, (None, None))) for sid in chunk_sessions],
                )

        written += len(ok_rows)
        logger.info(
            "user_friction chunk {}/{}: {} ok, {} errors, {:.1f}s",
            chunk_index,
            total_chunks,
            len(ok_rows),
            errors,
            time.monotonic() - t0,
        )

    completed_sessions = processed_sessions - aborted_sessions
    if completed_sessions:
        checkpoint.mark_completed(
            pipeline="user_friction",
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in completed_sessions],
        )
    logger.info("user_friction: wrote {} total rows", written)
    return written


def detect_user_friction(
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
    """Classify short user messages for friction signals.

    See the module docstring for the tier structure. ``dry_run`` (the
    default) returns a plan dict; a real run returns the row count.

    ``limit`` caps SESSIONS, not candidate messages: an admitted session
    always has its whole candidate set classified before it is checkpointed,
    so no message is ever stranded outside every tier.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.user_friction_dir)

    if provider is None or spec is None:
        provider, spec = build_provider(settings, "friction")

    if dry_run:
        # Measured estimation: candidate count × ~250 in-tokens (template +
        # short message), capped at the session ceiling like the real run.
        bounds = reader.session_bounds(since_days=since_days, limit=limit)
        max_sessions = settings.llm_max_sessions_per_run
        if limit is not None:
            max_sessions = min(max_sessions, int(limit))
        n = 0
        capped_sessions = 0
        for sid in bounds:
            if capped_sessions >= max_sessions:
                break
            c = len(
                candidate_messages(reader.load_steps(sid), max_chars=settings.friction_max_chars)
            )
            if c:
                capped_sessions += 1
                n += c
        # Budget-guard posture: assume EVERY candidate reaches the LLM tier.
        # Roughly half survive the fast tiers in practice, but a budget
        # estimate must bound the worst case, not the average.
        llm_n = n
        # Per-call input measured from what each call actually sends: the
        # system prompt + the rendered template + ~250 chars of message.
        per_call_chars = (
            len(USER_FRICTION_SYSTEM_PROMPT) + len(_USER_PROMPT_TEMPLATE) + _AVG_MESSAGE_CHARS
        )
        in_tokens = llm_n * tokens_for_chars(per_call_chars)
        pricing = (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
        cost = estimate_cost_tokens(in_tokens, llm_n * _AVG_OUT_TOKENS, pricing)
        logger.info(
            "user_friction --dry-run: {} candidates (~{} hit LLM), estimated cost ~${:.2f}",
            n,
            llm_n,
            cost,
        )
        return {
            "pipeline": "friction",
            "candidates": n,
            "capped_candidates": n,
            "llm_calls": llm_n,
            "estimated_input_tokens": in_tokens,
            "estimated_output_tokens": llm_n * _AVG_OUT_TOKENS,
            "estimated_cost_usd": round(cost, 4),
            "max_sessions_per_run": max_sessions,
            "max_cost_usd_per_run": settings.llm_max_cost_usd_per_run,
            "model": spec.model_id,
            "since_days": since_days,
            "limit": limit,
            "friction_max_chars": settings.friction_max_chars,
            "dry_run": True,
        }

    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)
    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)
    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, "friction"):
        return asyncio.run(
            _friction_async(
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


__all__ = ["candidate_messages", "detect_user_friction", "deterministic_stamps"]
