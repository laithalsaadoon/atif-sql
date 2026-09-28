# SPDX-License-Identifier: Apache-2.0

"""Conversion output must not depend on the process hash seed.

``materialize --workers 1`` has to stay byte-identical to the process pool, and
two conversions of the same session have to agree byte for byte, or every
re-conversion reads as a change. A list built from a Python ``set`` breaks both:
string hashing is salted per process (``PYTHONHASHSEED``), so the set's
iteration order, and the list dumped from it, moves between processes.

Each fixture here is converted in several subprocesses, one per hash seed, and
every artifact the converter hands the corpus (the trajectory, edges, session
events, loss report and blobs) is serialized without ``sort_keys``, so both
list order and dict insertion order count. The Claude Code fixture spreads
several distinct values over ``cwd``, ``gitBranch`` and ``agentId``, which is
what a set-order leak needs to show up; with a single value per field it would
pass by luck.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from codex_fixtures import write_codex_rollout
from harbor_oracle import (
    diff_paths,
    harbor_claude_code_trajectory,
    parity_diffs,
    require_harbor_private_api,
)
from subagent_fixtures import write_parallel_subagent_session

from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session

#: Seeds whose string hashes order small sets differently; "random" adds a
#: fresh salt per run on top of the fixed ones.
_HASH_SEEDS = ("0", "1", "2", "3", "random")

_SESSION_ID = "33333333-3333-3333-3333-333333333333"

#: Chosen so no two share a first-seen and a sorted position: a converter that
#: sorts and one that keeps first-seen order produce different lists here.
_CWDS = ("/w/zeta", "/w/alpha", "/w/mu", "/w/beta", "/w/kappa", "/w/delta")
_BRANCHES = ("topic/z", "main", "topic/m", "release", "topic/k", "hotfix")
_AGENT_IDS = ("a7", "a2", "a9", "a4", "a6", "a1")

#: Runs in the child: convert each path, print one canonical byte dump.
_CHILD = r"""
import json
import sys
from pathlib import Path

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit

out = []
for agent, raw_path in zip(sys.argv[1::2], sys.argv[2::2], strict=True):
    convert = convert_and_audit if agent == "claude-code" else convert_codex_and_audit
    result, report = convert(Path(raw_path))
    out.append(
        {
            "trajectory": json.dumps(result.trajectory, ensure_ascii=False),
            "validation_errors": list(result.validation_errors),
            "edges": list(result.edges_lines),
            "events": list(result.events_lines),
            "loss_report": json.dumps(report.to_json(), ensure_ascii=False),
            "blobs": [
                [blob.ref.sha256, blob.ref.media_type, blob.data.hex()] for blob in result.blobs
            ],
        }
    )
