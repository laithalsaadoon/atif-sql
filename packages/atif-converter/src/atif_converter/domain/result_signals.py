# SPDX-License-Identifier: Apache-2.0

"""Typed tool-outcome, attachment and subagent fields, written into the trajectory.

A second post-conversion pass beside :mod:`atif_converter.domain.enrichment`
and :mod:`atif_converter.domain.codex_enrichment`: pure functions over the
trajectory dict and the raw records, no harbor import, no I/O. Everything it
adds lands under ``extra``, which ATIF leaves free-form, so the trajectory
still validates, and the harbor port itself stays at parity with its oracle.

What it writes
--------------
On every ``observation.results[]`` entry, under ``extra``:

``is_error``
    Claude Code: the ``is_error`` flag of the ``tool_result`` block, the flag
    the harness sent the model. The Messages API defaults it to false, so an
    absent flag is ``false``. Codex: ``true`` when the command failed (a
    ``failed`` status, a non-zero exit code, an MCP ``isError``), ``false``
    when it completed cleanly, absent when the rollout says neither.
``exit_code``
    The process exit code, only where the transcript states one. Claude Code
    states it for a failed ``Bash`` call alone, as the fixed ``Exit code N``
    first line of the result; a successful call states nothing (and a call
    with a ``returnCodeInterpretation``, such as grep's "No matches found",
    is not an error yet exited non-zero), so it stays absent rather than being
    guessed as 0. Codex states it structurally on the ``CommandExecution``
    item, and in the ``Process exited with code N`` / ``Exit code: N`` header
    of an output that has no item.
``interrupted``
    Claude Code's ``toolUseResult.interrupted`` (``Bash`` reports it).
``images``
    The attachments :mod:`atif_converter.domain.blobs` lifted out of this
    result, as ``BlobRef.to_json()`` entries (``image/*`` only).

On steps, under ``extra``:

``agent_id``
    The subagent a sidechain step belongs to. The port sets it on assistant
    steps; this pass fills it on the remaining steps (the subagent's prompt,
    an orphan tool result) from the ``agentId`` of the records the step came
    from, when those records name exactly one.
``images``
    On a user step: attachments pasted into the message itself.

On the trajectory, under ``extra``:

``subagents``
    One entry per subagent the session spawned, from its ``agent-*.meta.json``
    sidecar when there is one (type, description, the ``toolUseId`` of the
    spawning call, spawn depth, parent agent) and otherwise from the spawning
    call's own result (``toolUseResult.agentId``), which is the structured
    link the result text used to be regexed for. ``link_source`` says which.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from atif_converter.domain.blobs import BlobIndex, BlobRef

#: Claude Code's first line on a failed Bash call.
_CLAUDE_EXIT_RE = re.compile(r"\A(?:Error: )?Exit code (-?\d+)\b")

#: Codex's output headers that state an exit code, within the first lines.
_CODEX_EXIT_RE = re.compile(r"^(?:Process exited with code|Exit code:) (-?\d+)\b", re.MULTILINE)

#: How much of a Codex output the header search reads. The header sits in the
#: first few lines; bounding the search keeps an exit code printed deep inside
#: the command's own output from being taken for the process's.
_CODEX_HEADER_CHARS = 512

#: Claude Code tools whose failed result leads with ``Exit code N``.
_EXIT_CODE_TOOLS: frozenset[str] = frozenset({"Bash"})

#: Sidecar keys copied into a ``subagents`` entry, under snake_case names.
_SIDECAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("agentType", "agent_type"),
    ("description", "description"),
    ("toolUseId", "parent_tool_call_id"),
    ("spawnDepth", "spawn_depth"),
    ("parentAgentId", "parent_agent_id"),
)


def _extra(holder: dict[str, Any]) -> dict[str, Any]:
    extra = holder.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        holder["extra"] = extra
    return extra


def _results(steps: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for step in steps:
        if not isinstance(step, dict):
            continue
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else None
        if not isinstance(results, list):
            continue
        for result in results:
            if isinstance(result, dict):
                yield result


def _image_json(refs: Sequence[BlobRef]) -> list[dict[str, Any]]:
    return [ref.to_json() for ref in refs if ref.is_image]


def _parse_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ClaudeResult:
    """The raw facts about one tool result, as its record carried them."""

    is_error: bool
    first_text: str | None
    tool_use_result: Any
    agent_id: str | None


def _first_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                return text
    return None


def _index_claude_records(
    records: Iterable[Any],
) -> tuple[dict[str, str], dict[str, _ClaudeResult], dict[str, str]]:
    """``(tool name by call id, result facts by call id, agentId by record uuid)``.

    The first occurrence of a call id wins, as harbor's own dedup keeps it.
    """
    tool_names: dict[str, str] = {}
    results: dict[str, _ClaudeResult] = {}
    agent_by_uuid: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        agent_id = record.get("agentId")
        agent_id = agent_id if isinstance(agent_id, str) and agent_id else None
        uuid = record.get("uuid")
        if agent_id and isinstance(uuid, str) and uuid:
            agent_by_uuid.setdefault(uuid, agent_id)
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            call_id = block.get("id") if block.get("type") == "tool_use" else None
            if isinstance(call_id, str) and call_id and isinstance(block.get("name"), str):
                tool_names.setdefault(call_id, block["name"])
            if block.get("type") != "tool_result":
                continue
            result_id = block.get("tool_use_id")
            if not isinstance(result_id, str) or not result_id or result_id in results:
                continue
            results[result_id] = _ClaudeResult(
                is_error=block.get("is_error") is True,
                first_text=_first_text(block.get("content")),
                tool_use_result=record.get("toolUseResult"),
                agent_id=agent_id,
            )
    return tool_names, results, agent_by_uuid


def _claude_exit_code(tool_name: str | None, facts: _ClaudeResult) -> int | None:
    if tool_name not in _EXIT_CODE_TOOLS or not facts.is_error:
        return None
    for text in (facts.first_text, facts.tool_use_result):
        if isinstance(text, str):
            match = _CLAUDE_EXIT_RE.match(text)
            if match:
                return _parse_int(match.group(1))
    return None


def _annotate_claude_results(
    steps: Sequence[Any],
    tool_names: Mapping[str, str],
    results: Mapping[str, _ClaudeResult],
    blob_index: BlobIndex,
) -> None:
    for result in _results(steps):
        call_id = result.get("source_call_id")
        if not isinstance(call_id, str):
            continue
        facts = results.get(call_id)
        images = _image_json(blob_index.by_tool_call_id.get(call_id, ()))
        if facts is None and not images:
            continue
        extra = _extra(result)
        if facts is not None:
            extra["is_error"] = facts.is_error
            exit_code = _claude_exit_code(tool_names.get(call_id), facts)
            if exit_code is not None:
                extra["exit_code"] = exit_code
            tool_use_result = facts.tool_use_result
            if isinstance(tool_use_result, dict) and isinstance(
                tool_use_result.get("interrupted"), bool
            ):
                extra["interrupted"] = tool_use_result["interrupted"]
        if images:
            extra["images"] = images


def _annotate_steps(
    steps: Sequence[Any],
    agent_by_uuid: Mapping[str, str],
    blob_index: BlobIndex,
) -> None:
    """Fill ``agent_id`` on steps the port left without one; attach user-message images."""
    for step in steps:
        if not isinstance(step, dict):
            continue
        extra = step.get("extra")
        uuids = extra.get("source_uuids") if isinstance(extra, dict) else None
        if not isinstance(uuids, list):
            continue
        if isinstance(extra, dict) and not extra.get("agent_id"):
            agents = {agent_by_uuid[uuid] for uuid in uuids if uuid in agent_by_uuid}
            if len(agents) == 1:
                extra["agent_id"] = agents.pop()
        if step.get("source") == "user":
            refs: list[BlobRef] = []
            for uuid in uuids:
                refs.extend(blob_index.by_record_key.get(uuid, ()))
            images = _image_json(refs)
            if images:
                _extra(step)["images"] = images


def _subagent_entries(
    sidecars: Mapping[str, Any],
    tool_names: Mapping[str, str],
    results: Mapping[str, _ClaudeResult],
    agent_ids_seen: Iterable[str],
) -> list[dict[str, Any]]:
    """One ``trajectory.extra.subagents`` entry per subagent, sorted by ``agent_id``.

    ``sidecars`` maps an agent id to its parsed ``agent-<id>.meta.json``. A
    spawning call's result that names the agent (``toolUseResult.agentId``)
    supplies the link when the sidecar has none, and its ``description`` /
    ``agentType`` fill the gaps a sidecar leaves.
    """
    from_results: dict[str, tuple[str, dict[str, Any]]] = {}
    for call_id, facts in results.items():
        tool_use_result = facts.tool_use_result
        if not isinstance(tool_use_result, dict):
            continue
        agent_id = tool_use_result.get("agentId")
        if isinstance(agent_id, str) and agent_id and tool_names.get(call_id) in {"Task", "Agent"}:
            from_results.setdefault(agent_id, (call_id, tool_use_result))

    entries: list[dict[str, Any]] = []
    for agent_id in sorted({*sidecars, *from_results, *agent_ids_seen}):
        entry: dict[str, Any] = {"agent_id": agent_id}
        sidecar = sidecars.get(agent_id)
        if isinstance(sidecar, dict):
            for source_key, target_key in _SIDECAR_FIELDS:
                value = sidecar.get(source_key)
                if value is not None and value != "":
                    entry[target_key] = value
        link = from_results.get(agent_id)
        if "parent_tool_call_id" in entry:
            entry["link_source"] = "meta"
        elif link is not None:
            entry["parent_tool_call_id"] = link[0]
            entry["link_source"] = "tool_result"
        if link is not None:
            for source_key, target_key in (
                ("agentType", "agent_type"),
                ("description", "description"),
            ):
                value = link[1].get(source_key)
                if target_key not in entry and isinstance(value, str) and value:
                    entry[target_key] = value
        entries.append(entry)
    return entries


def annotate_claude_code_trajectory(
    trajectory: dict[str, Any],
    records: Sequence[Any],
    blob_index: BlobIndex,
    sidecars: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the typed fields (module docstring) into ``trajectory`` in place; return it.

    ``records`` are every parsed record of the session (main and side files),
    already rewritten by :func:`~atif_converter.domain.blobs.extract_claude_code_blobs`.
    ``sidecars`` maps agent id -> parsed ``agent-<id>.meta.json``. Run it AFTER
    :func:`~atif_converter.domain.enrichment.enrich_trajectory`: the step-level
    fields follow ``source_uuids``.
    """
    steps = trajectory.get("steps")
    step_list: list[Any] = steps if isinstance(steps, list) else []
    tool_names, results, agent_by_uuid = _index_claude_records(records)
    _annotate_claude_results(step_list, tool_names, results, blob_index)
    _annotate_steps(step_list, agent_by_uuid, blob_index)
    subagents = _subagent_entries(sidecars or {}, tool_names, results, set(agent_by_uuid.values()))
    if subagents:
        _extra(trajectory)["subagents"] = subagents
    return trajectory


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CodexOutcome:
    is_error: bool | None
    exit_code: int | None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _command_outcome(item: dict[str, Any]) -> _CodexOutcome:
    status = item.get("status")
    exit_code = _int_or_none(item.get("exit_code"))
    is_error: bool | None = None
    if status == "failed" or (exit_code is not None and exit_code != 0):
        is_error = True
    elif status == "completed" or exit_code == 0:
        is_error = False
    return _CodexOutcome(is_error=is_error, exit_code=exit_code)


