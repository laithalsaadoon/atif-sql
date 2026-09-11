# SPDX-License-Identifier: Apache-2.0

"""The ported Claude Code conversion, on the branches the synthetic fixture misses.

Each test names the harbor 0.22.0 behavior it pins and asserts it directly on
our output. Where the live oracle is still available, the same records are
also written to disk and converted by harbor, so every case here is a parity
case too; when harbor drops the private method the direct assertions remain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from harbor_oracle import diff_paths, harbor_claude_code_trajectory, harbor_has_private_api

from atif_converter.domain import claude_code_conversion
from atif_converter.domain.claude_code_conversion import convert_claude_code_records
from atif_converter.infrastructure.claude_code_converter import (
    convert_claude_code_session,
    discover_side_files,
    read_session_records,
)

SID = "22222222-2222-2222-2222-222222222222"
MODEL = "claude-test-1"


def _user(uuid: str, ts: str, content: Any, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": f"2026-08-22T00:00:{ts}Z",
        "isSidechain": False,
        "message": {"role": "user", "content": content},
        **extra,
    }


def _assistant(
    uuid: str,
    ts: str,
    content: Any,
    *,
    msg_id: str | None = "msg_1",
    model: str | None = MODEL,
    usage: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if msg_id is not None:
        message["id"] = msg_id
    if model is not None:
        message["model"] = model
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": f"2026-08-22T00:00:{ts}Z",
        "isSidechain": False,
        "message": message,
        **extra,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _convert(
    tmp_path: Path,
    main: list[dict[str, Any]],
    side: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Convert via the pure function, then cross-check the file path against harbor."""
    ours = convert_claude_code_records(main, side, fallback_session_id="-p")
    ours_dict = None if ours is None else ours.to_json_dict()

    main_path = tmp_path / "projects" / "-p" / f"{SID}.jsonl"
    _write_jsonl(main_path, main)
    for name, records in (side or {}).items():
        _write_jsonl(main_path.parent / SID / "subagents" / name, records)
    via_files = convert_claude_code_session(main_path)
    via_files_dict = None if via_files is None else via_files.to_json_dict()
    assert via_files_dict == ours_dict, "file path and pure path disagree"

    if harbor_has_private_api("claude-code"):
        theirs = harbor_claude_code_trajectory(main_path)
        if theirs is None or ours_dict is None:
            assert theirs is ours_dict
        else:
            assert diff_paths(theirs, ours_dict) == []
    return ours_dict


