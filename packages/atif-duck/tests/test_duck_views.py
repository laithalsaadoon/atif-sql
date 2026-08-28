# SPDX-License-Identifier: Apache-2.0

"""Tests for the atif-duck registry — drift catchers + functional coverage.

Drift tests — the static catalog can lie, so these compare it to the DDL:

* ``test_view_schema_matches_describe`` — DESCRIBE per view over the fixture
  corpus must equal the static ``VIEW_SCHEMA`` dict column-for-column.
* ``test_macro_signatures_match_ddl`` — regex-parses ``CREATE OR REPLACE
  MACRO <name>(<args>)`` from the registry source and asserts equality with
  ``MACRO_SIGNATURES`` (and that its keys equal ``MACRO_NAMES``).

Functional tests run over the synthetic contract-shaped corpus built in
``conftest.py``.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import duckdb
import pytest
from duck_fixtures import EDGE_COUNTS, SESSION_IDS
from loguru import logger

from atif_duck.domain.catalog import (
    DEFAULT_PRICING,
    MACRO_NAMES,
    MACRO_SIGNATURES,
    VIEW_NAMES,
    VIEW_SCHEMA,
)
from atif_duck.infrastructure.registry import register, register_macros


@pytest.fixture
def con(corpus_root: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    register(connection, corpus_root)
    return connection


# ---------------------------------------------------------------------------
# Drift catchers
# ---------------------------------------------------------------------------


def test_view_schema_matches_describe(con: duckdb.DuckDBPyConnection) -> None:
    """Every entry in ``VIEW_SCHEMA`` must match an inline ``DESCRIBE``."""
    for view_name, expected_cols in VIEW_SCHEMA.items():
        rows = con.execute(f"DESCRIBE {view_name}").fetchall()
        observed_cols = tuple((str(r[0]), str(r[1])) for r in rows)
        assert observed_cols == expected_cols, (
            f"VIEW_SCHEMA[{view_name!r}] diverges from DESCRIBE output.\n"
            f"  expected: {expected_cols}\n  observed: {observed_cols}"
        )


def test_view_names_match_schema_keys() -> None:
    """``VIEW_NAMES`` and ``VIEW_SCHEMA`` must cover the same views."""
    assert set(VIEW_NAMES) == set(VIEW_SCHEMA)


def test_macro_signatures_match_ddl() -> None:
    """``MACRO_SIGNATURES`` must equal the args parsed from the DDL strings."""
    source = inspect.getsource(register_macros)
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
    assert parsed == MACRO_SIGNATURES, (
        f"DDL-parsed macro signatures diverge from MACRO_SIGNATURES.\n"
        f"  DDL: {parsed}\n  static: {MACRO_SIGNATURES}"
    )
    assert set(MACRO_SIGNATURES) == set(MACRO_NAMES), (
        f"MACRO_SIGNATURES keys must match MACRO_NAMES.\n"
        f"  signatures: {set(MACRO_SIGNATURES)}\n  names: {set(MACRO_NAMES)}"
    )


def test_all_macros_registered(con: duckdb.DuckDBPyConnection) -> None:
    """Every catalog macro must exist on the registered connection."""
    rows = con.execute(
        """
        SELECT DISTINCT function_name
        FROM duckdb_functions()
        WHERE schema_name = 'main'
          AND function_type IN ('macro', 'table_macro')
        """
    ).fetchall()
    registered = {str(r[0]) for r in rows}
    assert set(MACRO_NAMES) <= registered


# ---------------------------------------------------------------------------
# Functional coverage — sessions and steps
# ---------------------------------------------------------------------------


def test_sessions_count_and_fields(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT session_id, cwd, git_branch, agent_steps, step_count "
        "FROM sessions ORDER BY session_id"
    ).fetchall()
    assert [r[0] for r in rows] == SESSION_IDS
    assert rows[0][1] == "/home/alice/proj"
    assert rows[0][2] == "main"
    assert rows[0][3] == 4  # agent-source steps in session 1
    assert rows[0][4] == 7
    assert rows[1][4] == 2


def test_steps_count(con: duckdb.DuckDBPyConnection) -> None:
    total = con.execute("SELECT count(*) FROM steps").fetchone()
    assert total is not None
    assert total[0] == 9  # 7 + 2


def test_steps_token_columns_including_cache_creation(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """cache_creation must be recovered from metrics.extra (fidelity gap 6)."""
    row = con.execute(
        "SELECT prompt_tokens, completion_tokens, cached_tokens, cache_creation, "
        "llm_call_count FROM steps WHERE session_id = ? AND step_id = 5",
        [SESSION_IDS[0]],
    ).fetchone()
    assert row == (63073, 396, 60118, 2953, 1)


def test_steps_sidechain_and_compact_flags(con: duckdb.DuckDBPyConnection) -> None:
    sidechain = con.execute(
        "SELECT step_id FROM steps WHERE session_id = ? AND is_sidechain ORDER BY step_id",
        [SESSION_IDS[0]],
    ).fetchall()
    assert [r[0] for r in sidechain] == [3, 4]
    compact = con.execute(
        "SELECT step_id, message FROM steps WHERE session_id = ? AND is_compact_summary",
        [SESSION_IDS[0]],
    ).fetchall()
    assert len(compact) == 1
    # List-typed ContentPart message flattens text parts with blank lines.
    assert compact[0] == (7, "Session summary part one.\n\nPart two.")


def test_steps_source_uuids_enrichment(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute(
        "SELECT source_uuids FROM steps WHERE session_id = ? AND step_id = 2",
        [SESSION_IDS[0]],
    ).fetchone()
    assert row is not None
    assert row[0] == '["a-1","a-2"]'


def test_subagent_steps_is_sidechain_filter(con: duckdb.DuckDBPyConnection) -> None:
    total = con.execute("SELECT count(*) FROM subagent_steps").fetchone()
    assert total is not None
    assert total[0] == 2


# ---------------------------------------------------------------------------
# Functional coverage — messages COMPAT view (edges-derived)
# ---------------------------------------------------------------------------


def test_messages_rows_equal_edges_lines(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("SELECT session_id, count(*) FROM messages GROUP BY session_id").fetchall()
    assert dict(rows) == EDGE_COUNTS


def test_messages_carries_raw_record_identity(con: duckdb.DuckDBPyConnection) -> None:
    """The dropped attachment record survives in messages (not in steps)."""
    row = con.execute(
        "SELECT type, parent_uuid, is_compact_summary FROM messages WHERE uuid = 'att-1'"
    ).fetchone()
    assert row == ("attachment", None, False)
    compact = con.execute("SELECT is_compact_summary FROM messages WHERE uuid = 'u-2'").fetchone()
    assert compact is not None
    assert compact[0] is True


def test_messages_parent_uuid_is_varchar(con: duckdb.DuckDBPyConnection) -> None:
    """parent_uuid must be VARCHAR, not a JSON union, so recursive-CTE walks work."""
    cols = {row[0]: row[1] for row in con.execute("DESCRIBE messages").fetchall()}
    assert cols["parent_uuid"] == "VARCHAR"


# ---------------------------------------------------------------------------
# Functional coverage — tool_calls and tool_results
# ---------------------------------------------------------------------------


def test_tool_calls_count_and_mapping(con: duckdb.DuckDBPyConnection) -> None:
    total = con.execute("SELECT count(*) FROM tool_calls").fetchone()
    assert total is not None
    assert total[0] == 9  # 8 in session 1 + 1 in session 2
    row = con.execute(
        "SELECT tool_name, tool_use_id, json_extract_string(tool_input, '$.subagent_type') "
        "FROM tool_calls WHERE tool_use_id = 'toolu_02'"
    ).fetchone()
    assert row == ("Task", "toolu_02", "general-purpose")


def test_tool_results_join_tool_calls(con: duckdb.DuckDBPyConnection) -> None:
    total = con.execute("SELECT count(*) FROM tool_results").fetchone()
    assert total is not None
    assert total[0] == 9
    joined = con.execute(
        "SELECT count(*) FROM tool_calls tc JOIN tool_results tr USING (tool_use_id)"
    ).fetchone()
    assert joined is not None
    assert joined[0] == 9


# ---------------------------------------------------------------------------
# Functional coverage — todos and tasks
# ---------------------------------------------------------------------------


def test_todo_events_count(con: duckdb.DuckDBPyConnection) -> None:
    total = con.execute("SELECT count(*) FROM todo_events").fetchone()
    assert total is not None
    assert total[0] == 5  # 2 + 2 in session 1, 1 in session 2


def test_todo_state_current_latest_wins(con: duckdb.DuckDBPyConnection) -> None:
    rows = dict(
        con.execute(
            "SELECT subject, status FROM todo_state_current WHERE session_id = ?",
            [SESSION_IDS[0]],
        ).fetchall()
    )
    # The second TodoWrite snapshot (step 5) is the current state.
    assert rows == {"Task A": "completed", "Task B": "in_progress"}


def test_subagent_spawns_detected(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT spawn_tool, subagent_type, description FROM subagent_spawns"
    ).fetchall()
    assert rows == [("Task", "general-purpose", "Research the API")]


def test_tasks_state_current_recovers_task_id(con: duckdb.DuckDBPyConnection) -> None:
    """Every COALESCE branch of the task_id recovery must be reachable.

    ``regexp_extract`` returns '' rather than NULL on a no-match, and ''
    satisfies COALESCE — so without the NULLIF guard the $.taskId and
    creation-order branches are dead code and the two non-prose results both
    key on ''. Asserting all three subjects pins each branch.
    """
    rows = dict(
        con.execute(
            "SELECT subject, task_id FROM tasks_state_current WHERE session_id = ?",
            [SESSION_IDS[0]],
        ).fetchall()
    )
    assert rows == {
        "Ship duck views": "1",  # "Task #1 created successfully"
        "Wire the CLI": "42",  # {"taskId": "42"}
        "Write the docs": "3",  # no id in the result -> creation order
    }
    empty = con.execute(
        "SELECT count(*) FROM tasks_state_current WHERE task_id = '' OR task_id IS NULL"
    ).fetchone()
    assert empty is not None
    assert empty[0] == 0


def test_tasks_state_current_does_not_collide_task_ids(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Distinct tasks must not share one key, or they share one status.

    A collapsed-to-'' task_id makes every colliding row match the same
    latest_status row in the join, so a status set on one task leaks onto the
    others. Session 1's TaskUpdate targets task 1 only.
    """
    ids = con.execute(
        "SELECT count(*), count(DISTINCT task_id) FROM tasks_state_current WHERE session_id = ?",
        [SESSION_IDS[0]],
    ).fetchone()
    assert ids is not None
    assert ids[0] == ids[1] == 3
    statuses = dict(
        con.execute(
            "SELECT subject, status FROM tasks_state_current WHERE session_id = ?",
            [SESSION_IDS[0]],
        ).fetchall()
    )
    assert statuses == {
        "Ship duck views": "completed",
        "Wire the CLI": "pending",
        "Write the docs": "pending",
    }