def _mcp_outcome(item: dict[str, Any]) -> _CodexOutcome | None:
    status = item.get("status")
    result = item.get("result")
    if status == "failed" or (
        isinstance(result, dict) and (result.get("isError") is True or "Err" in result)
    ):
        return _CodexOutcome(is_error=True, exit_code=None)
    if status == "completed":
        return _CodexOutcome(is_error=False, exit_code=None)
    return None


def _item_outcome(item: dict[str, Any]) -> _CodexOutcome | None:
    """What a completed ``CommandExecution`` / ``McpToolCall`` item says about its call."""
    item_type = item.get("type")
    if item_type == "CommandExecution":
        return _command_outcome(item)
    if item_type == "McpToolCall":
        return _mcp_outcome(item)
    return None


def _legacy_metadata_outcome(output: str) -> _CodexOutcome | None:
    """``{"output": ..., "metadata": {"exit_code": N}}``, the older exec output shape."""
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    metadata = parsed.get("metadata") if isinstance(parsed, dict) else None
    code = _int_or_none(metadata.get("exit_code")) if isinstance(metadata, dict) else None
    return None if code is None else _CodexOutcome(is_error=code != 0, exit_code=code)


def _output_text(output: Any) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        for item in output:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                return text
    return None


def _output_outcome(output: Any) -> _CodexOutcome | None:
    """The exit code a tool output's own header or legacy metadata states."""
    if isinstance(output, str) and output.startswith("{"):
        return _legacy_metadata_outcome(output)
    text = _output_text(output)
    match = _CODEX_EXIT_RE.search(text[:_CODEX_HEADER_CHARS]) if text is not None else None
    exit_code = _parse_int(match.group(1)) if match is not None else None
    return (
        None if exit_code is None else _CodexOutcome(is_error=exit_code != 0, exit_code=exit_code)
    )