class TestReasoning:
    def test_thinking_blocks_join_into_reasoning_content(self, tmp_path: Path) -> None:
        """Thinking/reasoning/analysis blocks strip and join with a blank line; `text` beats `thinking`."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    [
                        {"type": "thinking", "thinking": "  first  "},
                        {"type": "reasoning", "text": "second", "thinking": "ignored"},
                        {"type": "analysis", "thinking": {"nested": 1}},
                        {"type": "text", "text": "answer"},
                    ],
                ),
            ],
        )
        assert out is not None
        step = out["steps"][1]
        assert step["reasoning_content"] == 'first\n\nsecond\n\n{"nested": 1}'
        assert step["message"] == "answer"

    def test_redacted_thinking_openrouter_envelope_decodes_else_dropped(
        self, tmp_path: Path
    ) -> None:
        """OpenRouter's `openrouter.reasoning:<b64>` surfaces as reasoning; real ciphertext vanishes."""
        import base64

        payload = base64.b64encode(
            json.dumps({"text": " plain ", "type": "reasoning.text"}).encode()
        )
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    [
                        {"type": "redacted_thinking", "data": "EncryptedOpaqueBytes=="},
                        {
                            "type": "redacted_thinking",
                            "data": "openrouter.reasoning:" + payload.decode(),
                        },
                        {"type": "redacted_thinking", "data": "openrouter.reasoning:%%%not-base64"},
                        {"type": "text", "text": "answer"},
                    ],
                ),
            ],
        )
        assert out is not None
        assert out["steps"][1]["reasoning_content"] == "plain"
        assert "Encrypted" not in out["steps"][1]["message"]

    def test_code_block_and_unknown_blocks_land_in_text(self, tmp_path: Path) -> None:
        """A `code` block contributes its code; an unknown dict block is json-encoded; a bare string block too."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    [{"type": "code", "code": "print(1)"}, {"type": "weird", "x": 1}, "loose"],
                ),
            ],
        )
        assert out is not None
        assert out["steps"][1]["message"] == 'print(1)\n\n{"type": "weird", "x": 1}\n\nloose'

    def test_string_assistant_content_is_stripped(self, tmp_path: Path) -> None:
        out = _convert(tmp_path, [_user("u1", "01", "go"), _assistant("a1", "02", "  plain  ")])
        assert out is not None
        assert out["steps"][1]["message"] == "plain"


class TestToolResults:
    def test_list_content_and_tool_use_result_formatting(self, tmp_path: Path) -> None:
        """List content is json-encoded per item; toolUseResult renders stdout/stderr/exit_code/metadata chunks."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}],
                ),
                _user(
                    "u2",
                    "03",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "out"}, " ", {"type": "image"}],
                        }
                    ],
                    toolUseResult={
                        "stdout": "o\n",
                        "stderr": "e",
                        "exitCode": 2,
                        "interrupted": True,
                        "isImage": False,
                        "durationMs": 5,
                    },
                ),
            ],
        )
        assert out is not None
        result = out["steps"][1]["observation"]["results"][0]
        assert result["content"] == (
            '{"type": "text", "text": "out"}\n\n{"type": "image"}\n\n'
            "[stdout]\no\n[stderr]\ne\n[exit_code] 2\n[interrupted] True\n"
            '[metadata] {"durationMs": 5}'
        )
        assert result["extra"]["tool_result_metadata"]["tool_use_result"]["exitCode"] == 2
        assert "tool_result_is_error" not in result["extra"]

    def test_is_error_appends_failure_marker_even_without_tool_use_result(
        self, tmp_path: Path
    ) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]
                ),
                _user(
                    "u2",
                    "03",
                    [{"type": "tool_result", "tool_use_id": "t1", "content": "", "is_error": True}],
                ),
            ],
        )
        assert out is not None
        result = out["steps"][1]["observation"]["results"][0]
        assert result["content"] == "[error] tool reported failure"
        assert result["extra"]["tool_result_metadata"]["is_error"] is True
        assert result["extra"]["tool_result_is_error"] is True

    def test_empty_tool_result_leaves_call_without_observation(self, tmp_path: Path) -> None:
        """A result that formats to nothing sets output None, so the step has tool_calls but no observation."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]
                ),
                _user("u2", "03", [{"type": "tool_result", "tool_use_id": "t1", "content": "  "}]),
            ],
        )
        assert out is not None
        assert "observation" not in out["steps"][1]
        assert out["steps"][1]["tool_calls"][0]["extra"] == {
            "raw_arguments": {},
            "tool_use_name": "Read",
        }

    def test_non_dict_tool_input_is_wrapped(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", [{"type": "tool_use", "id": "t1", "name": "Echo", "input": "raw"}]
                ),
            ],
        )
        assert out is not None
        call = out["steps"][1]["tool_calls"][0]
        assert call["arguments"] == {"input": "raw"}
        assert call["extra"]["raw_arguments"] == "raw"

    def test_orphan_tool_result_with_name_becomes_its_own_agent_step(self, tmp_path: Path) -> None:
        """No pending tool_use: a named orphan is a standalone tool_call step; a nameless one is dropped."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _user(
                    "u2",
                    "02",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "gone",
                            "content": "late",
                            "name": "Bash",
                        },
                        {"type": "tool_result", "tool_use_id": "nameless", "content": "x"},
                    ],
                    isSidechain=True,
                    toolUseResult={"stdout": "late"},
                ),
            ],
        )
        assert out is not None
        assert len(out["steps"]) == 2
        orphan = out["steps"][1]
        assert orphan["source"] == "agent"
        assert orphan["tool_calls"] == [
            {"tool_call_id": "gone", "function_name": "Bash", "arguments": {}}
        ]
        assert orphan["observation"]["results"][0]["content"] == "late\n\n[stdout]\nlate"
        assert orphan["extra"]["is_sidechain"] is True
        # The same metadata dict lands twice on an orphan: as the step's
        # `metadata` (via _convert_event_to_step) and as `tool_result_metadata`.
        assert orphan["extra"]["metadata"]["raw_tool_result"]["tool_use_id"] == "gone"
        assert orphan["extra"]["tool_result_metadata"] == orphan["extra"]["metadata"]
        assert "model_name" not in orphan

    def test_orphan_without_tool_use_result_carries_no_metadata(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _user(
                    "u2",
                    "02",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "gone",
                            "content": "late",
                            "name": "Bash",
                        }
                    ],
                ),
            ],
        )
        assert out is not None
        assert out["steps"][1]["extra"] == {"is_sidechain": False}

    def test_duplicate_tool_use_and_result_after_compaction_are_dropped(
        self, tmp_path: Path
    ) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
                ),
                _user(
                    "u2", "03", [{"type": "tool_result", "tool_use_id": "t1", "content": "first"}]
                ),
                _assistant(
                    "a2",
                    "04",
                    [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                    msg_id="msg_2",
                ),
                _user(
                    "u3",
                    "05",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "again",
                            "name": "Bash",
                        }
                    ],
                ),
            ],
        )
        assert out is not None
        assert [s["source"] for s in out["steps"]] == ["user", "agent", "agent"]
        assert out["steps"][1]["observation"]["results"][0]["content"] == "first"
        assert "tool_calls" not in out["steps"][2]

    def test_user_record_mixing_text_and_tool_result_feeds_both_steps(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
                ),
                _user(
                    "u2",
                    "03",
                    [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                        {"type": "text", "text": "  and a note  "},
                        {"type": "image", "source": {}},
                    ],
                ),
            ],
        )
        assert out is not None
        assert out["steps"][1]["observation"]["results"][0]["content"] == "ok"
        assert out["steps"][2]["message"] == '  and a note  \n\n{"type": "image", "source": {}}'


