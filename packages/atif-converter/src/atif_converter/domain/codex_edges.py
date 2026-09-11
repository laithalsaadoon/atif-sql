# SPDX-License-Identifier: Apache-2.0

"""edges.jsonl for Codex rollouts — the id-keyed skeleton of a rollout.

Pure functions over already-parsed record dicts; no harbor, no I/O. The line
shape is the SAME nine contract fields the Claude Code emitter produces
(:mod:`atif_converter.domain.edges`), so atif-duck's ``messages`` view reads
one projection for every agent:

    {uuid, parent_uuid, message_id, type, ts, is_sidechain,
     is_compact_summary, source_file, tool_use_ids: [..]}

Three fields mean something different for a rollout than for a Claude Code
transcript, and the difference is the point of this module rather than a
shortcut:

``uuid``
    A rollout record has no session-wide uuid. ``response_item`` payloads
    usually carry an ``id`` (``msg_*`` / ``call_*`` / ``rs_*``), and newer
    Codex builds add a per-file ``ordinal`` to every record — but a
    ``function_call_output`` carries neither, and rollouts written before the
    ordinal landed carry it nowhere. :func:`record_id` therefore falls back to
    a SYNTHETIC ``<type>:L<line>`` id, deterministic in the file's bytes and
    visibly synthetic so a reader cannot mistake it for something Codex
    assigned.

``parent_uuid``
    Always ``None``. A rollout is a flat append-only log with no parent
    pointer: the ordering IS the structure. Inventing a chain by pairing each
    record with its predecessor would produce a tree that looks authoritative
    and describes nothing, so the column stays null and
    ``turn_id``-derived grouping is left to ``codex_turn_id`` on the steps.

``type``
    The raw ROLE where the record has one (``user`` / ``assistant`` /
    ``developer``), and the item kind otherwise (``function_call``,
    ``reasoning``, ``compacted``, …). This is what preserves the
    developer-vs-system distinction that harbor's step conversion flattens
    (:attr:`~atif_converter.domain.codex_fidelity.CodexFidelityGap.DEVELOPER_ROLE_FLATTENED_TO_SYSTEM`).
"""

from __future__ import annotations

from typing import Any

from atif_converter.domain.edges import EDGE_FIELDS, edge_to_jsonl_line

#: Payload types whose ``call_id`` is a tool-call join key back to ATIF
#: ``tool_calls[].tool_call_id`` / ``observation.results[].source_call_id``.
_CALL_ID_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "tool_search_call",
        "tool_search_output",
    }
)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def record_id(record: dict[str, Any], line_number: int) -> str:
    """A stable id for one rollout record: its own, else a synthetic one.

    Preference order, and each rung exists because the one above it is absent
    on real rollouts (measured 2026-09-11 over 78 local rollouts): the
    payload's own ``id``, then the record's per-file ``ordinal``, then the
    1-based ``line_number``. The synthetic forms carry the record type so a
    reader can see at a glance that Codex did not assign the id.
    """
    payload = _payload(record)
    own_id = payload.get("id")
    if isinstance(own_id, str) and own_id:
        return own_id
    record_type = record.get("type")
    kind = record_type if isinstance(record_type, str) and record_type else "record"
    ordinal = record.get("ordinal")
    if isinstance(ordinal, int):
        return f"{kind}:{ordinal}"
    return f"{kind}:L{line_number}"


def _edge_type(record: dict[str, Any]) -> str | None:
    """The raw role for a message, else the item/record kind."""
    record_type = record.get("type")
    payload = _payload(record)
    if record_type == "response_item":
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            return role if isinstance(role, str) and role else "message"
        return item_type if isinstance(item_type, str) else None
    if record_type == "event_msg":
        item_type = payload.get("type")
        return f"event:{item_type}" if isinstance(item_type, str) else "event"
    return record_type if isinstance(record_type, str) else None


def _tool_use_ids(record: dict[str, Any]) -> list[str]:
    """The record's ``call_id``, when it is a tool call or a tool output."""
    payload = _payload(record)
    if payload.get("type") not in _CALL_ID_ITEM_TYPES:
        return []
    call_id = payload.get("call_id")
    return [call_id] if isinstance(call_id, str) and call_id else []


def codex_edge_from_record(
    record: dict[str, Any],
    source_file: str,
    line_number: int,
) -> dict[str, Any]:
    """Project one rollout record onto the contract's edge shape."""
    payload = _payload(record)
    message_id = payload.get("id") if payload.get("type") == "message" else None
    return {
        "uuid": record_id(record, line_number),
        # Flat log, no parent pointer — see the module docstring.
        "parent_uuid": None,
        "message_id": message_id if isinstance(message_id, str) else None,
        "type": _edge_type(record),
        "ts": record.get("timestamp"),
        # Codex sub-agents get their own rollout file rather than an inlined
        # sidechain, so no record of THIS session is a sidechain record.
        "is_sidechain": False,
        "is_compact_summary": record.get("type") == "compacted",
        "source_file": source_file,
        "tool_use_ids": _tool_use_ids(record),
    }


def build_codex_edges(records: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """Build every edge from ``(record, source_file)`` pairs, in FILE order.

    File order, not timestamp order: a rollout is append-only, so its line
    order is the causal order, and two records inside one millisecond share a
    timestamp often enough that sorting by it would shuffle a tool call past
    its own output. The line number is also what
    :func:`record_id` falls back to, so the two must be counted the same way —
    per source file, 1-based.
    """
    line_numbers: dict[str, int] = {}
    used_ids: set[str] = set()
    edges: list[dict[str, Any]] = []
    for record, source_file in records:
        line_numbers[source_file] = line_numbers.get(source_file, 0) + 1
        edge = codex_edge_from_record(record, source_file, line_numbers[source_file])
        # UNIQUENESS GUARD. `messages` is a uuid-keyed view, so two edges
        # sharing a uuid would silently double a record. No duplicate payload
        # id appears across the 74 local rollouts measured 2026-09-11, but the
        # ids come from a source this workspace does not control, so a repeat
        # falls back to the synthetic form rather than shadowing the first.
        if str(edge["uuid"]) in used_ids:
            kind = record.get("type")
            prefix = kind if isinstance(kind, str) and kind else "record"
            edge["uuid"] = f"{prefix}:L{line_numbers[source_file]}"
        used_ids.add(str(edge["uuid"]))
        edges.append(edge)
    return edges


def codex_edges_jsonl_lines(records: list[tuple[dict[str, Any], str]]) -> list[str]:
    """``build_codex_edges`` + serialization: the ready-to-write edges.jsonl body."""
    return [edge_to_jsonl_line(edge) for edge in build_codex_edges(records)]


def codex_edge_ids(records: list[tuple[dict[str, Any], str]]) -> list[str]:
    """Every record's edge id, in the same order :func:`build_codex_edges` uses.

    The enrichment pass attributes steps to these ids, so it must derive them
    from the one function the edges are built with rather than recomputing the
    fallback rules and drifting from them.
    """
    return [str(edge["uuid"]) for edge in build_codex_edges(records)]


__all__ = [
    "EDGE_FIELDS",
    "build_codex_edges",
    "codex_edge_from_record",
    "codex_edge_ids",
    "codex_edges_jsonl_lines",
    "record_id",
]
