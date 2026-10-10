# SPDX-License-Identifier: Apache-2.0

"""Typed tool-outcome, attachment and subagent fields, written into the trajectory.

A second post-conversion pass beside :mod:`atif_converter.domain.enrichment`
and :mod:`atif_converter.domain.codex_enrichment`: pure functions over the
trajectory dict and the raw records, no harbor import, no I/O. Everything it
adds lands under ``extra``, which ATIF leaves free-form, apart from a Codex
script's nested tool calls, each a ``ToolCall`` with its result on the step
that already holds the script's call, which is the shape the validator asks
for. The trajectory still validates, and the harbor port itself, which this
pass runs after, stays at parity with its oracle.

What it writes
--------------
On every ``observation.results[]`` entry, under ``extra``:

``is_error``
    Claude Code: the ``is_error`` flag of the ``tool_result`` block, the flag
    the harness sent the model. The Messages API defaults it to false, so an
    absent flag is ``false``. Codex: ``true`` when the call failed (a
    ``failed`` status on its item, a non-zero exit code, an MCP ``isError``),
    ``false`` when it completed cleanly, absent when the rollout says neither.
    A code-mode ``exec`` script is ``true`` when its output says ``Script
    failed`` or a command, MCP call or patch it ran failed, and absent while
    it's still running.
``exit_code``
    The process exit code, only where the transcript states one. Claude Code
    states it for a failed ``Bash`` call alone, as the fixed ``Exit code N``
    first line of the result; a successful call states nothing (and a call
    with a ``returnCodeInterpretation``, such as grep's "No matches found",
    is not an error yet exited non-zero), so it stays absent rather than being
    guessed as 0. Codex states it structurally on the ``CommandExecution``
    item, and in the ``Process exited with code N`` / ``Exit code: N`` header
    of an output that has no item. An ``exec`` script's commands complete
    items of their own, with no link back to the script, so they're matched
    to it by position (between its call and its output, in the same turn); its
    exit code is the first non-zero one they report, else 0, and absent when
    it ran no command.
``interrupted``
    Claude Code's ``toolUseResult.interrupted`` (``Bash`` reports it).
``images``
    The attachments :mod:`atif_converter.domain.blobs` lifted out of this
    result, as ``BlobRef.to_json()`` entries (``image/*`` only).

Nested tool calls
-----------------
A code-mode ``exec`` script's nested MCP calls and commands
(:data:`~atif_converter.domain.codex_nested_calls.NESTED_CALL_ITEM_TYPES`
items, attributed to the script by position as ``exit_code`` above says) are
added to the script's step as tool calls of their own, right after the
``exec`` call, each with ``extra.nested_in`` naming it and one result carrying
its own ``is_error`` / ``exit_code`` (and ``images``). A script whose
attribution is ambiguous adds none, an item whose id is already a call id is
never added twice, and the ``exec`` result keeps the outcome folded from its
items. :mod:`atif_converter.domain.codex_nested_calls` says what each field
holds.

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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from atif_converter.domain.codex_nested_calls import (
    NESTED_CALL_ITEM_TYPES,
    nested_result_content,
    nested_tool_call,
)

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

#: The first line of an ``exec`` script's output: how the script itself ended.
_CODEX_SCRIPT_RE = re.compile(r"\AScript (completed|failed|running)\b")

#: Codex's code-mode tool: one JavaScript script that calls other tools.
_EXEC_TOOL = "exec"

#: Where a Codex call record names its turn.
_PASSTHROUGH_KEY = "internal_chat_message_metadata_passthrough"

#: Response items that open a tool call, and the ones that answer it.
_CODEX_CALL_TYPES: frozenset[str] = frozenset({"function_call", "custom_tool_call"})
_CODEX_OUTPUT_TYPES: frozenset[str] = frozenset({"function_call_output", "custom_tool_call_output"})

#: Completed items whose only outcome signal is a ``status``.
_STATUS_ITEM_TYPES: frozenset[str] = frozenset({"FileChange", "CollabAgentToolCall"})

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
    if _str(tool_name) not in _EXIT_CODE_TOOLS or not facts.is_error:
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
        if (
            isinstance(agent_id, str)
            and agent_id
            and _str(tool_names.get(call_id)) in {"Task", "Agent"}
        ):
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


def _status_outcome(item: dict[str, Any]) -> _CodexOutcome | None:
    """A ``FileChange`` / ``CollabAgentToolCall`` item's ``status``, the only signal it carries."""
    status = item.get("status")
    if status == "failed":
        return _CodexOutcome(is_error=True, exit_code=None)
    if status == "completed":
        return _CodexOutcome(is_error=False, exit_code=None)
    return None


