# SPDX-License-Identifier: Apache-2.0

"""Synthetic Codex rollout fixture for pinning harbor 0.22.0's Codex converter.

One rollout under ``<tmp>/sessions/2026/09/11/`` carrying every record shape
the fidelity policy makes a claim about:

- ``session_meta`` with the cli version, cwd and git struct;
- ``turn_context`` naming the model;
- a ``developer`` message and a ``user`` message (both flatten differently);
- a ``reasoning`` item whose ``summary`` is EMPTY and whose content is
  encrypted — the shape that loses reasoning entirely;
- an assistant message with NO text, followed by a ``function_call`` and its
  ``function_call_output`` in the same API call, which is the case a
  text-matching walk cannot attribute;
- ``tool_search_call`` / ``tool_search_output``, which harbor drops;
- ``token_count`` events closing each API call, carrying the cache split;
- a ``compacted`` record, a ``world_state`` record and a
  ``token_usage_record``, all of which harbor drops.

The ids are deliberately mixed: some records carry a payload ``id`` and some
carry none, so the synthetic-id fallback in
:func:`atif_converter.domain.codex_edges.record_id` is exercised by the fixture
rather than only by a unit test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

#: The session uuid — the rollout filename's trailing five hyphen groups, and
#: the ``id`` inside ``session_meta``, which real rollouts always agree on.
CODEX_SESSION_ID = "01a09182-2858-7f42-936b-7f027b341fdf"

#: The rollout filename, in Codex's own shape.
CODEX_ROLLOUT_NAME = f"rollout-2026-09-11T17-27-01-{CODEX_SESSION_ID}.jsonl"

#: The model ``turn_context`` names, which harbor uses for every agent step.
CODEX_MODEL = "bedrock-native/global.openai.gpt-6-astra"

#: codex-cli version in ``session_meta``; harbor reports it as agent.version.
CODEX_CLI_VERSION = "0.154.0"


def _token_count(input_tokens: int, output_tokens: int, cache_write: int) -> dict[str, Any]:
    """A ``token_count`` event — what closes one model API call for harbor."""
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }
    return {
        "type": "token_count",
        "info": {
            "total_token_usage": dict(usage),
            "last_token_usage": dict(usage),
            "model_context_window": 258_400,
        },
    }


def codex_rollout_records() -> list[dict[str, Any]]:
    """The rollout's records, in file order."""
    return [
        {
            "timestamp": "2026-09-11T17:27:01.000Z",
            "type": "session_meta",
            "payload": {
                "id": CODEX_SESSION_ID,
                "session_id": CODEX_SESSION_ID,
                "timestamp": "2026-09-11T17:27:01.000Z",
                "cwd": "/home/alice/proj",
                "originator": "codex-tui",
                "cli_version": CODEX_CLI_VERSION,
                "source": "cli",
                "git": {"branch": "main", "commit_hash": "abc1234"},
            },
        },
        {
            "timestamp": "2026-09-11T17:27:01.100Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-09-11T17:27:01.200Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "cwd": "/home/alice/proj", "model": CODEX_MODEL},
        },
        {
            "timestamp": "2026-09-11T17:27:01.300Z",
            "type": "world_state",
            "payload": {"full": True, "state": {"model": CODEX_MODEL}},
        },
        {
            "timestamp": "2026-09-11T17:27:01.400Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_dev_1",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<skills_instructions>use them"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:01.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_user_1",
                "role": "user",
                "content": [{"type": "input_text", "text": "read marker.txt with a shell command"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "rs_1",
                # Empty summary + encrypted content: reasoning is unrecoverable.
                "summary": [],
                "encrypted_content": "rsn_opaque",
            },
        },
        {
            # An assistant message with NO text. harbor filters empty message
            # parts out of the bundle, so this record contributes nothing to
            # any step's text and only its API call id can place it.
            "timestamp": "2026-09-11T17:27:02.100Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_agent_empty",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.200Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": '{"cmd":"cat marker.txt"}',
                "status": "completed",
            },
        },
        {
            # No payload id at all — the synthetic-id fallback case.
            "timestamp": "2026-09-11T17:27:02.300Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"output":"marker-ok","metadata":{"exit_code":0}}',
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.400Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_call",
                "id": "tsc_1",
                "call_id": "call_search_1",
                "status": "completed",
                "arguments": {"query": "outlook email tools"},
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.500Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_output",
                "call_id": "call_search_1",
                "status": "completed",
                "tools": [{"type": "function", "name": "email_read"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.600Z",
            "type": "event_msg",
            "payload": _token_count(1_000, 40, 900),
        },
        {
            "timestamp": "2026-09-11T17:27:02.700Z",
            "type": "token_usage_record",
            "payload": {"turn_id": "turn-1", "usage": {"input_tokens": 1_000}},
        },
        {
            "timestamp": "2026-09-11T17:27:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_agent_final",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "marker-ok"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:03.100Z",
            "type": "event_msg",
            "payload": _token_count(1_100, 8, 0),
        },
        {
            "timestamp": "2026-09-11T17:27:03.200Z",
            "type": "compacted",
            "payload": {"message": "summary of the work so far", "replacement_history": []},
        },
        {
            "timestamp": "2026-09-11T17:27:03.300Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
    ]


def write_codex_rollout(sessions_root: Path, records: list[dict[str, Any]] | None = None) -> Path:
    """Write the rollout into Codex's ``<YYYY>/<MM>/<DD>/`` layout; return its path."""
    day_dir = sessions_root / "2026" / "09" / "11"
    day_dir.mkdir(parents=True, exist_ok=True)
    rollout = day_dir / CODEX_ROLLOUT_NAME
    payload = codex_rollout_records() if records is None else records
    rollout.write_text("\n".join(json.dumps(record) for record in payload) + "\n")
    return rollout


@pytest.fixture
def codex_rollout(tmp_path: Path) -> Path:
    """Path to the synthetic rollout, in Codex's on-disk layout."""
    return write_codex_rollout(tmp_path / "sessions")
