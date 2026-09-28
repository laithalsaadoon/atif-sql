# SPDX-License-Identifier: Apache-2.0

"""``step_author`` and the three views on it: every rule, every kind, every outcome.

The corpus here is built for the authorship surface: one session per
``session_outcomes.kind`` x ``outcome`` shape the live corpora show, each
carrying the machine-written user-role text that session kind really gets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from duck_fixtures import READ_PATHS, register_via

from atif_duck.domain import authorship
from atif_duck.domain.authorship import (
    AUTHOR_PREFIX_RULES,
    AUTHOR_STRIP_CHARS,
    AUTHOR_VALUES,
    INTERRUPT_PREFIXES,
)

INTERACTIVE = "10000000-0000-0000-0000-000000000001"
AUDIT_PASS = "10000000-0000-0000-0000-000000000002"
AUDIT_BLOCK = "10000000-0000-0000-0000-000000000003"
ONE_SHOT = "10000000-0000-0000-0000-000000000004"
INTERRUPTED = "10000000-0000-0000-0000-000000000005"
NO_USER = "10000000-0000-0000-0000-000000000006"


def _step(
    step_id: int,
    source: str,
    message: str,
    *,
    sidechain: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "is_sidechain": sidechain,
        "source_uuids": [f"x-{step_id}"],
    }
    if compact:
        extra["is_compact_summary"] = True
    step: dict[str, Any] = {
        "step_id": step_id,
        "timestamp": f"2026-09-20T10:{step_id:02d}:00.000Z",
        "source": source,
        "message": message,
        "extra": extra,
    }
    if source == "agent":
        step["model_name"] = "claude-opus-4-6"
    return step


def _write(root: Path, session_id: str, turns: list[tuple[str, str, dict[str, bool]]]) -> None:
    steps = [_step(i + 1, src, msg, **flags) for i, (src, msg, flags) in enumerate(turns)]
    sdir = root / "sessions" / session_id
    sdir.mkdir(parents=True)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "claude-opus-4-6"},
        "steps": steps,
        "final_metrics": {"total_steps": len(steps)},
    }
    (sdir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
    edges = [
        {
            "uuid": f"x-{s['step_id']}",
            "parent_uuid": None,
            "session_id": session_id,
            "ts": s["timestamp"],
            "type": "user" if s["source"] == "user" else "assistant",
            "is_sidechain": s["extra"]["is_sidechain"],
            "is_compact_summary": False,
            "message_id": None,
            "tool_use_ids": [],
            "source_file": "transcript.jsonl",
        }
        for s in steps
    ]
    (sdir / "edges.jsonl").write_text("\n".join(json.dumps(e) for e in edges) + "\n")
    (sdir / "loss_report.json").write_text(
        json.dumps(
            {
                "record_counts": {"user": 1},
                "records_total": 1,
                "records_converted": 1,
                "records_dropped": 0,
                "gaps_observed": [],
                "subagent_files_found": 0,
                "subagent_files_convertible": 0,
                "workflow_subagent_files_found": 0,
            }
        )
    )
    (sdir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "source_mtime_ns": 1,
                "source_files": ["transcript.jsonl"],
                "harbor_version": "0.22.0",
                "converter_version": "0.1.0",
                "materialized_at": "2026-09-20T11:00:00Z",
            }
        )
    )


_NONE: dict[str, bool] = {}


@pytest.fixture(scope="module", params=READ_PATHS)
def con(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> duckdb.DuckDBPyConnection:
    read_path = str(request.param)
    root = tmp_path_factory.mktemp("authorship")
    _write(
        root,
        INTERACTIVE,
        [
            ("user", "Refactor the auth middleware.", _NONE),
            ("agent", "Reading the middleware.", _NONE),
            ("user", "Base directory for this skill: /skills/x\n# Skill body", _NONE),
            ("user", "Delegate this.", {"sidechain": True}),
            ("agent", "Subagent done.", {"sidechain": True}),
            ("user", "<task-notification>\n<task-id>t1</task-id>", _NONE),
            ("agent", "Done, tests pass.", _NONE),
            ("user", "Stop hook feedback: the claim has no evidence.", _NONE),
            ("agent", "Here is the evidence.", _NONE),
            ("user", "  looks good, ship it", _NONE),
            ("user", "Summary of the work so far.", {"compact": True}),
            ("agent", "Shipped.", _NONE),
        ],
    )
    _write(
        root,
        AUDIT_PASS,
        [
            ("user", "You are auditing an agent turn for integrity. Transcript: ...", _NONE),
            ("agent", '{"ok": true}', _NONE),
        ],
    )
    _write(
        root,
        AUDIT_BLOCK,
        [
            ("user", "You are auditing an agent turn for integrity. Transcript: ...", _NONE),
            ("agent", "Let me look.", _NONE),
            ("agent", '```json\n{ "ok": false, "reason": "no evidence" }\n```', _NONE),
        ],
    )
    _write(
        root,
        ONE_SHOT,
        [
            ("user", "Run the nightly report.", _NONE),
            ("agent", "", _NONE),
            ("user", "Your previous attempt hit a transient error.", _NONE),
            ("agent", "Brief posted.", _NONE),
        ],
    )
    _write(
        root,
        INTERRUPTED,
        [
            ("user", "Start the migration.", _NONE),
            ("agent", "Starting.", _NONE),
            ("user", "[Request interrupted by user]", _NONE),
            ("user", "no, do the schema first", _NONE),
            ("agent", "Schema first then.", _NONE),
        ],
    )
    _write(root, NO_USER, [("agent", "Nothing asked.", _NONE)])
    connection = duckdb.connect()
    register_via(connection, root, read_path)
    return connection


def _author(con: duckdb.DuckDBPyConnection, source: str, message: str | None) -> str | None:
    row = con.execute("SELECT step_author(?, ?)", [source, message]).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("expected", "prefix"), AUTHOR_PREFIX_RULES)
def test_every_rule_fires_with_leading_whitespace(
    con: duckdb.DuckDBPyConnection, expected: str, prefix: str
) -> None:
    assert _author(con, "user", f" \n\t{prefix} and then some text") == expected


@pytest.mark.parametrize(("_author_label", "prefix"), AUTHOR_PREFIX_RULES)
def test_a_prefix_inside_human_text_stays_human(
    con: duckdb.DuckDBPyConnection, _author_label: str, prefix: str
) -> None:
    """The match is anchored: quoting a hook message does not make it one."""
    assert _author(con, "user", f"why did I get this: {prefix}") == "human"


def test_no_rule_is_shadowed_by_an_earlier_one() -> None:
    """An earlier prefix that starts a later one would make the later rule dead."""
    for i, (author_i, prefix_i) in enumerate(AUTHOR_PREFIX_RULES):
        for author_j, prefix_j in AUTHOR_PREFIX_RULES[i + 1 :]:
            assert not prefix_j.startswith(prefix_i), (
                f"{prefix_j!r} ({author_j}) can never match: {prefix_i!r} ({author_i}) wins first"
            )


def test_labels_are_the_documented_set() -> None:
    assert {a for a, _ in AUTHOR_PREFIX_RULES} | {"human"} == set(AUTHOR_VALUES)


def test_interrupts_are_harness() -> None:
    by_prefix = {p: a for a, p in AUTHOR_PREFIX_RULES}
    for prefix in INTERRUPT_PREFIXES:
        assert by_prefix.get(prefix) == "harness"


def test_strip_chars_sql_names_the_same_characters() -> None:
    codes = [int(tok.strip()[4:-1]) for tok in authorship._STRIP_CHARS_SQL.split("||")]
    assert sorted(codes) == sorted(ord(c) for c in AUTHOR_STRIP_CHARS)


def test_non_user_and_empty(con: duckdb.DuckDBPyConnection) -> None:
    assert _author(con, "agent", "Stop hook feedback: x") is None
    assert _author(con, "system", "hi") is None
    assert _author(con, "user", None) == "harness"
    assert _author(con, "user", " \n ") == "harness"
    assert _author(con, "user", "go") == "human"


# ---------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------


def test_user_steps_authors(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT step_id, is_sidechain, author FROM user_steps "
        "WHERE session_id = ? ORDER BY step_id",
        [INTERACTIVE],
    ).fetchall()
    assert rows == [
        (1, False, "human"),
        (3, False, "harness"),
        (4, True, "human"),
        (6, False, "task_notification"),
        (8, False, "stop_hook"),
        (10, False, "human"),
        # The compaction flag wins over the text, which reads as human.
        (11, False, "harness"),
    ]


def test_user_steps_uuid_is_the_first_source_uuid(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute(
        "SELECT uuid FROM user_steps WHERE session_id = ? AND step_id = 1", [INTERACTIVE]
    ).fetchone()
    assert row == ("x-1",)


def test_human_turns_drop_sidechain_and_machine_text(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT session_id, step_id FROM human_turns ORDER BY session_id, step_id"
    ).fetchall()
    assert rows == [
        (INTERACTIVE, 1),
        (INTERACTIVE, 10),
        (ONE_SHOT, 1),
        (INTERRUPTED, 1),
        (INTERRUPTED, 4),
    ]


def test_session_outcomes(con: duckdb.DuckDBPyConnection) -> None:
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT session_id, kind, outcome, human_turns, interrupts, reviewer_blocks "
            "FROM session_outcomes"
        ).fetchall()
    }
    assert rows == {
        INTERACTIVE: ("interactive", "reviewer_blocked", 2, 0, 1),
        AUDIT_PASS: ("turn_audit", "pass", 0, 0, 0),
        AUDIT_BLOCK: ("turn_audit", "block", 0, 0, 0),
        ONE_SHOT: ("one_shot_job", "clean_end", 1, 0, 0),
        INTERRUPTED: ("interactive", "interrupted", 2, 1, 0),
        NO_USER: ("one_shot_job", "clean_end", 0, 0, 0),
    }
