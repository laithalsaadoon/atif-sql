# SPDX-License-Identifier: Apache-2.0

"""The typed tool-outcome columns, the ``images`` view and the ``subagents`` view.

Each test runs twice, once over the JSON artifacts and once over the columnar
parquets, because the two readers must answer the same rows (the equivalence
suite in ``test_columnar.py`` checks that wholesale; these pin the values).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from duck_fixtures import PASTED_SHA, READ_SHA, SESSION_IDS, register_via
from test_columnar import add_columnar

from atif_duck.infrastructure.registry import register


@pytest.fixture(params=["json", "columnar", "lake"])
def con(request: pytest.FixtureRequest, corpus_root: Path) -> duckdb.DuckDBPyConnection:
    if request.param in {"columnar", "lake"}:
        add_columnar(corpus_root, session_ids=tuple(SESSION_IDS))
    connection = duckdb.connect(":memory:")
    if request.param == "lake":
        register_via(connection, corpus_root, "lake")
        return connection
    sources = register(connection, corpus_root)
    assert bool(sources.columnar_session_ids) == (request.param == "columnar")
    return connection


def test_tool_results_carry_typed_outcomes(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT tool_use_id, is_error, exit_code, interrupted FROM tool_results "
        "WHERE is_error IS NOT NULL ORDER BY tool_use_id"
    ).fetchall()
    assert rows == [("toolu_02", False, None, None), ("toolu_21", True, 2, False)]
    unstated = con.execute(
        "SELECT count(*) FROM tool_results WHERE is_error IS NULL AND exit_code IS NULL"
    ).fetchone()
    assert unstated is not None
    assert unstated[0] > 0


def test_images_view_lists_tool_result_and_pasted_images(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT origin, tool_use_id, step_id, sha256, media_type, size_bytes, width, height, "
        "blob_path FROM images ORDER BY origin"
    ).fetchall()
    assert rows == [
        (
            "tool_result",
            "toolu_21",
            2,
            READ_SHA,
            "image/png",
            70,
            3,
            2,
            f"blobs/sha256/ab/{READ_SHA}.png",
        ),
        (
            "user_message",
            None,
            1,
            PASTED_SHA,
            "image/png",
            90,
            5,
            4,
            f"blobs/sha256/cd/{PASTED_SHA}.png",
        ),
    ]


def test_steps_name_their_subagent(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT step_id, agent_id FROM subagent_steps ORDER BY session_id, step_id"
    ).fetchall()
    assert rows == [(3, "agent-r1"), (4, "agent-r1")]
    main_chain = con.execute(
        "SELECT count(*) FROM steps WHERE NOT is_sidechain AND agent_id IS NOT NULL"
    ).fetchone()
    assert main_chain == (0,)


def test_subagents_view_links_the_spawning_call(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT session_id, agent_id, agent_type, description, parent_tool_call_id, "
        "parent_step_id, link_source, spawn_depth, first_ts::VARCHAR, last_ts::VARCHAR, "
        "step_count FROM subagents"
    ).fetchall()
    assert rows == [
        (
            SESSION_IDS[0],
            "agent-r1",
            "general-purpose",
            # Not in the declared entry: taken from the spawning Task call's input.
            "Research the API",
            "toolu_02",
            2,
            "meta",
            1,
            "2026-08-20 10:00:20",
            "2026-08-20 10:00:30",
            2,
        )
    ]
