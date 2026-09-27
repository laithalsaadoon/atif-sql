# SPDX-License-Identifier: Apache-2.0

"""A Claude Code session with two PARALLEL subagents, images, and tool outcomes.

Shaped after the real transcripts surveyed on 2026-09-27 (session cc69906b and
the corpus-wide shape census):

- the main chain spawns two ``Agent`` calls in ONE assistant turn, and the
  two subagents' records interleave in time, so a converter that attributes
  sidechain steps by position rather than by ``agentId`` gets them crossed;
- subagent ``aaa`` has an ``agent-aaa.meta.json`` sidecar carrying its
  ``toolUseId``; subagent ``bbb``'s sidecar has none (the shape 2,636 real
  sidecars have), so its link has to come from the spawning call's own
  ``toolUseResult.agentId``;
- ``aaa`` reads a PNG: the tool result carries the image as a base64 block AND
  the ``toolUseResult.file.base64`` copy of the same bytes;
- ``bbb`` runs a failing ``Bash`` (``is_error`` true, ``Exit code 2`` first
  line); the main chain runs a clean one (``is_error`` false,
  ``interrupted`` false);
- the user pastes a second, different PNG into a message beside text.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

PARALLEL_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def png_bytes(width: int, height: int) -> bytes:
    """A real, minimal RGB PNG of ``width`` x ``height`` black pixels."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


#: The PNG subagent ``aaa`` reads (3 x 2).
READ_PNG = png_bytes(3, 2)
#: A second, different PNG the user pastes (5 x 4), declared as PNG.
PASTED_PNG = png_bytes(5, 4)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _base(uuid: str, ts: str, **extra: Any) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "sessionId": PARALLEL_SESSION_ID,
        "timestamp": f"2026-09-27T00:00:{ts}Z",
        "version": "2.1.300",
        "cwd": "/w",
        "gitBranch": "main",
        **extra,
    }


def _assistant(
    uuid: str, ts: str, msg_id: str, content: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        **_base(uuid, ts, **extra),
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-test-1",
            "content": content,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    }


def _user(uuid: str, ts: str, content: Any, **extra: Any) -> dict[str, Any]:
    return {
        **_base(uuid, ts, **extra),
        "type": "user",
        "message": {"role": "user", "content": content},
    }


def _tool_use(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def _image_block(data: bytes, media_type: str = "image/png") -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": _b64(data)},
    }


def write_parallel_subagent_session(project_dir: Path) -> Path:
    """Write the session under ``project_dir``; return the main JSONL path."""
    main = project_dir / f"{PARALLEL_SESSION_ID}.jsonl"
    side = project_dir / PARALLEL_SESSION_ID / "subagents"
    _write_jsonl(
        main,
        [
            _user("u-1", "01", "survey two things"),
            _assistant(
                "a-1",
                "02",
                "msg_main_1",
                [
                    _tool_use(
                        "toolu_A",
                        "Agent",
                        subagent_type="Explore",
                        description="Alpha scout",
                        prompt="look at alpha",
                    ),
                    _tool_use(
                        "toolu_B",
                        "Agent",
                        subagent_type="Explore",
                        description="Beta scout",
                        prompt="look at beta",
                    ),
                ],
            ),
            _user(
                "u-2",
                "20",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_A",
                        "content": [{"type": "text", "text": "alpha done"}],
                    }
                ],
                toolUseResult={"status": "completed", "agentId": "aaa", "agentType": "Explore"},
            ),
            _user(
                "u-3",
                "21",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_B",
                        "content": [{"type": "text", "text": "beta done"}],
                    }
                ],
                toolUseResult={
                    "status": "completed",
                    "agentId": "bbb",
                    "agentType": "Explore",
                    "description": "Beta scout",
                },
            ),
            _user(
                "u-4",
                "22",
                [{"type": "text", "text": "see this screenshot"}, _image_block(PASTED_PNG)],
            ),
            _assistant("a-2", "23", "msg_main_2", [_tool_use("toolu_C", "Bash", command="true")]),
            _user(
                "u-5",
                "24",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_C",
                        "content": "",
                        "is_error": False,
                    }
                ],
                toolUseResult={
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": True,
                },
            ),
            _assistant("a-3", "25", "msg_main_3", [{"type": "text", "text": "all done"}]),
        ],
    )
    # The two subagents run at once: their records interleave in time.
    _write_jsonl(
        side / "agent-aaa.jsonl",
        [
            _user("sa-u1", "03", "look at alpha", isSidechain=True, agentId="aaa"),
            _assistant(
                "sa-a1",
                "05",
                "msg_a_1",
                [_tool_use("toolu_RA", "Read", file_path="/w/shot.png")],
                isSidechain=True,
                agentId="aaa",
            ),
            _user(
                "sa-u2",
                "07",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_RA",
                        "content": [_image_block(READ_PNG)],
                    }
                ],
                isSidechain=True,
                agentId="aaa",
                toolUseResult={
                    "type": "image",
                    "file": {
                        "base64": _b64(READ_PNG),
                        "type": "image/png",
                        "originalSize": len(READ_PNG),
                        "dimensions": {"originalWidth": 3, "originalHeight": 2},
                    },
                },
            ),
            _assistant(
                "sa-a2",
                "09",
                "msg_a_2",
                [{"type": "text", "text": "alpha answer"}],
                isSidechain=True,
                agentId="aaa",
            ),
        ],
    )
    _write_jsonl(
        side / "agent-bbb.jsonl",
        [
            _user("sb-u1", "04", "look at beta", isSidechain=True, agentId="bbb"),
            _assistant(
                "sb-a1",
                "06",
                "msg_b_1",
                [_tool_use("toolu_RB", "Bash", command="exit 2")],
                isSidechain=True,
                agentId="bbb",
            ),
            _user(
                "sb-u2",
                "08",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_RB",
                        "content": "Exit code 2\nboom",
                        "is_error": True,
                    }
                ],
                isSidechain=True,
                agentId="bbb",
                toolUseResult="Error: Exit code 2\nboom",
            ),
            _assistant(
                "sb-a2",
                "10",
                "msg_b_2",
                [{"type": "text", "text": "beta answer"}],
                isSidechain=True,
                agentId="bbb",
            ),
        ],
    )
    (side / "agent-aaa.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "Alpha scout",
                "toolUseId": "toolu_A",
                "spawnDepth": 1,
            }
        ),
        encoding="utf-8",
    )
    (side / "agent-bbb.meta.json").write_text(
        json.dumps({"agentType": "Explore", "spawnDepth": 1}), encoding="utf-8"
    )
    return main


@pytest.fixture
def parallel_subagent_session(tmp_path: Path) -> Path:
    """The main JSONL of the parallel-subagent session (module docstring)."""
    return write_parallel_subagent_session(tmp_path / "projects" / "-w")
