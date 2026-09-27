# SPDX-License-Identifier: Apache-2.0

"""session_events: which non-message records are kept, and how their rows look.

Record shapes below are copied from real Claude Code 2.1.28x transcripts and
codex-cli 0.154 rollouts (2026-09-27), trimmed to the fields that matter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.codex_edges import codex_edges_jsonl_lines
from atif_converter.domain.fidelity import FidelityGap
from atif_converter.domain.session_events import (
    PAYLOAD_MAX_BYTES,
    PAYLOAD_STRING_MAX_CHARS,
    REPORTED_COST_KEY,
    SESSION_EVENT_FIELDS,
    bound_payload,
    claude_reported_cost,
    claude_session_events,
    codex_session_events,
    session_event_to_jsonl_line,
)

SID = "22222222-2222-2222-2222-222222222222"
MAIN = f"{SID}.jsonl"


def _envelope(uuid: str, ts: str, parent: str | None = "a-1") -> dict[str, Any]:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-09-27T20:00:{ts}Z",
        "isSidechain": False,
        "sessionId": SID,
        "cwd": "/work",
        "version": "2.1.283",
        "gitBranch": "main",
        "userType": "external",
        "entrypoint": "cli",
    }


def _attachment(uuid: str, ts: str, body: dict[str, Any]) -> dict[str, Any]:
    return {**_envelope(uuid, ts), "type": "attachment", "attachment": body}


def _system(uuid: str, ts: str, subtype: str, **fields: Any) -> dict[str, Any]:
    return {**_envelope(uuid, ts), "type": "system", "subtype": subtype, **fields}


KEPT: list[dict[str, Any]] = [
    _attachment(
        "h-1",
        "01",
        {
            "type": "hook_success",
            "hookName": "SessionStart:startup",
            "toolUseID": "t-hook",
            "hookEvent": "SessionStart",
            "content": "hello",
        },
    ),
    _attachment(
        "h-2",
        "02",
        {
            "type": "hook_blocking_error",
            "hookName": "Stop:Callback",
            "hookEvent": "Stop",
            "blockingError": {"blockingError": "report the commit", "command": "callback"},
        },
    ),
    _attachment(
        "h-3",
        "03",
        {"type": "hook_cancelled", "hookName": "UserPromptSubmit", "timedOut": True},
    ),
    _attachment(
        "h-4",
        "04",
        {
            "type": "hook_additional_context",
            "content": ["<voice>we</voice>"],
            "hookName": "UserPromptSubmit",
        },
    ),
    _attachment(
        "q-1",
        "05",
        {"type": "queued_command", "prompt": "<task-notification>done</task-notification>"},
    ),
    _system("s-1", "06", "stop_hook_summary", hookCount=1, preventedContinuation=False),
    _system("s-2", "07", "api_error", level="error", error={"message": "500 backend"}),
    _system(
        "s-3",
        "08",
        "compact_boundary",
        content="Conversation compacted",
        compactMetadata={"trigger": "auto", "preTokens": 467728},
    ),
    _system(
        "s-4",
        "09",
        "model_refusal_fallback",
        originalModel="claude-fable-5-1[1m]",
        fallbackModel="claude-opus-4-8[1m]",
    ),
    {
        "type": "cost-state",
        "sessionId": SID,
        "totalCostUSD": 1.5,
        "modelUsage": {"claude-opus-5-5[1m]": {"costUSD": 1.5}},
        "hasUnknownModelCost": False,
    },
    {"type": "mode", "mode": "normal", "sessionId": SID},
    {"type": "permission-mode", "permissionMode": "bypassPermissions", "sessionId": SID},
]

DROPPED: list[dict[str, Any]] = [
    _attachment("x-1", "10", {"type": "skill_listing", "content": "..."}),
    _attachment("x-2", "11", {"type": "total_tokens_reminder"}),
    _system("x-3", "12", "turn_duration", durationMs=5),
    {"type": "last-prompt", "sessionId": SID},
    {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-27T20:00:13Z"},
]


class TestClaudeSelection:
    def test_every_kept_kind_becomes_one_row_and_nothing_else_does(self) -> None:
        rows = claude_session_events([(r, MAIN) for r in [*KEPT, *DROPPED]])
        kinds = [(row["event_type"], row["subtype"]) for row in rows]
        assert kinds == [
            ("attachment", "hook_success"),
            ("attachment", "hook_blocking_error"),
            ("attachment", "hook_cancelled"),
            ("attachment", "hook_additional_context"),
            ("attachment", "queued_command"),
            ("system", "stop_hook_summary"),
            ("system", "api_error"),
            ("system", "compact_boundary"),
            ("system", "model_refusal_fallback"),
            ("cost-state", None),
            ("mode", None),
            ("permission-mode", None),
        ]
        assert [row["seq"] for row in rows] == list(range(len(rows)))

    def test_row_columns_come_from_the_envelope(self) -> None:
        (row,) = claude_session_events([(KEPT[0], MAIN)])
        assert tuple(row) == SESSION_EVENT_FIELDS
        assert row["uuid"] == "h-1"
        assert row["parent_uuid"] == "a-1"
        assert row["tool_use_id"] == "t-hook"
        assert row["ts"] == "2026-09-27T20:00:01Z"
        assert row["source_file"] == MAIN
        assert row["is_sidechain"] is False
        # The attachment body, minus its type, is the payload; the envelope isn't.
        assert row["payload"] == {
            "hookName": "SessionStart:startup",
            "hookEvent": "SessionStart",
            "content": "hello",
        }
        assert row["payload_truncated"] is False

    def test_records_without_a_timestamp_keep_a_null_ts(self) -> None:
        (row,) = claude_session_events([(KEPT[9], MAIN)])
        assert row["ts"] is None
        assert row["payload"]["totalCostUSD"] == 1.5

    def test_line_is_compact_and_key_ordered(self) -> None:
        (row,) = claude_session_events([(KEPT[6], MAIN)])
        line = session_event_to_jsonl_line(row)
        assert list(json.loads(line)) == list(SESSION_EVENT_FIELDS)
        assert ", " not in line


class TestPayloadBound:
    def test_small_payload_passes_through(self) -> None:
        payload = {"a": "b"}
        assert bound_payload(payload) == (payload, len('{"a":"b"}'), False)

    def test_long_strings_are_cut_and_the_original_size_kept(self) -> None:
        payload = {"content": "x" * 20_000, "hookName": "Stop"}
        bounded, original, truncated = bound_payload(payload)
        assert truncated is True
        assert original == len(json.dumps(payload, separators=(",", ":")))
        assert len(bounded["content"]) == PAYLOAD_STRING_MAX_CHARS + 1
        assert bounded["hookName"] == "Stop"

    def test_a_payload_still_too_large_becomes_a_preview(self) -> None:
        payload = {f"k{i}": "y" * 1000 for i in range(40)}
        bounded, original, truncated = bound_payload(payload)
        assert truncated is True
        assert set(bounded) == {"truncated_preview"}
        assert len(bounded["truncated_preview"].encode()) <= PAYLOAD_MAX_BYTES
        assert original > PAYLOAD_MAX_BYTES

    def test_every_bounded_payload_fits(self) -> None:
        for payload in (
            {"list": list(range(10_000))},
            {"nested": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": "z" * 9000}}}}}}}}}},
            {"text": "é" * 9000},
        ):
            bounded, _original, truncated = bound_payload(payload)
            assert truncated is True
            text = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
            assert len(text.encode("utf-8")) <= PAYLOAD_MAX_BYTES


class TestReportedCost:
    def test_last_cost_state_wins(self) -> None:
        records = [
            ({"type": "cost-state", "totalCostUSD": 1.0}, MAIN),
            ({"type": "cost-state", "totalCostUSD": 4.5, "hasUnknownModelCost": True}, MAIN),
        ]
        assert claude_reported_cost(records) == {
            "reported_cost_usd": 4.5,
            "reported_cost_source": "claude_code_cost_state",
            "reported_cost_has_unknown_model_cost": True,
        }

    def test_no_cost_state_reports_nothing(self) -> None:
        assert claude_reported_cost([(KEPT[0], MAIN)]) == {}

    def test_a_non_numeric_total_is_ignored(self) -> None:
        records = [
            ({"type": "cost-state", "totalCostUSD": 2.0}, MAIN),
            ({"type": "cost-state", "totalCostUSD": "n/a"}, MAIN),
        ]
        assert claude_reported_cost(records)[REPORTED_COST_KEY] == 2.0


def _write_session(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "projects" / "-work" / MAIN
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _turn() -> list[dict[str, Any]]:
    return [
        {
            **_envelope("u-1", "00", parent=None),
            "type": "user",
            "message": {"role": "user", "content": "go"},
        },
        {
            **_envelope("a-1", "00", parent="u-1"),
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "done"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                },
            },
        },
    ]


class TestClaudeUseCase:
    def test_events_ride_the_result_and_count_as_captured(self, tmp_path: Path) -> None:
        session = _write_session(tmp_path, [*_turn(), *KEPT, *DROPPED])
        result, report = convert_and_audit(session)
        assert len(result.events_lines) == len(KEPT)
        assert report.records_captured == len(KEPT)
        assert report.records_converted == 2
        assert report.records_dropped == len(DROPPED)
        assert report.records_total == 2 + len(KEPT) + len(DROPPED)
        assert FidelityGap.NON_MESSAGE_RECORDS_DROPPED in report.gaps_observed
        # The events never become steps.
        assert len(result.trajectory["steps"]) == 2

    def test_all_non_message_records_captured_means_nothing_dropped(self, tmp_path: Path) -> None:
        session = _write_session(tmp_path, [*_turn(), *KEPT])
        _result, report = convert_and_audit(session)
        assert report.records_dropped == 0
        assert FidelityGap.NON_MESSAGE_RECORDS_DROPPED not in report.gaps_observed
        assert report.to_json()["records_captured"] == len(KEPT)

    def test_reported_cost_lands_beside_the_estimate(self, tmp_path: Path) -> None:
        session = _write_session(tmp_path, [*_turn(), *KEPT])
        result, _report = convert_and_audit(session)
        final = result.trajectory["final_metrics"]
        assert final["extra"]["reported_cost_usd"] == 1.5
        assert final["extra"]["reported_cost_source"] == "claude_code_cost_state"
        # claude-opus-5-5 is priced from its override, so the estimate is real too.
        assert final["total_cost_usd"] > 0

    def test_repaired_gaps_are_not_reported(self, tmp_path: Path) -> None:
        session = _write_session(tmp_path, _turn())
        _result, report = convert_and_audit(session)
        assert FidelityGap.UUID_NOT_PRESERVED not in report.gaps_observed
        assert FidelityGap.CACHE_SPLIT_PARTIAL not in report.gaps_observed
        assert FidelityGap.COMPACT_SUMMARY_UNHANDLED not in report.gaps_observed
        assert FidelityGap.PARENT_CHAIN_FLATTENED in report.gaps_observed

    def test_unattributed_steps_report_uuid_not_preserved(self, tmp_path: Path) -> None:
        """GUARD: a user record with no uuid can't be attributed, so gap 7 stays reported."""
        records = _turn()
        del records[0]["uuid"]
        session = _write_session(tmp_path, records)
        result, report = convert_and_audit(session)
        assert result.trajectory["extra"]["enrichment_unattributed_steps"] == 1
        assert FidelityGap.UUID_NOT_PRESERVED in report.gaps_observed

    def test_unattributed_compact_summary_reports_its_gap(self, tmp_path: Path) -> None:
        records = _turn()
        records[0]["isCompactSummary"] = True
        records.append(
            {
                "type": "user",
                "uuid": "orphan-summary",
                "isCompactSummary": True,
                "timestamp": "2026-09-27T20:00:30Z",
                "message": {"role": "user", "content": []},
            }
        )
        session = _write_session(tmp_path, records)
        _result, report = convert_and_audit(session)
        assert FidelityGap.COMPACT_SUMMARY_UNHANDLED in report.gaps_observed


