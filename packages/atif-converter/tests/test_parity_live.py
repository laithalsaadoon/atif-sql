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
    def test_claude_code(self) -> None:
        require_harbor_private_api("claude-code")
        sessions = _claude_sessions()
        if not sessions:
            pytest.skip("no local Claude Code transcripts")
        from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session

        divergences: dict[Path, list[str]] = {}
        for session in sessions:
            theirs = harbor_claude_code_trajectory(session)
            ours = convert_claude_code_session(session)
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

    def test_codex(self) -> None:
        require_harbor_private_api("codex")
        rollouts = _codex_rollouts()
        if not rollouts:
            pytest.skip("no local Codex rollouts")
        from atif_converter.infrastructure.codex_converter import convert_codex_rollout

        divergences: dict[Path, list[str]] = {}
        for rollout in rollouts:
            theirs = harbor_codex_trajectory(rollout)
            ours = convert_codex_rollout(rollout)
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
