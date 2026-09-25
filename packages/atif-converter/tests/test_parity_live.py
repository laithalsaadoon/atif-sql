# SPDX-License-Identifier: Apache-2.0

"""Parity against harbor over REAL transcripts on this machine, when there are any.

The synthetic goldens pin the shapes we thought of. This pins the ones we did
not: the newest ``ATIF_PARITY_LIMIT`` sessions of each agent under the local
roots are converted by both harbor's private converter and ours, and every
divergent JSON path is reported at once. Skips cleanly where there is no local
corpus (CI), and where harbor no longer ships the private method.

Roots: ``ATIF_PARITY_CLAUDE_ROOT`` (default ``$CLAUDE_CONFIG_DIR/projects`` or
``~/.claude/projects``) and ``ATIF_PARITY_CODEX_ROOT`` (default
``$CODEX_HOME/sessions`` or ``~/.codex/sessions``). ``ATIF_PARITY_LIMIT=0``
means every session, which is how the full-corpus run before a release goes.

Each session is SNAPSHOTTED before either converter reads it: the newest
sessions are usually live, and a transcript appended between harbor's read and
ours reads as a divergence that neither converter caused (seen 2026-09-25:
``total_steps: harbor=7880 ours=7881``). The snapshot keeps the
``<slug>/<stem>.jsonl`` + ``<slug>/<stem>/`` layout both converters discover
side-files by, and trims every file to its last complete line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harbor_oracle import (
    diff_paths,
    harbor_claude_code_trajectory,
    harbor_codex_trajectory,
    require_harbor_private_api,
)

pytestmark = pytest.mark.integration


def _copy_complete_lines(src: Path, dst: Path) -> None:
    """Copy ``src`` up to and including its last newline: a live writer's partial line stays out."""
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data[: data.rfind(b"\n") + 1])


def _snapshot(session: Path, into: Path) -> Path:
    """Freeze one session (and its ``<stem>/`` side-files) under ``into``, same relative layout."""
    frozen = into / session.parent.name / session.name
    _copy_complete_lines(session, frozen)
    side_dir = session.parent / session.stem
    if side_dir.is_dir():
        for side_file in side_dir.rglob("*.jsonl"):
            _copy_complete_lines(
                side_file, frozen.parent / session.stem / side_file.relative_to(side_dir)
            )
    return frozen


def _limit() -> int | None:
    raw = os.environ.get("ATIF_PARITY_LIMIT", "25")
    value = int(raw)
    return None if value == 0 else value


def _newest(paths: list[Path]) -> list[Path]:
    ordered = sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)
    limit = _limit()
    return ordered if limit is None else ordered[:limit]


def _claude_sessions() -> list[Path]:
    root = Path(
        os.environ.get("ATIF_PARITY_CLAUDE_ROOT")
        or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
    ).expanduser()
    if not root.is_dir():
        return []
    return _newest([p for p in root.glob("*/*.jsonl") if p.is_file()])


def _codex_rollouts() -> list[Path]:
    root = Path(
        os.environ.get("ATIF_PARITY_CODEX_ROOT")
        or Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    ).expanduser()
    if not root.is_dir():
        return []
    return _newest([p for p in root.glob("*/*/*/rollout-*.jsonl") if p.is_file()])


def _report(divergences: dict[Path, list[str]], total: int) -> str:
    lines = [f"{len(divergences)} of {total} sessions diverge from harbor:"]
    for path, diffs in divergences.items():
        lines.append(f"  {path}")
        lines.extend(f"    {d}" for d in diffs[:12])
        if len(diffs) > 12:
            lines.append(f"    ... {len(diffs) - 12} more")
    return "\n".join(lines)


class TestLiveParity:
    def test_claude_code(self, tmp_path: Path) -> None:
        require_harbor_private_api("claude-code")
        sessions = _claude_sessions()
        if not sessions:
            pytest.skip("no local Claude Code transcripts")
        from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session

        divergences: dict[Path, list[str]] = {}
        for session in sessions:
            frozen = _snapshot(session, tmp_path)
            theirs = harbor_claude_code_trajectory(frozen)
            ours = convert_claude_code_session(frozen)
            ours_dict = None if ours is None else ours.to_json_dict()
            if theirs is None or ours_dict is None:
                if theirs is not ours_dict:
                    divergences[session] = [
                        f"harbor={'None' if theirs is None else 'Trajectory'} ours={'None' if ours_dict is None else 'Trajectory'}"
                    ]
                continue
            diffs = diff_paths(theirs, ours_dict)
            if diffs:
                divergences[session] = diffs
        assert not divergences, _report(divergences, len(sessions))

    def test_codex(self, tmp_path: Path) -> None:
        require_harbor_private_api("codex")
        rollouts = _codex_rollouts()
        if not rollouts:
            pytest.skip("no local Codex rollouts")
        from atif_converter.infrastructure.codex_converter import convert_codex_rollout

        divergences: dict[Path, list[str]] = {}
        for rollout in rollouts:
            frozen = _snapshot(rollout, tmp_path)
            theirs = harbor_codex_trajectory(frozen)
            ours = convert_codex_rollout(frozen)
            ours_dict = None if ours is None else ours.to_json_dict()
            if theirs is None or ours_dict is None:
                if theirs is not ours_dict:
                    divergences[rollout] = [
                        f"harbor={'None' if theirs is None else 'Trajectory'} ours={'None' if ours_dict is None else 'Trajectory'}"
                    ]
                continue
            diffs = diff_paths(theirs, ours_dict)
            if diffs:
                divergences[rollout] = diffs
        assert not divergences, _report(divergences, len(rollouts))
