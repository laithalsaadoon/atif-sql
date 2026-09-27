# SPDX-License-Identifier: Apache-2.0

"""session_events.jsonl: the non-message records worth keeping, one typed row each.

Pure functions over already-parsed ``(record, source_file)`` pairs; no harbor,
no I/O. The step converter keeps user and assistant records only
(:attr:`~atif_converter.domain.fidelity.FidelityGap.NON_MESSAGE_RECORDS_DROPPED`),
which loses records that say what happened AROUND the turns: hooks that ran,
blocked or were cancelled, context a hook or a queued command injected into
the model's view, API errors, compaction boundaries, refusal fallbacks,
Claude Code's own running cost, and mode changes. This module picks those out
and writes each one as a row. They're deliberately NOT ATIF steps: the steps
view and everything built on it stay exactly as they were.

ROW SHAPE (:data:`SESSION_EVENT_FIELDS`, key order stable)::

    {
        seq,
        ts,
        event_type,
        subtype,
        uuid,
        parent_uuid,
        tool_use_id,
        is_sidechain,
        source_file,
        payload,
        payload_bytes,
        payload_truncated,
    }

``event_type`` is the raw record ``type`` (``system``, ``attachment``,
``cost-state``, ``mode``, ``permission-mode``; for Codex ``compacted`` or
``event_msg``) and ``subtype`` names the kind inside it (``system.subtype``,
``attachment.type``, a Codex ``payload.type``; ``None`` where the record type
is the whole story). ``seq`` is the row's position in the session's record
order (main file first, then side files, as read), because ``cost-state`` and
``mode`` records carry no timestamp. ``parent_uuid`` is the record's
``parentUuid``: for an attachment it's the record the attachment follows,
which is how a row joins back to ``steps.source_uuids``.

BOUNDED PAYLOAD. ``payload`` is the record minus its envelope (the columns
above plus per-record noise such as ``cwd`` and ``version``). Strings longer
than :data:`PAYLOAD_STRING_MAX_CHARS` and lists longer than
:data:`PAYLOAD_LIST_MAX_ITEMS` are cut, and a payload still over
:data:`PAYLOAD_MAX_BYTES` becomes ``{"truncated_preview": <first bytes>}``.
``payload_bytes`` is always the ORIGINAL payload's UTF-8 JSON length and
``payload_truncated`` says whether anything was cut, so a reader can tell a
short payload from a shortened one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Final

from atif_converter.domain.codex_edges import record_id

#: Stable key order for one session_events.jsonl line.
SESSION_EVENT_FIELDS: Final = (
    "seq",
    "ts",
    "event_type",
    "subtype",
    "uuid",
    "parent_uuid",
    "tool_use_id",
    "is_sidechain",
    "source_file",
    "payload",
    "payload_bytes",
    "payload_truncated",
)

#: Hard ceiling for one serialized payload, in UTF-8 bytes.
PAYLOAD_MAX_BYTES: Final = 8192
#: Any string leaf longer than this is cut to it.
PAYLOAD_STRING_MAX_CHARS: Final = 2048
#: Any list longer than this keeps its first items only.
PAYLOAD_LIST_MAX_ITEMS: Final = 32
#: Nesting below this depth is replaced by its JSON text, then cut as a string.
_PAYLOAD_MAX_DEPTH: Final = 8
_ELLIPSIS: Final = "…"

# --------------------------------------------------------------------------- Claude Code

#: ``system`` records kept, by ``subtype``.
CLAUDE_SYSTEM_SUBTYPES: Final = frozenset(
    {"stop_hook_summary", "api_error", "compact_boundary", "model_refusal_fallback"}
)
#: ``attachment`` records kept, by ``attachment.type``, besides every ``hook_*`` type.
CLAUDE_ATTACHMENT_TYPES: Final = frozenset({"queued_command"})
_CLAUDE_HOOK_ATTACHMENT_PREFIX: Final = "hook_"
#: Record types kept whole (no subtype).
CLAUDE_WHOLE_RECORD_TYPES: Final = frozenset({"cost-state", "mode", "permission-mode"})

#: Record keys that are columns or per-record noise, never payload.
_CLAUDE_ENVELOPE: Final = frozenset(
    {
        "type",
        "subtype",
        "uuid",
        "parentUuid",
        "timestamp",
        "isSidechain",
        "toolUseID",
        "sessionId",
        "userType",
        "entrypoint",
        "cwd",
        "version",
        "gitBranch",
        "slug",
        "isMeta",
        "attachment",
        "rendered",
    }
)

# --------------------------------------------------------------------------- Codex

#: Top-level Codex record types kept whole.
CODEX_WHOLE_RECORD_TYPES: Final = frozenset({"compacted"})
#: ``event_msg`` records kept, by ``payload.type``.
CODEX_EVENT_MSG_TYPES: Final = frozenset(
    {"turn_aborted", "error", "stream_error", "context_compacted", "warning"}
)


def _bounded_container(value: dict[str, Any] | list[Any], depth: int) -> tuple[Any, bool]:
    if depth >= _PAYLOAD_MAX_DEPTH:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return _bounded(text, depth)[0], True
    if isinstance(value, dict):
        cut = False
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)], item_cut = _bounded(item, depth + 1)
            cut = cut or item_cut
        return out, cut
    cut = len(value) > PAYLOAD_LIST_MAX_ITEMS
    items: list[Any] = []
    for item in value[:PAYLOAD_LIST_MAX_ITEMS]:
        bounded_item, item_cut = _bounded(item, depth + 1)
        items.append(bounded_item)
        cut = cut or item_cut
    return items, cut


def _bounded(value: Any, depth: int = 0) -> tuple[Any, bool]:
    """``value`` with long strings and lists cut; the flag says whether anything was."""
    if isinstance(value, str):
        if len(value) > PAYLOAD_STRING_MAX_CHARS:
            return value[:PAYLOAD_STRING_MAX_CHARS] + _ELLIPSIS, True
        return value, False
    if isinstance(value, dict | list):
        return _bounded_container(value, depth)
    return value, False


def bound_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    """``(bounded payload, original UTF-8 JSON length, truncated?)`` for one event."""
    original = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    original_bytes = len(original.encode("utf-8"))
    if original_bytes <= PAYLOAD_MAX_BYTES:
        return payload, original_bytes, False
    bounded, _cut = _bounded(payload)
    text = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) <= PAYLOAD_MAX_BYTES:
        return bounded, original_bytes, True
    preview = original.encode("utf-8")[:PAYLOAD_MAX_BYTES].decode("utf-8", errors="ignore")
    return {"truncated_preview": preview}, original_bytes, True


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _claude_subtype(record: dict[str, Any], record_type: str) -> str | None:
    """The kept subtype of a ``system`` / ``attachment`` record, else ``None``."""
    if record_type == "system":
        subtype = record.get("subtype")
        return subtype if isinstance(subtype, str) and subtype in CLAUDE_SYSTEM_SUBTYPES else None
    if record_type == "attachment":
        attachment = record.get("attachment")
        kind = attachment.get("type") if isinstance(attachment, dict) else None
        if isinstance(kind, str) and (
            kind.startswith(_CLAUDE_HOOK_ATTACHMENT_PREFIX) or kind in CLAUDE_ATTACHMENT_TYPES
        ):
            return kind
    return None


def _claude_kind(record: dict[str, Any]) -> tuple[str, str | None] | None:
    """``(event_type, subtype)`` when a Claude Code record is kept, else ``None``."""
    record_type = record.get("type")
    if not isinstance(record_type, str):
        return None
    if record_type in CLAUDE_WHOLE_RECORD_TYPES:
        return record_type, None
    subtype = _claude_subtype(record, record_type)
    return None if subtype is None else (record_type, subtype)


def is_claude_session_event(record: dict[str, Any]) -> bool:
    """Whether a Claude Code record becomes a session_events row."""
    return _claude_kind(record) is not None


def _claude_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in record.items() if key not in _CLAUDE_ENVELOPE}
    attachment = record.get("attachment")
    if isinstance(attachment, dict):
        payload.update(
            {key: value for key, value in attachment.items() if key not in {"type", "toolUseID"}}
        )
    return payload


def _claude_tool_use_id(record: dict[str, Any]) -> str | None:
    own = _str_or_none(record.get("toolUseID"))
    if own is not None:
        return own
    attachment = record.get("attachment")
    return _str_or_none(attachment.get("toolUseID")) if isinstance(attachment, dict) else None


def _row(
    seq: int,
    *,
    ts: Any,
    kind: tuple[str, str | None],
    uuid: str | None,
    parent_uuid: str | None,
    tool_use_id: str | None,
    is_sidechain: bool,
    source_file: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    bounded, original_bytes, truncated = bound_payload(payload)
    return {
        "seq": seq,
        "ts": _str_or_none(ts),
        "event_type": kind[0],
        "subtype": kind[1],
        "uuid": uuid,
        "parent_uuid": parent_uuid,
        "tool_use_id": tool_use_id,
        "is_sidechain": is_sidechain,
        "source_file": source_file,
        "payload": bounded,
        "payload_bytes": original_bytes,
        "payload_truncated": truncated,
    }


def claude_session_events(records: Iterable[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """The kept Claude Code records as rows, in the order the pairs arrive."""
    rows: list[dict[str, Any]] = []
    for record, source_file in records:
        kind = _claude_kind(record)
        if kind is None:
            continue
        rows.append(
            _row(
                len(rows),
                ts=record.get("timestamp"),
                kind=kind,
                uuid=_str_or_none(record.get("uuid")),
                parent_uuid=_str_or_none(record.get("parentUuid")),
                tool_use_id=_claude_tool_use_id(record),
                is_sidechain=bool(record.get("isSidechain", False)),
                source_file=source_file,
                payload=_claude_payload(record),
            )
        )
    return rows


def _codex_kind(record: dict[str, Any]) -> tuple[str, str | None] | None:
    record_type = record.get("type")
    if not isinstance(record_type, str):
        return None
    if record_type in CODEX_WHOLE_RECORD_TYPES:
        return record_type, None
    payload = record.get("payload")
    if record_type == "event_msg" and isinstance(payload, dict):
        kind = payload.get("type")
        if isinstance(kind, str) and kind in CODEX_EVENT_MSG_TYPES:
            return record_type, kind
    return None


def is_codex_session_event(record: dict[str, Any]) -> bool:
    """Whether a Codex rollout record becomes a session_events row."""
    return _codex_kind(record) is not None


def codex_session_events(records: Iterable[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """The kept Codex records as rows, in file order.

    ``uuid`` is the same id ``edges.jsonl`` gives the record
    (:func:`~atif_converter.domain.codex_edges.record_id`, line-numbered per
    source file), so a row joins ``messages`` on it. A rollout has no parent
    pointer and no sidechain, so those columns are ``None`` / ``False``.
    """
    rows: list[dict[str, Any]] = []
    line_numbers: dict[str, int] = {}
    for record, source_file in records:
        line_numbers[source_file] = line_numbers.get(source_file, 0) + 1
        kind = _codex_kind(record)
        if kind is None:
            continue
        payload = record.get("payload")
        body = (
            {key: value for key, value in payload.items() if key != "type"}
            if isinstance(payload, dict)
            else {}
        )
        rows.append(
            _row(
                len(rows),
                ts=record.get("timestamp"),
                kind=kind,
                uuid=record_id(record, line_numbers[source_file]),
                parent_uuid=None,
                tool_use_id=None,
                is_sidechain=False,
                source_file=source_file,
                payload=body,
            )
        )
    return rows


#: ``final_metrics.extra`` keys the reported cost lands under.
REPORTED_COST_KEY: Final = "reported_cost_usd"
REPORTED_COST_SOURCE_KEY: Final = "reported_cost_source"
REPORTED_UNKNOWN_MODEL_COST_KEY: Final = "reported_cost_has_unknown_model_cost"
#: ``reported_cost_source`` for a figure read off a Claude Code ``cost-state`` record.
REPORTED_COST_SOURCE_COST_STATE: Final = "claude_code_cost_state"


def claude_reported_cost(records: Iterable[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    """What Claude Code itself says the session cost, for ``final_metrics.extra``.

    Claude Code appends a ``cost-state`` record as the session runs; its
    ``totalCostUSD`` is cumulative across resumes (measured 2026-09-27: the
    value only grows and ``startTime`` stays fixed across a resume), so the
    LAST one in record order is the session's figure. Returns ``{}`` when no
    record carries a numeric ``totalCostUSD``. ``hasUnknownModelCost`` is kept
    beside it because Claude Code sets it when it met a model it couldn't
    price, which makes its own total a lower bound.
    """
    last: dict[str, Any] | None = None
    for record, _source_file in records:
        if record.get("type") != "cost-state" or record.get("isSidechain"):
            continue
        total = record.get("totalCostUSD")
        if isinstance(total, int | float) and not isinstance(total, bool):
            last = record
    if last is None:
        return {}
    reported: dict[str, Any] = {
        REPORTED_COST_KEY: float(last["totalCostUSD"]),
        REPORTED_COST_SOURCE_KEY: REPORTED_COST_SOURCE_COST_STATE,
    }
    unknown = last.get("hasUnknownModelCost")
    if isinstance(unknown, bool):
        reported[REPORTED_UNKNOWN_MODEL_COST_KEY] = unknown
    return reported


def session_event_to_jsonl_line(row: dict[str, Any]) -> str:
    """Serialize one row as a compact, key-order-stable JSONL line."""
    ordered = {key: row.get(key) for key in SESSION_EVENT_FIELDS}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def session_events_jsonl_lines(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The ready-to-write session_events.jsonl body."""
    return [session_event_to_jsonl_line(row) for row in rows]


__all__ = [
    "CLAUDE_ATTACHMENT_TYPES",
    "CLAUDE_SYSTEM_SUBTYPES",
    "CLAUDE_WHOLE_RECORD_TYPES",
    "CODEX_EVENT_MSG_TYPES",
    "CODEX_WHOLE_RECORD_TYPES",
    "PAYLOAD_LIST_MAX_ITEMS",
    "PAYLOAD_MAX_BYTES",
    "PAYLOAD_STRING_MAX_CHARS",
    "REPORTED_COST_KEY",
    "REPORTED_COST_SOURCE_COST_STATE",
    "REPORTED_COST_SOURCE_KEY",
    "REPORTED_UNKNOWN_MODEL_COST_KEY",
    "SESSION_EVENT_FIELDS",
    "bound_payload",
    "claude_reported_cost",
    "claude_session_events",
    "codex_session_events",
    "is_claude_session_event",
    "is_codex_session_event",
    "session_event_to_jsonl_line",
    "session_events_jsonl_lines",
]