def _index_codex_records(records: Iterable[Any]) -> dict[str, _CodexOutcome]:
    items: dict[str, _CodexOutcome] = {}
    outputs: dict[str, _CodexOutcome] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if record.get("type") == "event_msg" and payload_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                outcome = _item_outcome(item)
                if outcome is not None:
                    items.setdefault(item["id"], outcome)
        elif record.get("type") == "response_item" and payload_type in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                outcome = _output_outcome(payload.get("output"))
                if outcome is not None:
                    outputs.setdefault(call_id, outcome)
    # A structured item outranks a parsed header for the same call.
    return {**outputs, **items}


def annotate_codex_trajectory(
    trajectory: dict[str, Any],
    records: Sequence[Any],
    blob_index: BlobIndex,
) -> dict[str, Any]:
    """The Codex half: ``is_error`` / ``exit_code`` / ``images`` per result, user-step images.

    ``records`` are the rollout's parsed records, already rewritten by
    :func:`~atif_converter.domain.blobs.extract_codex_blobs` with the same
    record keys the enrichment pass writes into ``source_uuids``.
    """
    steps = trajectory.get("steps")
    step_list: list[Any] = steps if isinstance(steps, list) else []
    outcomes = _index_codex_records(records)
    for result in _results(step_list):
        call_id = result.get("source_call_id")
        if not isinstance(call_id, str):
            continue
        outcome = outcomes.get(call_id)
        images = _image_json(blob_index.by_tool_call_id.get(call_id, ()))
        if outcome is None and not images:
            continue
        extra = _extra(result)
        if outcome is not None and outcome.is_error is not None:
            extra["is_error"] = outcome.is_error
        if outcome is not None and outcome.exit_code is not None:
            extra["exit_code"] = outcome.exit_code
        if images:
            extra["images"] = images
    _annotate_steps(step_list, {}, blob_index)
    return trajectory


__all__ = [
    "annotate_claude_code_trajectory",
    "annotate_codex_trajectory",
]
