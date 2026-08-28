# SPDX-License-Identifier: Apache-2.0

"""FakeProvider-driven pipeline tests: parquet rows, checkpoints, retry, refusals.

NO live LLM calls: every provider is the deterministic
:class:`analytics_fixtures.FakeProvider`.
"""

from __future__ import annotations

from typing import Any, override

import polars as pl
import pytest
from analytics_fixtures import SESSION_IDS, FakeProvider

from atif_analytics.application.use_cases._shared import RunBudget
from atif_analytics.application.use_cases.classify import classify_sessions
from atif_analytics.application.use_cases.conflicts import detect_conflicts
from atif_analytics.application.use_cases.friction import detect_user_friction
from atif_analytics.application.use_cases.trajectory import trajectory_messages
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
    assert df["autonomy_tier"].to_list() == ["assisted", "assisted"]
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


def test_trajectory_backing_off_session_is_not_billed(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    """A freshly-failed session (backoff pending) is skipped, not re-billed."""
    retry_queue.enqueue(
        ports["state_db"], pipeline="trajectory", unit_id=SESSION_IDS[0], error="boom"
    )
    provider = FakeProvider()
    trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df is not None
    assert set(df["session_id"].to_list()) == {SESSION_IDS[1]}


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
    assert sentinel["autonomy_tier"] == "unknown"
    assert sentinel["work_category"] == "unknown"
    assert sentinel["success"] == "unknown"
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
    expected = (expected_in * SPEC.pricing_in + 2 * 300 * SPEC.pricing_out) / 1_000_000
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


# ---------------------------------------------------------------------------
# trajectory
# ---------------------------------------------------------------------------


def test_trajectory_writes_one_row_per_window(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider()
    n = trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    windows_expected = len(reader.text_windows(SESSION_IDS[0])) + len(
        reader.text_windows(SESSION_IDS[1])
    )
    assert n == windows_expected
    df = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df is not None
    assert df.height == windows_expected
    # Session-first rows carry null prev + null delta.
    firsts = df.filter(pl.col("prev_uuid").is_null())
    assert firsts.height == 2
    assert firsts["delta"].null_count() == 2
    # Rerun is a no-op (checkpoint) and does not duplicate rows.
    n2 = trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert n2 == 0
    df2 = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df2 is not None
    assert df2.height == windows_expected


def test_trajectory_refused_chunk_becomes_placeholders(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    # Refuse session one's chunks (its window XML contains the flaky text).
    provider = FakeProvider(refuse_prompts_containing="flaky auth test")
    trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df is not None
    s1 = df.filter(pl.col("session_id") == SESSION_IDS[0])
    # Every session-one row is a neutral placeholder at confidence 0.
    assert s1.height == len(reader.text_windows(SESSION_IDS[0]))
    assert (s1["confidence"] == 0.0).all()
    assert (s1["transition_kind"] == "none").all()
    # Refusal is terminal: nothing queued for retry.
    assert retry_queue.pending_count(ports["state_db"], pipeline="trajectory") == 0


def test_trajectory_failure_enqueues_session(
    settings: AnalyticsSettings, reader: CorpusReader, ports: dict[str, Any]
) -> None:
    provider = FakeProvider(fail_prompts_containing="flaky auth test")
    trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    assert retry_queue.pending_count(ports["state_db"], pipeline="trajectory") == 1
    # Session two (no marker) still wrote its rows.
    df = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df is not None
    assert set(df["session_id"].to_list()) == {SESSION_IDS[1]}


def test_trajectory_flushes_paid_chunks_before_abandoning_session(
    settings: AnalyticsSettings,
    reader: CorpusReader,
    ports: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful chunks' rows land even when a LATER chunk fails.

    Paid work is never discarded: the failed session flushes its completed
    chunks, is NOT checkpointed, and the retry re-run replaces its rows
    without duplication (replace_sessions semantics preserved).
    """
    # Force multi-chunk sessions (fixture sessions are < 16 windows).
    from atif_analytics.domain.trajectory import WindowRow, chunk_windows

    def _two_per_chunk(windows: list[WindowRow]) -> list[list[WindowRow]]:
        return chunk_windows(windows, chunk_size=2)

    monkeypatch.setattr(
        "atif_analytics.application.use_cases.trajectory.chunk_windows",
        _two_per_chunk,
    )

    class _SecondChunkFails(FakeProvider):
        """Fail session one's SECOND chunk; session one's windows carry the
        flaky-test text only in the early turns, so key off call order."""

        def __init__(self) -> None:
            super().__init__()
            self._s1_calls = 0

        @override
        async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
            if "flaky auth test" in prompt or 'uuid="u-0' in prompt:
                self._s1_calls += 1
                if self._s1_calls == 2:
                    self.calls.append((schema.__name__, prompt))
                    from atif_models.domain.ports import ProviderUnavailable

                    msg = "scripted second-chunk failure"
                    raise ProviderUnavailable(msg)
            return await super().classify_structured(system=system, prompt=prompt, schema=schema)

    provider = _SecondChunkFails()
    trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=provider,
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df is not None
    s1 = df.filter(pl.col("session_id") == SESSION_IDS[0])
    # First chunk (2 windows) flushed; the session's remaining windows are not.
    assert s1.height == 2
    # The failed session is queued and NOT checkpointed.
    assert retry_queue.pending_count(ports["state_db"], pipeline="trajectory") == 1
    assert SESSION_IDS[0] not in load_as_map(ports["state_db"], "trajectory")

    # Retry re-run (drain due now): the session reprocesses whole and
    # replace_sessions drops the partial flush — no duplicate window pairs.
    import sqlite3

    con = sqlite3.connect(ports["state_db"])
    con.execute("UPDATE retry_queue SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    trajectory_messages(
        settings,
        dry_run=False,
        reader=reader,
        provider=FakeProvider(),
        spec=SPEC,
        checkpoint=ports["checkpoint"],
        retry=ports["retry"],
    )
    df2 = ParquetCache(ports["layout"].trajectory_dir).read_all()
    assert df2 is not None
    s1_after = df2.filter(pl.col("session_id") == SESSION_IDS[0])
    expected = len(reader.text_windows(SESSION_IDS[0]))
    assert s1_after.height == expected
    assert s1_after["curr_uuid"].n_unique() == expected


def test_trajectory_dry_run_counts_chunks(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    plan = trajectory_messages(
        settings, dry_run=True, reader=reader, provider=FakeProvider(), spec=SPEC
    )
    assert isinstance(plan, dict)
    turns = len(reader.text_windows(SESSION_IDS[0])) + len(reader.text_windows(SESSION_IDS[1]))
    assert plan["turns"] == turns
    # Chunks are per-session (each session's windows are chunked separately).
    expected_calls = sum((len(reader.text_windows(sid)) + 15) // 16 for sid in SESSION_IDS)
    assert plan["llm_calls"] == expected_calls
    assert plan["candidates"] == 2
    assert plan["capped_candidates"] == 2
    # Input tokens are measured from the windows' clipped text lengths plus
    # the per-call envelope (system prompt + schema reminder) — the estimate
    # must be at least the raw window text and no more than raw + envelope*2.
    from atif_analytics.application.prompts import TRAJECTORY_SYSTEM_PROMPT
    from atif_analytics.application.use_cases.trajectory import _SCHEMA_REMINDER

    raw_chars = sum(
        min(len(w[5] or ""), 2000) + min(len(w[6]), 2000)
        for sid in SESSION_IDS
        for w in reader.text_windows(sid)
    )
    envelope = len(TRAJECTORY_SYSTEM_PROMPT) + len(_SCHEMA_REMINDER)
    lo = raw_chars // 4
    hi = (raw_chars * 2 + expected_calls * envelope * 2) // 4
    assert lo <= plan["estimated_input_tokens"] <= hi
    assert plan["max_sessions_per_run"] == 50
    assert plan["max_cost_usd_per_run"] == 25.0


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
    # The system marker never produced a row.
    assert "u-marker" not in by_uuid
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
