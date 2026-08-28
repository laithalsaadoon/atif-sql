# SPDX-License-Identifier: Apache-2.0

"""Perceived-error pipeline: eligibility, uuid guard, checkpoint/retry/budget.

FakeProvider-driven (no live LLM calls). Fixture facts this file leans on:
session one has TWO completed human-AI text pairs (eligible); session two
has ONE (user → agent) — ineligible under the LangSmith >=2-pairs gate.
FakeProvider's canned PerceivedErrorsResult carries one valid row anchored
on ``u-04`` plus one hallucinated ``not-a-real-uuid`` row that the edges
guard must drop.
"""

from __future__ import annotations

from typing import Any, override

import polars as pl
import pytest
from analytics_fixtures import SESSION_IDS, FakeProvider

from atif_analytics.application.use_cases._shared import RunBudget
from atif_analytics.application.use_cases.perceived import detect_perceived_errors
from atif_analytics.domain.models import PerceivedError, PerceivedErrorsResult
from atif_analytics.domain.transcript import StepEvent, human_ai_pair_count
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.parquet_cache import ParquetCache
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_analytics.infrastructure.sqlite_state import retry_queue
from atif_analytics.infrastructure.sqlite_state.checkpointer import (
    SqliteCheckpoint,
    load_as_map,
)
from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
from atif_models.domain.registry import resolve

SPEC = resolve("medium")


@pytest.fixture
def ports(settings: AnalyticsSettings) -> dict[str, Any]:
    layout = settings.layout()
    return {
        "checkpoint": SqliteCheckpoint(layout.state_db_path),
        "retry": SqliteRetryQueue(layout.state_db_path),
        "state_db": layout.state_db_path,
        "layout": layout,
    }


# ---------------------------------------------------------------------------
# eligibility (pure domain)
# ---------------------------------------------------------------------------


def _u(text: str) -> StepEvent:
    return StepEvent(ts="t", role="user", text=text)


def _a(text: str) -> StepEvent:
    return StepEvent(ts="t", role="assistant", text=text)


def test_human_ai_pair_count_semantics() -> None:
    # Two clean pairs.
    assert human_ai_pair_count([_u("q1"), _a("r1"), _u("q2"), _a("r2")]) == 2
    # Consecutive user turns collapse into one pending pair.
    assert human_ai_pair_count([_u("q1"), _u("q1b"), _a("r1")]) == 1
    # Consecutive assistant turns only complete one pair.
    assert human_ai_pair_count([_u("q1"), _a("r1"), _a("r1b")]) == 1
    # A trailing unanswered user turn completes nothing.
    assert human_ai_pair_count([_u("q1"), _a("r1"), _u("q2")]) == 1
    # Assistant-first text doesn't count; sidechain/compact/empty excluded.
    assert human_ai_pair_count([_a("hello"), _u("q"), _a("r")]) == 1
    side = StepEvent(ts="t", role="assistant", text="sub", is_sidechain=True)
    compact = StepEvent(ts="t", role="user", text="summary", is_compact_summary=True)
    empty = StepEvent(ts="t", role="assistant", text="")
    assert human_ai_pair_count([_u("q"), side, compact, empty]) == 0


def test_bookkeeping_only_user_turns_are_ineligible() -> None:
    """CLI bookkeeping strings don't count as the human responding to AI."""
    resume = _u("Continue from where you left off.")
    interrupt = _u("[Request interrupted by user for tool use]")
    # A session whose only user turns are bookkeeping strings: zero pairs.
    assert human_ai_pair_count([resume, _a("r1"), interrupt, _a("r2")]) == 0
    # Bookkeeping doesn't open a pair, but a real user turn still does.
    assert human_ai_pair_count([resume, _u("real question"), _a("r1")]) == 1
    # And bookkeeping between a real user turn and the reply doesn't break it.
    assert human_ai_pair_count([_u("q1"), interrupt, _a("r1")]) == 1


