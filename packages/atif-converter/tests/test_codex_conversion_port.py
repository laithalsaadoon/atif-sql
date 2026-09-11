# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ported Codex converter, on the branches the fixture misses.

The synthetic rollout in ``codex_fixtures.py`` walks one path through
harbor's converter; the goldens pin that path. Every test here pins one
branch that path does not reach — compaction records, reasoning with a real
summary, web searches and custom tool calls, the output-blob shapes, the
``token_count`` grouping rules, a rollout with no model, a model LiteLLM
cannot price — and names the harbor 0.22.0 behavior it holds still.

Each test asserts the behavior directly, so it can fail on its own after
harbor drops the private method. While that method is still there, the
``_convert`` helper ALSO diffs our trajectory against harbor's for the same
records, which is what makes each pinned expectation a measured one rather
than a reading of harbor's source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from codex_fixtures import (
    CODEX_CLI_VERSION,
    CODEX_MODEL,
    CODEX_SESSION_ID,
    codex_rollout_records,
    write_codex_rollout,
)
from harbor_oracle import diff_paths, harbor_codex_trajectory, harbor_has_private_api

from atif_converter.domain.codex_conversion import convert_codex_records
from atif_converter.infrastructure.codex_converter import convert_codex_rollout

#: A model LiteLLM's pricing table knows, for the cost branches.
PRICED_MODEL = "gpt-4o"
#: A model no pricing table will ever carry.
UNPRICED_MODEL = "nonexistent-provider/no-such-model-xyz"

_TS = "2026-09-11T17:27:02.000Z"


def _session_meta() -> dict[str, Any]:
    return codex_rollout_records()[0]


def _turn_context(model: str | None = CODEX_MODEL, turn_id: str = "turn-1") -> dict[str, Any]:
    payload: dict[str, Any] = {"turn_id": turn_id, "cwd": "/home/alice/proj"}
    if model is not None:
        payload["model"] = model
    return {"timestamp": _TS, "type": "turn_context", "payload": payload}


def _item(payload: dict[str, Any], ts: str = _TS) -> dict[str, Any]:
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def _event(payload: dict[str, Any], ts: str = _TS) -> dict[str, Any]:
    return {"timestamp": ts, "type": "event_msg", "payload": payload}


def _message(role: str | None, text: str, ts: str = _TS) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "message",
        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
    }
    if role is not None:
        payload["role"] = role
    return _item(payload, ts)


def _user(text: str, ts: str = _TS) -> dict[str, Any]:
    return _message("user", text, ts)


def _assistant(text: str, ts: str = _TS) -> dict[str, Any]:
    return _message("assistant", text, ts)


def _function_call(
    call_id: str | None, name: str, arguments: Any, ts: str = _TS, status: str = "completed"
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "function_call", "name": name, "status": status}
    if call_id is not None:
        payload["call_id"] = call_id
    if arguments is not None:
        payload["arguments"] = arguments
    return _item(payload, ts)


def _function_call_output(call_id: str, output: Any, ts: str = _TS, **more: Any) -> dict[str, Any]:
    return _item({"type": "function_call_output", "call_id": call_id, "output": output, **more}, ts)


def _token_count(
    input_tokens: int,
    output_tokens: int,
    *,
    cached: int = 0,
    cache_write: int = 0,
    total_override: dict[str, Any] | None = None,
    info_extra: dict[str, Any] | None = None,
    ts: str = _TS,
) -> dict[str, Any]:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }
    info: dict[str, Any] = {
        "total_token_usage": total_override if total_override is not None else dict(usage),
        "last_token_usage": dict(usage),
        "model_context_window": 258_400,
    }
    if info_extra:
        info.update(info_extra)
    return _event({"type": "token_count", "info": info}, ts)


def _convert(
    tmp_path: Path, records: list[dict[str, Any]], *, cross_check: bool = True
) -> dict[str, Any] | None:
    """Write ``records`` as a rollout, convert with our port, cross-check with harbor.

    The cross-check runs only while harbor still ships the private method; a
    test that turns it off says why in its body.
    """
    rollout = write_codex_rollout(tmp_path / "sessions", records)
    ours = convert_codex_rollout(rollout)
    ours_dict = None if ours is None else ours.to_json_dict()
    if cross_check and harbor_has_private_api("codex"):
        theirs = harbor_codex_trajectory(rollout)
        if theirs is None or ours_dict is None:
            assert theirs is ours_dict, f"harbor={theirs!r} ours={ours_dict!r}"
        else:
            assert diff_paths(theirs, ours_dict) == []
    return ours_dict


