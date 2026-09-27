# SPDX-License-Identifier: Apache-2.0

"""FakeProvider-driven pipeline tests: parquet rows, checkpoints, retry, refusals.

NO live LLM calls: every provider is the deterministic
:class:`analytics_fixtures.FakeProvider`.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from analytics_fixtures import (
    S1_MACHINE_UUIDS,
    SESSION_IDS,
    FakeProvider,
    write_session,
)

from atif_analytics.application.use_cases._shared import RunBudget
from atif_analytics.application.use_cases.classify import classify_sessions
from atif_analytics.application.use_cases.conflicts import detect_conflicts
from atif_analytics.application.use_cases.friction import detect_user_friction
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
# classify
# ---------------------------------------------------------------------------


def test_classify_writes_rows_and_checkpoints(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    n = classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 2
    cache = ParquetCache(ports["layout"].classifications_dir)
    df = cache.read_all()
    assert df is not None
    assert set(df["session_id"].to_list()) == set(SESSION_IDS)
    # The dropped LLM labels are not written at all.
    assert df.columns == ["session_id", "work_category", "goal", "confidence", "classified_at"]
    assert df["work_category"].to_list() == ["sde", "sde"]
    assert df["goal"][0] == "Fix the flaky auth test."
    # Checkpoint stamped for both sessions at current bounds.
    ckpt = load_as_map(ports["state_db"], "classify")
    assert set(ckpt) == set(SESSION_IDS)
    # A rerun is a no-op: checkpoint + anti-join keep everything out.
    n2 = classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n2 == 0


def test_classify_enqueues_retry_on_provider_unavailable(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    # Session one's transcript contains the flaky-test text — fail just it.
    provider = FakeProvider(fail_prompts_containing="flaky auth test")
    n = classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 1  # session two still landed
    pending = retry_queue.drain(ports["state_db"], pipeline="classify", now=None, max_attempts=5)
    # Not due yet (2-min backoff) — check the row exists via pending_count.
    assert retry_queue.pending_count(ports["state_db"], pipeline="classify") == 1
    assert pending == []
    # The failed session is NOT checkpointed.
    assert SESSION_IDS[0] not in load_as_map(ports["state_db"], "classify")


def test_classify_exhausted_retry_session_is_not_billed_again(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """A 5-times-failed session must NOT re-enter via the checkpoint path.

    It was never checkpointed (failures aren't), so without the blocked-units
    gate every subsequent run would re-admit and re-bill it forever.
    """
    for i in range(5):
        retry_queue.enqueue(
            ports["state_db"], pipeline="classify", unit_id=SESSION_IDS[0], error=f"e{i}"
        )
    provider = FakeProvider()
    n = classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 1  # only session two ran
    billed_sessions = {p for _, p in provider.calls}
    assert all("flaky auth test" not in p for p in billed_sessions)
    # The exhausted session is still not checkpointed (the queue owns it).
    assert SESSION_IDS[0] not in load_as_map(ports["state_db"], "classify")


def test_classify_session_cap_limits_real_run(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    capped = settings.model_copy(update={"llm_max_sessions_per_run": 1})
    provider = FakeProvider()
    n = classify_sessions(
        capped,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 1
    assert len(provider.calls) == 1
    # Only the newest session (session two, 2026-08-21) ran; session one is
    # deferred, not stamped.
    ckpt = load_as_map(ports["state_db"], "classify")
    assert set(ckpt) == {SESSION_IDS[1]}


def test_classify_cost_ceiling_aborts_without_stamping(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """An exhausted RunBudget stops dispatch before any call; nothing stamped."""
    from atif_models.domain.ports import CallUsage

    budget = RunBudget(max_cost_usd=0.01)
    provider = FakeProvider()
    # Simulate prior spend past the ceiling (classify_sessions itself
    # registers this provider's accumulator with the budget).
    provider.usage.add(CallUsage(input_tokens=10_000_000, output_tokens=0))
    n = classify_sessions(
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
    assert provider.calls == []  # no LLM call was billed
    assert load_as_map(ports["state_db"], "classify") == {}  # nothing stamped
    assert retry_queue.pending_count(ports["state_db"], pipeline="classify") == 0


def test_classify_refusal_is_terminal_and_writes_sentinel_row(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider(refuse_prompts_containing="flaky auth test")
    n = classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 2  # one real row + one refusal sentinel
    # Refused session: no retry row, but checkpointed (never re-billed).
    assert retry_queue.pending_count(ports["state_db"], pipeline="classify") == 0
    assert SESSION_IDS[0] in load_as_map(ports["state_db"], "classify")
    # The refusal is a durable, queryable sentinel row (documented shape).
    df = ParquetCache(ports["layout"].classifications_dir).read_all()
    assert df is not None
    sentinel = df.filter(pl.col("session_id") == SESSION_IDS[0]).to_dicts()[0]
    assert sentinel["goal"] == "[refused]"
    assert sentinel["work_category"] == "unknown"
    assert sentinel["confidence"] == 0.0
    # The anti-join sees the sentinel, so a rerun never re-bills the session.
    provider2 = FakeProvider(refuse_prompts_containing="flaky auth test")
    n2 = classify_sessions(
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


def test_classify_dry_run_plan_math(settings: AnalyticsSettings, reader: CorpusReader) -> None:
    plan = classify_sessions(
        settings, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    assert plan["pipeline"] == "classify"
    assert plan["candidates"] == 2
    assert plan["capped_candidates"] == 2
    assert plan["llm_calls"] == 2
    # Input tokens are MEASURED from the rendered transcripts (chars/4).
    expected_in = sum(len(reader.session_text(sid)) // 4 for sid in SESSION_IDS)
    assert plan["estimated_input_tokens"] == expected_in
    # Unpriced aliases carry ``None``, which would make the cost arithmetic below
    # meaningless rather than merely wrong.
    assert SPEC.pricing_in is not None
    assert SPEC.pricing_out is not None
    expected = (expected_in * SPEC.pricing_in + 2 * 150 * SPEC.pricing_out) / 1_000_000
    assert plan["estimated_cost_usd"] == round(expected, 4)
    # The budget ceilings surface in the plan.
    assert plan["max_sessions_per_run"] == 50
    assert plan["max_cost_usd_per_run"] == 25.0
    assert plan["model"] == SPEC.model_id
    assert plan["dry_run"] is True


def test_classify_dry_run_estimate_tracks_rendered_length(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    """The fixture session's estimate is within 2x of its real rendered length.

    The old static 8K prior understated a 60K-char tool-result transcript
    ~20x; measured chars/4 must stay within a factor of two of reality.
    """
    plan = classify_sessions(
        settings, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    real_tokens = sum(len(reader.session_text(sid)) for sid in SESSION_IDS) / 4
    assert real_tokens > 0
    assert real_tokens / 2 <= plan["estimated_input_tokens"] <= real_tokens * 2
    # Session one carries a 60K-char tool result — the estimate must reflect
    # transcript scale, not the old ~8K static prior.
    assert plan["estimated_input_tokens"] > 10_000


def test_classify_dry_run_caps_session_count(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    capped = settings.model_copy(update={"llm_max_sessions_per_run": 1})
    plan = classify_sessions(
        capped, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    assert plan["candidates"] == 2
    assert plan["capped_candidates"] == 1
    assert plan["llm_calls"] == 1
    assert plan["max_sessions_per_run"] == 1


AUDIT_SID = "cccccccc-3333-3333-3333-333333333333"
ONE_SHOT_SID = "dddddddd-4444-4444-4444-444444444444"
RETRY = "Your previous attempt hit a transient error. Try that again."


def _add_non_interactive_sessions(settings: AnalyticsSettings) -> None:
    """A turn audit and a one-shot job, both NEWER than the fixture sessions."""
    write_session(
        settings.corpus_root,
        AUDIT_SID,
        [
            ("user", "You are auditing an agent turn for integrity. Transcript follows."),
            ("agent", '{"ok": true}'),
        ],
        day=25,
    )
    write_session(
        settings.corpus_root,
        ONE_SHOT_SID,
        [
            ("user", "Run the nightly report."),
            ("agent", "Working."),
            ("user", RETRY),
            ("user", "Stop hook feedback: cite the source."),
            ("agent", "Brief posted with sources."),
        ],
        day=26,
    )


@pytest.mark.parametrize("run_stage", [classify_sessions, detect_conflicts])
def test_turn_audits_and_one_shot_jobs_are_never_billed(
    settings: AnalyticsSettings, ports: dict[str, Any], run_stage: Any
) -> None:
    """Only interactive sessions reach the model; the rest are checkpointed as skipped.

    The one-shot job has three user-role steps, but only one is human (a
    retry nudge and a Stop hook are not the user), so it is one_shot_job,
    not interactive. Both extra sessions are the NEWEST in the corpus, so a
    cap of two would spend both slots on them if the gate ran after the cap.
    """
    _add_non_interactive_sessions(settings)
    reader = CorpusReader(settings.corpus_root)
    assert reader.session_kind(AUDIT_SID) == "turn_audit"
    assert reader.session_kind(ONE_SHOT_SID) == "one_shot_job"
    capped = settings.model_copy(update={"llm_max_sessions_per_run": 2})
    provider = FakeProvider()
    run_stage(
        capped,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    prompts = [p for _, p in provider.calls]
    assert len(prompts) == 2
    assert not any("auditing an agent turn" in p or "nightly report" in p for p in prompts)
    pipeline = "classify" if run_stage is classify_sessions else "conflicts"
    stamped = set(load_as_map(ports["state_db"], pipeline))
    assert {AUDIT_SID, ONE_SHOT_SID} <= stamped


def test_classify_dry_run_skips_non_interactive(settings: AnalyticsSettings) -> None:
    _add_non_interactive_sessions(settings)
    plan = classify_sessions(
        settings,
        dry_run=True,
        reader=CorpusReader(settings.corpus_root),
        provider=FakeProvider(),
        spec=SPEC,
    )
    assert isinstance(plan, dict)
    assert plan["candidates"] == 4
    assert plan["skipped_not_interactive"] == 2
    assert plan["llm_calls"] == 2


def test_classify_renders_machine_text_under_its_author(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """The model reads a Stop hook as [stop_hook ...], never as [user ...]."""
    provider = FakeProvider()
    classify_sessions(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    s1 = next(p for _, p in provider.calls if "flaky auth test" in p)
    assert "[stop_hook 2026-08-20T10:01:01.000Z] Stop hook feedback" in s1
    assert "[harness 2026-08-20T10:01:02.000Z] Your previous attempt" in s1
    assert "[user 2026-08-20T10:01:02" not in s1


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def test_conflicts_writes_valid_pairs_and_drops_invalid_uuids(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    n = detect_conflicts(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 2  # sessions processed
    df = ParquetCache(ports["layout"].conflicts_dir).read_all()
    assert df is not None
    # FakeProvider returns 2 pairs per session; the invalid-uuid pair must be
    # dropped by the edges guard, leaving one VALID pair per session — but
    # only session one's edges contain u-01/u-04, so session two's canned
    # pair is dropped entirely.
    s1 = df.filter(pl.col("session_id") == SESSION_IDS[0])
    assert s1.height == 1
    assert s1["turn_a_uuid"][0] == "u-01"
    assert s1["turn_b_uuid"][0] == "u-04"
    assert s1["conflict_kind"][0] == "correction"
    s2 = df.filter(pl.col("session_id") == SESSION_IDS[1])
    assert s2.height == 0
    # The conflicts prompt carried uuid headers.
    conflict_prompts = [p for name, p in provider.calls if name == "ConflictsResult"]
    assert all("[uuid=" in p for p in conflict_prompts)


def test_conflicts_refusal_writes_audit_sidecar_row(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """A refused conflicts session lands in analytics/refusals — queryable,
    zero pair rows (view semantics untouched), checkpointed (never re-billed)."""
    provider = FakeProvider(refuse_prompts_containing="flaky auth test")
    n = detect_conflicts(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n == 2  # both sessions processed (one refused)
    # Zero pair rows for the refused session.
    pairs = ParquetCache(ports["layout"].conflicts_dir).read_all()
    if pairs is not None:
        assert pairs.filter(pl.col("session_id") == SESSION_IDS[0]).height == 0
    # Durable audit row in the sidecar.
    refusals = ParquetCache(ports["layout"].refusals_dir).read_all()
    assert refusals is not None
    row = refusals.to_dicts()[0]
    assert row["pipeline"] == "conflicts"
    assert row["unit_id"] == SESSION_IDS[0]
    assert "refusal" in row["reason"]
    assert row["refused_at"] is not None
    # Terminal: checkpointed, nothing queued.
    assert SESSION_IDS[0] in load_as_map(ports["state_db"], "conflicts")
    assert retry_queue.pending_count(ports["state_db"], pipeline="conflicts") == 0


def test_conflicts_rerun_noop(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    detect_conflicts(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    n2 = detect_conflicts(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n2 == 0


# ---------------------------------------------------------------------------
# friction
# ---------------------------------------------------------------------------


def test_friction_tiers_and_llm_rows(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    n = detect_user_friction(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df = ParquetCache(ports["layout"].user_friction_dir).read_all()
    assert df is not None
    assert df.height == n
    by_uuid = {row["uuid"]: row for row in df.to_dicts()}
    # Stamp tier hits (source='sql').
    assert by_uuid["u-05"]["label"] == "unmet_expectation"
    assert by_uuid["u-05"]["source"] == "sql"
    assert by_uuid["u-06"]["label"] == "correction"
    assert by_uuid["u-04"]["label"] == "confusion"
    # Everything else went to the LLM and came back 'none'.
    assert by_uuid["u-01"]["source"] == "llm"
    assert by_uuid["u-01"]["label"] == "none"
    # Machine-written user-role text never produced a row: not the Stop
    # hook, not the continuation marker, and not the retry nudge or the
    # screenshot metadata, whose repeats rule 1 stamped as unmet_expectation
    # when every user-role step was a candidate.
    for machine in S1_MACHINE_UUIDS:
        assert machine not in by_uuid
    assert set(by_uuid) == {"u-01", "u-04", "u-05", "u-06", "v-01", "v-03"}
    # LLM prompts wrap the message in the template.
    friction_prompts = [p for name, p in provider.calls if name == "UserFrictionSignal"]
    assert all("SHORT USER MESSAGE" in p for p in friction_prompts)


def test_friction_refusal_row_and_retry_enqueue(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    # u-01 ("please fix the flaky auth test") goes to the LLM tier; refuse it.
    provider = FakeProvider(refuse_prompts_containing="please fix the flaky auth test")
    detect_user_friction(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df = ParquetCache(ports["layout"].user_friction_dir).read_all()
    assert df is not None
    refused = df.filter(pl.col("source") == "refused")
    assert refused.height == 1
    assert refused["uuid"][0] == "u-01"
    assert refused["label"][0] == "none"
    assert refused["confidence"][0] == 0.0


def test_friction_transport_failure_enqueues_uuid(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    fail_provider = FakeProvider(fail_prompts_containing="draft the launch memo")
    detect_user_friction(
        settings,
        dry_run=False,
        reader=reader,
        provider=fail_provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert retry_queue.pending_count(ports["state_db"], pipeline="user_friction") == 1


def test_friction_dry_run_counts_candidates(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    plan = detect_user_friction(
        settings, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    assert plan["pipeline"] == "friction"
    assert plan["candidates"] > 0
    # Budget-guard posture: assume every candidate reaches the LLM tier.
    assert plan["llm_calls"] == plan["candidates"]
    # Per-call input is measured (system prompt + template + message), not
    # the old bare-message ~200-token prior — must be within 2x of the real
    # per-call bill (~2330 tokens measured live 2026-08-23).
    per_call = plan["estimated_input_tokens"] // plan["llm_calls"]
    assert 2330 / 2 <= per_call <= 2330 * 2
    assert plan["friction_max_chars"] == 300