CODEX_RECORDS: list[dict[str, Any]] = [
    {
        "timestamp": "2026-09-16T18:28:00.000Z",
        "type": "session_meta",
        "payload": {"id": "01a0baf5-e0ab-77a0-bf9b-f733d8f8797e", "cli_version": "0.154.0"},
    },
    {
        "timestamp": "2026-09-16T18:28:04.715Z",
        "ordinal": 10,
        "type": "compacted",
        "payload": {"message": "summary " * 2000, "replacement_history": [{"x": 1}] * 50},
    },
    {
        "timestamp": "2026-09-16T00:31:57.696Z",
        "ordinal": 14,
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"},
    },
    {
        "timestamp": "2026-09-16T00:31:58.000Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": None},
    },
]


class TestCodex:
    def test_compactions_and_aborted_turns_are_kept(self) -> None:
        pairs = [(record, "rollout.jsonl") for record in CODEX_RECORDS]
        rows = codex_session_events(pairs)
        assert [(row["event_type"], row["subtype"]) for row in rows] == [
            ("compacted", None),
            ("event_msg", "turn_aborted"),
        ]
        assert rows[0]["payload_truncated"] is True
        assert rows[1]["payload"] == {"turn_id": "t1", "reason": "interrupted"}

    def test_row_uuid_is_the_edge_uuid(self) -> None:
        pairs = [(record, "rollout.jsonl") for record in CODEX_RECORDS]
        edge_ids = {json.loads(line)["uuid"] for line in codex_edges_jsonl_lines(pairs)}
        assert {row["uuid"] for row in codex_session_events(pairs)} <= edge_ids

    def test_use_case_counts_them_captured(self, tmp_path: Path) -> None:
        rollout = (
            tmp_path / "rollout-2026-09-16T18-28-00-01a0baf5-e0ab-77a0-bf9b-f733d8f8797e.jsonl"
        )
        message = {
            "timestamp": "2026-09-16T18:28:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        }
        rollout.write_text(
            "".join(json.dumps(record) + "\n" for record in [message, *CODEX_RECORDS])
        )
        result, report = convert_codex_and_audit(rollout)
        assert len(result.events_lines) == 2
        assert report.records_captured == 2
        assert report.records_dropped == report.records_total - report.records_converted - 2
