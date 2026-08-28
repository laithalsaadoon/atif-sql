# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures: a synthetic raw transcript corpus with side-files.

Builds the CONTRACT.md source shape under a tmp dir:

- ``projects/-proj-a/<uuid>.jsonl`` main transcripts,
- flat subagent side-files at ``<stem>/subagents/agent-*.jsonl``,
- workflow-nested side-files at
  ``<stem>/subagents/workflows/wf_*/agent-*.jsonl``,
- a ``*.meta.json`` decoy that discovery must exclude.

Mtimes are set explicitly with ``os.utime`` so quiescence and watermark
decisions are pinned against a fixed ``NOW_NS``, never the wall clock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"

#: Fixed "now" for every plan decision: 2026-01-02T00:00:00Z in epoch ns.
NOW_NS = 1_767_312_000 * 1_000_000_000

#: One hour before NOW_NS — comfortably quiescent at the default 300s.
STALE_NS = NOW_NS - 3_600 * 1_000_000_000

#: Ten seconds before NOW_NS — inside the quiesce window (live).
LIVE_NS = NOW_NS - 10 * 1_000_000_000


def write_session(
    source_root: Path,
    session_id: str,
    *,
    mtime_ns: int,
    with_side_files: bool = False,
) -> Path:
    """Create one session (and optionally its side-file tree) with fixed mtimes."""
    project_dir = source_root / "-proj-a"
    project_dir.mkdir(parents=True, exist_ok=True)
    main = project_dir / f"{session_id}.jsonl"
    main.write_text(json.dumps({"type": "user", "uuid": "u-1"}) + "\n")

    if with_side_files:
        side_root = project_dir / session_id / "subagents"
        wf_dir = side_root / "workflows" / "wf_001"
        wf_dir.mkdir(parents=True, exist_ok=True)
        flat = side_root / "agent-aaaa.jsonl"
        flat.write_text(json.dumps({"type": "user", "uuid": "sa-1"}) + "\n")
        nested = wf_dir / "agent-bbbb.jsonl"
        nested.write_text(json.dumps({"type": "user", "uuid": "wf-1"}) + "\n")
        decoy = side_root / "agent-aaaa.meta.json"
        decoy.write_text("{}\n")
        for side in (flat, nested, decoy):
            os.utime(side, ns=(mtime_ns, mtime_ns))

    os.utime(main, ns=(mtime_ns, mtime_ns))
    return main


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """An empty transcript source root (``<config>/projects`` shape)."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    """A materialized-corpus root (created lazily by the use case)."""
    return tmp_path / "corpus"
