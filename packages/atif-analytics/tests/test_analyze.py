# SPDX-License-Identifier: Apache-2.0

"""run_analyze orchestration: budget ceilings across the LLM stages, and the opt-in narrate and review stages.

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
    "atif_analytics.application.use_cases.conflicts",
    "atif_analytics.application.use_cases.friction",
    "atif_analytics.application.use_cases.perceived",
    "atif_analytics.application.use_cases.narrate",
    "atif_analytics.application.use_cases.review",
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

    def _fake_build(settings: AnalyticsSettings, pipeline: str, **kwargs: Any) -> tuple[Any, Any]:
        del settings, pipeline, kwargs
        provider = _BillingFakeProvider()
        created.append(provider)
        return provider, SPEC

    for module in _USE_CASE_MODULES:
        monkeypatch.setattr(f"{module}.build_provider", _fake_build)
    return created


def test_dry_run_summary_surfaces_budget(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, dry_run=True, since_days=None)
    assert summary["llm_budget"] == {
        "max_sessions_per_run": 50,
        "max_cost_usd_per_run": 25.0,
    }
    assert summary["budget_exhausted"] is False
    for stage in ("classify", "conflicts", "friction", "perceived"):
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
    summary = run_analyze(capped, dry_run=False, since_days=None)
    assert summary["classify"] == 2
    skipped = {"skipped": "budget_exhausted", "consecutive_skips": 1}
    assert summary["conflicts"] == skipped
    assert summary["friction"] == skipped
    assert summary["perceived"] == skipped
    assert summary["budget_exhausted"] is True
    assert summary["llm_spent_usd"] == pytest.approx(4.0)
    # Unstarted stages stamped nothing.
    state_db = capped.layout().state_db_path
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
            summary = run_analyze(starved, dry_run=False, since_days=None)
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
    run_analyze(settings, dry_run=False, since_days=None)
    assert checkpointer.budget_skip_count(state_db, "perceived") == 0


@pytest.mark.usefixtures("fake_providers")
def test_under_ceiling_runs_every_stage(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, dry_run=False, since_days=None)
    assert summary["budget_exhausted"] is False
    for stage in ("classify", "conflicts", "friction", "perceived"):
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
    run_analyze(starved, dry_run=False, since_days=None)
    assert checkpointer.budget_skip_count(state_db, "classify") == 1

    # $3 ceiling against $2 per call over two sessions: classify RUNS, bills
    # past the ceiling, and leaves the budget exhausted for everyone after it.
    partial = settings.model_copy(update={"llm_max_cost_usd_per_run": 3.0})
    summary = run_analyze(partial, dry_run=False, since_days=None)
    assert summary["budget_exhausted"] is True
    assert not isinstance(summary["classify"], dict), "classify ran; it was not skipped outright"
    assert checkpointer.budget_skip_count(state_db, "classify") == 1


def test_narrate_is_off_unless_asked_for(settings: AnalyticsSettings) -> None:
    """An existing unattended ``analyze`` caller doesn't start narrating by upgrading."""
    summary = run_analyze(settings, dry_run=True, since_days=None)
    assert "narrate" not in summary


def test_narrate_dry_run_plans_last(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, dry_run=True, since_days=None, narrate=True)
    assert list(summary).index("narrate") > list(summary).index("perceived")
    plan = summary["narrate"]
    assert plan["dry_run"] is True
    # Both fixture sessions are eligible, one main-chain window and one story
    # each; session one's sidechain step is a third window with its own story.
    assert (plan["capped_candidates"], plan["windows"], plan["stories"]) == (2, 3, 2)
    assert (plan["subagent_streams"], plan["subagent_stories"], plan["llm_calls"]) == (1, 1, 6)


def test_narrate_runs_after_the_other_stages_under_the_same_budget(
    settings: AnalyticsSettings, fake_providers: list[_BillingFakeProvider]
) -> None:
    # Six narrate calls at $2 each on top of the other stages': give the
    # run room, so the ceiling isn't what this test is about.
    roomy = settings.model_copy(update={"llm_max_cost_usd_per_run": 100.0})
    summary = run_analyze(roomy, dry_run=False, since_days=None, narrate=True)
    assert summary["narrate"] == 2
    assert summary["budget_exhausted"] is False
    state_db = settings.layout().state_db_path
    assert len(checkpointer.load_as_map(state_db, "narrate")) == 2
    # The narrate provider was built last and billed into the shared budget.
    narrator = fake_providers[-1]
    assert {name for name, _ in narrator.calls} == {
        "WindowNarration",
        "SubagentStory",
        "SessionStory",
    }
    assert summary["llm_spent_usd"] == pytest.approx(
        2.0 * sum(len(provider.calls) for provider in fake_providers)
    )


@pytest.mark.usefixtures("fake_providers")
def test_narrate_is_budget_skipped_like_any_stage(settings: AnalyticsSettings) -> None:
    starved = settings.model_copy(update={"llm_max_cost_usd_per_run": 0.0})
    summary = run_analyze(starved, dry_run=False, since_days=None, narrate=True)
    assert summary["narrate"] == {"skipped": "budget_exhausted", "consecutive_skips": 1}
    assert not settings.layout().narration_dir.exists()


def test_review_is_off_unless_asked_for(settings: AnalyticsSettings) -> None:
    """An existing unattended ``analyze`` caller doesn't start reviewing by upgrading."""
    summary = run_analyze(settings, dry_run=True, since_days=None, narrate=True)
    assert "review" not in summary


def test_review_dry_run_plans_after_narrate(settings: AnalyticsSettings) -> None:
    summary = run_analyze(settings, dry_run=True, since_days=None, narrate=True, review=True)
    assert list(summary).index("review") > list(summary).index("narrate")
    plan = summary["review"]
    assert (plan["dry_run"], plan["route"]) == (True, "narration")
    # Nothing is narrated yet in a dry run, so both sessions wait.
    assert (plan["capped_candidates"], plan["unnarrated"], plan["llm_calls"]) == (0, 2, 0)


def test_review_runs_after_narrate_and_reads_what_it_wrote(
    settings: AnalyticsSettings, fake_providers: list[_BillingFakeProvider]
) -> None:
    roomy = settings.model_copy(update={"llm_max_cost_usd_per_run": 100.0})
    summary = run_analyze(roomy, dry_run=False, since_days=None, narrate=True, review=True)
    # Narrate ran first in the same run, so both sessions had narrations to read.
    assert summary["narrate"] == 2
    assert summary["review"] == 2
    reviewer = fake_providers[-1]
    assert {name for name, _ in reviewer.calls} == {"ReviewTriage", "ReviewFindings"}
    assert all("<narrations id=" in prompt for _, prompt in reviewer.calls)
    state_db = settings.layout().state_db_path
    assert len(checkpointer.load_as_map(state_db, "review")) == 2
    assert summary["llm_spent_usd"] == pytest.approx(
        2.0 * sum(len(provider.calls) for provider in fake_providers)
    )


@pytest.mark.usefixtures("fake_providers")
def test_review_is_budget_skipped_like_any_stage(settings: AnalyticsSettings) -> None:
    starved = settings.model_copy(update={"llm_max_cost_usd_per_run": 0.0})
    summary = run_analyze(starved, dry_run=False, since_days=None, review=True)
    assert summary["review"] == {"skipped": "budget_exhausted", "consecutive_skips": 1}
    assert not settings.layout().reviews_dir.exists()