class TestTurnBundling:
    def test_same_message_id_across_events_is_one_step_with_last_usage(
        self, tmp_path: Path
    ) -> None:
        """Events sharing message.id merge: text joins, tool_uses bundle, the LAST usage lands on the step."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    [{"type": "text", "text": "part one"}],
                    usage={"input_tokens": 1, "output_tokens": 1},
                    stop_reason=None,
                ),
                _assistant(
                    "a2",
                    "03",
                    [{"type": "tool_use", "id": "t1", "name": "A", "input": {}}],
                    usage={"input_tokens": 1, "output_tokens": 7, "cache_read_input_tokens": None},
                ),
                _assistant(
                    "a3",
                    "04",
                    [
                        {"type": "tool_use", "id": "t2", "name": "B", "input": {}},
                        {"type": "text", "text": "two"},
                    ],
                ),
            ],
        )
        assert out is not None
        assert len(out["steps"]) == 2
        step = out["steps"][1]
        assert step["timestamp"] == "2026-08-22T00:00:02Z"
        assert step["message"] == "part one\n\ntwo"
        assert [c["function_name"] for c in step["tool_calls"]] == ["A", "B"]
        assert step["metrics"] == {
            "prompt_tokens": 1,
            "completion_tokens": 7,
            "cached_tokens": 0,
            "extra": {"cache_read_input_tokens": None},
        }
        assert out["final_metrics"]["total_completion_tokens"] == 7

    def test_null_output_tokens_count_as_zero(self, tmp_path: Path) -> None:
        """An interrupted stream leaves `output_tokens: null`; harbor reads it as 0, not a TypeError."""
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    "x",
                    usage={"input_tokens": 3, "output_tokens": None, "service_tier": "standard"},
                ),
            ],
        )
        assert out is not None
        assert out["steps"][1]["metrics"]["completion_tokens"] == 0
        assert out["final_metrics"]["extra"]["service_tiers"] == ["standard"]

    def test_usage_absent_yields_no_metrics_and_no_totals(self, tmp_path: Path) -> None:
        out = _convert(tmp_path, [_user("u1", "01", "go"), _assistant("a1", "02", "x")])
        assert out is not None
        assert "metrics" not in out["steps"][1]
        assert out["final_metrics"] == {"total_steps": 2}

    def test_no_message_id_makes_one_step_per_event(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant("a1", "02", "one", msg_id=None),
                _assistant("a2", "03", "two", msg_id=None),
            ],
        )
        assert out is not None
        assert [s["message"] for s in out["steps"]] == ["go", "one", "two"]

    def test_event_extras_are_copied_onto_the_agent_step(self, tmp_path: Path) -> None:
        """requestId is read off the MESSAGE, id/agent_id/cwd/userType off the EVENT; external userType is dropped."""
        record = _assistant(
            "a1", "02", "x", id="ev-1", agent_id="ag-1", cwd="/w", userType="internal"
        )
        record["message"]["requestId"] = "req-1"
        record["message"]["stop_sequence"] = "STOP"
        out = _convert(tmp_path, [_user("u1", "01", "go", userType="external"), record])
        assert out is not None
        assert out["steps"][1]["extra"] == {
            "stop_sequence": "STOP",
            "requestId": "req-1",
            "id": "ev-1",
            "agent_id": "ag-1",
            "cwd": "/w",
            "user_type": "internal",
            "is_sidechain": False,
        }
        assert out["agent"]["extra"] == {"cwds": ["/w"]}


class TestSidechains:
    def test_main_chain_model_wins_over_an_earlier_sidechain_model(self, tmp_path: Path) -> None:
        """agent.model_name and the model-less fallback come from the main chain even when a sidechain is first."""
        side = [
            {
                **_assistant("s1", "00.5", "sub", model="claude-small", msg_id="m_s"),
                "isSidechain": True,
            },
        ]
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant("a1", "02", "main"),
                _assistant("a2", "03", "later", model=None, msg_id="m2"),
            ],
            {"agent-x.jsonl": side},
        )
        assert out is not None
        assert out["agent"]["model_name"] == MODEL
        assert [s["model_name"] for s in out["steps"] if s["source"] == "agent"] == [
            "claude-small",
            MODEL,
            MODEL,
        ]
        assert out["steps"][0]["extra"]["is_sidechain"] is True

    def test_sidechain_model_is_the_fallback_when_main_has_none(self, tmp_path: Path) -> None:
        side = [
            {
                **_assistant("s1", "05", "sub", model="claude-small", msg_id="m_s"),
                "isSidechain": True,
            }
        ]
        out = _convert(
            tmp_path,
            [_user("u1", "01", "go"), _assistant("a1", "02", "main", model=None)],
            {"agent-x.jsonl": side},
        )
        assert out is not None
        assert out["agent"]["model_name"] == "claude-small"
        assert out["steps"][1]["model_name"] == "claude-small"

    def test_agent_ids_and_branches_collect_into_agent_extra(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go", gitBranch="main", agentId=""),
                _assistant("a1", "02", "x", agentId="ag-9", gitBranch=""),
            ],
        )
        assert out is not None
        assert out["agent"]["extra"] == {"git_branches": ["main"], "agent_ids": ["ag-9"]}


class TestRecordFiltering:
    def test_compact_summary_is_a_plain_user_step_and_summary_records_vanish(
        self, tmp_path: Path
    ) -> None:
        """harbor keeps the compact summary text as a user step with no marker; `summary` records have no message."""
        out = _convert(
            tmp_path,
            [
                {"type": "summary", "uuid": "sum-1", "summary": "title", "leafUuid": "u1"},
                _user("u1", "01", "This session is being continued...", isCompactSummary=True),
                _assistant("a1", "02", "x"),
            ],
        )
        assert out is not None
        assert [s["source"] for s in out["steps"]] == ["user", "agent"]
        assert out["steps"][0]["extra"] == {"is_sidechain": False}
        assert "is_compact_summary" not in out["steps"][0]["extra"]

    def test_whitespace_only_user_content_is_dropped_but_raw_bytes_survive(
        self, tmp_path: Path
    ) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u0", "00", "   \n"),
                _user("u1", "01", "  keep me  \n"),
                _assistant("a1", "02", "x"),
            ],
        )
        assert out is not None
        assert out["steps"][0]["message"] == "  keep me  \n"
        assert len(out["steps"]) == 2

    def test_non_string_non_list_user_content_is_json_encoded(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [_user("u1", "01", {"k": 1}), _user("u2", "02", 0), _assistant("a1", "03", "x")],
        )
        assert out is not None
        assert [s["message"] for s in out["steps"]] == ['{"k": 1}', "0", "x"]

    def test_duplicate_uuid_keeps_first_in_read_order(self, tmp_path: Path) -> None:
        """Dedup is by uuid, first occurrence wins; side-files are read in sorted-name order after the main file."""
        first = _user("dup", "05", "from main")
        from_b = _user("dup", "05", "from b")
        from_a = _user("dup", "05", "from a")
        out = _convert(
            tmp_path,
            [_user("u1", "01", "go"), first, _assistant("a1", "02", "x")],
            {"b.jsonl": [from_b], "a.jsonl": [from_a]},
        )
        assert out is not None
        assert [s["message"] for s in out["steps"]] == ["go", "x", "from main"]

        without_main_copy = convert_claude_code_records(
            [_user("u1", "01", "go"), _assistant("a1", "02", "x")],
            {"b.jsonl": [from_b], "a.jsonl": [from_a]},
        )
        assert without_main_copy is not None
        assert without_main_copy.steps[-1].message == "from a"

    def test_records_without_a_message_dict_produce_none(self, tmp_path: Path) -> None:
        assert (
            _convert(
                tmp_path, [{"type": "attachment", "uuid": "x", "timestamp": "2026-01-01T00:00:00Z"}]
            )
            is None
        )
        assert _convert(tmp_path, []) is None

    def test_session_id_and_version_fall_back(self, tmp_path: Path) -> None:
        """No sessionId -> the staging dir name; no version -> "unknown"."""
        records = [_user("u1", "01", "go"), _assistant("a1", "02", "x")]
        for r in records:
            del r["sessionId"]
        out = _convert(tmp_path, records)
        assert out is not None
        assert out["session_id"] == "-p"
        assert out["agent"]["version"] == "unknown"


class TestCost:
    def test_no_model_anywhere_means_no_cost_and_no_cost_source(self, tmp_path: Path) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", "x", model=None, usage={"input_tokens": 5, "output_tokens": 1}
                ),
            ],
        )
        assert out is not None
        assert "model_name" not in out["agent"]
        assert "model_name" not in out["steps"][1]
        assert "total_cost_usd" not in out["final_metrics"]
        assert "cost_source" not in out["final_metrics"].get("extra", {})

    def test_configured_model_name_fills_the_gap(self) -> None:
        """harbor's self.model_name is the last fallback for agent.model_name and each agent step."""
        out = convert_claude_code_records(
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", "x", model=None, usage={"input_tokens": 5, "output_tokens": 1}
                ),
            ],
            model_name="claude-configured",
        )
        assert out is not None
        assert out.agent.model_name == "claude-configured"
        assert out.steps[1].model_name == "claude-configured"

    def test_zero_token_steps_are_skipped_and_priced_steps_set_cost_source(
        self, tmp_path: Path
    ) -> None:
        out = _convert(
            tmp_path,
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1", "02", "x", model=None, usage={"input_tokens": 0, "output_tokens": 0}
                ),
                _assistant(
                    "a2", "03", "y", msg_id="m2", usage={"input_tokens": 5, "output_tokens": 1}
                ),
            ],
        )
        assert out is not None
        assert out["final_metrics"]["extra"]["cost_source"] == "litellm_estimate"
        assert isinstance(out["final_metrics"]["total_cost_usd"], float)

    def test_litellm_error_or_absence_yields_no_cost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records = [
            _user("u1", "01", "go"),
            _assistant("a1", "02", "x", usage={"input_tokens": 5, "output_tokens": 1}),
        ]
        import litellm

        def boom(**_: Any) -> tuple[float, float]:
            msg = "no such model"
            raise ValueError(msg)

        monkeypatch.setattr(litellm, "cost_per_token", boom)
        out = convert_claude_code_records(records)
        assert out is not None
        assert out.final_metrics is not None
        assert out.final_metrics.total_cost_usd is None
        assert out.final_metrics.extra is None

        monkeypatch.setitem(sys.modules, "litellm", None)
        out = convert_claude_code_records(records)
        assert out is not None
        assert out.final_metrics is not None
        assert out.final_metrics.total_cost_usd is None

    def test_cache_totals_come_from_metrics_extra(self) -> None:
        """Only int-typed cache fields add to the totals; a float lands in the step but not the sum."""
        out = convert_claude_code_records(
            [
                _user("u1", "01", "go"),
                _assistant(
                    "a1",
                    "02",
                    "x",
                    usage={
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 4,
                        "cache_read_input_tokens": 7.0,
                    },
                ),
            ]
        )
        assert out is not None
        assert out.final_metrics is not None
        assert out.final_metrics.extra is not None
        assert out.final_metrics.extra["total_cache_creation_input_tokens"] == 4
        assert "total_cache_read_input_tokens" not in out.final_metrics.extra


