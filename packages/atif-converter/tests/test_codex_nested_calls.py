# SPDX-License-Identifier: Apache-2.0

"""A Codex code-mode script's nested MCP calls and commands become tool calls.

Every test runs the real use case (``convert_codex_and_audit``) over a rollout
on disk, because a nested call is only right when the blob pre-pass, the port,
the enrichment pass and the result-signals pass agree about the same records.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from codex_fixtures import codex_rollout_records, write_codex_rollout
from subagent_fixtures import READ_PNG

from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain import result_signals
from atif_converter.infrastructure.codex_converter import convert_codex_rollout

_TS = "2026-09-11T17:27:02.{:03d}Z"


class _Rollout:
    """The records of synthetic ``exec`` scripts and their nested items, in file order."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def _add(self, record_type: str, payload: dict[str, Any]) -> None:
        stamp = _TS.format(310 + len(self.records))
        self.records.append({"timestamp": stamp, "type": record_type, "payload": payload})

    def _item(self, item: dict[str, Any], turn_id: str = "turn-1") -> None:
        self._add(
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": turn_id,
                "item": item,
                "started_at_ms": 1_000,
                "completed_at_ms": 1_250,
            },
        )

    def script(self, call_id: str, *, turn_id: str = "turn-1") -> None:
        self._add(
            "response_item",
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": "exec",
                "input": "text(await tools.mcp__chora__db_query({sql:'SELECT 1'}));",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        )

    def script_output(self, call_id: str, header: str = "Script completed") -> None:
        self._add(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": call_id,
                "output": [{"type": "input_text", "text": f"{header}\nWall time 0.1 seconds\n"}],
            },
        )

    def mcp(
        self,
        item_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        text: str,
        is_error: bool = False,
    ) -> None:
        self._item(
            {
                "type": "McpToolCall",
                "id": item_id,
                "server": server,
                "tool": tool,
                "arguments": arguments,
                "status": "failed" if is_error else "completed",
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
                "duration": {"secs": 1, "nanos": 500_000},
            }
        )

    def raw_item(self, item: dict[str, Any], turn_id: str = "turn-1") -> None:
        self._item(item, turn_id)

    def command(self, item_id: str, script: str, exit_code: int, output: str) -> None:
        self._item(
            {
                "type": "CommandExecution",
                "id": item_id,
                "command": ["/usr/bin/bash", "-lc", script],
                "cwd": "file:///home/alice/proj",
                "source": "unified_exec_startup",
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "aggregated_output": output,
                "duration": {"secs": 0, "nanos": 3_850},
            }
        )

    def function(self, call_id: str, name: str, arguments: str, output: str) -> None:
        self._add(
            "response_item",
            {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
        )
        self._add(
            "response_item",
            {"type": "function_call_output", "call_id": call_id, "output": output},
        )


def _rollout_path(tmp_path: Path, rollout: _Rollout) -> Path:
    base = codex_rollout_records()
    return write_codex_rollout(tmp_path / "sessions", base[:10] + rollout.records + base[10:])


def _convert(tmp_path: Path, rollout: _Rollout) -> dict[str, Any]:
    result, _ = convert_codex_and_audit(_rollout_path(tmp_path, rollout))
    assert result.validation_errors == ()
    return result.trajectory


def _calls(trajectory: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (step["step_id"], call)
        for step in trajectory["steps"]
        for call in step.get("tool_calls") or []
    ]


def _nested(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    return [call for _step, call in _calls(trajectory) if "nested_in" in (call.get("extra") or {})]


def _result(trajectory: dict[str, Any], call_id: str) -> dict[str, Any]:
    for step in trajectory["steps"]:
        for result in (step.get("observation") or {}).get("results") or []:
            if result.get("source_call_id") == call_id:
                return result
    raise AssertionError(call_id)


def _signals(trajectory: dict[str, Any], call_id: str) -> dict[str, Any]:
    extra = _result(trajectory, call_id).get("extra") or {}
    return {key: extra[key] for key in ("is_error", "exit_code") if key in extra}


def _without_nested(trajectory: dict[str, Any]) -> dict[str, Any]:
    """The trajectory with every ``nested_in`` call and its result taken out."""
    stripped = json.loads(json.dumps(trajectory))
    for step in stripped["steps"]:
        calls = step.get("tool_calls") or []
        nested_ids = {c["tool_call_id"] for c in calls if "nested_in" in (c.get("extra") or {})}
        if not nested_ids:
            continue
        step["tool_calls"] = [c for c in calls if c["tool_call_id"] not in nested_ids]
        results = step["observation"]["results"]
        step["observation"]["results"] = [
            r for r in results if r.get("source_call_id") not in nested_ids
        ]
    return stripped


def _one_script() -> _Rollout:
    rollout = _Rollout()
    rollout.script("call_exec")
    rollout.mcp("exec-m1", "chora", "db_query", {"sql": "SELECT 1"}, text='[{"1":1}]')
    rollout.mcp(
        "exec-m2",
        "gateway",
        "web-retrieval_read_url",
        {"url": "https://example.com/missing"},
        text="Upstream HTTP error: 404 Not Found",
        is_error=True,
    )
    rollout.command("exec-c1", "cd /home/alice/proj && false", 2, "boom\n")
    rollout.script_output("call_exec")
    return rollout


class TestNestedCalls:
    def test_one_script_emits_its_three_nested_calls_in_order(self, tmp_path: Path) -> None:
        trajectory = _convert(tmp_path, _one_script())
        steps = {step["step_id"]: step for step in trajectory["steps"]}
        exec_step = next(
            sid for sid, call in _calls(trajectory) if call["tool_call_id"] == "call_exec"
        )
        names = [call["tool_call_id"] for call in steps[exec_step]["tool_calls"]]
        position = names.index("call_exec")
        assert names[position : position + 4] == ["call_exec", "exec-m1", "exec-m2", "exec-c1"]
        result_ids = [r["source_call_id"] for r in steps[exec_step]["observation"]["results"]]
        position = result_ids.index("call_exec")
        assert result_ids[position : position + 4] == ["call_exec", "exec-m1", "exec-m2", "exec-c1"]

        assert _nested(trajectory) == [
            {
                "tool_call_id": "exec-m1",
                "function_name": "mcp__chora__db_query",
                "arguments": {"sql": "SELECT 1"},
                "extra": {"nested_in": "call_exec", "via": "exec", "duration_ms": 1000.5},
            },
            {
                "tool_call_id": "exec-m2",
                "function_name": "mcp__gateway__web-retrieval_read_url",
                "arguments": {"url": "https://example.com/missing"},
                "extra": {"nested_in": "call_exec", "via": "exec", "duration_ms": 1000.5},
            },
            {
                "tool_call_id": "exec-c1",
                "function_name": "exec_command",
                "arguments": {"cmd": "cd /home/alice/proj && false"},
                "extra": {"nested_in": "call_exec", "via": "exec", "duration_ms": 0.004},
            },
        ]
        assert _result(trajectory, "exec-m1") == {
            "source_call_id": "exec-m1",
            "content": '[{"1":1}]',
            "extra": {"is_error": False},
        }
        assert _result(trajectory, "exec-m2") == {
            "source_call_id": "exec-m2",
            "content": "Upstream HTTP error: 404 Not Found",
            "extra": {"is_error": True},
        }
        assert _result(trajectory, "exec-c1") == {
            "source_call_id": "exec-c1",
            "content": "boom\n",
            "extra": {"is_error": True, "exit_code": 2},
        }

    def test_the_exec_result_keeps_its_folded_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking the nested calls out leaves exactly what the pass wrote without them."""
        with_nested = _convert(tmp_path / "with", _one_script())
        assert _signals(with_nested, "call_exec") == {"is_error": True, "exit_code": 2}

        def _no_emit(*_args: object) -> int:
            return 0

        monkeypatch.setattr(result_signals, "_emit_nested_calls", _no_emit)
        without = _convert(tmp_path / "without", _one_script())
        assert _nested(without) == []
        assert _without_nested(with_nested) == without

    def test_overlapping_scripts_emit_no_nested_calls(self, tmp_path: Path) -> None:
        rollout = _Rollout()
        rollout.script("call_one")
        rollout.mcp("exec-m0", "chora", "db_query", {"sql": "SELECT 0"}, text="ok")
        rollout.script("call_two")
        rollout.command("exec-a", "false", 1, "")
        rollout.script_output("call_one")
        rollout.script_output("call_two", "Script failed")
        rollout.script("call_three")
        rollout.command("exec-b", "true", 0, "")
        rollout.script_output("call_three")
        trajectory = _convert(tmp_path, rollout)
        assert [call["tool_call_id"] for call in _nested(trajectory)] == ["exec-b"]
        assert _nested(trajectory)[0]["extra"]["nested_in"] == "call_three"
        assert _signals(trajectory, "call_one") == {}
        assert _signals(trajectory, "call_two") == {"is_error": True}

    def test_an_item_of_a_top_level_call_is_not_duplicated(self, tmp_path: Path) -> None:
        """A non-code-mode command's item carries its call's id, even inside a script's window."""
        rollout = _Rollout()
        rollout.function(
            "call_shell",
            "exec_command",
            '{"cmd":"ls"}',
            "Chunk ID: 1\nWall time: 0.0 seconds\nProcess exited with code 0\nOutput:\n",
        )
        rollout.script("call_exec")
        rollout.command("call_shell", "ls", 0, "")
        # Its call record comes later in the file than its item.
        rollout.command("call_late", "pwd", 0, "/home/alice/proj\n")
        rollout.command("exec-c1", "true", 0, "")
        rollout.script_output("call_exec")
        rollout.function("call_late", "exec_command", '{"cmd":"pwd"}', "Process exited with code 0")
        trajectory = _convert(tmp_path, rollout)
        ids = [call["tool_call_id"] for _step, call in _calls(trajectory)]
        assert ids.count("call_shell") == 1
        assert ids.count("call_late") == 1
        assert [call["tool_call_id"] for call in _nested(trajectory)] == ["exec-c1"]

    def test_an_mcp_call_that_failed_before_any_result(self, tmp_path: Path) -> None:
        rollout = _Rollout()
        rollout.script("call_exec")
        rollout.raw_item(
            {
                "type": "McpToolCall",
                "id": "exec-m1",
                "server": "chora",
                "tool": "list_mcp_resources",
                "arguments": '{"server": "chora"}',
                "status": "failed",
                "error": {"message": "resources/list failed: Method not found"},
            }
        )
        rollout.script_output("call_exec")
        trajectory = _convert(tmp_path, rollout)
        (call,) = _nested(trajectory)
        assert call["function_name"] == "mcp__chora__list_mcp_resources"
        assert call["arguments"] == {"server": "chora"}
        assert call["extra"] == {"nested_in": "call_exec", "via": "exec", "duration_ms": 250.0}
        assert _result(trajectory, "exec-m1") == {
            "source_call_id": "exec-m1",
            "content": "resources/list failed: Method not found",
            "extra": {"is_error": True},
        }

    def test_an_image_in_a_nested_result_goes_to_the_blob_store(self, tmp_path: Path) -> None:
        encoded = base64.b64encode(READ_PNG).decode()
        rollout = _Rollout()
        rollout.script("call_exec")
        rollout.raw_item(
            {
                "type": "McpToolCall",
                "id": "exec-shot",
                "server": "browser",
                "tool": "screenshot",
                "arguments": {},
                "status": "completed",
                "result": {
                    "content": [
                        {"type": "text", "text": "captured"},
                        {"type": "image", "data": encoded, "mimeType": "image/png"},
                    ],
                    "isError": False,
                },
            }
        )
        rollout.script_output("call_exec")
        result, _ = convert_codex_and_audit(_rollout_path(tmp_path, rollout))
        assert result.validation_errors == ()
        assert encoded not in json.dumps(result.trajectory)
        sha = hashlib.sha256(READ_PNG).hexdigest()
        assert [blob.ref.sha256 for blob in result.blobs] == [sha]
        nested = _result(result.trajectory, "exec-shot")
        assert (
            nested["content"] == f"captured\n[image sha256:{sha} image/png {len(READ_PNG)} bytes]"
        )
        assert [image["sha256"] for image in nested["extra"]["images"]] == [sha]
        assert nested["extra"]["is_error"] is False

    def test_the_harbor_port_emits_no_nested_calls(self, tmp_path: Path) -> None:
        """Parity: the port stays harbor's, so the nested calls exist only after the passes."""
        path = _rollout_path(tmp_path, _one_script())
        port = convert_codex_rollout(path)
        assert port is not None
        port_ids = {call.tool_call_id for step in port.steps for call in step.tool_calls or []}
        assert port_ids.isdisjoint({"exec-m1", "exec-m2", "exec-c1"})
        assert "call_exec" in port_ids