def _item_outcome(item: dict[str, Any]) -> _CodexOutcome | None:
    """What a completed tool item says about the call it belongs to."""
    item_type = item.get("type")
    if item_type == "CommandExecution":
        return _command_outcome(item)
    if item_type == "McpToolCall":
        return _mcp_outcome(item)
    if _str(item_type) in _STATUS_ITEM_TYPES:
        return _status_outcome(item)
    return None


def _combine(primary: _CodexOutcome, fallback: _CodexOutcome) -> _CodexOutcome:
    """Two statements about one call: any failure wins, ``primary``'s exit code first."""
    if primary.is_error is True or fallback.is_error is True:
        is_error: bool | None = True
    else:
        is_error = primary.is_error if primary.is_error is not None else fallback.is_error
    exit_code = primary.exit_code if primary.exit_code is not None else fallback.exit_code
    return _CodexOutcome(is_error=is_error, exit_code=exit_code)


@dataclass(slots=True)
class _ExecWindow:
    """One ``exec`` script call, from its call record to its output record.

    The items a script's nested tool calls complete carry ids of their own and
    no link to the script, so they're attributed by position: an item that
    completes while exactly one script of its turn is open belongs to it. An
    item that could belong to more than one open script makes each of them
    ``ambiguous``, and an ambiguous script claims nothing its items said.
    ``calls`` keeps the ``item_completed`` payloads of the MCP calls and
    commands attributed to it, in completion order, for
    :func:`_emit_nested_calls`.
    """

    turn_id: str | None
    nested: list[_CodexOutcome] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False


def _exec_outcome(output: Any, window: _ExecWindow | None) -> _CodexOutcome | None:
    """An ``exec`` script's outcome: its header, then the items its nested calls completed.

    ``Script failed`` is an error. ``Script completed`` is an error when a
    nested command, MCP call or patch failed. ``Script running`` (the script
    yielded) states nothing yet. The exit code describes the processes the
    script ran: the first non-zero one, else 0 when every one exited 0, absent
    when it ran none.
    """
    text = _output_text(output)
    match = _CODEX_SCRIPT_RE.match(text) if text is not None else None
    if match is None or match.group(1) == "running":
        return None
    failed = match.group(1) == "failed"
    if window is None or window.ambiguous:
        return _CodexOutcome(is_error=True, exit_code=None) if failed else None
    codes = [outcome.exit_code for outcome in window.nested if outcome.exit_code is not None]
    exit_code = next((code for code in codes if code != 0), 0 if codes else None)
    is_error = failed or any(outcome.is_error is True for outcome in window.nested)
    return _CodexOutcome(is_error=is_error, exit_code=exit_code)


def _turn_id(holder: Any) -> str | None:
    value = holder.get("turn_id") if isinstance(holder, dict) else None
    return value if isinstance(value, str) and value else None


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


