# SPDX-License-Identifier: Apache-2.0

"""v2 analytics views + macros: registration gating, compat columns, drift.

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
    ANALYTICS_VIEW_SCHEMA,
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
    # Sharded LLM caches. session_classifications spans BOTH shard schemas: a
    # pre-2026-09-27 shard that still carries the LLM autonomy_tier/success
    # labels, and a current one without them. The view must read both.
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s0}', 'assisted', 'sde', 'success', 'Fix the flaky test.',
             CAST(0.9 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00')
        ) t(session_id, autonomy_tier, work_category, success, goal, confidence, classified_at)
        """,
        a / "session_classifications" / "part-1.parquet",
    )
    _write_parquet(
        scratch,
        f"""
        SELECT * FROM (VALUES
            ('{s1}', 'admin', 'Tidy the docs.',
             CAST(0.4 AS FLOAT), TIMESTAMPTZ '2026-09-27 01:00:00+00')
        ) t(session_id, work_category, goal, confidence, classified_at)
        """,
        a / "session_classifications" / "part-2.parquet",
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


def test_classifications_read_both_shard_schemas(
    analytics_con: duckdb.DuckDBPyConnection,
) -> None:
    """Old and new shards union by NAME, and the dropped labels read as NULL.

    The old shard still holds ``autonomy_tier='assisted'`` and
    ``success='success'``; the view must not surface either, because those
    LLM labels were dropped as unreliable. A positional union would instead
    put the old shard's ``autonomy_tier`` text into ``work_category``.
    """
    rows = analytics_con.execute(
        "SELECT session_id, work_category, category, goal, autonomy_tier, success "
        "FROM session_classifications ORDER BY session_id"
    ).fetchall()
    assert rows == [
        (SESSION_IDS[0], "sde", "sde", "Fix the flaky test.", None, None),
        (SESSION_IDS[1], "admin", "admin", "Tidy the docs.", None, None),
    ]


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
    mix = dict(analytics_con.execute("SELECT * FROM work_mix(3650)").fetchall())
    assert mix == {"sde": 1, "admin": 1}
    # friction_counts excludes 'none'.
    counts = analytics_con.execute("SELECT label, n FROM friction_counts(NULL)").fetchall()
    assert counts == [("confusion", 1)]
    # friction_rate divides by HUMAN turns. Session one's user steps are a
    # slash-command wrapper (harness), a sidechain prompt and a compaction
    # summary (harness), so it has none: the rate is NULL, not 1/2 as the old
    # every-user-step denominator made it.
    rate = analytics_con.execute(
        "SELECT session_id, n_friction, n_user_msgs, rate FROM friction_rate(NULL)"
    ).fetchall()
    assert rate == [(SESSION_IDS[0], 1, 0, None)]
    # friction_examples filters by label.
    ex = analytics_con.execute("SELECT * FROM friction_examples('confusion', 5)").fetchall()
    assert len(ex) == 1
    # conflicts_over_time recovers conversation time via turn_b_uuid.
    cot = analytics_con.execute("SELECT * FROM conflicts_over_time(NULL)").fetchall()
    assert len(cot) == 1
    assert cot[0][2] == SESSION_IDS[0]  # root_session_id == session_id


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
    # perceived_rate divides by human turns (none in session one, see above).
    rate = analytics_con.execute(
        "SELECT session_id, n_errors, n_user_msgs, rate FROM perceived_rate(NULL)"
    ).fetchall()
    assert rate == [(SESSION_IDS[0], 2, 0, None)]
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


def test_analytics_view_schema_matches_describe(
    analytics_con: duckdb.DuckDBPyConnection,
) -> None:
    """``ANALYTICS_VIEW_SCHEMA`` equals DESCRIBE, column for column and in order.

    ``atif-sql schema`` prints this catalog for the analytics views, so a
    column added, dropped or retyped in the DDL must fail here rather than
    reach an agent as a wrong schema.
    """
    assert set(ANALYTICS_VIEW_SCHEMA) == set(ANALYTICS_VIEW_NAMES)
    for view, expected in ANALYTICS_VIEW_SCHEMA.items():
        got = tuple(
            (str(r[0]), str(r[1])) for r in analytics_con.execute(f"DESCRIBE {view}").fetchall()
        )
        assert got == expected, f"ANALYTICS_VIEW_SCHEMA[{view!r}] diverges: {got}"


def test_removed_analytics_surfaces_stay_unbound(corpus_root: Path) -> None:
    """A corpus still holding the cut pipelines' parquets binds none of them.

    The live corpora keep ``message_trajectory/`` and the structural files
    until an operator deletes them, so registration must ignore them rather
    than resurrect a view the catalog no longer lists.
    """
    _populate_analytics(corpus_root)
    a = corpus_root / "analytics"
    scratch = duckdb.connect()
    _write_parquet(
        scratch,
        "SELECT 's' AS session_id, 'u-1' AS curr_uuid",
        a / "message_trajectory" / "part-1.parquet",
    )
    _write_parquet(scratch, "SELECT 'u-1' AS uuid, 0 AS cluster_id", a / "clusters.parquet")
    _write_parquet(scratch, "SELECT 0 AS cluster_id, 't' AS term", a / "cluster_terms.parquet")
    _write_parquet(
        scratch, "SELECT 's' AS session_id, 0 AS community_id", a / "session_communities.parquet"
    )
    scratch.close()
    con = duckdb.connect()
    register(con, corpus_root)
    names = {str(r[0]) for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()} | {
        str(r[0]) for r in con.execute("SELECT function_name FROM duckdb_functions()").fetchall()
    }
    for removed in (
        "message_trajectory",
        "message_clusters",
        "cluster_terms",
        "session_communities",
        "community_profile",
        "autonomy_trend",
        "success_rate_by_work",
        "sentiment_arc",
        "cluster_top_terms",
        "community_top_topics",
    ):
        assert removed not in names, f"{removed} is still registered"
    con.close()