def test_fixture_sessions_split_on_eligibility(reader: CorpusReader) -> None:
    """The pins the pipeline tests below rely on."""
    assert human_ai_pair_count(reader.load_steps(SESSION_IDS[0])) >= 2
    assert human_ai_pair_count(reader.load_steps(SESSION_IDS[1])) == 1


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def test_perceived_writes_valid_rows_and_drops_hallucinated_uuid(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    # Only the eligible session (one) was processed via the LLM.
    assert n == 1
    assert len(provider.calls) == 1
    df = ParquetCache(ports["layout"].perceived_errors_dir).read_all()
    assert df is not None
    # FakeProvider returned 2 rows; the hallucinated-uuid row was dropped.
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["session_id"] == SESSION_IDS[0]
    assert row["turn_uuid"] == "u-04"
    assert row["signal"] == "correction"
    assert row["severity"] == "minor"
    assert row["evidence"] == "why is it failing?"
    assert row["confidence"] == pytest.approx(0.8)
    # The transcript carried uuid headers.
    assert all("[uuid=" in p for _, p in provider.calls)


class _AssistantUuidProvider(FakeProvider):
    """Returns one perceived-error row anchored on an ASSISTANT turn's uuid.

    ``u-02`` is a real raw-record uuid (it's in edges.jsonl and in the
    rendered ``[uuid=...]`` headers) but belongs to an agent step — the
    documented invariant is USER turns only, so the guard must drop it.
    """

    @override
    async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
        if schema is PerceivedErrorsResult:
            self.calls.append((schema.__name__, prompt))
            return PerceivedErrorsResult(
                errors=[
                    PerceivedError(
                        turn_uuid="u-02",
                        signal="acknowledged_mistake",
                        severity="minor",
                        evidence="Reading the test file now.",
                        agent_error_summary="Anchored on an assistant turn.",
                        confidence=0.7,
                    )
                ]
            )
        return await super().classify_structured(system=system, prompt=prompt, schema=schema)


def test_perceived_drops_row_anchored_on_assistant_turn_uuid(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """USER-turn enforcement: an edges-valid assistant uuid is still dropped."""
    provider = _AssistantUuidProvider()
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 1  # the session processed fine — only the row was dropped
    df = ParquetCache(ports["layout"].perceived_errors_dir).read_all()
    assert df is None or df.height == 0


def test_perceived_ineligible_session_checkpointed_without_billing(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    # Session two (1 pair) never reached the provider…
    assert all("draft the launch memo" not in p for _, p in provider.calls)
    # …but IS checkpointed as skipped, so it never re-renders.
    ckpt = load_as_map(ports["state_db"], "perceived")
    assert set(ckpt) == set(SESSION_IDS)
    # Rerun is a full no-op: no calls, no rows.
    provider2 = FakeProvider()
    n2 = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider2,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n2 == 0
    assert provider2.calls == []


def test_perceived_failure_enqueues_retry_and_skips_checkpoint(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider(fail_prompts_containing="flaky auth test")
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 0  # the only eligible session failed
    assert retry_queue.pending_count(ports["state_db"], pipeline="perceived") == 1
    assert SESSION_IDS[0] not in load_as_map(ports["state_db"], "perceived")


def test_perceived_blocked_retry_unit_is_not_rebilled(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """A freshly-failed session (backoff pending) is skipped, not re-billed."""
    retry_queue.enqueue(
        ports["state_db"], pipeline="perceived", unit_id=SESSION_IDS[0], error="boom"
    )
    provider = FakeProvider()
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 0
    assert provider.calls == []


def test_perceived_refusal_writes_audit_sidecar_row(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider(refuse_prompts_containing="flaky auth test")
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 1  # processed (refusal is terminal)
    # Zero error rows for the refused session.
    df = ParquetCache(ports["layout"].perceived_errors_dir).read_all()
    assert df is None or df.filter(pl.col("session_id") == SESSION_IDS[0]).height == 0
    # Durable audit row in the sidecar.
    refusals = ParquetCache(ports["layout"].refusals_dir).read_all()
    assert refusals is not None
    row = refusals.to_dicts()[0]
    assert row["pipeline"] == "perceived"
    assert row["unit_id"] == SESSION_IDS[0]
    # Terminal: checkpointed, nothing queued.
    assert SESSION_IDS[0] in load_as_map(ports["state_db"], "perceived")
    assert retry_queue.pending_count(ports["state_db"], pipeline="perceived") == 0


def test_perceived_cost_ceiling_aborts_without_stamping(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    from atif_models.domain.ports import CallUsage

    budget = RunBudget(max_cost_usd=0.01)
    provider = FakeProvider()
    provider.usage.add(CallUsage(input_tokens=10_000_000, output_tokens=0))
    n = detect_perceived_errors(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
        budget=budget,
    )
    assert n == 0
    assert provider.calls == []
    # The eligible session is unstamped (the ineligible one may checkpoint —
    # its skip costs nothing and is not budget-gated).
    assert SESSION_IDS[0] not in load_as_map(ports["state_db"], "perceived")
    assert retry_queue.pending_count(ports["state_db"], pipeline="perceived") == 0


def test_perceived_dry_run_plan_measures_eligible_transcripts(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    plan = detect_perceived_errors(
        settings, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    assert plan["pipeline"] == "perceived"
    assert plan["candidates"] == 2
    assert plan["eligible_candidates"] == 1  # session two fails the pair gate
    assert plan["capped_candidates"] == 1
    assert plan["llm_calls"] == 1
    # Input tokens are MEASURED from the eligible session's uuid-headed
    # transcript (chars/4) — session one carries a 60K-char tool result.
    expected_in = len(reader.session_text(SESSION_IDS[0], include_uuids=True)) // 4
    assert plan["estimated_input_tokens"] == expected_in
    assert plan["estimated_input_tokens"] > 10_000
    # Unpriced aliases carry ``None``, which would make the cost arithmetic below
    # meaningless rather than merely wrong.
    assert SPEC.pricing_in is not None
    assert SPEC.pricing_out is not None
    expected = (expected_in * SPEC.pricing_in + 400 * SPEC.pricing_out) / 1_000_000
    assert plan["estimated_cost_usd"] == round(expected, 4)
    assert plan["max_sessions_per_run"] == 50
    assert plan["max_cost_usd_per_run"] == 25.0
    assert plan["model"] == SPEC.model_id
    assert plan["dry_run"] is True
