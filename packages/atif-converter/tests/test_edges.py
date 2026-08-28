# SPDX-License-Identifier: Apache-2.0

"""edges.jsonl emitter: shape, ordering, and tool_use_id extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atif_converter.domain.edges import EDGE_FIELDS, build_edges, edges_jsonl_lines
from atif_converter.infrastructure.raw_records import (
    discover_session_files,
    read_snapshot_records,
    take_session_snapshot,
)

SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def edges(synthetic_session: Path) -> list[dict[str, Any]]:
    return build_edges(read_snapshot_records(take_session_snapshot(synthetic_session)))


class TestDiscovery:
    def test_all_files_found_main_first(self, synthetic_session: Path) -> None:
        files = discover_session_files(synthetic_session)
        assert files[0] == synthetic_session
        names = [f.name for f in files]
        assert "agent-abc.jsonl" in names
        assert "agent-def.jsonl" in names  # workflow-nested
        assert not any(n.endswith(".meta.json") for n in names)


class TestEdges:
    def test_one_line_per_raw_record(self, edges: list[dict[str, Any]]) -> None:
        # main 6 + flat subagent 2 + workflow 1
        assert len(edges) == 9

    def test_contract_shape(self, edges: list[dict[str, Any]]) -> None:
        for edge in edges:
            assert set(edge.keys()) == set(EDGE_FIELDS)

    def test_deterministic_ts_uuid_order(self, edges: list[dict[str, Any]]) -> None:
        keys = [(e["ts"] or "", e["uuid"] or "") for e in edges]
        assert keys == sorted(keys)

    def test_assistant_tool_use_ids(self, edges: list[dict[str, Any]]) -> None:
        a1 = next(e for e in edges if e["uuid"] == "a-1")
        assert a1["tool_use_ids"] == ["toolu_01"]
        assert a1["message_id"] == "msg_01"
        assert a1["parent_uuid"] == "u-1"

    def test_user_tool_result_ids(self, edges: list[dict[str, Any]]) -> None:
        u2 = next(e for e in edges if e["uuid"] == "u-2")
        assert u2["tool_use_ids"] == ["toolu_01"]
        assert u2["message_id"] is None

    def test_sidechain_and_source_file(self, edges: list[dict[str, Any]]) -> None:
        su1 = next(e for e in edges if e["uuid"] == "su-1")
        assert su1["is_sidechain"] is True
        assert su1["source_file"] == f"{SESSION_ID}/subagents/agent-abc.jsonl"
        wu1 = next(e for e in edges if e["uuid"] == "wu-1")
        assert wu1["source_file"] == (f"{SESSION_ID}/subagents/workflows/wf_123/agent-def.jsonl")
        main = next(e for e in edges if e["uuid"] == "u-1")
        assert main["source_file"] == f"{SESSION_ID}.jsonl"

    def test_non_message_records_present(self, edges: list[dict[str, Any]]) -> None:
        types = {e["uuid"]: e["type"] for e in edges}
        assert types["att-1"] == "attachment"
        assert types["q-1"] == "queue-operation"

    def test_is_compact_summary_default_false(self, edges: list[dict[str, Any]]) -> None:
        assert all(e["is_compact_summary"] is False for e in edges)


class TestJsonlSerialization:
    def test_lines_are_compact_stable_json(self, synthetic_session: Path) -> None:
        lines = edges_jsonl_lines(read_snapshot_records(take_session_snapshot(synthetic_session)))
        assert len(lines) == 9
        for line in lines:
            parsed = json.loads(line)
            assert list(parsed.keys()) == list(EDGE_FIELDS)
            assert ": " not in line.split('"source_file"')[0]  # compact separators
