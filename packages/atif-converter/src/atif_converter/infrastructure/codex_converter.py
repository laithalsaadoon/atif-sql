# SPDX-License-Identifier: Apache-2.0
# Ported from harbor 0.22.0, src/harbor/agents/installed/codex.py (Apache-2.0,
# Copyright the Harbor authors), so that atif-converter depends on harbor's
# PUBLIC trajectory models only.

"""Read one Codex rollout and convert it — the file half of the Codex port.

harbor's ``Codex._convert_events_to_trajectory`` takes a DIRECTORY: it globs
``*.jsonl`` inside it, converts ``max(...)`` of the matches (one file, chosen
by name, the rest dropped) and reads that file line by line. The adapter this
replaces satisfied that shape by symlinking one rollout alone into a scratch
directory. This module reads the rollout directly instead, and the two are
the same thing by construction: with exactly one file in the directory,
``max`` is that file, so "the directory's one file" and "this file" are one
read.

The line loop is harbor's, kept exact rather than replaced with
``read_text().splitlines()``: iterating the handle splits on newlines only,
where ``str.splitlines`` also splits on U+2028 / U+2029 / form feed, which are
legal unescaped inside a JSON string — a rollout carrying one would parse into
different records under the two loops.

What differs from harbor's read is the ``session_id`` fallback for a rollout
with no ``session_meta``: harbor uses the directory's name, which in its own
layout is the ``<DD>`` day directory; here that is the rollout's parent
directory name, the same value on a real Codex tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Trajectory  # type: ignore[import-untyped]
from loguru import logger

from atif_converter.domain.codex_conversion import convert_codex_records


def read_codex_rollout(rollout_jsonl: Path) -> list[dict[str, Any]]:
    """The rollout's JSON records in file order, malformed lines skipped.

    harbor's read loop: blank lines are ignored, a line that is not JSON is
    logged at debug level and dropped, and everything that parses is kept
    (harbor does not check that a parsed line is an object).

    Raises
    ------
        OSError: the file cannot be opened or read.
    """
    raw_events: list[dict[str, Any]] = []
    with rollout_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_events.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.debug("Skipping malformed JSONL line in {}: {}", rollout_jsonl, exc)
    return raw_events


def convert_codex_rollout(rollout_jsonl: Path) -> Trajectory | None:
    """Convert one Codex rollout JSONL into an ATIF trajectory.

    ``None`` exactly when harbor's converter returns ``None`` for the same
    file staged alone: the rollout has no records, or none that becomes a
    step. Validation and the domain error taxonomy are the application
    layer's business, as they were for the adapter this replaces.

    Raises
    ------
        OSError: the file cannot be opened or read.
    """
    raw_events = read_codex_rollout(rollout_jsonl)
    return convert_codex_records(raw_events, fallback_session_id=rollout_jsonl.parent.name)


__all__ = ["convert_codex_rollout", "read_codex_rollout"]