class _CodexIndex:
    """The outcome per call id, built in one pass over a rollout's records."""

    def __init__(self) -> None:
        self.items: dict[str, _CodexOutcome] = {}
        self.outputs: dict[str, _CodexOutcome] = {}
        self.call_ids: set[str] = set()
        self.exec_calls: dict[str, _ExecWindow] = {}
        self.open_exec: dict[str, _ExecWindow] = {}

    def call(self, payload: dict[str, Any]) -> None:
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        self.call_ids.add(call_id)
        if payload.get("type") == "custom_tool_call" and payload.get("name") == _EXEC_TOOL:
            window = _ExecWindow(turn_id=_turn_id(payload.get(_PASSTHROUGH_KEY)))
            self.exec_calls.setdefault(call_id, window)
            self.open_exec[call_id] = window

    def item(self, payload: dict[str, Any]) -> None:
        item = payload.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return
        outcome = _item_outcome(item)
        if outcome is None:
            return
        item_id = item["id"]
        known = self.items.get(item_id)
        self.items[item_id] = outcome if known is None else _combine(known, outcome)
        if item_id in self.call_ids:
            return
        turn_id = _turn_id(payload)
        candidates = [
            window
            for window in self.open_exec.values()
            if turn_id is None or window.turn_id is None or window.turn_id == turn_id
        ]
        if len(candidates) == 1:
            candidates[0].nested.append(outcome)
            if item.get("type") in NESTED_CALL_ITEM_TYPES:
                candidates[0].calls.append(payload)
        else:
            for window in candidates:
                window.ambiguous = True

    def output(self, payload: dict[str, Any]) -> None:
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        self.open_exec.pop(call_id, None)
        if call_id in self.exec_calls:
            outcome = _exec_outcome(payload.get("output"), self.exec_calls[call_id])
        else:
            outcome = _output_outcome(payload.get("output"))
        if outcome is not None:
            self.outputs.setdefault(call_id, outcome)

    def outcomes(self) -> dict[str, _CodexOutcome]:
        """A structured item outranks a parsed header for the same call's exit code."""
        merged = dict(self.outputs)
        for call_id, outcome in self.items.items():
            known = merged.get(call_id)
            merged[call_id] = outcome if known is None else _combine(outcome, known)
        return merged


def _index_codex_records(records: Iterable[Any]) -> _CodexIndex:
    index = _CodexIndex()
    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        record_type = record.get("type")
        if record_type == "event_msg" and payload_type == "item_completed":
            index.item(payload)
        elif record_type == "response_item" and _str(payload_type) in _CODEX_CALL_TYPES:
            index.call(payload)
        elif record_type == "response_item" and _str(payload_type) in _CODEX_OUTPUT_TYPES:
            index.output(payload)
    return index


def _write_outcome(
    result: dict[str, Any], outcome: _CodexOutcome | None, images: list[dict[str, Any]]
) -> None:
    if outcome is None and not images:
        return
    extra = _extra(result)
    if outcome is not None and outcome.is_error is not None:
        extra["is_error"] = outcome.is_error
    if outcome is not None and outcome.exit_code is not None:
        extra["exit_code"] = outcome.exit_code
    if images:
        extra["images"] = images


def _trajectory_call_ids(steps: Iterable[Any]) -> set[str]:
    ids: set[str] = set()
    for step in steps:
        calls = step.get("tool_calls") if isinstance(step, dict) else None
        for call in calls if isinstance(calls, list) else ():
            call_id = call.get("tool_call_id") if isinstance(call, dict) else None
            if isinstance(call_id, str):
                ids.add(call_id)
    return ids


