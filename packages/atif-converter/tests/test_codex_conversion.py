# SPDX-License-Identifier: Apache-2.0

"""Pin the Codex conversion behavior our converter inherited from harbor 0.22.0.

Every assertion here encodes a behavior EMPIRICALLY OBSERVED in harbor's Codex
conversion at 0.22.0 (probed 2026-09-11 against 73 real rollouts under
``~/.codex/sessions`` plus the synthetic fixture in ``codex_fixtures.py``) and
carried into our port (``atif_converter.domain.codex_conversion``) on purpose.
The parity oracle holds the port to that behavior; these tests say what the
behavior IS, in terms of ``atif_converter/domain/codex_fidelity.py``. Changing
one is a decision to diverge from harbor, recorded in the policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from codex_fixtures import (
    CODEX_CLI_VERSION,
    CODEX_MODEL,
    CODEX_ROLLOUT_NAME,
    CODEX_SESSION_ID,
    codex_rollout_records,
    write_codex_rollout,
)

from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.agents import DEFAULT_AGENT, AgentSource
from atif_converter.domain.codex_edges import (
    build_codex_edges,
    codex_edges_jsonl_lines,
    record_id,
)
from atif_converter.domain.codex_enrichment import enrich_codex_trajectory
from atif_converter.domain.codex_fidelity import (
    CODEX_CONVERTIBLE_ITEM_TYPES,
    CodexFidelityGap,
    CodexRecordType,
)
from atif_converter.domain.errors import EmptySessionError, InvalidSessionInput
from atif_converter.domain.fidelity import FidelityGap, LossReport
from atif_converter.infrastructure.codex_adapter import codex_session_id
from atif_converter.infrastructure.harbor_adapter import ConversionResult

Converted = tuple[ConversionResult, LossReport]


@pytest.fixture
def converted_codex(codex_rollout: Path) -> Converted:
    return convert_codex_and_audit(codex_rollout)


def _steps(result: ConversionResult) -> list[dict[str, Any]]:
    steps = result.trajectory["steps"]
    assert isinstance(steps, list)
    return steps


class TestAgentIdentity:
    def test_default_agent_is_claude_code(self) -> None:
        """The pre-Codex behavior is what a caller naming no agent still gets."""
        assert DEFAULT_AGENT is AgentSource.CLAUDE_CODE

    def test_values_are_harbors_agent_names(self, converted_codex: Converted) -> None:
        """``AgentSource`` values double as harbor's ``agent.name`` — that is wire contract."""
        result, _ = converted_codex
        assert result.trajectory["agent"]["name"] == AgentSource.CODEX.value

    def test_trajectory_carries_cli_version_and_model(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        agent = result.trajectory["agent"]
        assert agent["version"] == CODEX_CLI_VERSION
        assert agent["model_name"] == CODEX_MODEL


class TestConversion:
    def test_conversion_validates_clean(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        assert result.is_valid, f"validator errors: {result.validation_errors}"
        assert result.trajectory["schema_version"].startswith("ATIF-")

    def test_session_id_comes_from_session_meta(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        assert result.trajectory["session_id"] == CODEX_SESSION_ID

    def test_tool_call_bundled_with_its_output(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        tool_steps = [step for step in _steps(result) if step.get("tool_calls")]
        assert len(tool_steps) == 1
        step = tool_steps[0]
        assert step["tool_calls"][0]["function_name"] == "exec_command"
        assert step["tool_calls"][0]["arguments"] == {"cmd": "cat marker.txt"}
        assert step["observation"]["results"][0]["source_call_id"] == "call_1"
        assert "marker-ok" in step["observation"]["results"][0]["content"]

    def test_developer_message_flattens_to_system_gap7(self, converted_codex: Converted) -> None:
        """A developer message and a system message are indistinguishable in the steps."""
        result, report = converted_codex
        system_steps = [step for step in _steps(result) if step["source"] == "system"]
        assert len(system_steps) == 1
        assert system_steps[0]["message"].startswith("<skills_instructions>")
        assert CodexFidelityGap.DEVELOPER_ROLE_FLATTENED_TO_SYSTEM in report.gaps_observed

    def test_reasoning_is_lost_when_only_encrypted_gap4(self, converted_codex: Converted) -> None:
        """An empty ``summary`` with encrypted content yields NO reasoning at all."""
        result, report = converted_codex
        assert all(not step.get("reasoning_content") for step in _steps(result))
        assert CodexFidelityGap.REASONING_ENCRYPTED_DROPPED in report.gaps_observed

    def test_tool_search_items_are_dropped_gap5(self, converted_codex: Converted) -> None:
        """A tool search and its result reach no step and raise no warning upstream."""
        result, report = converted_codex
        tool_names = {
            call["function_name"]
            for step in _steps(result)
            for call in (step.get("tool_calls") or [])
        }
        assert "tool_search_call" not in tool_names
        assert CodexFidelityGap.TOOL_SEARCH_CALLS_DROPPED in report.gaps_observed

    def test_compaction_reaches_no_step_gap3(self, converted_codex: Converted) -> None:
        result, report = converted_codex
        assert all(
            not (step.get("extra") or {}).get("is_compact_summary") for step in _steps(result)
        )
        assert CodexFidelityGap.COMPACTION_UNHANDLED in report.gaps_observed
        assert result.trajectory["extra"]["codex_compaction_count"] == 1

    def test_cache_creation_survives_only_in_metrics_extra(
        self, converted_codex: Converted
    ) -> None:
        """Codex spells the creation split ``cache_write_input_tokens``, in extra."""
        result, _ = converted_codex
        step = next(step for step in _steps(result) if step.get("tool_calls"))
        metrics = step["metrics"]
        assert metrics["extra"]["cache_write_input_tokens"] == 900
        assert "cache_creation_input_tokens" not in metrics["extra"]

    def test_final_metrics_totals(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        final = result.trajectory["final_metrics"]
        assert final["total_prompt_tokens"] == 1_100
        assert final["total_completion_tokens"] == 8
        assert final["total_steps"] == len(_steps(result))

    def test_rejects_a_non_jsonl_path(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSessionInput):
            convert_codex_and_audit(tmp_path / "rollout-2026-09-11T00-00-00-x.json")

    def test_rejects_a_rollout_with_no_convertible_events(self, tmp_path: Path) -> None:
        rollout = write_codex_rollout(
            tmp_path / "sessions",
            records=[{"type": "world_state", "timestamp": "2026-09-11T00:00:00Z", "payload": {}}],
        )
        with pytest.raises(EmptySessionError):
            convert_codex_and_audit(rollout)


class TestLossReport:
    def test_counts_every_record_and_only_convertible_items(
        self, converted_codex: Converted
    ) -> None:
        """``records_converted`` counts convertible RESPONSE ITEMS, not every record."""
        _result, report = converted_codex
        counts = report.record_counts
        assert counts[CodexRecordType.RESPONSE_ITEM] == 9
        assert counts[CodexRecordType.EVENT_MSG] == 4
        assert counts[CodexRecordType.COMPACTED] == 1
        assert counts[CodexRecordType.WORLD_STATE] == 1
        assert counts[CodexRecordType.TOKEN_USAGE_RECORD] == 1
        assert report.records_total == len(codex_rollout_records())
        # 2 messages + 1 empty assistant message + 1 final assistant message
        # + function_call + its output. reasoning and the two tool_search items
        # are response items that convert to nothing.
        assert report.records_converted == 6
        assert report.records_dropped == report.records_total - 6

    def test_json_shape_matches_the_claude_code_one(self, converted_codex: Converted) -> None:
        """One ``loss_report.json`` projection serves both agents."""
        _result, report = converted_codex
        payload = report.to_json()
        assert set(payload) == {
            "record_counts",
            "records_total",
            "records_converted",
            "records_dropped",
            "gaps_observed",
            "subagent_files_found",
            "subagent_files_convertible",
            "workflow_subagent_files_found",
        }
        json.dumps(payload)

    def test_gap_values_are_namespaced_away_from_claude_codes(self) -> None:
        """A mixed ``gaps_observed`` array says which policy each entry came from."""
        codex_values = {gap.value for gap in CodexFidelityGap}
        claude_values = {gap.value for gap in FidelityGap}
        assert not codex_values & claude_values
        assert all(value.startswith("codex_") for value in codex_values)

    def test_a_rollout_never_reports_subagent_side_files(self, converted_codex: Converted) -> None:
        """A Codex sub-agent is its own session, so a rollout has no side-files."""
        _result, report = converted_codex
        assert report.subagent_files_found == 0
        assert report.workflow_subagent_files_found == 0

    def test_convertible_item_types_are_the_ones_harbor_dispatches(self) -> None:
        assert "tool_search_call" not in CODEX_CONVERTIBLE_ITEM_TYPES
        assert "reasoning" not in CODEX_CONVERTIBLE_ITEM_TYPES
        assert {"message", "function_call", "function_call_output"} <= CODEX_CONVERTIBLE_ITEM_TYPES


class TestEdges:
    def test_one_line_per_raw_record(self, converted_codex: Converted) -> None:
        result, report = converted_codex
        assert len(result.edges_lines) == report.records_total

    def test_ids_are_unique_and_prefer_the_payloads_own(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        ids = [json.loads(line)["uuid"] for line in result.edges_lines]
        assert len(set(ids)) == len(ids)
        assert "msg_user_1" in ids
        assert "fc_1" in ids

    def test_a_record_without_an_id_gets_a_visibly_synthetic_one(self) -> None:
        """``function_call_output`` carries no id, so the fallback must fire."""
        record = {"type": "response_item", "payload": {"type": "function_call_output"}}
        assert record_id(record, 10) == "response_item:L10"

    def test_an_ordinal_is_preferred_over_the_line_number(self) -> None:
        record = {"type": "event_msg", "ordinal": 41, "payload": {"type": "token_count"}}
        assert record_id(record, 7) == "event_msg:41"

    def test_a_repeated_payload_id_falls_back_instead_of_colliding(self) -> None:
        records = [
            ({"type": "response_item", "payload": {"type": "message", "id": "dup"}}, "r.jsonl"),
            ({"type": "response_item", "payload": {"type": "message", "id": "dup"}}, "r.jsonl"),
        ]
        ids = [edge["uuid"] for edge in build_codex_edges(records)]
        assert ids == ["dup", "response_item:L2"]

    def test_type_carries_the_raw_role_so_developer_survives(
        self, converted_codex: Converted
    ) -> None:
        result, _ = converted_codex
        types = [json.loads(line)["type"] for line in result.edges_lines]
        assert "developer" in types
        assert "user" in types
        assert "assistant" in types

    def test_compacted_record_is_flagged_as_a_compact_summary(
        self, converted_codex: Converted
    ) -> None:
        result, _ = converted_codex
        compacted = [
            json.loads(line)
            for line in result.edges_lines
            if json.loads(line)["type"] == "compacted"
        ]
        assert len(compacted) == 1
        assert compacted[0]["is_compact_summary"] is True

    def test_parent_uuid_is_always_null(self, converted_codex: Converted) -> None:
        """A rollout is a flat log; a fabricated chain would describe nothing."""
        result, _ = converted_codex
        assert all(json.loads(line)["parent_uuid"] is None for line in result.edges_lines)

    def test_tool_use_ids_carry_the_call_id_on_both_sides(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        edges = [json.loads(line) for line in result.edges_lines]
        with_call = [edge for edge in edges if edge["tool_use_ids"] == ["call_1"]]
        assert len(with_call) == 2  # the call and its output

    def test_file_order_is_preserved_not_timestamp_order(self) -> None:
        """Two records inside one timestamp must not be shuffled."""
        stamp = "2026-09-11T00:00:00.000Z"
        records = [
            (
                {
                    "type": "response_item",
                    "timestamp": stamp,
                    "payload": {"type": "function_call", "id": "b", "call_id": "c1"},
                },
                "r.jsonl",
            ),
            (
                {
                    "type": "response_item",
                    "timestamp": stamp,
                    "payload": {"type": "function_call_output", "id": "a", "call_id": "c1"},
                },
                "r.jsonl",
            ),
        ]
        assert [edge["uuid"] for edge in build_codex_edges(records)] == ["b", "a"]

    def test_lines_are_compact_and_key_ordered(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        line = result.edges_lines[0]
        assert ", " not in line
        assert list(json.loads(line)) == [
            "uuid",
            "parent_uuid",
            "message_id",
            "type",
            "ts",
            "is_sidechain",
            "is_compact_summary",
            "source_file",
            "tool_use_ids",
        ]


class TestEnrichment:
    def test_every_step_is_attributed(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        for step in _steps(result):
            assert (step.get("extra") or {}).get("source_uuids"), (
                f"step {step['step_id']} unattributed"
            )

    def test_an_empty_assistant_message_is_attributed_by_api_call_id(
        self, converted_codex: Converted
    ) -> None:
        """The case no text-matching walk can place: empty text, real record."""
        result, _ = converted_codex
        tool_step = next(step for step in _steps(result) if step.get("tool_calls"))
        assert "msg_agent_empty" in tool_step["extra"]["source_uuids"]

    def test_tool_records_are_attributed_by_call_id(self, converted_codex: Converted) -> None:
        result, _ = converted_codex
        tool_step = next(step for step in _steps(result) if step.get("tool_calls"))
        attributed = tool_step["extra"]["source_uuids"]
        assert "fc_1" in attributed
        assert "response_item:L10" in attributed

    def test_a_dropped_reasoning_record_is_not_claimed(self, converted_codex: Converted) -> None:
        """Its content never reached a step, so no step may claim it contributed."""
        result, _ = converted_codex
        attributed = {
            uuid
            for step in _steps(result)
            for uuid in (step.get("extra") or {}).get("source_uuids", [])
        }
        assert "rs_1" not in attributed

    def test_cache_total_is_republished_under_the_claude_code_key(self) -> None:
        """One key answers cache-creation for both agents."""
        trajectory: dict[str, Any] = {
            "steps": [],
            "final_metrics": {"extra": {"total_cache_write_input_tokens": 4_096}},
        }
        enriched = enrich_codex_trajectory(trajectory, [])
        assert enriched["extra"]["cache_creation_total"] == 4_096

    def test_a_positional_disagreement_refuses_and_records_the_step(self) -> None:
        """A user step whose text no record reproduces stops attribution loudly."""
        trajectory: dict[str, Any] = {
            "steps": [{"step_id": 1, "source": "user", "message": "not what the record says"}],
        }
        records: list[tuple[dict[str, Any], str]] = [
            (
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "msg_1",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "something else"}],
                    },
                },
                "r.jsonl",
            )
        ]
        enriched = enrich_codex_trajectory(trajectory, records)
        assert enriched["extra"]["enrichment_truncated_at_step"] == 1
        assert enriched["extra"]["enrichment_unattributed_steps"] == 1
        assert not (enriched["steps"][0].get("extra") or {}).get("source_uuids")

    def test_message_records_that_reach_no_step_are_counted(self) -> None:
        """The silent end of the positional walk: records left over, no step refused.

        Running out of RECORDS refuses a step and is loud. Running out of STEPS
        is invisible — the leftover records attribute to nothing while the loss
        report still counts them convertible — so the count is the only marker
        that the conversion lost something.
        """
        trajectory: dict[str, Any] = {
            "steps": [{"step_id": 1, "source": "user", "message": "first"}],
        }
        records: list[tuple[dict[str, Any], str]] = [
            (
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": f"msg_{index}",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                },
                "r.jsonl",
            )
            for index, text in ((1, "first"), (2, "second"), (3, "third"))
        ]
        enriched = enrich_codex_trajectory(trajectory, records)
        assert enriched["extra"]["enrichment_leftover_messages"] == 2
        assert "enrichment_truncated_at_step" not in enriched["extra"]
        assert (enriched["steps"][0]["extra"])["source_uuids"] == ["msg_1"]

    def test_no_leftover_key_when_every_record_reached_a_step(self) -> None:
        """The key is present only when nonzero, so its presence means loss."""
        trajectory: dict[str, Any] = {
            "steps": [{"step_id": 1, "source": "user", "message": "only"}],
        }
        records: list[tuple[dict[str, Any], str]] = [
            (
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "msg_1",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "only"}],
                    },
                },
                "r.jsonl",
            )
        ]
        enriched = enrich_codex_trajectory(trajectory, records)
        assert "enrichment_leftover_messages" not in (enriched.get("extra") or {})

    def test_copy_input_leaves_the_caller_s_dict_untouched(self) -> None:
        trajectory: dict[str, Any] = {"steps": [{"step_id": 1, "source": "agent", "message": ""}]}
        enriched = enrich_codex_trajectory(trajectory, [], copy_input=True)
        assert enriched is not trajectory
        assert "extra" not in trajectory["steps"][0]

    def test_edges_and_attribution_agree_on_every_id(self, converted_codex: Converted) -> None:
        """An attributed id that is not an edge id would join to nothing in SQL."""
        result, _ = converted_codex
        edge_ids = {json.loads(line)["uuid"] for line in result.edges_lines}
        attributed = {
            uuid
            for step in _steps(result)
            for uuid in (step.get("extra") or {}).get("source_uuids", [])
        }
        assert attributed <= edge_ids


class TestSessionIdFromFilename:
    def test_reads_the_trailing_uuid(self) -> None:
        assert codex_session_id(Path(CODEX_ROLLOUT_NAME)) == CODEX_SESSION_ID

    def test_filename_id_matches_session_meta(self, converted_codex: Converted) -> None:
        """Discovery names a session before anything parses the file, so the two must agree."""
        result, _ = converted_codex
        assert codex_session_id(Path(CODEX_ROLLOUT_NAME)) == result.trajectory["session_id"]

    @pytest.mark.parametrize("name", ["session.jsonl", "rollout-2026.jsonl", "rollout-.jsonl"])
    def test_rejects_a_name_that_carries_no_session_uuid(self, name: str) -> None:
        with pytest.raises(InvalidSessionInput):
            codex_session_id(Path(name))


def test_edges_helper_returns_ready_to_write_lines() -> None:
    """The public helper the corpus writer consumes: strings, no trailing newlines."""
    records: list[tuple[dict[str, Any], str]] = [
        (
            {"type": "response_item", "payload": {"type": "message", "id": "m", "role": "user"}},
            "r.jsonl",
        )
    ]
    lines = codex_edges_jsonl_lines(records)
    assert lines and all(not line.endswith("\n") for line in lines)
