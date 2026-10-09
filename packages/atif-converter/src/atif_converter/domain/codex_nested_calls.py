# SPDX-License-Identifier: Apache-2.0

"""A Codex code-mode script's nested MCP calls and commands, as ATIF tool calls.

In code mode Codex calls one ``exec`` tool whose JavaScript calls
``tools.mcp__<server>__<tool>(...)`` and ``tools.exec_command(...)``. The
rollout records each nested call as an ``event_msg`` ``item_completed`` item
(``McpToolCall`` or ``CommandExecution``, id ``exec-<uuid>``) between the
script's ``custom_tool_call`` and its ``custom_tool_call_output``, and harbor's
converter turns none of them into a tool call. Pure functions over one such
item: the :class:`~atif_converter.domain.atif.ToolCall` and
:class:`~atif_converter.domain.atif.ObservationResult` JSON it becomes. Which
script an item belongs to is :mod:`atif_converter.domain.result_signals`'s
attribution; that pass also writes the result's ``is_error`` / ``exit_code``.

The tool call
-------------
``tool_call_id``
    The item's own ``id``.
``function_name``
    ``mcp__<server>__<tool>`` for an MCP call, the name the script called;
    ``exec_command`` for a command.
``arguments``
    The MCP call's ``arguments`` object; ``{"cmd": <script>}`` for a command,
    unwrapped from Codex's ``[<shell>, "-c" | "-lc", <script>]`` argv.
``extra``
    ``nested_in`` (the ``exec`` call's id), ``via`` (``"exec"``) and, when the
    item states it, ``duration_ms``.

The result's content is the MCP result's content blocks rendered the way the
port renders a top-level tool output's blocks
(:func:`~atif_converter.domain.codex_conversion.structured_output_text`), or a
command's ``aggregated_output``.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Final

from atif_converter.domain.codex_conversion import structured_output_text

#: Completed item types that become a nested tool call.
NESTED_CALL_ITEM_TYPES: Final[frozenset[str]] = frozenset({"McpToolCall", "CommandExecution"})

#: The ``function_name`` a nested command gets: the tool the script called.
COMMAND_TOOL_NAME: Final = "exec_command"

#: ``extra.via`` on every nested call: the tool that ran it.
VIA_EXEC: Final = "exec"

#: A shell's flags before the script in Codex's command argv.
_SHELL_SCRIPT_FLAGS: Final[frozenset[str]] = frozenset({"-c", "-lc"})

#: The length of a ``[<shell>, <flag>, <script>]`` argv.
_SHELL_ARGV_LENGTH: Final = 3

_NANOS_PER_MS: Final = 1_000_000
_MS_PER_SEC: Final = 1000


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _arguments(value: Any) -> dict[str, Any]:
    """An MCP item's ``arguments`` as an object, by the port's rule for a call's arguments."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
    if value is None:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _command_text(command: Any) -> Any:
    """The script a command ran: unwrapped from its shell argv, else the argv as one line."""
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        if len(command) == _SHELL_ARGV_LENGTH and command[1] in _SHELL_SCRIPT_FLAGS:
            return command[2]
        return shlex.join(command)
    return command


def _duration_ms(item: dict[str, Any], payload: dict[str, Any]) -> float | None:
    """The call's duration: the item's ``{secs, nanos}``, else the payload's two timestamps."""
    duration = item.get("duration")
    if isinstance(duration, dict):
        secs, nanos = duration.get("secs"), duration.get("nanos")
        if isinstance(secs, int) and isinstance(nanos, int):
            return round(secs * _MS_PER_SEC + nanos / _NANOS_PER_MS, 3)
    started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
    if isinstance(started, int) and isinstance(completed, int) and completed >= started:
        return float(completed - started)
    return None


def nested_tool_call(payload: dict[str, Any], exec_call_id: str) -> dict[str, Any]:
    """The ``ToolCall`` JSON one ``item_completed`` payload's item becomes, nested in ``exec_call_id``."""
    item: dict[str, Any] = payload["item"]
    if item.get("type") == "CommandExecution":
        function_name = COMMAND_TOOL_NAME
        arguments: dict[str, Any] = {"cmd": _command_text(item.get("command"))}
    else:
        function_name = f"mcp__{item.get('server')}__{item.get('tool')}"
        arguments = _arguments(item.get("arguments"))
    extra: dict[str, Any] = {"nested_in": exec_call_id, "via": VIA_EXEC}
    duration = _duration_ms(item, payload)
    if duration is not None:
        extra["duration_ms"] = duration
    return {
        "tool_call_id": item["id"],
        "function_name": function_name,
        "arguments": arguments,
        "extra": extra,
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else _json_text(value)


def _result_text(result: dict[str, Any]) -> str:
    """An MCP result's text: its ``Err``, else its content blocks, else its JSON text."""
    if "Err" in result:
        return _text(result["Err"])
    content = result.get("content")
    if not isinstance(content, list):
        return _json_text(result)
    text = structured_output_text(content)
    if text is not None:
        return text
    return _json_text(content) if content else ""


def _mcp_content(item: dict[str, Any]) -> str | None:
    """The result's text, else the error's message (an item that failed before any result)."""
    result = item.get("result")
    if isinstance(result, dict):
        return _result_text(result)
    error = item.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str):
        return message
    return None if error is None else _text(error)


def nested_result_content(item: dict[str, Any]) -> str | None:
    """The observation content of one nested item: its MCP result or command output."""
    if item.get("type") == "CommandExecution":
        output = item.get("aggregated_output")
        return output if isinstance(output, str) else None
    return _mcp_content(item)


__all__ = [
    "COMMAND_TOOL_NAME",
    "NESTED_CALL_ITEM_TYPES",
    "VIA_EXEC",
    "nested_result_content",
    "nested_tool_call",
]