def _nested_pairs(
    window: _ExecWindow,
    exec_call_id: str,
    taken: set[str],
    outcomes: Mapping[str, _CodexOutcome],
    blob_index: BlobIndex,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The ``(ToolCall, ObservationResult)`` JSON pairs one script's nested items become.

    An item id already taken (a top-level call's id, or an item emitted
    before) is skipped, so no call appears twice.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for payload in window.calls:
        item = payload["item"]
        item_id = item["id"]
        if item_id in taken:
            continue
        taken.add(item_id)
        result: dict[str, Any] = {"source_call_id": item_id}
        content = nested_result_content(item)
        if content is not None:
            result["content"] = content
        _write_outcome(
            result,
            outcomes.get(item_id),
            _image_json(blob_index.by_tool_call_id.get(item_id, ())),
        )
        pairs.append((nested_tool_call(payload, exec_call_id), result))
    return pairs


def _insert_after(results: list[Any], exec_call_id: str, nested: list[dict[str, Any]]) -> None:
    """Put ``nested`` right after the ``exec`` call's own result, or last when it has none."""
    for position, result in enumerate(results):
        if isinstance(result, dict) and result.get("source_call_id") == exec_call_id:
            results[position + 1 : position + 1] = nested
            return
    results.extend(nested)


def _emit_nested_calls(
    steps: Sequence[Any],
    index: _CodexIndex,
    outcomes: Mapping[str, _CodexOutcome],
    blob_index: BlobIndex,
) -> int:
    """Add each script's nested MCP calls and commands to the step of its ``exec`` call.

    A script whose window is ``ambiguous`` emits nothing, as it claims no
    outcome. Each nested call follows its ``exec`` call in ``tool_calls`` and
    its result follows the script's result, in completion order. Returns how
    many calls were added.
    """
    taken = _trajectory_call_ids(steps) | index.call_ids
    emitted = 0
    for step in steps:
        calls = step.get("tool_calls") if isinstance(step, dict) else None
        if not isinstance(calls, list):
            continue
        new_calls: list[Any] = []
        nested_results: list[tuple[str, list[dict[str, Any]]]] = []
        for call in calls:
            new_calls.append(call)
            call_id = call.get("tool_call_id") if isinstance(call, dict) else None
            window = index.exec_calls.get(call_id) if isinstance(call_id, str) else None
            if call_id is None or window is None or window.ambiguous:
                continue
            pairs = _nested_pairs(window, call_id, taken, outcomes, blob_index)
            new_calls.extend(tool_call for tool_call, _result in pairs)
            if pairs:
                nested_results.append((call_id, [result for _call, result in pairs]))
        if not nested_results:
            continue
        step["tool_calls"] = new_calls
        observation = step.get("observation")
        if not isinstance(observation, dict) or not isinstance(observation.get("results"), list):
            observation = {"results": []}
            step["observation"] = observation
        for exec_call_id, results in nested_results:
            _insert_after(observation["results"], exec_call_id, results)
            emitted += len(results)
    return emitted


def annotate_codex_trajectory(
    trajectory: dict[str, Any],
    records: Sequence[Any],
    blob_index: BlobIndex,
) -> dict[str, Any]:
    """The Codex half: ``is_error`` / ``exit_code`` / ``images`` per result, user-step images.

    Also emits each code-mode script's nested MCP calls and commands as tool
    calls of their own (module docstring, and
    :mod:`atif_converter.domain.codex_nested_calls`).

    ``records`` are the rollout's parsed records, already rewritten by
    :func:`~atif_converter.domain.blobs.extract_codex_blobs` with the same
    record keys the enrichment pass writes into ``source_uuids``.
    """
    steps = trajectory.get("steps")
    step_list: list[Any] = steps if isinstance(steps, list) else []
    index = _index_codex_records(records)
    outcomes = index.outcomes()
    for result in _results(step_list):
        call_id = result.get("source_call_id")
        if not isinstance(call_id, str):
            continue
        _write_outcome(
            result,
            outcomes.get(call_id),
            _image_json(blob_index.by_tool_call_id.get(call_id, ())),
        )
    _emit_nested_calls(step_list, index, outcomes, blob_index)
    _annotate_steps(step_list, {}, blob_index)
    return trajectory


def _str(value: object) -> str | None:
    """``value`` when it is a string, else ``None``: a set-membership key that cannot raise.

    A transcript field can hold any JSON value, and an object or a list is unhashable, so
    testing one against a set raised ``TypeError`` past the converter (found by the fuzzer).
    A non-string value never matched one of these string sets, so no result changes.
    """
    return value if isinstance(value, str) else None


__all__ = [
    "annotate_claude_code_trajectory",
    "annotate_codex_trajectory",
]
