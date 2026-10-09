# SPDX-License-Identifier: Apache-2.0

"""A code-mode script's nested calls reach ``tool_calls`` and ``tool_results``.

One rollout whose ``exec`` script ran an MCP call that failed and a command
that exited 2, materialized through the CLI and queried back, because the rows
are only right when the converter, the corpus writer and the lake views agree.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from atif_cli.app import materialize, query
from atif_cli.output import OutputFormat

_SESSION_ID = "01a11e48-ecd5-7e10-8d09-43926dcfdcc9"
_TOKENS = {
    "input_tokens": 100,
    "cached_input_tokens": 0,
    "output_tokens": 2,
    "total_tokens": 102,
}


def _item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-09T01:31:00.300Z",
        "type": "event_msg",
        "payload": {"type": "item_completed", "turn_id": "turn-1", "item": item},
    }


def _records() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-10-09T01:30:58.000Z",
            "type": "session_meta",
            "payload": {"id": _SESSION_ID, "cwd": "/home/alice/proj", "cli_version": "0.160.1"},
        },
        {
            "timestamp": "2026-10-09T01:30:58.100Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-10-09T01:30:58.200Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
        },
        {
            "timestamp": "2026-10-09T01:30:58.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "check the table"}],
            },
        },
        {
            "timestamp": "2026-10-09T01:31:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_exec",
                "name": "exec",
                "input": "text(await tools.mcp__chora__db_query({sql:'SELECT x'}));",
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
        _item(
            {
                "type": "McpToolCall",
                "id": "exec-m1",
                "server": "chora",
                "tool": "db_query",
                "arguments": {"sql": "SELECT x"},
                "status": "failed",
                "result": {
                    "content": [{"type": "text", "text": "Error: no such column: x"}],
                    "isError": True,
                },
            }
        ),
        _item(
            {
                "type": "CommandExecution",
                "id": "exec-c1",
                "command": ["/usr/bin/bash", "-c", "false"],
                "status": "failed",
                "exit_code": 2,
                "aggregated_output": "",
            }
        ),
        {
            "timestamp": "2026-10-09T01:31:00.500Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_exec",
                "output": [{"type": "input_text", "text": "Script completed\nOutput:\n"}],
            },
        },
        {
            "timestamp": "2026-10-09T01:31:00.600Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": _TOKENS, "last_token_usage": _TOKENS},
            },
        },
        {
            "timestamp": "2026-10-09T01:31:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "the column is missing"}],
            },
        },
    ]


@pytest.mark.integration
def test_nested_calls_are_tool_call_and_result_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
    day_dir = tmp_path / "sessions" / "2026" / "10" / "09"
    day_dir.mkdir(parents=True)
    rollout = day_dir / f"rollout-2026-10-09T01-30-58-{_SESSION_ID}.jsonl"
    rollout.write_text("\n".join(json.dumps(record) for record in _records()) + "\n")
    stale = time.time_ns() - 3_600 * 1_000_000_000
    os.utime(rollout, ns=(stale, stale))
    corpus_root = tmp_path / "corpus"

    materialize(
        agent="codex",
        source_root=tmp_path / "sessions",
        corpus_root=corpus_root,
        fmt=OutputFormat.JSON,
    )
    assert json.loads(capsys.readouterr().out)["materialized"] == 1

    query(
        "SELECT tool_use_id, tool_name, CAST(tool_input AS VARCHAR) AS tool_input, "
        "is_error, exit_code FROM tool_calls JOIN tool_results USING (tool_use_id) "
        "ORDER BY tool_use_id",
        corpus_root=corpus_root,
        fmt=OutputFormat.JSON,
    )
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "tool_use_id": "call_exec",
            "tool_name": "exec",
            "tool_input": '{"input":"text(await tools.mcp__chora__db_query({sql:\'SELECT x\'}));"}',
            "is_error": True,
            "exit_code": 2,
        },
        {
            "tool_use_id": "exec-c1",
            "tool_name": "exec_command",
            "tool_input": '{"cmd":"false"}',
            "is_error": True,
            "exit_code": 2,
        },
        {
            "tool_use_id": "exec-m1",
            "tool_name": "mcp__chora__db_query",
            "tool_input": '{"sql":"SELECT x"}',
            "is_error": True,
            "exit_code": None,
        },
    ]
