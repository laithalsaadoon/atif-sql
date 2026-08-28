# SPDX-License-Identifier: Apache-2.0

"""The ``analyze`` orchestration composing the eight pipelines.

Stage order (materializing is atif-corpus's job and embedding is
atif-embed's, so neither appears here):

1. cluster     (UMAP+HDBSCAN over the lance store; zero LLM cost)
2. terms       (c-TF-IDF labels for clusters; zero LLM cost)
3. community   (Leiden+CPM over session centroids; zero LLM cost)
4. classify    (LLM; honors ``dry_run``)
5. trajectory  (LLM; honors ``dry_run``)
6. conflicts   (LLM; honors ``dry_run``)
7. friction    (LLM; honors ``dry_run``)
8. perceived   (LLM; honors ``dry_run``)

``structural_only`` / ``llm_only`` are the cron-lane groupings (CONTRACT-V2
§Cron: structural :17, llm nightly); ``skip_*`` flags opt out per stage.
``dry_run`` defaults to True (cost guard) — the LLM stages then return plan
dicts instead of running.

Heavy pipeline imports are DEFERRED into the body so importing this module
never drags umap/boto3 onto the CLI's fast path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from atif_analytics.infrastructure.settings import AnalyticsSettings


def run_analyze(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = 30,
    limit: int | None = None,
    dry_run: bool = True,
    structural_only: bool = False,
    llm_only: bool = False,
    skip_cluster: bool = False,
    skip_terms: bool = False,
    skip_community: bool = False,
    skip_classify: bool = False,
    skip_trajectory: bool = False,
    skip_conflicts: bool = False,
    skip_friction: bool = False,
    skip_perceived: bool = False,
    force_cluster: bool = False,
    force_community: bool = False,
) -> dict[str, Any]:
    """Run the analytics pipeline end-to-end: structure → LLM analytics.

    Returns a per-stage summary dict. ``structural_only`` and ``llm_only``
    are mutually exclusive lane selectors; ``skip_*`` flags subtract
    individual stages from whichever lane runs.
    """
    if structural_only and llm_only:
        msg = "structural_only and llm_only are mutually exclusive"
        raise ValueError(msg)

    run_structural = not llm_only
    run_llm = not structural_only

    summary: dict[str, Any] = {"dry_run": dry_run}

    # One shared corpus reader across every stage: the parsed-steps memo is
    # the expensive part, and every LLM stage walks the same sessions.
    from atif_analytics.infrastructure.corpus_reader import CorpusReader

    reader = CorpusReader(settings.corpus_root, caps=settings.transcript_caps())

    if run_structural:
        if not skip_cluster:
            from atif_analytics.application.use_cases.cluster import run_clustering

            stats = run_clustering(settings, force=force_cluster)
            logger.info(
                "analyze/cluster: {} messages, {} clusters, {} noise (skipped={})",
                stats["total"],
                stats["clusters"],
                stats["noise"],
                stats["skipped"],
            )
            summary["cluster"] = stats

        if not skip_terms:
            from atif_analytics.application.use_cases.terms import run_terms

            tstats = run_terms(settings, force=force_cluster, reader=reader)
            logger.info(
                "analyze/terms: {} clusters, {} term-rows (skipped={})",
                tstats["clusters"],
                tstats["terms"],
                tstats["skipped"],
            )
            summary["terms"] = tstats

        if not skip_community:
            from atif_analytics.application.use_cases.community import run_communities

            cstats = run_communities(settings, force=force_community, reader=reader)
            logger.info(
                "analyze/community: {} sessions, {} communities (skipped={})",
                cstats["sessions"],
                cstats["communities"],
                cstats["skipped"],
            )
            summary["community"] = cstats

    if run_llm:
        from atif_analytics.application.use_cases._shared import RunBudget
        from atif_analytics.application.use_cases.classify import classify_sessions
        from atif_analytics.application.use_cases.conflicts import detect_conflicts
        from atif_analytics.application.use_cases.friction import detect_user_friction
        from atif_analytics.application.use_cases.perceived import detect_perceived_errors
        from atif_analytics.application.use_cases.trajectory import trajectory_messages

        # THE BUDGET CEILING (unattended-cron money guard). Two independent
        # caps, both settings-driven (ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN /
        # ATIF_SQL_LLM_MAX_COST_USD_PER_RUN) and CLI-overridable:
        # * llm_max_sessions_per_run — enforced DURING each pipeline's
        #   newest-first admission walk (fresh sessions win), so a deferred
        #   session is never rendered;
        # * llm_max_cost_usd_per_run — one RunBudget shared by every stage,
        #   priced from the providers' UsageAccumulator RUNNING ACTUALS.
        #
        # The cost cap is a STOP-DISPATCH trigger, not a hard cap: actual
        # spend only becomes visible after a call returns, so the run can end
        # above the ceiling by whatever was in flight at the crossing — at
        # most BUDGET_CHECK_BATCH units on the priciest watched model. It is
        # re-read every batch (not once per write chunk, which is wider than
        # the session ceiling), so the overshoot does not scale with the
        # batch size. When crossed, remaining LLM work stops: the ceiling hit
        # is logged, nothing is stamped (no checkpoint/cache row) for
        # unstarted sessions, and the report flags budget_exhausted=true.
        budget = RunBudget(settings.llm_max_cost_usd_per_run)
        summary["llm_budget"] = {
            "max_sessions_per_run": settings.llm_max_sessions_per_run,
            "max_cost_usd_per_run": settings.llm_max_cost_usd_per_run,
        }

        from atif_analytics.infrastructure.sqlite_state import checkpointer

        #: Consecutive budget-skips before a stage's starvation escalates
        #: to ERROR (the tail stages of a backlogged corpus can otherwise
        #: starve silently behind WARNING noise).
        max_consecutive_budget_skips = 3

        state_db = settings.layout().state_db_path
        stages = [
            ("classify", classify_sessions, skip_classify),
            ("trajectory", trajectory_messages, skip_trajectory),
            ("conflicts", detect_conflicts, skip_conflicts),
            ("friction", detect_user_friction, skip_friction),
            ("perceived", detect_perceived_errors, skip_perceived),
        ]
        for name, fn, skip in stages:
            if skip:
                continue
            if not dry_run and budget.exhausted():
                # Starvation visibility: persist the skip so consecutive
                # streaks survive across runs, and escalate to ERROR when
                # the SAME stage has been budget-starved 3+ runs in a row.
                streak = checkpointer.record_budget_skip(state_db, name)
                log = logger.error if streak >= max_consecutive_budget_skips else logger.warning
                log(
                    "analyze/{}: SKIPPED — cost ceiling hit "
                    "(${:.2f} spent >= ${:.2f} ceiling); nothing stamped "
                    "(budget-skipped {} consecutive runs)",
                    name,
                    budget.spent_usd(),
                    budget.max_cost_usd,
                    streak,
                )
                summary[name] = {"skipped": "budget_exhausted", "consecutive_skips": streak}
                continue
            n = fn(
                settings,
                since_days=since_days,
                limit=limit,
                dry_run=dry_run,
                reader=reader,
                budget=budget,
            )
            if not dry_run and not budget.exhausted():
                # Clear only when the stage finished INSIDE its budget. A stage
                # that runs but aborts mid-chunk on the ceiling was starved just
                # as surely as one skipped before it started, and clearing on
                # "it ran at all" resets the streak every run, so the 3-strike
                # escalation above can never fire for the stage that needs it
                # most. A stage that spends its last cent doing all of its work
                # also holds its streak; that errs toward the louder log, which
                # is the right direction for a spend guard.
                checkpointer.clear_budget_skips(state_db, name)
            logger.info("analyze/{}: {} (dry_run={})", name, n, dry_run)
            summary[name] = n

        summary["budget_exhausted"] = False if dry_run else budget.exhausted()
        if not dry_run:
            summary["llm_spent_usd"] = round(budget.spent_usd(), 4)

    logger.info("analyze: done")
    return summary


__all__ = ["run_analyze"]