# ---------------------------------------------------------------------------
# Functional coverage — skills
# ---------------------------------------------------------------------------


def test_skill_invocations_both_shapes(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT source, skill_id, args FROM skill_invocations ORDER BY source"
    ).fetchall()
    assert rows == [
        ("slash_command", "foo", "--fast now"),
        ("tool", "personal-plugins:erpaval", "implement the duck views"),
    ]


def test_skill_usage_builtin_heuristic(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT skill_id, skill_name, plugin, is_builtin FROM skill_usage ORDER BY skill_id"
    ).fetchall()
    assert rows == [
        ("foo", "foo", None, True),
        ("personal-plugins:erpaval", "erpaval", "personal-plugins", False),
    ]


# ---------------------------------------------------------------------------
# Functional coverage — loss reports
# ---------------------------------------------------------------------------


def test_loss_reports_per_session(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT session_id, records_total, records_converted, records_dropped "
        "FROM loss_reports ORDER BY session_id"
    ).fetchall()
    assert rows == [(SESSION_IDS[0], 9, 8, 1), (SESSION_IDS[1], 3, 2, 1)]
    gaps = con.execute(
        "SELECT json_extract_string(gaps_observed, '$[0]') FROM loss_reports LIMIT 1"
    ).fetchone()
    assert gaps is not None
    assert gaps[0] == "parent_chain_flattened"


