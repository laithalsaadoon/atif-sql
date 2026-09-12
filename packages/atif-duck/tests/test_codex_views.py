# SPDX-License-Identifier: Apache-2.0

"""The view surface over a CODEX corpus: agent identity and the cache split.

A separate fixture corpus rather than a third session in ``duck_fixtures``: the
two-session fixture's counts are asserted all over ``test_duck_views.py``, and a
Codex session added to it would restate every one of those numbers for no gain.
The trajectory (``duck_fixtures.codex_trajectory``) is handwritten in harbor's
Codex shape (no dependency on atif-converter — the independence contract), so
what these tests pin is the SQL's reading of that shape.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from duck_fixtures import CODEX_SESSION_ID, write_codex_session

from atif_duck.domain.catalog import VIEW_SCHEMA
from atif_duck.infrastructure.registry import register


@pytest.fixture
def codex_corpus_root(tmp_path: Path) -> Path:
    """A one-session Codex corpus in the CONTRACT layout (see duck_fixtures)."""
    write_codex_session(tmp_path)
    return tmp_path


@pytest.fixture
def codex_con(codex_corpus_root: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    register(connection, codex_corpus_root)
    return connection


def test_sessions_reports_the_producing_agent(codex_con: duckdb.DuckDBPyConnection) -> None:
    """The one column every agent-aware query reads, straight from the trajectory."""
    row = codex_con.execute("SELECT agent, agent_version, model_name FROM sessions").fetchone()
    assert row is not None
    assert row[0] == "codex"
    assert row[1] == "0.154.0"
    assert row[2] == "bedrock-native/global.openai.gpt-6-astra"


def test_sessions_reads_codex_cwd_and_git_branch(codex_con: duckdb.DuckDBPyConnection) -> None:
    """Codex writes a cwd STRING and a git STRUCT where Claude Code writes sets."""
    row = codex_con.execute("SELECT cwd, git_branch FROM sessions").fetchone()
    assert row == ("/home/alice/proj", "feature/x")


def test_sessions_schema_still_matches_the_catalog(codex_con: duckdb.DuckDBPyConnection) -> None:
    """The drift catcher, re-run against a Codex corpus rather than a Claude Code one."""
    rows = codex_con.execute("DESCRIBE sessions").fetchall()
    assert tuple((str(r[0]), str(r[1])) for r in rows) == VIEW_SCHEMA["sessions"]


def test_cache_creation_reads_the_codex_spelling(codex_con: duckdb.DuckDBPyConnection) -> None:
    """``cache_write_input_tokens`` must reach the same column as Claude Code's key."""
    total = codex_con.execute("SELECT sum(cache_creation) FROM steps").fetchone()
    assert total is not None
    assert total[0] == 900


def test_steps_and_tool_calls_read_a_codex_trajectory(
    codex_con: duckdb.DuckDBPyConnection,
) -> None:
    counts = codex_con.execute(
        "SELECT count(*) FILTER (WHERE source = 'agent'), count(*) FROM steps"
    ).fetchone()
    assert counts == (2, 3)
    tool = codex_con.execute("SELECT tool_name, tool_use_id FROM tool_calls").fetchone()
    assert tool == ("exec_command", "call_1")


def test_tool_results_join_tool_calls_on_the_call_id(
    codex_con: duckdb.DuckDBPyConnection,
) -> None:
    row = codex_con.execute(
        """
        SELECT tc.tool_name, tr.content::VARCHAR
        FROM tool_calls tc JOIN tool_results tr USING (tool_use_id)
        """
    ).fetchone()
    assert row is not None
    assert row[0] == "exec_command"
    assert "marker-ok" in row[1]


def test_messages_keeps_the_developer_role_the_steps_flatten(
    codex_con: duckdb.DuckDBPyConnection,
) -> None:
    """The distinction harbor loses in the steps survives in the corpus."""
    types = {row[0] for row in codex_con.execute("SELECT DISTINCT type FROM messages").fetchall()}
    assert {"developer", "user", "assistant", "reasoning", "compacted"} <= types


def test_messages_flags_the_compaction_record(codex_con: duckdb.DuckDBPyConnection) -> None:
    row = codex_con.execute("SELECT uuid FROM messages WHERE is_compact_summary").fetchone()
    assert row == ("compacted:L17",)


def test_source_uuids_join_steps_back_to_messages(codex_con: duckdb.DuckDBPyConnection) -> None:
    """What the enrichment bought: step-to-raw-record identity, in SQL."""
    joined = codex_con.execute(
        """
        SELECT count(*)
        FROM steps s,
             UNNEST(json_extract(s.source_uuids, '$[*]')) AS u(source_uuid)
        JOIN messages m ON m.uuid = json_extract_string(source_uuid, '$')
        """
    ).fetchone()
    assert joined is not None
    assert joined[0] == 5


def test_loss_reports_carries_the_codex_gaps(codex_con: duckdb.DuckDBPyConnection) -> None:
    row = codex_con.execute(
        "SELECT records_total, gaps_observed::VARCHAR FROM loss_reports"
    ).fetchone()
    assert row is not None
    assert row[0] == 12
    assert "codex_" in row[1]


def test_model_used_and_cost_estimate_still_bind(codex_con: duckdb.DuckDBPyConnection) -> None:
    """Both macros bind over a Codex corpus, and the unpriced model is REPORTED.

    ``DEFAULT_PRICING`` carries Claude rates only, so a Codex session's steps
    are unpriced — and ``cost_estimate``'s trust signal is what says so, rather
    than a zero that reads like "free". The session's real spend is in
    ``sessions.total_cost_usd``, which harbor computes per API call.

    ``model_used`` is a SCALAR macro and ``cost_estimate`` a table macro, so
    they are called in the two different shapes the catalog declares.
    """
    from atif_duck.infrastructure.registry import register_macros

    register_macros(codex_con)
    model = codex_con.execute("SELECT model_used(?)", [CODEX_SESSION_ID]).fetchone()
    assert model is not None
    assert model[0] == "bedrock-native/global.openai.gpt-6-astra"
    estimate = codex_con.execute(
        "SELECT est_cost_usd, priced_steps, unpriced_steps FROM cost_estimate(?)",
        [CODEX_SESSION_ID],
    ).fetchone()
    assert estimate is not None
    assert estimate[1] == 0, "no Codex model is in DEFAULT_PRICING"
    assert estimate[2] == 2, "both agent steps are reported unpriced rather than free"

    total_cost = codex_con.execute("SELECT total_cost_usd FROM sessions").fetchone()
    assert total_cost is not None
    assert total_cost[0] == pytest.approx(0.0256)
