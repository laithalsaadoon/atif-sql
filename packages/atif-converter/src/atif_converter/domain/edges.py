# SPDX-License-Identifier: Apache-2.0

"""edges.jsonl: one line per RAW session record — the uuid-keyed skeleton.

Pure functions over already-parsed record dicts; no harbor, no I/O. The
corpus contract (docs/CONTRACT.md §corpus layout) fixes the line shape:

    {uuid, parent_uuid, message_id, type, ts, is_sidechain,
     is_compact_summary, source_file, tool_use_ids: [..]}

``tool_use_ids`` carries the ids of ``tool_use`` blocks for assistant
records and the ``tool_use_id`` of ``tool_result`` blocks for user records
— the join key back to ATIF ``tool_calls`` / ``observation.source_call_ids``.
"""

from __future__ import annotations

import json
from typing import Any

#: Stable key order for one edges.jsonl line (contract-fixed shape).
EDGE_FIELDS: tuple[str, ...] = (
    "uuid",
    "parent_uuid",
    "message_id",
    "type",
    "ts",
    "is_sidechain",
    "is_compact_summary",
    "source_file",
    "tool_use_ids",
)


def _tool_use_ids(record: dict[str, Any]) -> list[str]:
    """Extract tool_use ids (assistant) or tool_result ids (user) in block order."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    record_type = record.get("type")
    ids: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if record_type == "assistant" and block.get("type") == "tool_use":
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id:
                ids.append(block_id)
        elif record_type == "user" and block.get("type") == "tool_result":
            block_id = block.get("tool_use_id")
            if isinstance(block_id, str) and block_id:
                ids.append(block_id)
    return ids


def edge_from_record(record: dict[str, Any], source_file: str) -> dict[str, Any]:
    """Project one raw record onto the contract's edge shape.

    ``source_file`` is the record's origin path RELATIVE to the session
    JSONL's parent directory (e.g. ``<sid>.jsonl`` or
    ``<sid>/subagents/workflows/wf_x/agent-y.jsonl``).
    """
    message = record.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    return {
        "uuid": record.get("uuid"),
        "parent_uuid": record.get("parentUuid"),
        "message_id": message_id if isinstance(message_id, str) else None,
        "type": record.get("type"),
        "ts": record.get("timestamp"),
        "is_sidechain": bool(record.get("isSidechain", False)),
        "is_compact_summary": bool(record.get("isCompactSummary", False)),
        "source_file": source_file,
        "tool_use_ids": _tool_use_ids(record),
    }


def build_edges(records: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """Build all edges from ``(record, source_file)`` pairs.

    Ordered deterministically by ``(ts, uuid)``, with missing values sorting
    first as empty strings.
    """
    edges = [edge_from_record(record, source_file) for record, source_file in records]
    edges.sort(key=lambda e: (e["ts"] or "", e["uuid"] or ""))
    return edges


def edge_to_jsonl_line(edge: dict[str, Any]) -> str:
    """Serialize one edge as a compact, key-order-stable JSONL line."""
    ordered = {key: edge.get(key) for key in EDGE_FIELDS}
    return json.dumps(ordered, separators=(",", ":"))


def edges_jsonl_lines(records: list[tuple[dict[str, Any], str]]) -> list[str]:
    """``build_edges`` + serialization: the ready-to-write edges.jsonl body."""
    return [edge_to_jsonl_line(edge) for edge in build_edges(records)]