class TestStepConversion:
    def test_unsupported_kind_and_incomplete_tool_call_raise(self) -> None:
        with pytest.raises(ValueError, match="Unsupported event kind"):
            claude_code_conversion._convert_event_to_step({"kind": "nope"}, 1, None)
        with pytest.raises(ValueError, match="missing call_id"):
            claude_code_conversion._convert_event_to_step(
                {"kind": "tool_call", "call_id": "", "tool_name": "x"}, 1, None
            )

    def test_message_kind_maps_roles_to_sources(self) -> None:
        convert = claude_code_conversion._convert_event_to_step
        assert (
            convert(
                {"kind": "message", "role": "assistant", "text": "a", "model_name": "m"}, 1, None
            ).source
            == "agent"
        )
        assert convert({"kind": "message", "role": "user", "text": "u"}, 1, None).source == "user"
        system = convert({"kind": "message", "role": "tool", "text": "s"}, 1, "cfg")
        assert system.source == "system"
        assert system.model_name is None
        assert system.llm_call_count is None
        assert (
            convert({"kind": "message", "role": "assistant", "text": "a"}, 1, "cfg").model_name
            == "cfg"
        )


class TestFileReader:
    def test_malformed_lines_are_skipped_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text('{"a": 1}\n\nnot json\n   \n{"b": 2}\n')
        assert read_session_records(path) == [{"a": 1}, {"b": 2}]

    def test_side_file_names_flatten_like_the_old_staging(self, tmp_path: Path) -> None:
        main = tmp_path / "-p" / f"{SID}.jsonl"
        _write_jsonl(main, [])
        side_dir = main.parent / SID
        _write_jsonl(side_dir / "subagents" / "agent-a.jsonl", [])
        _write_jsonl(side_dir / "subagents" / "workflows" / "wf_1" / "agent-b.jsonl", [])
        _write_jsonl(side_dir / "loose.jsonl", [])
        (side_dir / "subagents" / "agent-a.meta.json").write_text("{}")
        assert sorted(discover_side_files(main)) == [
            "agent-a.jsonl",
            "loose.jsonl",
            "workflows__wf_1__agent-b.jsonl",
        ]
        assert discover_side_files(main, include_subagents=False) == {}

    def test_include_subagents_false_reads_only_the_main_file(self, tmp_path: Path) -> None:
        main = tmp_path / "-p" / f"{SID}.jsonl"
        _write_jsonl(main, [_user("u1", "01", "go"), _assistant("a1", "02", "x")])
        _write_jsonl(main.parent / SID / "subagents" / "agent-a.jsonl", [_user("s1", "03", "sub")])
        with_side = convert_claude_code_session(main)
        without = convert_claude_code_session(main, include_subagents=False)
        assert with_side is not None
        assert without is not None
        assert len(with_side.steps) == 3
        assert len(without.steps) == 2