def _steps(trajectory: dict[str, Any] | None) -> list[dict[str, Any]]:
    assert trajectory is not None
    steps = trajectory["steps"]
    assert isinstance(steps, list)
    return steps


def _agent_steps(trajectory: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [s for s in _steps(trajectory) if s["source"] == "agent"]


class TestDroppedRecords:
    def test_compacted_records_produce_no_step(self, tmp_path: Path) -> None:
        """``compacted`` is not a ``response_item``, so harbor never looks inside it."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("hello"),
            {
                "timestamp": _TS,
                "type": "compacted",
                "payload": {"message": "summary so far", "replacement_history": [_user("x")]},
            },
            _assistant("after compaction"),
            _token_count(10, 2),
        ]
        steps = _steps(_convert(tmp_path, records))
        assert [s["source"] for s in steps] == ["user", "agent"]
        assert steps[1]["message"] == "after compaction"

    def test_function_call_without_call_id_is_skipped(self, tmp_path: Path) -> None:
        """harbor ``continue``s on a falsy ``call_id`` before recording model output."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("go"),
            _function_call(None, "exec_command", '{"cmd":"ls"}'),
            _token_count(10, 2),  # no model output seen -> does NOT close a call
            _assistant("done"),
            _token_count(20, 3),
        ]
        steps = _steps(_convert(tmp_path, records))
        assert len(steps) == 2
        assert steps[1]["extra"]["api_call_id"] == "api_call_1"
        assert steps[1]["metrics"]["prompt_tokens"] == 20

    def test_message_content_not_a_list_yields_empty_text(self, tmp_path: Path) -> None:
        """``_extract_message_text`` runs only on a list; anything else is ``""``."""
        records = [
            _session_meta(),
            _item({"type": "message", "role": "user", "content": "a bare string"}),
        ]
        steps = _steps(_convert(tmp_path, records))
        assert steps == [{"step_id": 1, "timestamp": _TS, "source": "user", "message": ""}]