# ---------------------------------------------------------------------------
# Functional coverage — macros
# ---------------------------------------------------------------------------


def test_ago_macro_returns_past_timestamp(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("SELECT ago('14 days') < current_timestamp").fetchone()
    assert row is not None
    assert row[0] is True


def test_model_used(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("SELECT model_used(?)", [SESSION_IDS[0]]).fetchone()
    assert row is not None
    assert row[0] == "claude-opus-4-6"


def test_cost_estimate_positive_and_prefix_matched(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Session 2 carries a DATED model id; the pricing join must strip it.

    haiku pricing (1.0, 5.0): uncached input = 1000 - 300 = 700 tokens.
    (700 * 1.0 + 200 * 5.0) / 1e6 = 0.0017 USD.

    Also pins the trust signal's reachability: both counters skip steps with
    no model_name, so session 2's USER step lands in NEITHER bucket and
    unpriced_steps is 0 at full pricing coverage. Counting user turns as
    unpriced would make the documented ``unpriced_steps = 0`` condition
    unreachable for any session containing a single user message.
    """
    row = con.execute(
        "SELECT est_cost_usd, priced_steps, unpriced_steps FROM cost_estimate(?)",
        [SESSION_IDS[1]],
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.0017)
    assert (row[1], row[2]) == (1, 0)
    # Session 2 has a user step that carries no model; it must not inflate
    # either counter.
    user_steps = con.execute(
        "SELECT count(*) FROM steps WHERE session_id = ? AND model_name IS NULL",
        [SESSION_IDS[1]],
    ).fetchone()
    assert user_steps is not None
    assert user_steps[0] == 1
    # Session 1 is all claude-opus-4-6 at (5.0, 25.0). Its four agent steps sum
    # to 97,953 uncached input and 1,244 completion tokens, so the estimate is
    # (97953 * 5.0 + 1244 * 25.0) / 1e6. Asserted EXACTLY, not merely positive:
    # a positivity check passes at any rate, so a 20x error on the one model
    # every cost test exercises would go unseen.
    opus = con.execute("SELECT est_cost_usd FROM cost_estimate(?)", [SESSION_IDS[0]]).fetchone()
    assert opus is not None
    assert opus[0] == pytest.approx(0.520865)


def test_cost_estimate_reports_unpriced_steps(corpus_root: Path) -> None:
    """An unpriced model must be COUNTED, not silently dropped.

    An inner join drops unmatched steps, so a session mixing a priced and an
    unpriced model returns a partial number indistinguishable from a complete
    one. Pricing session 1's opus but not session 2's haiku makes session 2's
    only priced step vanish: est_cost_usd must go NULL with unpriced_steps > 0
    rather than reporting a confident zero-cost estimate.

    unpriced_steps is 1, not 2: the haiku agent step is the genuine pricing
    gap, while the user step carries no model and is excluded from both
    counters.
    """
    connection = duckdb.connect(":memory:")
    register(connection, corpus_root, pricing={"claude-opus-4-6": (5.0, 25.0)})
    row = connection.execute(
        "SELECT est_cost_usd, priced_steps, unpriced_steps FROM cost_estimate(?)",
        [SESSION_IDS[1]],
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert (row[1], row[2]) == (0, 1)
    # Session 1 IS priced, so its estimate stands and announces completeness
    # over its agent steps.
    priced = connection.execute(
        "SELECT est_cost_usd, unpriced_steps FROM cost_estimate(?)", [SESSION_IDS[0]]
    ).fetchone()
    assert priced is not None
    assert priced[0] > 0


#: Every model DEFAULT_PRICING is expected to carry, with the published per-1M
#: (input, output) list rates. Transcribed from Anthropic's pricing page rather
#: than from the table under test, so this is an independent oracle. Compared
#: for EQUALITY, not containment: a subset check lets an entry be deleted or
#: mispriced without any test noticing, and the fixture corpus's own model
#: (claude-opus-4-6) sits in exactly that blind spot.
_EXPECTED_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def test_default_pricing_matches_published_list_rates_exactly() -> None:
    """DEFAULT_PRICING must equal the oracle key-for-key and rate-for-rate.

    Equality catches all three drift directions a containment check misses: a
    deleted model (its steps silently become unpriced_steps), a mispriced model
    (a confidently wrong estimate), and an invented model absent from the
    published table.
    """
    assert DEFAULT_PRICING == _EXPECTED_PRICING


def test_default_pricing_rates_are_sane() -> None:
    """Output tokens always cost more than input, and no rate is zero/negative.

    A transposed or zeroed pair still joins cleanly and still reports
    unpriced_steps = 0, so the only thing standing between a typo and a
    confidently wrong cost estimate is this check.
    """
    for model, (in_rate, out_rate) in DEFAULT_PRICING.items():
        assert in_rate > 0, model
        assert out_rate > in_rate, model


def test_cost_estimate_trust_signal_is_reachable_and_discriminating(
    corpus_root: Path,
) -> None:
    """unpriced_steps must separate a REAL pricing gap from ordinary user turns.

    The macro documents est_cost_usd as meaningful only when unpriced_steps = 0,
    so that condition has to be reachable at full coverage AND has to break when
    coverage is actually incomplete. Both sessions carry user steps, so a
    counter that included them would report unpriced_steps > 0 in every case
    below and the signal would carry no information.

    Session 1 is all-opus; session 2 is all-haiku. Pricing only opus therefore
    makes session 1 fully priced and session 2 fully unpriced, and the two
    columns must disagree accordingly.
    """
    complete = duckdb.connect(":memory:")
    register(complete, corpus_root)
    both_covered = complete.execute(
        """
        SELECT (SELECT unpriced_steps FROM cost_estimate($a)),
               (SELECT unpriced_steps FROM cost_estimate($b))
        """,
        {"a": SESSION_IDS[0], "b": SESSION_IDS[1]},
    ).fetchone()
    assert both_covered is not None
    # Full coverage: the documented trust condition is actually attainable.
    assert both_covered == (0, 0)

    partial = duckdb.connect(":memory:")
    register(partial, corpus_root, pricing={"claude-opus-4-6": (5.0, 25.0)})
    gap = partial.execute(
        """
        SELECT (SELECT unpriced_steps FROM cost_estimate($a)),
               (SELECT unpriced_steps FROM cost_estimate($b)),
               (SELECT priced_steps   FROM cost_estimate($a))
        """,
        {"a": SESSION_IDS[0], "b": SESSION_IDS[1]},
    ).fetchone()
    assert gap is not None
    # Session 1 stays trustworthy; session 2's real gap is announced.
    assert gap[0] == 0
    assert gap[1] > 0
    # The priced count tracks agent steps only, never the user turns.
    agent_steps = partial.execute(
        "SELECT count(*) FROM steps WHERE session_id = ? AND model_name IS NOT NULL",
        [SESSION_IDS[0]],
    ).fetchone()
    assert agent_steps is not None
    assert gap[2] == agent_steps[0]


def test_default_pricing_covers_no_unpriced_agent_steps(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Every model the fixture corpus actually uses must be priced.

    Guards the DEFAULT_PRICING/corpus drift that left 99.19% of the live
    store's model-carrying steps unpriced (414,128 of 417,508). Only agent
    steps carry a model_name.
    """
    unpriced = con.execute(
        """
        SELECT DISTINCT regexp_replace(model_name, '-\\d{8}$', '')
        FROM steps
        WHERE model_name IS NOT NULL
          AND regexp_replace(model_name, '-\\d{8}$', '') NOT IN (
              SELECT unnest($models)
          )
        """,
        {"models": list(DEFAULT_PRICING)},
    ).fetchall()
    assert unpriced == []


def test_tool_rank_orders_by_count(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("SELECT * FROM tool_rank(36500)").fetchall()
    counts = [int(r[1]) for r in rows]
    assert counts == sorted(counts, reverse=True)
    # TodoWrite (3, across both sessions) and TaskCreate (3) tie for the top,
    # so assert the tied SET rather than a nondeterministic first row.
    assert {r[0] for r in rows if r[1] == counts[0]} == {"TodoWrite", "TaskCreate"}
    assert counts[0] == 3


def test_todo_velocity(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("SELECT todo_velocity(?)", [SESSION_IDS[0]]).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.5)  # 1 completed of 2 subjects


def test_subagent_fanout_counts_spawns(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute("SELECT subagent_fanout(?)", [SESSION_IDS[0]]).fetchone()
    assert row is not None
    assert row[0] == 1
    row2 = con.execute("SELECT subagent_fanout(?)", [SESSION_IDS[1]]).fetchone()
    assert row2 is not None
    assert row2[0] == 0


def test_skill_rank_and_source_mix(con: duckdb.DuckDBPyConnection) -> None:
    rank = con.execute("SELECT skill_id, n FROM skill_rank(36500)").fetchall()
    assert set(rank) == {("foo", 1), ("personal-plugins:erpaval", 1)}
    # skill_source_mix excludes builtins — only the plugin skill remains.
    mix = con.execute("SELECT skill_id, n_tool, n_slash FROM skill_source_mix(36500)").fetchall()
    assert mix == [("personal-plugins:erpaval", 1, 0)]


# ---------------------------------------------------------------------------
# Registration behavior
# ---------------------------------------------------------------------------


def test_pricing_model_names_are_escaped(corpus_root: Path) -> None:
    """A quote in a model name must be a literal, not DDL.

    The pricing rows are interpolated into macro DDL, so an unescaped
    apostrophe closes the string literal and the rest of the name is parsed as
    SQL. Registration must succeed and the row must match by exact name.
    """
    connection = duckdb.connect(":memory:")
    hostile = "o'brien', 1.0, 2.0), ('injected"
    register(connection, corpus_root, pricing={hostile: (7.0, 11.0)})
    row = connection.execute(
        "SELECT est_cost_usd, priced_steps, unpriced_steps FROM cost_estimate(?)",
        [SESSION_IDS[1]],
    ).fetchone()
    assert row is not None
    # No real step carries that name, so nothing prices and the one
    # model-carrying step counts as unpriced.
    assert row[0] is None
    assert (row[1], row[2]) == (0, 1)


def test_register_is_idempotent(corpus_root: Path, con: duckdb.DuckDBPyConnection) -> None:
    """CREATE OR REPLACE everywhere: re-registering must not raise."""
    register(con, corpus_root)
    total = con.execute("SELECT count(*) FROM sessions").fetchone()
    assert total is not None
    assert total[0] == 2


def test_register_fails_loud_on_empty_corpus(tmp_path: Path) -> None:
    """An unmaterialized corpus is an error, not an empty result set."""
    connection = duckdb.connect(":memory:")
    with pytest.raises(duckdb.Error):
        register(connection, tmp_path)


def test_torn_session_dir_without_meta_is_invisible(corpus_root: Path) -> None:
    """A crashed writer (artifacts present, meta.json missing) contributes
    NOTHING to any view — the corpus stays queryable at the old generation
    instead of surfacing a torn artifact set — and the exclusion is LOGGED
    as skipped-incomplete (a silent gap is undiagnosable)."""
    torn_id = "33333333-3333-3333-3333-333333333333"
    complete = corpus_root / "sessions" / SESSION_IDS[0]
    torn = corpus_root / "sessions" / torn_id
    torn.mkdir(parents=True)
    # trajectory + edges written, crash before loss_report/meta.
    (torn / "trajectory.json").write_text(
        (complete / "trajectory.json").read_text().replace(SESSION_IDS[0], torn_id)
    )
    (torn / "edges.jsonl").write_text((complete / "edges.jsonl").read_text())

    warnings: list[str] = []
    sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        connection = duckdb.connect(":memory:")
        register(connection, corpus_root)
    finally:
        logger.remove(sink_id)

    session_rows = connection.execute("SELECT session_id FROM sessions ORDER BY 1").fetchall()
    assert [row[0] for row in session_rows] == SESSION_IDS
    for view, key in (("steps", "session_id"), ("messages", "session_id")):
        torn_rows = connection.execute(
            f"SELECT count(*) FROM {view} WHERE {key} = ?", [torn_id]
        ).fetchone()
        assert torn_rows is not None
        assert torn_rows[0] == 0
    skip_warnings = [w for w in warnings if "incomplete session dir" in w and torn_id in w]
    assert len(skip_warnings) == 1
