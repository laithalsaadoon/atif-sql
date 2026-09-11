# SPDX-License-Identifier: Apache-2.0

"""The view surface over a CODEX corpus: agent identity and the cache split.

A separate fixture corpus rather than a third session in ``duck_fixtures``: the
two-session fixture's counts are asserted all over ``test_duck_views.py``, and a
Codex session added to it would restate every one of those numbers for no gain.
The trajectories here are handwritten in harbor's Codex shape (no dependency on
atif-converter — the independence contract), so what they pin is the SQL's
reading of that shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from atif_duck.domain.catalog import VIEW_SCHEMA
from atif_duck.infrastructure.registry import register

CODEX_SESSION_ID = "01a09182-2858-7f42-936b-7f027b341fdf"

#: Codex's cache-creation spelling in ``metrics.extra`` — the one the Claude
#: Code adapter does NOT use, which is why the view coalesces both.
_CODEX_CACHE_WRITE_KEY = "cache_write_input_tokens"


def _codex_trajectory() -> dict[str, Any]:
    """One Codex session: a user turn, a tool bundle, and a final answer."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": CODEX_SESSION_ID,
        "agent": {
            "name": "codex",
            "version": "0.154.0",
            "model_name": "bedrock-native/global.openai.gpt-6-astra",
            # Codex writes ONE cwd string and a git STRUCT; Claude Code writes
            # sets under different keys. The sessions view reads both shapes.
            "extra": {
                "originator": "codex-tui",
                "cwd": "/home/alice/proj",
                "git": {"branch": "feature/x", "commit_hash": "abc1234"},
            },
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-09-11T17:27:01.500Z",
                "source": "user",
                "message": "read marker.txt",
                "extra": {"source_uuids": ["msg_user_1"]},
            },
            {
                "step_id": 2,
                "timestamp": "2026-09-11T17:27:02.300Z",
                "source": "agent",
                "message": "",
                "model_name": "bedrock-native/global.openai.gpt-6-astra",
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "function_name": "exec_command",
                        "arguments": {"cmd": "cat marker.txt"},
                    }
                ],
                "observation": {"results": [{"source_call_id": "call_1", "content": "marker-ok"}]},
                "metrics": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 40,
                    "cached_tokens": 0,
                    "extra": {_CODEX_CACHE_WRITE_KEY: 900, "total_tokens": 1_040},
                },
                "llm_call_count": 1,
                "extra": {
                    "api_call_id": "api_call_1",
                    "codex_turn_id": "turn-1",
                    "source_uuids": ["fc_1", "response_item:L10", "msg_agent_empty"],
                },
            },
            {
                "step_id": 3,
                "timestamp": "2026-09-11T17:27:03.000Z",
                "source": "agent",
                "message": "marker-ok",
                "model_name": "bedrock-native/global.openai.gpt-6-astra",
                "metrics": {
                    "prompt_tokens": 1_100,
                    "completion_tokens": 8,
                    "cached_tokens": 1_000,
                    "extra": {_CODEX_CACHE_WRITE_KEY: 0},
                },
                "llm_call_count": 1,
                "extra": {"api_call_id": "api_call_2", "source_uuids": ["msg_agent_final"]},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 2_100,
            "total_completion_tokens": 48,
            "total_cached_tokens": 1_000,
            "total_cost_usd": 0.0256,
            "total_steps": 3,
            "extra": {"total_cache_write_input_tokens": 900},
        },
        "extra": {"cache_creation_total": 900, "codex_compaction_count": 1},
    }


def _codex_edges() -> list[dict[str, Any]]:
    """One line per raw rollout record, in the contract shape."""

    def edge(
        uuid: str,
        *,
        type_: str,
        ts: str,
        message_id: str | None = None,
        compact: bool = False,
        tool_use_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "uuid": uuid,
            # A rollout is a flat log: no parent pointer exists to record.
            "parent_uuid": None,
            "message_id": message_id,
            "type": type_,
            "ts": ts,
            "is_sidechain": False,
            "is_compact_summary": compact,
            "source_file": f"rollout-2026-09-11T17-27-01-{CODEX_SESSION_ID}.jsonl",
            "tool_use_ids": tool_use_ids or [],
        }

    return [
        edge("msg_dev_1", type_="developer", ts="2026-09-11T17:27:01.400Z", message_id="msg_dev_1"),
        edge("msg_user_1", type_="user", ts="2026-09-11T17:27:01.500Z", message_id="msg_user_1"),
        edge("rs_1", type_="reasoning", ts="2026-09-11T17:27:02.000Z"),
        edge(
            "msg_agent_empty",
            type_="assistant",
            ts="2026-09-11T17:27:02.100Z",
            message_id="msg_agent_empty",
        ),
        edge("fc_1", type_="function_call", ts="2026-09-11T17:27:02.200Z", tool_use_ids=["call_1"]),
        edge(
            "response_item:L10",
            type_="function_call_output",
            ts="2026-09-11T17:27:02.300Z",
            tool_use_ids=["call_1"],
        ),
        edge(
            "msg_agent_final",
            type_="assistant",
            ts="2026-09-11T17:27:03.000Z",
            message_id="msg_agent_final",
        ),
        edge("compacted:L17", type_="compacted", ts="2026-09-11T17:27:03.200Z", compact=True),
    ]


@pytest.fixture
def codex_corpus_root(tmp_path: Path) -> Path:
    """A one-session Codex corpus in the CONTRACT layout."""
    session_dir = tmp_path / "sessions" / CODEX_SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "trajectory.json").write_text(
        json.dumps(_codex_trajectory(), separators=(",", ":"))
    )
    (session_dir / "edges.jsonl").write_text(
        "\n".join(json.dumps(edge) for edge in _codex_edges()) + "\n"
    )
    (session_dir / "loss_report.json").write_text(
        json.dumps(
            {
                "record_counts": {"response_item": 8, "event_msg": 4},
                "records_total": 12,
                "records_converted": 6,
                "records_dropped": 6,
                "gaps_observed": [
                    "codex_item_ids_not_preserved",
                    "codex_reasoning_encrypted_dropped",
                ],
                "subagent_files_found": 0,
                "subagent_files_convertible": 0,
                "workflow_subagent_files_found": 0,
            },
            separators=(",", ":"),
        )
    )
    (session_dir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": CODEX_SESSION_ID,
                "source_mtime_ns": 1_789_000_000_000_000_000,
                "source_files": [f"rollout-2026-09-11T17-27-01-{CODEX_SESSION_ID}.jsonl"],
                "harbor_version": "0.22.0",
                "converter_version": "0.1.0",
                "materialized_at": "2026-09-11T18:00:00Z",
                "agent": "codex",
            },
            separators=(",", ":"),
        )
    )
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
