# SPDX-License-Identifier: Apache-2.0

"""Synthetic Claude Code session fixture for pinning harbor 0.22.0 behavior.

The fixture builds one session under ``<tmp>/projects/-tmp-proj/`` containing:

- a user message,
- an assistant turn with thinking + tool_use + usage (incl. both cache fields),
- the user tool_result with ``toolUseResult``,
- an attachment record and a queue-operation record (dropped by harbor),
- one flat subagent side-file (harbor-visible),
- one workflow-nested subagent side-file (invisible to harbor — gap 1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# litellm loads its pricing table from GitHub at import time unless told to use
# the table bundled with the installed version. The frozen goldens carry
# per-call cost_usd figures priced through that table, so left remote, a
# BerriAI edit to one model's price (or an unreachable raw.githubusercontent.com
# in CI) would fail the "never skipped" golden gate with no change in this repo,
# and TestFrozenGoldens would blame harbor. Pinned to the bundled table, a price
# change arrives as a litellm lockfile bump, attributable. setdefault, so an
# operator can still point a run at the remote table deliberately.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """Return the path to the synthetic session's main JSONL file."""
    # KEEP cwd / gitBranch / agentId single-valued across these events. harbor
    # collects them into Python SETS and dumps the sets as lists, so with two or
    # more distinct values the order follows the process hash seed and the
    # frozen golden would flake between pytest runs. That is harbor's behavior
    # and the port keeps it (sorting would diverge from the live oracle); the
    # fixture just stays out of the nondeterministic case.
    project_dir = tmp_path / "projects" / "-tmp-proj"
    main_jsonl = project_dir / f"{SESSION_ID}.jsonl"

    events: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:01Z",
            "cwd": "/tmp/proj",  # noqa: S108 — fixture data, not a real temp path
            "gitBranch": "main",
            "version": "2.0.0",
            "isSidechain": False,
            "message": {"role": "user", "content": "hello please run a tool"},
        },
        {
            "type": "assistant",
            "uuid": "a-1",
            "parentUuid": "u-1",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:02Z",
            "isSidechain": False,
            "requestId": "req_001",
            "message": {
                "id": "msg_01",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "running the tool"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                },
            },
        },
        {
            "type": "user",
            "uuid": "u-2",
            "parentUuid": "a-1",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:03Z",
            "isSidechain": False,
            "toolUseResult": {"stdout": "hi", "stderr": "", "interrupted": False},
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "hi"}],
            },
        },
        {
            "type": "attachment",
            "uuid": "att-1",
            "timestamp": "2026-08-22T00:00:04Z",
            "attachment": {"type": "file", "path": "/tmp/x"},  # noqa: S108 — fixture data
        },
        {
            "type": "queue-operation",
            "uuid": "q-1",
            "timestamp": "2026-08-22T00:00:05Z",
            "operation": "enqueue",
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "parentUuid": "u-2",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:06Z",
            "isSidechain": False,
            "requestId": "req_002",
            "message": {
                "id": "msg_02",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 110,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ]
    _write_jsonl(main_jsonl, events)

    subagent_events: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "su-1",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:02.500Z",
            "isSidechain": True,
            "message": {"role": "user", "content": "subagent prompt"},
        },
        {
            "type": "assistant",
            "uuid": "sa-1",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:02.600Z",
            "isSidechain": True,
            "message": {
                "id": "msg_s1",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [{"type": "text", "text": "subagent answer"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    ]
    side_dir = project_dir / SESSION_ID / "subagents"
    _write_jsonl(side_dir / "agent-abc.jsonl", subagent_events)

    # Workflow-nested side-file: found by our census, INVISIBLE to harbor's
    # rglob("subagents/*.jsonl") discovery (fidelity gap 1).
    workflow_events: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "wu-1",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-22T00:00:02.700Z",
            "isSidechain": True,
            "message": {"role": "user", "content": "workflow subagent prompt"},
        },
    ]
    _write_jsonl(side_dir / "workflows" / "wf_123" / "agent-def.jsonl", workflow_events)

    return main_jsonl


# The Codex rollout fixture lives in its own module so the two agents' fixture
# data stay separately readable; re-exported here because pytest resolves
# fixtures through conftest. The redundant alias is the explicit re-export form,
# which is what tells a type checker the import is deliberate.
from codex_fixtures import codex_rollout as codex_rollout  # noqa: E402, PLC0414
