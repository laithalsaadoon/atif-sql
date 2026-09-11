# SPDX-License-Identifier: Apache-2.0
# Ported from harbor 0.22.0, src/harbor/agents/installed/claude_code.py
# (Apache-2.0, Copyright the Harbor authors), so that atif-converter depends on
# harbor's PUBLIC trajectory models only.

"""Read one Claude Code session from disk and convert it, with no staging.

The file half of harbor's ``_convert_events_to_trajectory``: the JSONL reading
loop is harbor's (blank lines skipped, a malformed line logged and skipped,
the rest parsed), and side-file discovery is what the old adapter's staging
did before harbor ever saw the session — ``<parent>/<stem>/`` walked with
``rglob("*.jsonl")``, a leading ``subagents`` path part dropped, the remaining
parts joined with ``__``. That joined name is the key the domain function
sorts on, so a workflow-nested ``subagents/workflows/wf_1/agent-a.jsonl`` is
read in the same position it was when staged flat as
``subagents/workflows__wf_1__agent-a.jsonl``. When two side-files flatten to
the same name the first in ``rglob`` order wins, which is what the adapter's
``if not staged.exists()`` guard did.

No temp dir, no symlinks, no ``harbor.agents`` import. ``*.meta.json``
sidecars are excluded by the ``*.jsonl`` suffix filter, as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Trajectory  # type: ignore[import-untyped]
from loguru import logger

from atif_converter.domain.claude_code_conversion import convert_claude_code_records


def read_session_records(path: Path) -> list[dict[str, Any]]:
    """Parse one JSONL transcript the way harbor's reading loop does.

    Blank lines are skipped; a line that is not JSON is logged at debug and
    skipped rather than failing the session. A line that IS JSON but not an
    object is kept as-is, as harbor keeps it (it fails later, in the same
    place harbor fails).
    """
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.debug("Skipping malformed JSONL line in {}: {}", path, exc)
    return records


def discover_side_files(session_jsonl: Path, *, include_subagents: bool = True) -> dict[str, Path]:
    """Staged side-file name -> side-file path, for one session's ``<stem>/`` dir.

    Empty when ``include_subagents`` is false or the session has no side dir.
    """
    side_files: dict[str, Path] = {}
    side_dir = session_jsonl.parent / session_jsonl.stem
    if include_subagents and side_dir.is_dir():
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            rel_parts = side_file.relative_to(side_dir).parts
            if rel_parts and rel_parts[0] == "subagents":
                rel_parts = rel_parts[1:]
            side_files.setdefault("__".join(rel_parts), side_file)
    return side_files


def convert_claude_code_session(
    session_jsonl: Path, *, include_subagents: bool = True
) -> Trajectory | None:
    """Convert one Claude Code session JSONL (plus side-files) into an ATIF trajectory.

    Parameters
    ----------
    session_jsonl
        The main ``<session-id>.jsonl``. Read errors propagate as ``OSError``.
    include_subagents
        Whether to read the ``<stem>/`` side-files; off, only the main
        transcript is converted.

    Returns
    -------
    Trajectory | None
        ``None`` when the session yields no steps, as harbor returns ``None``.
    """
    main_records = read_session_records(session_jsonl)
    side_records = {
        name: read_session_records(path)
        for name, path in discover_side_files(
            session_jsonl, include_subagents=include_subagents
        ).items()
    }
    return convert_claude_code_records(
        main_records,
        side_records,
        fallback_session_id=session_jsonl.parent.name or "-unknown",
    )
