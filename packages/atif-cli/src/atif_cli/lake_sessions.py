# SPDX-License-Identifier: Apache-2.0

"""The lake behind atif-analytics' session port: what ``analyze`` reads when the lake holds the corpus.

atif-analytics may import no package of ours but atif-models, so it declares
the port (:class:`atif_analytics.domain.ports.SessionSource`) and atif-cli,
the composition root, adapts atif-duck's :class:`~atif_duck.infrastructure.lake_sessions.LakeSessionReader`
to it here. The rows become :class:`~atif_analytics.domain.transcript.StepEvent`
through :func:`~atif_analytics.domain.transcript.step_event`, the projection
the file source uses too, so both sources apply one set of rules, and the
author label comes from atif-analytics' rule table (pinned to atif-duck's
``step_author`` by ``test_authorship_twin_pin.py``).

Per session, the lake is used only when it holds the session's CURRENT
artifacts: its ``session_meta`` row must carry the ``materialized_at`` and
``source_mtime_ns`` the session's ``meta.json`` carries now. Anything else (a
session the lake has not loaded, one a failed or skipped lake write left
behind, one whose ``meta.json`` cannot be read) is read from its files, so a
lagging lake can never hand the pipelines an older transcript than the files
hold. Enumeration and the ``trajectory.json`` mtime bound come from the
session directories exactly as on the file path, which keeps the checkpoint's
bounds identical whichever source ran.

Order inside that per-session check matters: the trajectory is stat'ed before
``meta.json`` is read, so a materialize swapping the session in between leaves
an OLD mtime beside whatever was read, and the checkpoint re-admits the
session next run rather than skipping a transcript it never analyzed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from atif_analytics.domain.transcript import StepEvent, step_event
from atif_analytics.infrastructure.corpus_reader import (
    META_FILENAME,
    TrajectoryFileSource,
    complete_session_dirs,
    last_edge_ts,
    trajectory_mtime,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from atif_analytics.domain.ports import SessionBounds
    from atif_duck.infrastructure.lake import LakeLayout, LakeUnavailable
    from atif_duck.infrastructure.lake_sessions import LakeSessionReader, SessionFreshness

#: Sessions per gate-level lake read. One read costs about the same for one
#: session as for this many (it scans the same row groups' metadata), and the
#: gates walk the newest-first order, so the reader reads ahead by this much.
TURNS_BATCH_SIZE: int = 64

#: Sessions per full read (the tool payloads). Smaller: a batch of results can
#: be large, and the renderer only reads what it admits.
STEPS_BATCH_SIZE: int = 4


def _json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _is_current(session_dir: Path, recorded: SessionFreshness | None) -> bool:
    """True when the lake's row for this session is its current ``meta.json``'s."""
    if recorded is None:
        return False
    try:
        meta = json.loads((session_dir / META_FILENAME).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    return bool(
        meta.get("materialized_at") == recorded.materialized_at
        and meta.get("source_mtime_ns") == recorded.source_mtime_ns
    )


class LakeSessionSource:
    """atif-analytics' ``SessionSource`` over the lake, falling back to the files per session."""

    name: str = "lake"
    turns_batch_size: int = TURNS_BATCH_SIZE
    steps_batch_size: int = STEPS_BATCH_SIZE
    turns_are_complete: bool = False

    def __init__(self, reader: LakeSessionReader, corpus_root: Path) -> None:
        self._reader = reader
        self._sessions_dir = corpus_root / "sessions"
        self._files = TrajectoryFileSource(corpus_root)
        self._current: set[str] | None = None
        #: Sessions the last enumeration found on disk but read from files.
        self.from_files: tuple[str, ...] = ()

    @classmethod
    def open(
        cls, layout: LakeLayout, corpus_root: Path, *, memory_limit_bytes: int | None = None
    ) -> LakeSessionSource | LakeUnavailable:
        """Attach the lake for ``corpus_root``, or return why it cannot serve it."""
        from atif_duck.infrastructure.lake_sessions import LakeSessionReader

        reader = LakeSessionReader.open(layout, corpus_root, memory_limit_bytes=memory_limit_bytes)
        if not isinstance(reader, LakeSessionReader):
            return reader
        return cls(reader, corpus_root)

    def close(self) -> None:
        """Release the lake connection."""
        self._reader.close()

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def session_bounds(self) -> SessionBounds:
        """Every complete session's bounds; the lake supplies the newest-record bound when current."""
        freshness = self._reader.freshness()
        newest = self._reader.last_edge_ts()
        bounds: SessionBounds = {}
        current: set[str] = set()
        from_files: list[str] = []
        for session_dir in complete_session_dirs(self._sessions_dir):
            sid = session_dir.name
            mtime = trajectory_mtime(session_dir)
            if _is_current(session_dir, freshness.get(sid)):
                current.add(sid)
                bounds[sid] = (newest.get(sid), mtime)
            else:
                from_files.append(sid)
                bounds[sid] = (last_edge_ts(session_dir), mtime)
        self._current = current
        self.from_files = tuple(from_files)
        return bounds

    def _split(self, session_ids: Sequence[str]) -> tuple[list[str], list[str]]:
        """``(read from the lake, read from files)``, enumerating first if nothing has."""
        if self._current is None:
            self.session_bounds()
        current = self._current or set()
        in_lake = [sid for sid in session_ids if sid in current]
        on_disk = [sid for sid in session_ids if sid not in current]
        return in_lake, on_disk

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def load_turns(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        """Each session's steps without tool payloads, error flags included."""
        in_lake, on_disk = self._split(session_ids)
        out: dict[str, list[StepEvent]] = dict(self._files.load_turns(on_disk))
        errors = self._reader.error_steps(in_lake)
        grouped: dict[str, list[StepEvent]] = defaultdict(list)
        for sid, step_id, ts, source, message, sidechain, compact, uuids in self._reader.turns(
            in_lake
        ):
            grouped[str(sid)].append(
                step_event(
                    ts=ts,
                    source=source,
                    text=message,
                    source_uuids=_json(uuids),
                    is_sidechain=bool(sidechain),
                    is_compact_summary=bool(compact),
                    has_error_result=(str(sid), int(step_id)) in errors,
                )
            )
        for sid in in_lake:
            out[sid] = grouped.get(sid, [])
        return out

    def load_steps(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        """Each session's steps with every tool call and result, in source order."""
        in_lake, on_disk = self._split(session_ids)
        out: dict[str, list[StepEvent]] = dict(self._files.load_steps(on_disk))
        calls: dict[tuple[str, int], list[tuple[str | None, object]]] = defaultdict(list)
        for sid, step_id, name, tool_input in self._reader.tool_calls(in_lake):
            calls[(str(sid), int(step_id))].append((name, _json(tool_input)))
        results: dict[tuple[str, int], list[tuple[str | None, object, bool]]] = defaultdict(list)
        for sid, step_id, call_id, content, is_error in self._reader.tool_results(in_lake):
            results[(str(sid), int(step_id))].append((call_id, _json(content), is_error is True))
        grouped: dict[str, list[StepEvent]] = defaultdict(list)
        for sid, step_id, ts, source, message, sidechain, compact, uuids in self._reader.turns(
            in_lake
        ):
            key = (str(sid), int(step_id))
            grouped[key[0]].append(
                step_event(
                    ts=ts,
                    source=source,
                    text=message,
                    source_uuids=_json(uuids),
                    is_sidechain=bool(sidechain),
                    is_compact_summary=bool(compact),
                    tool_calls=calls.get(key, ()),
                    tool_results=results.get(key, ()),
                )
            )
        for sid in in_lake:
            out[sid] = grouped.get(sid, [])
        return out

    def edges_uuids(self, session_id: str) -> set[str] | None:
        """The session's raw-record uuids, from the lake when it holds the session's current rows."""
        in_lake, _ = self._split([session_id])
        if not in_lake:
            return self._files.edges_uuids(session_id)
        return self._reader.edge_uuids(session_id)


__all__ = ["STEPS_BATCH_SIZE", "TURNS_BATCH_SIZE", "LakeSessionSource"]
