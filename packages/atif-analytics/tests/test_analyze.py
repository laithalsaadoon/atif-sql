# SPDX-License-Identifier: Apache-2.0

"""run_analyze orchestration: budget ceilings across the LLM lane.

FakeProvider-driven (no boto3 client is ever built): ``build_provider`` is
monkeypatched at each use-case module's binding so every stage gets a
deterministic provider whose UsageAccumulator actually grows per call —
which is what the RunBudget cost ceiling prices.
"""

from __future__ import annotations

from typing import Any, override

import pytest
from analytics_fixtures import FakeProvider

from atif_analytics.application.analyze import run_analyze
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_analytics.infrastructure.sqlite_state import checkpointer
from atif_models.domain.ports import CallUsage
from atif_models.domain.registry import resolve

SPEC = resolve("medium")

# Every call passes since_days=None: the fixture corpus carries fixed 2026-08
# timestamps, and the production default (30 days against the wall clock) aged
# both sessions out on 2026-09-20, leaving these tests asserting over nothing.

_USE_CASE_MODULES = (
    "atif_analytics.application.use_cases.classify",
    "atif_analytics.application.use_cases.trajectory",
    "atif_analytics.application.use_cases.conflicts",
    "atif_analytics.application.use_cases.friction",
    "atif_analytics.application.use_cases.perceived",
)


class _BillingFakeProvider(FakeProvider):
    """FakeProvider that bills 1M input tokens per call (=$2 on terra)."""

    @override
    async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
        self.usage.add(CallUsage(input_tokens=1_000_000, output_tokens=0))
        return await super().classify_structured(system=system, prompt=prompt, schema=schema)


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch) -> list[_BillingFakeProvider]:
    """Patch every stage's ``build_provider`` binding; collect the providers."""
    created: list[_BillingFakeProvider] = []

    def _fake_build(settings: AnalyticsSettings, pipeline: str) -> tuple[Any, Any]:
        del settings, pipeline
        provider = _BillingFakeProvider()
        created.append(provider)
        return provider, SPEC

    for module in _USE_CASE_MODULES:
        monkeypatch.setattr(f"{module}.build_provider", _fake_build)
    return created


def test_dry_run_summary_surfaces_budget(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, llm_only=True, dry_run=True, since_days=None)
    assert summary["llm_budget"] == {
        "max_sessions_per_run": 50,
        "max_cost_usd_per_run": 25.0,
    }
    assert summary["budget_exhausted"] is False
    for stage in ("classify", "trajectory", "conflicts", "friction", "perceived"):
        plan = summary[stage]
        assert plan["dry_run"] is True
        assert plan["max_sessions_per_run"] == 50
        assert plan["max_cost_usd_per_run"] == 25.0


@pytest.mark.usefixtures("fake_providers")
def test_cost_ceiling_skips_remaining_stages_and_flags_report(
    settings: AnalyticsSettings,
) -> None:
    # $2/call on terra; classify makes 2 calls = $4 > $3 ceiling — every
    # later stage must be skipped with nothing stamped.
    capped = settings.model_copy(update={"llm_max_cost_usd_per_run": 3.0})
    summary = run_analyze(capped, llm_only=True, dry_run=False, since_days=None)
    assert summary["classify"] == 2
    skipped = {"skipped": "budget_exhausted", "consecutive_skips": 1}
    assert summary["trajectory"] == skipped
    assert summary["conflicts"] == skipped
    assert summary["friction"] == skipped
    assert summary["perceived"] == skipped
    assert summary["budget_exhausted"] is True
    assert summary["llm_spent_usd"] == pytest.approx(4.0)
    # Unstarted stages stamped nothing.
    state_db = capped.layout().state_db_path
    assert checkpointer.load_as_map(state_db, "trajectory") == {}
    assert checkpointer.load_as_map(state_db, "conflicts") == {}
    assert checkpointer.load_as_map(state_db, "user_friction") == {}
    assert checkpointer.load_as_map(state_db, "perceived") == {}


@pytest.mark.usefixtures("fake_providers")
def test_consecutive_budget_skips_escalate_to_error(settings: AnalyticsSettings) -> None:
    """Starvation visibility: 3 consecutive budget-skips log ERROR, not WARNING."""
    from loguru import logger

    # Ceiling 0.0: the budget reads exhausted before any call, so EVERY
    # stage is budget-skipped on every run — three deterministic skips.
    starved = settings.model_copy(update={"llm_max_cost_usd_per_run": 0.0})
    records: list[Any] = []
    sink_id = logger.add(records.append, level="WARNING")
    try:
        for run in range(3):
            records.clear()
            summary = run_analyze(starved, llm_only=True, dry_run=False, since_days=None)
            assert summary["perceived"] == {
                "skipped": "budget_exhausted",
                "consecutive_skips": run + 1,
            }
            perceived_skips = [
                r.record for r in records if "analyze/perceived: SKIPPED" in r.record["message"]
            ]
            assert len(perceived_skips) == 1
            expected_level = "ERROR" if run + 1 >= 3 else "WARNING"
            assert perceived_skips[0]["level"].name == expected_level
    finally:
        logger.remove(sink_id)
    state_db = starved.layout().state_db_path
    # The persisted streak survived across runs in the state db.
    assert checkpointer.budget_skip_count(state_db, "perceived") == 3
    # A run where the stage actually executes clears its streak.
    run_analyze(settings, llm_only=True, dry_run=False, since_days=None)
    assert checkpointer.budget_skip_count(state_db, "perceived") == 0


@pytest.mark.usefixtures("fake_providers")
def test_under_ceiling_runs_every_stage(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, llm_only=True, dry_run=False, since_days=None)
    assert summary["budget_exhausted"] is False
    for stage in ("classify", "trajectory", "conflicts", "friction", "perceived"):
        assert isinstance(summary[stage], int)


@pytest.mark.usefixtures("fake_providers")
def test_streak_survives_a_stage_that_runs_but_exhausts_the_budget(
    settings: AnalyticsSettings,
) -> None:
    """A stage starved MID-run holds its streak, so the 3-strike ERROR can fire.

    Two ways a stage gets starved: the ceiling is already hit when its turn
    comes (skipped outright), or it starts, bills, and runs out partway. Both
    leave work undone, so clearing the streak on "it ran at all" makes the
    escalation unreachable for the second — the stage that always aborts
    mid-chunk resets its own counter every run, forever.
    """
    state_db = settings.layout().state_db_path

    # Ceiling 0.0: budget-skipped before the first call, so classify takes a streak.
    starved = settings.model_copy(update={"llm_max_cost_usd_per_run": 0.0})
    run_analyze(starved, llm_only=True, dry_run=False, since_days=None)
    assert checkpointer.budget_skip_count(state_db, "classify") == 1

    # $3 ceiling against $2 per call over two sessions: classify RUNS, bills
    # past the ceiling, and leaves the budget exhausted for everyone after it.
    partial = settings.model_copy(update={"llm_max_cost_usd_per_run": 3.0})
    summary = run_analyze(partial, llm_only=True, dry_run=False, since_days=None)
    assert summary["budget_exhausted"] is True
    assert not isinstance(summary["classify"], dict), "classify ran; it was not skipped outright"
    assert checkpointer.budget_skip_count(state_db, "classify") == 1
