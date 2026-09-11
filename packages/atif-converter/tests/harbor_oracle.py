# SPDX-License-Identifier: Apache-2.0

"""The parity oracle: harbor's PRIVATE converters, reachable from tests only.

atif-converter used to call ``ClaudeCode._convert_events_to_trajectory`` and
``Codex._convert_events_to_trajectory`` in production. Those are not public API
(harbor documents the ``harbor.models.trajectories`` data classes and the
validator, and nothing that converts a native session log), and the two files
that hold them took 26 and 18 upstream commits between June and September 2026.
The converters now live in this package, built on the public models; harbor's
private ones survive HERE, as the oracle a port is measured against.

Two ways to consult the oracle:

* LIVE, through :func:`harbor_claude_code_trajectory` and
  :func:`harbor_codex_trajectory`, while harbor still ships the private method.
  A test that needs it calls :func:`require_harbor_private_api` first and is
  SKIPPED, loudly, the day the method is gone — a skip here is the signal that
  the frozen goldens are now the only oracle, never a failure.
* FROZEN, through the ``goldens/`` directory beside this module: harbor's
  output for each synthetic fixture, captured while the private method
  existed, so parity stays checkable after it is removed. ``test_goldens``
  asserts the live oracle still agrees with the frozen one, which is how an
  upstream behavior change becomes a failing test instead of silent drift.

Staging is replicated here rather than imported from the adapter, because the
adapter no longer stages anything: the private methods take a DIRECTORY and
discover the transcript inside it, which is the shape this module builds.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

GOLDENS_DIR = Path(__file__).parent / "goldens"

_CONVERT_METHOD = "_convert_events_to_trajectory"


def harbor_has_private_api(agent: str) -> bool:
    """Whether harbor still exposes the private converter for ``agent``."""
    if agent == "claude-code":
        from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

        return callable(getattr(ClaudeCode, _CONVERT_METHOD, None))
    if agent == "codex":
        from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

        return callable(getattr(Codex, _CONVERT_METHOD, None))
    msg = f"unknown agent {agent!r}"
    raise ValueError(msg)


def require_harbor_private_api(agent: str) -> None:
    """Skip the calling test when the live oracle is gone; never fail on it."""
    if not harbor_has_private_api(agent):
        pytest.skip(
            f"harbor no longer exposes {agent}'s private converter; the frozen "
            f"goldens under {GOLDENS_DIR} are now the only parity oracle"
        )


def _stage_claude_code_session(
    session_jsonl: Path, staging: Path, *, include_subagents: bool
) -> Path:
    """Symlink one session into the ``<projects>/<slug>/`` layout harbor discovers.

    Per-FILE symlinks under REAL directories (Python 3.13's ``rglob`` does not
    descend symlinked directories), and every side-file — including the
    workflow-nested ones harbor's own discovery cannot see — staged FLAT with
    ``__``-joined names. This is the staging the production adapter used to do.
    """
    session_dir = staging / "sessions" / "projects" / (session_jsonl.parent.name or "-unknown")
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / session_jsonl.name).symlink_to(session_jsonl.resolve())
    side_dir = session_jsonl.parent / session_jsonl.stem
    if include_subagents and side_dir.is_dir():
        staged_subagents = session_dir / side_dir.name / "subagents"
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            rel_parts = side_file.relative_to(side_dir).parts
            if rel_parts and rel_parts[0] == "subagents":
                rel_parts = rel_parts[1:]
            staged = staged_subagents / "__".join(rel_parts)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.symlink_to(side_file.resolve())
    return session_dir


def harbor_claude_code_trajectory(
    session_jsonl: Path, *, include_subagents: bool = True
) -> dict[str, Any] | None:
    """harbor's own conversion of one Claude Code session, as ``to_json_dict()``."""
    from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

    with tempfile.TemporaryDirectory(prefix="atif-oracle-") as scratch:
        staging = Path(scratch)
        session_dir = _stage_claude_code_session(
            session_jsonl, staging, include_subagents=include_subagents
        )
        logs_dir = staging / "logs"
        logs_dir.mkdir()
        trajectory = getattr(ClaudeCode(logs_dir=logs_dir), _CONVERT_METHOD)(session_dir)
    return None if trajectory is None else trajectory.to_json_dict()


def harbor_codex_trajectory(rollout_jsonl: Path) -> dict[str, Any] | None:
    """harbor's own conversion of one Codex rollout, staged alone, as ``to_json_dict()``."""
    from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

    with tempfile.TemporaryDirectory(prefix="atif-oracle-") as scratch:
        staging = Path(scratch)
        session_dir = staging / "sessions"
        session_dir.mkdir()
        (session_dir / rollout_jsonl.name).symlink_to(rollout_jsonl.resolve())
        logs_dir = staging / "logs"
        logs_dir.mkdir()
        trajectory = getattr(Codex(logs_dir=logs_dir), _CONVERT_METHOD)(session_dir)
    return None if trajectory is None else trajectory.to_json_dict()


def golden_path(name: str) -> Path:
    """Where the frozen oracle output for one named fixture lives."""
    return GOLDENS_DIR / f"{name}.trajectory.json"


def load_golden(name: str) -> dict[str, Any]:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def write_golden(name: str, trajectory: dict[str, Any]) -> None:
    golden_path(name).write_text(
        json.dumps(trajectory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def diff_paths(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Every JSON path where two trajectories disagree, with both values.

    A flat list rather than an assertion, so a parity failure names ALL the
    divergent fields at once instead of one per test run.
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                out.append(f"{path}.{key}: only in ours = {actual[key]!r}")
            elif key not in actual:
                out.append(f"{path}.{key}: only in harbor = {expected[key]!r}")
            else:
                out.extend(diff_paths(expected[key], actual[key], f"{path}.{key}"))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length harbor={len(expected)} ours={len(actual)}"]
        out = []
        for index, (e, a) in enumerate(zip(expected, actual, strict=True)):
            out.extend(diff_paths(e, a, f"{path}[{index}]"))
        return out
    if expected != actual:
        return [f"{path}: harbor={expected!r} ours={actual!r}"]
    return []