sys.stdout.write(json.dumps(out, ensure_ascii=False))
"""


def _record(uuid: str, second: int, record_type: str, index: int, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any]
    if record_type == "user":
        message = {"role": "user", "content": f"turn {index}"}
    else:
        message = {
            "id": f"msg_{uuid}",
            "role": "assistant",
            "model": "claude-test-1",
            "content": [{"type": "text", "text": f"reply {index}"}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    return {
        "uuid": uuid,
        "sessionId": _SESSION_ID,
        "timestamp": f"2026-09-27T00:00:{second:02d}Z",
        "version": "2.1.300",
        "type": record_type,
        "message": message,
        **extra,
    }


def _write_multi_valued_session(project_dir: Path) -> Path:
    """A session whose cwd, branch and subagent id each take several values."""
    main = project_dir / f"{_SESSION_ID}.jsonl"
    side_dir = project_dir / _SESSION_ID / "subagents"
    side_dir.mkdir(parents=True)
    main_records: list[dict[str, Any]] = []
    for index, (cwd, branch) in enumerate(zip(_CWDS, _BRANCHES, strict=True)):
        main_records.append(
            _record(f"u{index}", 2 * index, "user", index, cwd=cwd, gitBranch=branch)
        )
        main_records.append(
            _record(f"a{index}", 2 * index + 1, "assistant", index, cwd=cwd, gitBranch=branch)
        )
    main.write_text("".join(json.dumps(r) + "\n" for r in main_records), encoding="utf-8")
    for index, agent_id in enumerate(_AGENT_IDS):
        side = [
            _record(
                f"s{index}",
                20 + index,
                "assistant",
                index,
                cwd=_CWDS[index],
                gitBranch=_BRANCHES[index],
                agentId=agent_id,
                isSidechain=True,
            )
        ]
        (side_dir / f"agent-{agent_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in side), encoding="utf-8"
        )
    return main


def _convert_under_seed(seed: str, fixtures: list[tuple[str, Path]]) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    argv = [sys.executable, "-c", _CHILD]
    for agent, path in fixtures:
        argv += [agent, str(path)]
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script, test-owned paths
        argv, env=env, capture_output=True, text=True, check=False, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_artifacts_are_byte_identical_across_hash_seeds(tmp_path: Path) -> None:
    fixtures = [
        ("claude-code", _write_multi_valued_session(tmp_path / "projects" / "-w-multi")),
        ("claude-code", write_parallel_subagent_session(tmp_path / "projects" / "-w")),
        ("codex", write_codex_rollout(tmp_path / "sessions")),
    ]
    dumps = {seed: _convert_under_seed(seed, fixtures) for seed in _HASH_SEEDS}
    reference = dumps[_HASH_SEEDS[0]]
    differing = [seed for seed, dump in dumps.items() if dump != reference]
    assert not differing, f"artifacts differ from PYTHONHASHSEED=0 under seeds {differing}"


def test_agent_extra_lists_keep_first_seen_order(tmp_path: Path) -> None:
    """The order is chronological, so the list also says where the session went next."""
    fixture = _write_multi_valued_session(tmp_path / "projects" / "-w-multi")
    documents = json.loads(_convert_under_seed("0", [("claude-code", fixture)]))
    extra = json.loads(documents[0]["trajectory"])["agent"]["extra"]
    assert extra == {
        "cwds": list(_CWDS),
        "git_branches": list(_BRANCHES),
        "agent_ids": list(_AGENT_IDS),
    }


class TestHarborSetOrder:
    """harbor dumps these lists in set order; the oracle forgives order, never content."""

    def test_harbor_differs_only_in_order(self, tmp_path: Path) -> None:
        require_harbor_private_api("claude-code")
        fixture = _write_multi_valued_session(tmp_path / "projects" / "-w-multi")
        theirs = harbor_claude_code_trajectory(fixture)
        ours = convert_claude_code_session(fixture)
        assert theirs is not None
        assert ours is not None
        ours_dict = ours.to_json_dict()
        order_lines = [line for line in diff_paths(theirs, ours_dict) if "$.agent.extra." in line]
        assert order_lines, "harbor's set order should differ from first-seen order somewhere"
        for key in ("cwds", "git_branches", "agent_ids"):
            assert sorted(theirs["agent"]["extra"][key]) == sorted(ours_dict["agent"]["extra"][key])
        assert parity_diffs(theirs, ours_dict) == []

    def test_a_changed_value_still_fails(self) -> None:
        harbor = {"agent": {"extra": {"cwds": ["/b", "/a"], "git_branches": ["x", "y"]}}}
        ours = {"agent": {"extra": {"cwds": ["/a", "/b"], "git_branches": ["x", "z"]}}}
        assert len(diff_paths(harbor, ours)) == 3
        assert parity_diffs(harbor, ours) == ["$.agent.extra.git_branches[1]: harbor='y' ours='z'"]

    def test_a_missing_value_still_fails(self) -> None:
        harbor = {"agent": {"extra": {"agent_ids": ["a2", "a1"]}}}
        ours = {"agent": {"extra": {"agent_ids": ["a1"]}}}
        assert parity_diffs(harbor, ours) == ["$.agent.extra.agent_ids: length harbor=2 ours=1"]
