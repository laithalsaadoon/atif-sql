# SPDX-License-Identifier: Apache-2.0

"""v2 analytics views + macros: registration gating, aliases, dedup, drift.

The analytics parquets are written with DuckDB itself (``COPY TO``) so
atif-duck's test suite keeps its duckdb-only dependency surface.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import duckdb
import pytest
from duck_fixtures import SESSION_IDS

from atif_duck.domain.catalog import (
    ANALYTICS_MACRO_SIGNATURES,
    ANALYTICS_VIEW_NAMES,
)
from atif_duck.infrastructure import analytics as analytics_mod
from atif_duck.infrastructure.analytics import (
    register_analytics,
    register_analytics_macros,
)
from atif_duck.infrastructure.registry import register


def _write_parquet(con: duckdb.DuckDBPyConnection, sql: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT PARQUET);")


def _populate_analytics(corpus_root: Path) -> None:
    """Hand-written analytics artifacts matching the pipeline schemas."""
    a = corpus_root / "analytics"
    scratch = duckdb.connect()
    s0, s1 = SESSION_IDS
    # Sharded LLM caches: one part file each.
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', 'assisted', 'sde', 'success', 'Fix the flaky test.',
             CAST(0.9 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00'),
            ('{s1}', 'autonomous', 'admin', 'unknown', 'Tidy the docs.',
             CAST(0.4 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00')
        ) t(session_id, autonomy_tier, work_category, success, goal, confidence, classified_at)
        """,
        a / "session_classifications" / "part-1.parquet",
    )
    # Trajectory with a DUPLICATE window pair — the QUALIFY dedup must keep
    # the later classified_at.
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', NULL, 'u-1', NULL, 'neutral', NULL, false, 'none',
             CAST(0.8 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00'),
            ('{s0}', 'u-1', 'u-2', 'neutral', 'positive', CAST(1.0 AS DOUBLE), false,
             'resolution', CAST(0.5 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00'),
            ('{s0}', 'u-1', 'u-2', 'neutral', 'negative', CAST(-1.0 AS DOUBLE), false,
             'frustration_spike', CAST(0.9 AS FLOAT), TIMESTAMPTZ '2026-08-23 01:00:00+00')
        ) t(session_id, prev_uuid, curr_uuid, prev_sentiment, curr_sentiment, delta,
            is_transition, transition_kind, confidence, classified_at)
        """,
        a / "message_trajectory" / "part-1.parquet",
    )
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', 'u-1', 'a-3', 'correction', 'medium',
             'Keep the schema.', 'Add an index instead.',
             CAST(0.85 AS DOUBLE), TIMESTAMPTZ '2026-08-22 01:00:00+00')
        ) t(session_id, turn_a_uuid, turn_b_uuid, conflict_kind, severity,
            agent_position, user_position, confidence, detected_at)
        """,
        a / "session_conflicts" / "part-1.parquet",
    )
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('u-1', '{s0}', TIMESTAMPTZ '2026-08-20 10:00:00+00', 'why?',
             'confusion', 'questioning a completed action', 'llm',
             CAST(0.8 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00'),
            ('u-2', '{s0}', TIMESTAMPTZ '2026-08-20 10:03:00+00', 'ok',
             'none', 'ordinary', 'llm',
             CAST(0.9 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00')
        ) t(uuid, session_id, ts, text_snippet, label, rationale, source,
            confidence, classified_at)
        """,
        a / "user_friction" / "part-1.parquet",
    )
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', 'u-1', 'correction', 'minor',
             'no — I meant the key inside the file',
             'The agent renamed the file instead of the key.',
             CAST(0.9 AS DOUBLE), TIMESTAMPTZ '2026-08-22 01:00:00+00'),
            ('{s0}', 'u-2', 'unresolved_outcome', 'major',
             'forget it, I will do it myself',
             'The session ended with the rename still wrong.',
             CAST(0.8 AS DOUBLE), TIMESTAMPTZ '2026-08-22 01:00:00+00')
        ) t(session_id, turn_uuid, signal, severity, evidence,
            agent_error_summary, confidence, detected_at)
        """,
        a / "perceived_errors" / "part-1.parquet",
    )
    # Structural single-file parquets.
    _write_parquet(
        scratch,
        """
        SELECT * FROM (VALUES
            ('u-1', CAST(0 AS INT), CAST(NULL AS FLOAT), CAST(NULL AS FLOAT), false),
            ('a-1', CAST(0 AS INT), CAST(NULL AS FLOAT), CAST(NULL AS FLOAT), false),
            ('a-3', CAST(-1 AS INT), CAST(NULL AS FLOAT), CAST(NULL AS FLOAT), true)
        ) t(uuid, cluster_id, x, y, is_noise)
        """,
        a / "clusters.parquet",
    )
    _write_parquet(
        scratch,
        """
        SELECT * FROM (VALUES
            (CAST(0 AS INT), 'auth', CAST(0.9 AS FLOAT), CAST(1 AS INT)),
            (CAST(0 AS INT), 'flaky test', CAST(0.7 AS FLOAT), CAST(2 AS INT))
        ) t(cluster_id, term, weight, rank)
        """,
        a / "cluster_terms.parquet",
    )
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', CAST(0 AS INT), CAST(2 AS INT), true,
             CAST(0.8 AS FLOAT), CAST(0.3 AS FLOAT)),
            ('{s1}', CAST(0 AS INT), CAST(2 AS INT), false,
             CAST(0.8 AS FLOAT), CAST(0.3 AS FLOAT))
        ) t(session_id, community_id, size, is_medoid, coherence, gamma_used)
        """,
        a / "session_communities.parquet",
    )
    _write_parquet(
        scratch,
        """
        SELECT * FROM (VALUES
            (CAST(0.1 AS DOUBLE), CAST(3 AS INT), CAST(0.5 AS DOUBLE), CAST(100 AS INT))
        ) t(gamma, n_communities, quality, plateau_length)
        """,
        a / "community_profile.parquet",
    )
    scratch.close()


@pytest.fixture
def analytics_con(corpus_root: Path) -> duckdb.DuckDBPyConnection:
    """Full registration (base + analytics) over a populated fixture corpus."""
    _populate_analytics(corpus_root)
    con = duckdb.connect()
    register(con, corpus_root)
    return con


def test_all_analytics_views_register(analytics_con: duckdb.DuckDBPyConnection) -> None:
    for view in ANALYTICS_VIEW_NAMES:
        analytics_con.execute(f"SELECT * FROM {view} LIMIT 1")


def test_fresh_corpus_registers_nothing(corpus_root: Path) -> None:
    """Without analytics parquets: no views, no macros, register() still ok."""
    con = duckdb.connect()
    register(con, corpus_root)
    registered = register_analytics(con, corpus_root)
    assert registered == set()
    with pytest.raises(duckdb.CatalogException):
        con.execute("SELECT * FROM session_classifications")
    con.close()


def test_classification_aliases(analytics_con: duckdb.DuckDBPyConnection) -> None:
    row = analytics_con.execute(
        "SELECT autonomy, success_outcome, category FROM session_classifications "
        f"WHERE session_id = '{SESSION_IDS[0]}'"
    ).fetchone()
    assert row == ("assisted", "success", "sde")


def test_trajectory_qualify_dedup(analytics_con: duckdb.DuckDBPyConnection) -> None:
    rows = analytics_con.execute(
        "SELECT curr_sentiment, confidence FROM message_trajectory "
        "WHERE prev_uuid = 'u-1' AND curr_uuid = 'u-2'"
    ).fetchall()
    # Two shards carried the pair; the later classified_at wins.
    assert rows == [("negative", pytest.approx(0.9))]


def test_trajectory_alias_columns(analytics_con: duckdb.DuckDBPyConnection) -> None:
    cols = {d[0] for d in analytics_con.execute("DESCRIBE message_trajectory").fetchall()}
    assert {"sentiment", "transition"} <= cols


def test_session_goals_projection(analytics_con: duckdb.DuckDBPyConnection) -> None:
    cols = [d[0] for d in analytics_con.execute("DESCRIBE session_goals").fetchall()]
    assert cols == ["session_id", "goal", "confidence", "classified_at"]


def test_conflicts_summary_counts(analytics_con: duckdb.DuckDBPyConnection) -> None:
    rows = analytics_con.execute(
        "SELECT session_id, conflict_count FROM conflicts_summary"
    ).fetchall()
    assert rows == [(SESSION_IDS[0], 1)]


def test_analytics_macros_bind_and_run(analytics_con: duckdb.DuckDBPyConnection) -> None:
    # work_mix over the classification rows.
    mix = dict(analytics_con.execute("SELECT * FROM work_mix(365)").fetchall())
    assert mix == {"sde": 1, "admin": 1}
    # success_rate_by_work: known-denominator semantics — admin is all
    # unknown so its rates are NULL and unknown_fraction is 1.0.
    rows = {
        r[0]: r for r in analytics_con.execute("SELECT * FROM success_rate_by_work(365)").fetchall()
    }
    sde = rows["sde"]
    assert sde[1] == 1  # sessions
    assert sde[2] == 1  # known_sessions
    assert sde[3] == 0.0  # unknown_fraction
    assert sde[4] == 1.0  # success_rate
    admin = rows["admin"]
    assert admin[2] == 0
    assert admin[3] == 1.0
    assert admin[4] is None
    # autonomy_trend joins sessions.started_at (NOT classified_at).
    trend = analytics_con.execute("SELECT * FROM autonomy_trend(3650)").fetchall()
    assert {t[1] for t in trend} == {"assisted", "autonomous"}
    # sentiment_arc joins messages on curr_uuid.
    arc = analytics_con.execute(f"SELECT * FROM sentiment_arc('{SESSION_IDS[0]}')").fetchall()
    assert len(arc) == 2
    # friction_counts excludes 'none'.
    counts = analytics_con.execute("SELECT label, n FROM friction_counts(NULL)").fetchall()
    assert counts == [("confusion", 1)]
    # friction_rate computes a per-session rate against user steps.
    rate = analytics_con.execute(
        "SELECT session_id, n_friction FROM friction_rate(NULL)"
    ).fetchall()
    assert rate == [(SESSION_IDS[0], 1)]
    # friction_examples filters by label.
    ex = analytics_con.execute("SELECT * FROM friction_examples('confusion', 5)").fetchall()
    assert len(ex) == 1
    # conflicts_over_time recovers conversation time via turn_b_uuid.
    cot = analytics_con.execute("SELECT * FROM conflicts_over_time(NULL)").fetchall()
    assert len(cot) == 1
    assert cot[0][2] == SESSION_IDS[0]  # root_session_id == session_id
    # cluster_top_terms + community_top_topics.
    terms = analytics_con.execute("SELECT term FROM cluster_top_terms(0, 5)").fetchall()
    assert [t[0] for t in terms] == ["auth", "flaky test"]
    topics = analytics_con.execute("SELECT * FROM community_top_topics(0, 5)").fetchall()
    assert topics[0][0] == 0  # cluster 0 leads
    assert "auth" in topics[0][2]


def test_perceived_summary_aggregates(analytics_con: duckdb.DuckDBPyConnection) -> None:
    rows = analytics_con.execute(
        "SELECT session_id, n_errors, max_severity, signals FROM perceived_summary"
    ).fetchall()
    assert rows == [(SESSION_IDS[0], 2, "major", ["correction", "unresolved_outcome"])]


def test_perceived_macros_bind_and_run(analytics_con: duckdb.DuckDBPyConnection) -> None:
    # perceived_counts recovers conversation time via the anchor user turn.
    counts = {
        r[0]: r
        for r in analytics_con.execute(
            "SELECT signal, n, sessions, n_major, n_moderate, n_minor FROM perceived_counts(NULL)"
        ).fetchall()
    }
    assert counts["correction"][1:] == (1, 1, 0, 0, 1)
    assert counts["unresolved_outcome"][1:] == (1, 1, 1, 0, 0)
    # perceived_rate divides by user-role main-chain text steps.
    rate = analytics_con.execute(
        "SELECT session_id, n_errors, n_user_msgs, rate FROM perceived_rate(NULL)"
    ).fetchall()
    assert len(rate) == 1
    assert rate[0][0] == SESSION_IDS[0]
    assert rate[0][1] == 2
    assert rate[0][2] > 0
    assert rate[0][3] == pytest.approx(2 / rate[0][2])
    # perceived_examples filters by signal, confidence-ranked.
    ex = analytics_con.execute(
        "SELECT evidence FROM perceived_examples('correction', 5)"
    ).fetchall()
    assert ex == [("no — I meant the key inside the file",)]
    assert (
        analytics_con.execute("SELECT * FROM perceived_examples('rejected_action', 5)").fetchall()
        == []
    )


def test_macros_skip_when_views_missing(corpus_root: Path) -> None:
    con = duckdb.connect()
    register(con, corpus_root)  # fresh corpus: no analytics parquets
    register_analytics_macros(con, set())
    with pytest.raises(duckdb.CatalogException):
        con.execute("SELECT * FROM work_mix(30)")
    con.close()


def test_analytics_macro_signatures_match_ddl() -> None:
    """Drift catcher: DDL-parsed signatures equal the static catalog."""
    source = inspect.getsource(analytics_mod.register_analytics_macros)
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+MACRO\s+(\w+)\s*\(([^)]*)\)",
        re.IGNORECASE,
    )
    parsed: dict[str, tuple[str, ...]] = {}
    for match in pattern.finditer(source):
        name = match.group(1)
        raw_args = match.group(2).strip()
        args: tuple[str, ...] = (
            tuple(arg.strip() for arg in raw_args.split(",")) if raw_args else ()
        )
        parsed[name] = args
    assert parsed == ANALYTICS_MACRO_SIGNATURES


def test_analytics_view_schema_stability(analytics_con: duckdb.DuckDBPyConnection) -> None:
    """Join columns for all five LLM views, plus the perceived rollup."""
    cols = {d[0] for d in analytics_con.execute("DESCRIBE session_classifications").fetchall()}
    assert {
        "session_id",
        "autonomy_tier",
        "work_category",
        "success",
        "goal",
        "confidence",
        "classified_at",
        # The additive aliases, asserted for the same reason as
        # message_trajectory's below: a subset assertion passes when an alias
        # has replaced the column it was supposed to sit beside.
        "autonomy",
        "success_outcome",
        "category",
    } == cols
    cols = {d[0] for d in analytics_con.execute("DESCRIBE session_conflicts").fetchall()}
    assert {
        "session_id",
        "turn_a_uuid",
        "turn_b_uuid",
        "conflict_kind",
        "severity",
        "agent_position",
        "user_position",
        "confidence",
        "detected_at",
    } == cols
    cols = {d[0] for d in analytics_con.execute("DESCRIBE message_trajectory").fetchall()}
    assert {
        "session_id",
        "prev_uuid",
        "curr_uuid",
        "prev_sentiment",
        "curr_sentiment",
        "delta",
        "is_transition",
        "transition_kind",
        "confidence",
        "classified_at",
        # `_VIEW_PROJECTIONS` promises the alias projections are ADDITIVE, so
        # both the parquet's own name and its alias have to be present. An
        # equality assertion is what catches an alias that REPLACED a column.
        "sentiment",
        "transition",
    } == cols
    cols = {d[0] for d in analytics_con.execute("DESCRIBE user_friction").fetchall()}
    assert {
        "uuid",
        "session_id",
        "ts",
        "text_snippet",
        "label",
        "rationale",
        "source",
        "confidence",
        "classified_at",
    } == cols
    cols = {d[0] for d in analytics_con.execute("DESCRIBE perceived_errors").fetchall()}
    assert {
        "session_id",
        "turn_uuid",
        "signal",
        "severity",
        "evidence",
        "agent_error_summary",
        "confidence",
        "detected_at",
    } == cols
    cols = [d[0] for d in analytics_con.execute("DESCRIBE perceived_summary").fetchall()]
    assert cols == ["session_id", "n_errors", "max_severity", "signals"]