class TestReasoning:
    def test_summary_text_attaches_to_the_next_assistant_message(self, tmp_path: Path) -> None:
        """String and ``{"text": ...}`` summary items join with a newline."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _item(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "first"}, "second", {"x": 1}],
                    "encrypted_content": "opaque",
                }
            ),
            _assistant("a"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["reasoning_content"] == "first\nsecond"

    def test_encrypted_reasoning_with_empty_summary_is_dropped(self, tmp_path: Path) -> None:
        """Only ``summary`` is read; ``encrypted_content`` never reaches a step."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _item({"type": "reasoning", "summary": [], "encrypted_content": "opaque"}),
            _assistant("a"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert "reasoning_content" not in agent

    def test_a_user_message_consumes_pending_reasoning(self, tmp_path: Path) -> None:
        """Every ``message`` clears ``pending_reasoning``, whatever its role."""
        records = [
            _session_meta(),
            _turn_context(),
            _item({"type": "reasoning", "summary": ["thinking"]}),
            _user("interjection"),
            _assistant("a"),
            _token_count(10, 2),
        ]
        steps = _steps(_convert(tmp_path, records))
        assert "reasoning_content" not in steps[0]
        assert "reasoning_content" not in steps[1]

    def test_reasoning_before_a_tool_call_lands_on_the_bundled_step(self, tmp_path: Path) -> None:
        """A tool call carries the reasoning into its group; the group keeps the first."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _item({"type": "reasoning", "summary": ["plan"]}),
            _function_call("c1", "exec_command", '{"cmd":"ls"}'),
            _function_call_output("c1", "ok"),
            _item({"type": "reasoning", "summary": ["second thought"]}),
            _function_call("c2", "exec_command", '{"cmd":"pwd"}'),
            _function_call_output("c2", "/"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["reasoning_content"] == "plan"
        assert [tc["tool_call_id"] for tc in agent["tool_calls"]] == ["c1", "c2"]


class TestToolCallShapes:
    def test_web_search_call_has_empty_id_and_null_observation(self, tmp_path: Path) -> None:
        """``web_search_call`` becomes a tool call with ``call_id == ""`` and no output."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _item(
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "query": "harbor atif", "url": "https://x"},
                }
            ),
            _assistant("found it"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["tool_calls"] == [
            {
                "tool_call_id": "",
                "function_name": "web_search_call",
                "arguments": {"action_type": "search", "query": "harbor atif", "url": "https://x"},
            }
        ]
        # source_call_id "" -> None and content None: an empty result object.
        assert agent["observation"] == {"results": [{}]}
        assert agent["extra"]["tool_call_details"] == {"": {"status": "completed"}}
        assert agent["message"] == "found it"

    def test_custom_tool_call_input_string_becomes_the_input_argument(self, tmp_path: Path) -> None:
        """A non-JSON ``input`` is wrapped as ``{"input": raw}``; the raw string is kept."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _item(
                {
                    "type": "custom_tool_call",
                    "call_id": "cu1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch",
                    "status": "completed",
                }
            ),
            _item({"type": "custom_tool_call_output", "call_id": "cu1", "output": "Done!"}),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["tool_calls"] == [
            {
                "tool_call_id": "cu1",
                "function_name": "apply_patch",
                "arguments": {"input": "*** Begin Patch\n*** End Patch"},
            }
        ]
        assert agent["observation"]["results"] == [{"source_call_id": "cu1", "content": "Done!"}]
        assert agent["extra"]["tool_call_details"]["cu1"] == {
            "raw_arguments": "*** Begin Patch\n*** End Patch",
            "item_type": "custom_tool_call",
            "status": "completed",
        }

    @pytest.mark.parametrize(
        ("raw_arguments", "expected"),
        [
            ('{"cmd":"ls"}', {"cmd": "ls"}),
            ("[1, 2]", {"value": [1, 2]}),
            ('"just text"', {"value": "just text"}),
            ("null", {}),
            ("not json at all", {"input": "not json at all"}),
            (None, {}),
        ],
    )
    def test_function_call_arguments_shapes(
        self, tmp_path: Path, raw_arguments: str | None, expected: dict[str, Any]
    ) -> None:
        """JSON objects pass through; other JSON wraps in ``value``; non-JSON in ``input``."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _function_call("c1", "tool", raw_arguments),
            _function_call_output("c1", "ok"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["tool_calls"][0]["arguments"] == expected

    @pytest.mark.parametrize(
        ("raw_output", "content", "metadata"),
        [
            ('{"output":"x","metadata":{"exit_code":1}}', "x", {"exit_code": 1}),
            ('{"metadata":{"a":1}}', '{"metadata": {"a": 1}}', {"a": 1}),
            ('{"foo": 1}', '{"foo": 1}', None),
            ("{}", None, None),
            ("[1, 2]", "[1, 2]", None),
            ("42", "42", None),
            ("plain text", "plain text", None),
            (None, None, None),
        ],
    )
    def test_function_call_output_blob_shapes(
        self, tmp_path: Path, raw_output: str | None, content: str | None, metadata: Any
    ) -> None:
        """``_parse_output_blob``: object -> output/metadata, scalar -> str, text -> itself."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _function_call("c1", "tool", "{}"),
            _function_call_output("c1", raw_output),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        result = agent["observation"]["results"][0]
        assert result.get("content") == content
        details = agent["extra"]["tool_call_details"]["c1"]
        assert details.get("metadata") == metadata

    def test_orphan_output_synthesizes_a_call_with_empty_arguments(self, tmp_path: Path) -> None:
        """An output whose call was never seen becomes a call named from the output."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _assistant(""),
            _function_call_output("ghost", "spooky", name="shell"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["tool_calls"] == [
            {"tool_call_id": "ghost", "function_name": "shell", "arguments": {}}
        ]
        assert agent["observation"]["results"] == [{"source_call_id": "ghost", "content": "spooky"}]
        assert "tool_call_details" not in agent["extra"]

    def test_tool_calls_sort_by_call_order_not_output_order(self, tmp_path: Path) -> None:
        """``tool_order`` is assigned at the CALL; reversed outputs reorder only the timestamp."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _function_call("a", "tool", '{"n":1}', ts="2026-09-11T17:27:02.100Z"),
            _function_call("b", "tool", '{"n":2}', ts="2026-09-11T17:27:02.200Z"),
            _function_call_output("b", "B"),
            _function_call_output("a", "A"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert [tc["tool_call_id"] for tc in agent["tool_calls"]] == ["a", "b"]
        assert [r["content"] for r in agent["observation"]["results"]] == ["A", "B"]
        # The group's timestamp is the first NORMALIZED event's, and a call is
        # normalized when its OUTPUT arrives: b's output came first, so the
        # step carries b's call timestamp even though a sorts first.
        assert agent["timestamp"] == "2026-09-11T17:27:02.200Z"


class TestApiCallGrouping:
    def test_token_count_closes_a_call_only_after_model_output(self, tmp_path: Path) -> None:
        """A ``token_count`` before any assistant output leaves the counter at 1."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _token_count(5, 0),
            _assistant("one"),
            _token_count(10, 1),
            _assistant("two"),
            _token_count(20, 2),
        ]
        agent_steps = _agent_steps(_convert(tmp_path, records))
        assert [s["extra"]["api_call_id"] for s in agent_steps] == ["api_call_1", "api_call_2"]
        assert [s["metrics"]["prompt_tokens"] for s in agent_steps] == [10, 20]
        assert [s["metrics"]["completion_tokens"] for s in agent_steps] == [1, 2]

    def test_token_count_without_usage_still_advances_the_counter(self, tmp_path: Path) -> None:
        """No ``last_token_usage`` means no metrics for that call, but the call still closes."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _assistant("one"),
            _event({"type": "token_count", "info": None}),
            _assistant("two"),
            _token_count(20, 2),
        ]
        agent_steps = _agent_steps(_convert(tmp_path, records))
        assert [s["extra"]["api_call_id"] for s in agent_steps] == ["api_call_1", "api_call_2"]
        assert "metrics" not in agent_steps[0]
        assert agent_steps[1]["metrics"]["prompt_tokens"] == 20

    def test_assistant_messages_in_one_call_join_with_a_blank_line(self, tmp_path: Path) -> None:
        """Bundled message parts join on ``"\\n\\n"``; empty parts are filtered."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _assistant("first"),
            _assistant(""),
            _assistant("second"),
            _token_count(10, 2),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert agent["message"] == "first\n\nsecond"

    def test_output_after_token_count_stays_with_its_calls_request(self, tmp_path: Path) -> None:
        """A tool call's normalized event carries the request id current at the CALL."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _function_call("c1", "tool", "{}", ts="2026-09-11T17:27:02.100Z"),
            _token_count(10, 1),
            _function_call_output("c1", "late", ts="2026-09-11T17:27:09.000Z"),
            _assistant("done"),
            _token_count(20, 2),
        ]
        agent_steps = _agent_steps(_convert(tmp_path, records))
        assert len(agent_steps) == 2
        first, second = agent_steps
        assert first["extra"]["api_call_id"] == "api_call_1"
        assert first["metrics"]["prompt_tokens"] == 10
        assert first["timestamp"] == "2026-09-11T17:27:02.100Z"
        assert first["observation"]["results"] == [{"source_call_id": "c1", "content": "late"}]
        assert second["extra"]["api_call_id"] == "api_call_2"
        assert second["metrics"]["prompt_tokens"] == 20

    def test_zero_cached_tokens_are_omitted_but_zero_cache_write_is_kept(
        self, tmp_path: Path
    ) -> None:
        """``cached_tokens`` uses ``or None``; ``cache_write_input_tokens`` uses ``is not None``."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _assistant("a"),
            _token_count(10, 2, cached=0, cache_write=0),
        ]
        (agent,) = _agent_steps(_convert(tmp_path, records))
        assert "cached_tokens" not in agent["metrics"]
        assert agent["metrics"]["extra"]["cache_write_input_tokens"] == 0


class TestTurnIds:
    def test_turn_id_lifecycle(self, tmp_path: Path) -> None:
        """``task_started`` sets it, ``turn_context`` only fills a gap, ``task_complete`` clears."""
        records = [
            _session_meta(),
            _event({"type": "task_started", "turn_id": "turn-A"}),
            _turn_context(turn_id="turn-ignored"),
            _user("q"),
            _assistant("a"),
            _token_count(10, 1),
            _event({"type": "task_complete", "turn_id": "turn-A"}),
            _assistant("orphan"),
            _token_count(20, 2),
            _turn_context(turn_id="turn-B"),
            _assistant("b"),
            _token_count(30, 3),
        ]
        agent_steps = _agent_steps(_convert(tmp_path, records))
        assert [s["extra"].get("codex_turn_id") for s in agent_steps] == ["turn-A", None, "turn-B"]


class TestModelAndCost:
    def test_missing_model_leaves_model_name_and_cost_unset(self, tmp_path: Path) -> None:
        """No ``turn_context`` model and no ``model_name``: nothing to stamp, nothing to price."""
        records = [
            _session_meta(),
            _turn_context(model=None),
            _user("q"),
            _assistant("a"),
            _token_count(10, 2),
        ]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        assert "model_name" not in trajectory["agent"]
        (agent,) = _agent_steps(trajectory)
        assert "model_name" not in agent
        assert "cost_usd" not in agent["metrics"]
        assert "total_cost_usd" not in trajectory["final_metrics"]

    def test_model_name_parameter_is_harbors_self_model_name(self) -> None:
        """The parameter fills in when ``turn_context`` names no model, and prices calls.

        Domain-only: the oracle constructs ``Codex`` without a ``model_name``,
        so there is nothing to cross-check against.
        """
        records = [
            _session_meta(),
            _user("q"),
            _assistant("a"),
            _token_count(10, 2),
        ]
        trajectory = convert_codex_records(
            records, fallback_session_id="11", model_name=PRICED_MODEL
        )
        assert trajectory is not None
        assert trajectory.agent.model_name == PRICED_MODEL
        (agent,) = [s for s in trajectory.steps if s.source == "agent"]
        assert agent.model_name == PRICED_MODEL
        assert agent.metrics is not None
        assert isinstance(agent.metrics.cost_usd, float)

    def test_turn_context_model_wins_over_the_parameter(self) -> None:
        """harbor reads ``self.model_name`` only when no ``turn_context`` carries a model."""
        records = [_session_meta(), _turn_context(), _user("q"), _assistant("a")]
        trajectory = convert_codex_records(
            records, fallback_session_id="11", model_name=PRICED_MODEL
        )
        assert trajectory is not None
        assert trajectory.agent.model_name == CODEX_MODEL

    def test_unpriced_model_leaves_every_cost_none(self, tmp_path: Path) -> None:
        """A LiteLLM pricing miss on any call makes the estimated total ``None`` too."""
        records = [
            _session_meta(),
            _turn_context(model=UNPRICED_MODEL),
            _user("q"),
            _assistant("a"),
            _token_count(10, 2),
            _assistant("b"),
            _token_count(20, 3),
        ]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        for agent in _agent_steps(trajectory):
            assert "cost_usd" not in agent["metrics"]
        assert "total_cost_usd" not in trajectory["final_metrics"]

    def test_priced_model_sums_per_call_costs_into_the_total(self, tmp_path: Path) -> None:
        """``total_cost_usd`` is the sum of every call's LiteLLM cost when all price."""
        records = [
            _session_meta(),
            _turn_context(model=PRICED_MODEL),
            _user("q"),
            _assistant("a"),
            _token_count(1000, 20, cached=100, cache_write=50),
            _assistant("b"),
            _token_count(2000, 30),
        ]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        costs = [s["metrics"]["cost_usd"] for s in _agent_steps(trajectory)]
        assert all(c > 0 for c in costs)
        assert trajectory["final_metrics"]["total_cost_usd"] == pytest.approx(sum(costs))

    def test_info_total_cost_overrides_the_estimate_even_when_zero(self, tmp_path: Path) -> None:
        """``is None`` checks: a reported ``total_cost`` of 0 is a value, not a miss."""
        records = [
            _session_meta(),
            _turn_context(model=PRICED_MODEL),
            _user("q"),
            _assistant("a"),
            _token_count(1000, 20, info_extra={"total_cost": 0.0}),
        ]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        assert trajectory["final_metrics"]["total_cost_usd"] == 0.0
        (agent,) = _agent_steps(trajectory)
        assert agent["metrics"]["cost_usd"] > 0


class TestFinalMetricsAndIdentity:
    def test_final_metrics_come_from_the_last_token_count_with_totals(self, tmp_path: Path) -> None:
        """The LAST ``token_count`` carrying ``total_token_usage`` wins; zero prompt -> absent."""
        records = [
            _session_meta(),
            _turn_context(),
            _user("q"),
            _assistant("a"),
            _token_count(10, 2),
            _assistant("b"),
            _token_count(
                20,
                3,
                total_override={
                    "input_tokens": 0,
                    "cached_input_tokens": 7,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 12,
                },
            ),
            _event({"type": "token_count", "info": {"last_token_usage": {}}}),
        ]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        final = trajectory["final_metrics"]
        assert "total_prompt_tokens" not in final
        assert final["total_completion_tokens"] == 5
        assert final["total_cached_tokens"] == 7
        assert final["total_steps"] == 3
        assert final["extra"]["reasoning_output_tokens"] == 1
        assert final["extra"]["total_tokens"] == 12
        assert "total_cache_write_input_tokens" not in final["extra"]
        assert final["extra"]["last_token_usage"]["input_tokens"] == 20

    def test_agent_version_falls_back_to_unknown_and_extra_keeps_present_keys(
        self, tmp_path: Path
    ) -> None:
        """``cli_version`` missing -> ``"unknown"``; only the four known keys, when not None."""
        meta = _session_meta()
        del meta["payload"]["cli_version"]
        del meta["payload"]["git"]
        meta["payload"]["instructions"] = "be terse"
        meta["payload"]["ignored_key"] = "x"
        records = [meta, _user("q")]
        trajectory = _convert(tmp_path, records)
        assert trajectory is not None
        assert trajectory["agent"] == {
            "name": "codex",
            "version": "unknown",
            "extra": {
                "originator": "codex-tui",
                "cwd": "/home/alice/proj",
                "instructions": "be terse",
            },
        }
        assert trajectory["session_id"] == CODEX_SESSION_ID

    def test_cli_version_is_the_agent_version(self, tmp_path: Path) -> None:
        trajectory = _convert(tmp_path, [_session_meta(), _user("q")])
        assert trajectory is not None
        assert trajectory["agent"]["version"] == CODEX_CLI_VERSION

    def test_no_session_meta_uses_the_parent_directory_name(self, tmp_path: Path) -> None:
        """harbor falls back to ``session_dir.name``: the ``<DD>`` day directory in its layout.

        Not cross-checked: the oracle stages into a directory named ``sessions``,
        so harbor's answer there is a staging artifact, not the behavior pinned.
        """
        records = [_turn_context(), _user("q"), _assistant("a")]
        rollout = write_codex_rollout(tmp_path / "sessions", records)
        ours = convert_codex_rollout(rollout)
        assert ours is not None
        assert ours.session_id == rollout.parent.name == "11"
        assert ours.agent.version == "unknown"
        assert ours.agent.extra is None

    def test_developer_role_is_system_and_missing_role_is_user(self, tmp_path: Path) -> None:
        """Any role but ``user`` / ``assistant`` -> ``system``; no role at all -> ``user``."""
        records = [_session_meta(), _message("developer", "rules"), _message(None, "no role")]
        steps = _steps(_convert(tmp_path, records))
        assert [(s["source"], s["message"]) for s in steps] == [
            ("system", "rules"),
            ("user", "no role"),
        ]


class TestNoneAndMalformedInput:
    def test_empty_rollout_returns_none(self, tmp_path: Path) -> None:
        assert _convert(tmp_path, []) is None

    def test_rollout_with_no_step_records_returns_none(self, tmp_path: Path) -> None:
        """Metadata and token counts alone produce no step, so harbor returns ``None``."""
        assert _convert(tmp_path, [_session_meta(), _turn_context(), _token_count(1, 1)]) is None

    def test_malformed_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        """harbor logs and drops a non-JSON line and ignores blank ones."""
        rollout = write_codex_rollout(tmp_path / "sessions", [_session_meta(), _user("q")])
        body = rollout.read_text()
        rollout.write_text(
            body
            + "\n{not json\n\n"
            + '{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "a"}]}}\n'
        )
        ours = convert_codex_rollout(rollout)
        assert ours is not None
        assert [s.source for s in ours.steps] == ["user", "agent"]
        if harbor_has_private_api("codex"):
            assert diff_paths(harbor_codex_trajectory(rollout), ours.to_json_dict()) == []
