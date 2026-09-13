# SPDX-License-Identifier: Apache-2.0

"""Synthetic contract-shaped corpus fixture for atif-duck tests.

Handwritten ATIF-v1.7 trajectory dicts + edges lines per docs/CONTRACT.md —
NO dependency on atif-corpus/atif-converter (import-linter independence).
The corpus contains two sessions:

* ``SESSION_IDS[0]`` — the rich session: sidechain steps (harbor inlines
  subagents, marked via ``extra.is_sidechain``), TodoWrite / Task / Skill /
  TaskUpdate tool calls with realistic arguments, three TaskCreate calls
  covering all three task-id recovery branches ("Task #N" prose, a JSON
  ``taskId``, and a result naming no id at all), a user step
  whose text carries ``<command-name>/foo</command-name>``, metrics with
  cache extras (incl. the nested ``cache_creation`` breakdown), a
  compact-summary user step with list-typed ContentPart message, and
  ``step.extra.source_uuids`` per the CONTRACT enrichment pass.
* ``SESSION_IDS[1]`` — a small session with a DATED model id
  (``claude-haiku-4-5-20251001``) to exercise ``cost_estimate``'s
  suffix-stripping pricing join, plus one more TodoWrite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SESSION_IDS = [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
]

#: edges.jsonl line counts per session — the ``messages`` COMPAT view must
#: surface exactly one row per edge line.
EDGE_COUNTS = {SESSION_IDS[0]: 9, SESSION_IDS[1]: 3}


def _edge(
    uuid: str,
    *,
    parent: str | None,
    type_: str,
    ts: str,
    message_id: str | None = None,
    sidechain: bool = False,
    compact: bool = False,
    tool_use_ids: list[str] | None = None,
) -> dict[str, Any]:
    """One edges.jsonl line in the CONTRACT shape (one per RAW record)."""
    return {
        "uuid": uuid,
        "parent_uuid": parent,
        "message_id": message_id,
        "type": type_,
        "ts": ts,
        "is_sidechain": sidechain,
        "is_compact_summary": compact,
        "source_file": "transcript.jsonl",
        "tool_use_ids": tool_use_ids or [],
    }


def _metrics(
    prompt: int,
    completion: int,
    cached: int,
    creation: int,
) -> dict[str, Any]:
    """ATIF Metrics as harbor 0.22.0 emits them: ``prompt_tokens`` is the
    TOTAL input (non-cached + cache_read + cache_creation); the creation
    split survives only in ``extra`` (fidelity gap 6), including the nested
    ``cache_creation`` ephemeral breakdown seen on the live corpus."""
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "extra": {
            "cache_creation_input_tokens": creation,
            "cache_read_input_tokens": cached,
            "cache_creation": {
                "ephemeral_1h_input_tokens": creation,
                "ephemeral_5m_input_tokens": 0,
            },
            "service_tier": "standard",
        },
    }


def _session_one() -> dict[str, Any]:
    """The rich trajectory: 7 steps covering every viewed surface."""
    sid = SESSION_IDS[0]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {
            "name": "claude-code",
            "version": "2.1.218",
            "model_name": "claude-opus-4-6",
            "extra": {"cwds": ["/home/alice/proj"], "git_branches": ["main"]},
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": (
                    "<command-message>foo is running</command-message>"
                    "<command-name>/foo</command-name>"
                    "<command-args>--fast now</command-args>"
                ),
                "extra": {"is_sidechain": False, "source_uuids": ["u-1"]},
            },
            {
                "step_id": 2,
                "timestamp": "2026-08-20T10:00:10.000Z",
                "source": "agent",
                "model_name": "claude-opus-4-6",
                "message": "Planning the work.",
                "tool_calls": [
                    {
                        "tool_call_id": "toolu_01",
                        "function_name": "TodoWrite",
                        "arguments": {
                            "todos": [
                                {
                                    "content": "Task A",
                                    "status": "pending",
                                    "activeForm": "Doing Task A",
                                },
                                {
                                    "content": "Task B",
                                    "status": "pending",
                                    "activeForm": "Doing Task B",
                                },
                            ]
                        },
                    },
                    {
                        "tool_call_id": "toolu_02",
                        "function_name": "Task",
                        "arguments": {
                            "subagent_type": "general-purpose",
                            "description": "Research the API",
                            "prompt": "Find every caller of frobnicate().",
                        },
                    },
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "toolu_01",
                            "content": "Todos have been modified successfully",
                        },
                        {"source_call_id": "toolu_02", "content": "Agent started"},
                    ]
                },
                "metrics": _metrics(60120, 325, 0, 60118),
                "llm_call_count": 1,
                "extra": {"is_sidechain": False, "source_uuids": ["a-1", "a-2"]},
            },
            # Harbor INLINES subagent transcripts as sidechain steps.
            {
                "step_id": 3,
                "timestamp": "2026-08-20T10:00:20.000Z",
                "source": "user",
                "message": "Find every caller of frobnicate().",
                "extra": {"is_sidechain": True, "source_uuids": ["su-1"]},
            },
            {
                "step_id": 4,
                "timestamp": "2026-08-20T10:00:30.000Z",
                "source": "agent",
                "model_name": "claude-opus-4-6",
                "message": "Two callers: main.py and cli.py.",
                "metrics": _metrics(33878, 403, 0, 33876),
                "llm_call_count": 1,
                "extra": {"is_sidechain": True, "source_uuids": ["sa-1"]},
            },
            {
                "step_id": 5,
                "timestamp": "2026-08-20T10:01:00.000Z",
                "source": "agent",
                "model_name": "claude-opus-4-6",
                "message": "Invoking the review skill and tracking a task.",
                "tool_calls": [
                    {
                        "tool_call_id": "toolu_03",
                        "function_name": "Skill",
                        "arguments": {
                            "skill": "personal-plugins:erpaval",
                            "args": "implement the duck views",
                        },
                    },
                    {
                        "tool_call_id": "toolu_04",
                        "function_name": "TaskCreate",
                        "arguments": {
                            "subject": "Ship duck views",
                            "description": "Bind the DuckDB view surface",
                            "activeForm": "Shipping duck views",
                        },
                    },
                    # Result carries the id as JSON ``taskId`` instead of the
                    # "Task #N" prose — the second COALESCE branch.
                    {
                        "tool_call_id": "toolu_07",
                        "function_name": "TaskCreate",
                        "arguments": {
                            "subject": "Wire the CLI",
                            "description": "Expose the surface",
                            "activeForm": "Wiring the CLI",
                        },
                    },
                    # Result mentions NO id at all — must fall through to the
                    # per-session creation-order fallback.
                    {
                        "tool_call_id": "toolu_08",
                        "function_name": "TaskCreate",
                        "arguments": {
                            "subject": "Write the docs",
                            "description": "Document the surface",
                            "activeForm": "Writing the docs",
                        },
                    },
                    {
                        "tool_call_id": "toolu_05",
                        "function_name": "TodoWrite",
                        "arguments": {
                            "todos": [
                                {
                                    "content": "Task A",
                                    "status": "completed",
                                    "activeForm": "Doing Task A",
                                },
                                {
                                    "content": "Task B",
                                    "status": "in_progress",
                                    "activeForm": "Doing Task B",
                                },
                            ]
                        },
                    },
                ],
                "observation": {
                    "results": [
                        {"source_call_id": "toolu_03", "content": "Skill loaded"},
                        {
                            "source_call_id": "toolu_04",
                            "content": "Task #1 created successfully",
                        },
                        {"source_call_id": "toolu_07", "content": {"taskId": "42"}},
                        {"source_call_id": "toolu_08", "content": "Created successfully"},
                        {
                            "source_call_id": "toolu_05",
                            "content": "Todos have been modified successfully",
                        },
                    ]
                },
                "metrics": _metrics(63073, 396, 60118, 2953),
                "llm_call_count": 1,
                "extra": {"is_sidechain": False, "source_uuids": ["a-3"]},
            },
            {
                "step_id": 6,
                "timestamp": "2026-08-20T10:02:00.000Z",
                "source": "agent",
                "model_name": "claude-opus-4-6",
                "message": "Closing the task.",
                "tool_calls": [
                    {
                        "tool_call_id": "toolu_06",
                        "function_name": "TaskUpdate",
                        "arguments": {"taskId": "1", "status": "completed"},
                    }
                ],
                "observation": {
                    "results": [{"source_call_id": "toolu_06", "content": "Task #1 updated"}]
                },
                "metrics": _metrics(64000, 120, 63000, 900),
                "llm_call_count": 1,
                "extra": {"is_sidechain": False, "source_uuids": ["a-4"]},
            },
            # Compact-summary step with a list-typed ContentPart message —
            # exercises both the ``extra.is_compact_summary`` enrichment and
            # the ARRAY branch of the ``steps.message`` flattener.
            {
                "step_id": 7,
                "timestamp": "2026-08-20T10:03:00.000Z",
                "source": "user",
                "message": [
                    {"type": "text", "text": "Session summary part one."},
                    {"type": "text", "text": "Part two."},
                ],
                "extra": {
                    "is_sidechain": False,
                    "is_compact_summary": True,
                    "source_uuids": ["u-2"],
                },
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 221071,
            "total_completion_tokens": 1244,
            "total_cached_tokens": 123118,
            "total_cost_usd": 1.79,
            "total_steps": 7,
            "extra": {"total_cache_creation_input_tokens": 97847},
        },
        "extra": {"cache_creation_total": 97847},
    }


def _session_two() -> dict[str, Any]:
    """Small session with a dated model id for the pricing prefix match."""
    sid = SESSION_IDS[1]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {
            "name": "claude-code",
            "version": "2.1.218",
            "model_name": "claude-haiku-4-5-20251001",
            "extra": {"cwds": ["/home/alice/other"], "git_branches": ["dev"]},
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-21T09:00:00.000Z",
                "source": "user",
                "message": "Tidy the docs.",
                "extra": {"is_sidechain": False, "source_uuids": ["u2-1"]},
            },
            {
                "step_id": 2,
                "timestamp": "2026-08-21T09:00:05.000Z",
                "source": "agent",
                "model_name": "claude-haiku-4-5-20251001",
                "message": "On it.",
                "tool_calls": [
                    {
                        "tool_call_id": "toolu_21",
                        "function_name": "TodoWrite",
                        "arguments": {
                            "todos": [
                                {
                                    "content": "Tidy docs",
                                    "status": "in_progress",
                                    "activeForm": "Tidying docs",
                                }
                            ]
                        },
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "toolu_21",
                            "content": "Todos have been modified successfully",
                        }
                    ]
                },
                "metrics": _metrics(1000, 200, 300, 100),
                "llm_call_count": 1,
                "extra": {"is_sidechain": False, "source_uuids": ["a2-1"]},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 1000,
            "total_completion_tokens": 200,
            "total_cached_tokens": 300,
            "total_cost_usd": 0.0016,
            "total_steps": 2,
        },
    }


def _edges_one() -> list[dict[str, Any]]:
    """Nine raw-record edges for session 1 (incl. dropped record types)."""
    return [
        _edge("u-1", parent=None, type_="user", ts="2026-08-20T10:00:00.000Z"),
        _edge(
            "a-1",
            parent="u-1",
            type_="assistant",
            ts="2026-08-20T10:00:10.000Z",
            message_id="msg_01",
            tool_use_ids=["toolu_01", "toolu_02"],
        ),
        _edge(
            "a-2",
            parent="a-1",
            type_="assistant",
            ts="2026-08-20T10:00:11.000Z",
            message_id="msg_01",
        ),
        _edge(
            "su-1",
            parent=None,
            type_="user",
            ts="2026-08-20T10:00:20.000Z",
            sidechain=True,
        ),
        _edge(
            "sa-1",
            parent="su-1",
            type_="assistant",
            ts="2026-08-20T10:00:30.000Z",
            message_id="msg_s1",
            sidechain=True,
        ),
        _edge(
            "a-3",
            parent="a-2",
            type_="assistant",
            ts="2026-08-20T10:01:00.000Z",
            message_id="msg_02",
            tool_use_ids=["toolu_03", "toolu_04", "toolu_05", "toolu_07", "toolu_08"],
        ),
        _edge(
            "a-4",
            parent="a-3",
            type_="assistant",
            ts="2026-08-20T10:02:00.000Z",
            message_id="msg_03",
            tool_use_ids=["toolu_06"],
        ),
        _edge(
            "u-2",
            parent="a-4",
            type_="user",
            ts="2026-08-20T10:03:00.000Z",
            compact=True,
        ),
        # A raw record type harbor DROPS — present in edges, absent from steps.
        _edge("att-1", parent=None, type_="attachment", ts="2026-08-20T10:03:30.000Z"),
    ]


def _edges_two() -> list[dict[str, Any]]:
    return [
        _edge("u2-1", parent=None, type_="user", ts="2026-08-21T09:00:00.000Z"),
        _edge(
            "a2-1",
            parent="u2-1",
            type_="assistant",
            ts="2026-08-21T09:00:05.000Z",
            message_id="msg_21",
            tool_use_ids=["toolu_21"],
        ),
        _edge("sys-1", parent=None, type_="system", ts="2026-08-21T09:00:06.000Z"),
    ]


def _loss_report(counts: dict[str, int], converted: int) -> dict[str, Any]:
    total = sum(counts.values())
    return {
        "record_counts": counts,
        "records_total": total,
        "records_converted": converted,
        "records_dropped": total - converted,
        "gaps_observed": ["parent_chain_flattened", "cache_split_partial"],
        "subagent_files_found": 1,
        "subagent_files_convertible": 1,
        "workflow_subagent_files_found": 0,
    }


def _meta(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "source_mtime_ns": 1755861600000000000,
        "source_files": ["transcript.jsonl"],
        "harbor_version": "0.22.0",
        "converter_version": "0.1.0",
        "materialized_at": "2026-08-22T00:00:00Z",
    }


def build_corpus(root: Path) -> Path:
    """Write the two-session contract-shaped corpus under ``root``; return it.

    Plain function (not a fixture) so module-scoped fixtures — e.g. the
    examples contract test's one-registration connection — can build the
    same corpus without pytest's function-scope plumbing.
    """
    payloads: dict[str, tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = {
        SESSION_IDS[0]: (
            _session_one(),
            _edges_one(),
            _loss_report(
                {"user": 3, "assistant": 5, "attachment": 1},
                converted=8,
            ),
        ),
        SESSION_IDS[1]: (
            _session_two(),
            _edges_two(),
            _loss_report({"user": 1, "assistant": 1, "system": 1}, converted=2),
        ),
    }
    for session_id, (trajectory, edges, loss) in payloads.items():
        sdir = root / "sessions" / session_id
        sdir.mkdir(parents=True)
        # Compact JSON, per CONTRACT: separators=(',', ':').
        (sdir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
        (sdir / "edges.jsonl").write_text("\n".join(json.dumps(edge) for edge in edges) + "\n")
        (sdir / "loss_report.json").write_text(json.dumps(loss, separators=(",", ":")))
        (sdir / "meta.json").write_text(json.dumps(_meta(session_id), separators=(",", ":")))
    return root


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    """Write the two-session contract-shaped corpus; return its root."""
    return build_corpus(tmp_path)


# ---------------------------------------------------------------------------
# A Codex session, in harbor's Codex shape (no dependency on atif-converter).
# Shared by test_codex_views (which pins the SQL's reading of that shape) and
# test_columnar (which needs the Codex cache-write key and an empty assistant
# message in its equivalence corpus).
# ---------------------------------------------------------------------------

CODEX_SESSION_ID = "01a09182-2858-7f42-936b-7f027b341fdf"

#: Codex's cache-creation spelling in ``metrics.extra`` — the one the Claude
#: Code adapter does NOT use, which is why the steps view coalesces both.
CODEX_CACHE_WRITE_KEY = "cache_write_input_tokens"


def codex_trajectory() -> dict[str, Any]:
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
                    "extra": {CODEX_CACHE_WRITE_KEY: 900, "total_tokens": 1_040},
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
                    "extra": {CODEX_CACHE_WRITE_KEY: 0},
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


def codex_edges() -> list[dict[str, Any]]:
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


def write_codex_session(root: Path) -> Path:
    """Write the one Codex session under ``root/sessions/``; return its directory."""
    session_dir = root / "sessions" / CODEX_SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "trajectory.json").write_text(
        json.dumps(codex_trajectory(), separators=(",", ":"))
    )
    (session_dir / "edges.jsonl").write_text(
        "\n".join(json.dumps(edge) for edge in codex_edges()) + "\n"
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
    return session_dir


# ``register_vss`` no longer installs the lance extension (that download at
# query time was finding 4 of the MicroVM review), so the suite installs it
# once up front. A no-op where it is already present; a one-time download on a
# fresh runner, exactly what every register() call used to do implicitly.
# A public name on purpose: ``conftest.py`` re-exports this module with
# ``import *``, which skips underscore names, so an underscore here would leave
# the fixture unregistered (it did, on a runner with no extension cached).
@pytest.fixture(scope="session", autouse=True)
def lance_extension_present() -> None:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL lance")
    finally:
        con.close()
