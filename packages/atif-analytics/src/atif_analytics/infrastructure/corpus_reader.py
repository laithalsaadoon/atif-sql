# SPDX-License-Identifier: Apache-2.0

"""Reader over the materialized ATIF corpus (trajectory.json + edges.jsonl).

This is the infrastructure half of the transcript seam (CONTRACT-V2: the
pipelines read the MATERIALIZED corpus, not raw JSONL). It parses each
session directory's ``trajectory.json`` steps into
:class:`~atif_analytics.domain.transcript.StepEvent` rows and hands the
pure renderer (:mod:`atif_analytics.domain.transcript`) everything it
needs; ``edges.jsonl`` supplies the uuid universe for the conflicts
pipeline's returned-uuid validity guard.

Torn-set guard: like atif-duck, a session directory is visible only when
``meta.json`` exists (the corpus writer lands it last), so a crashed
writer's partial dir contributes nothing.

Session enumeration reads timestamps from ``edges.jsonl``, never from the
parsed steps: :meth:`CorpusReader.session_bounds` runs over every session
in the window on every tick, and parsing a trajectory (tool_result bodies
included) to learn one timestamp makes the cheap checkpoint skip cost as
much as the work it avoids.

Semantic notes (mirrors atif-duck's ``steps`` view):

* ``Step.message`` is ``str | ContentPart[]``; the list branch joins the
  parts' ``text`` fields with blank lines.
* the step uuid is ``extra.source_uuids[0]`` — the FIRST source uuid, the
  documented primary raw-record key (same choice the VSS branch embeds on).
* ATIF ``source`` maps ``agent`` → ``assistant`` for the prompt surface.
* error tool_results are recovered from
  ``observation.results[].extra.tool_result_metadata.is_error`` (harbor
  preserves the raw flag only there — same fidelity-gap recovery as
  atif-duck's cache columns).
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from atif_analytics.domain.config import TranscriptCaps
from atif_analytics.domain.transcript import (
    StepEvent,
    render_session_text,
    text_windows,
)

TRAJECTORY_FILENAME = "trajectory.json"
EDGES_FILENAME = "edges.jsonl"
META_FILENAME = "meta.json"

#: Parsed-step memo capacity. A single trajectory can decode to tens of MB
#: of tool_result strings, so this is an LRU rather than a grow-forever
#: dict: an unbounded memo pins the whole admitted corpus in RSS for the
#: run. The window only has to cover ONE session's intra-stage re-reads
#: (eligibility probe → render → uuid re-walk), not the whole batch.
DEFAULT_STEPS_CACHE_SIZE = 8


def parse_ts(raw: str | None) -> datetime | None:
    """Parse an ATIF ISO-8601 timestamp (``...Z``) to an aware UTC datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _flatten_message(message: Any) -> str:
    """Flatten ATIF ``Step.message`` (str | ContentPart[]) to one text body."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for part in message:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _step_event(step: dict[str, Any]) -> StepEvent:
    """Project one raw trajectory step dict into a :class:`StepEvent`."""
    extra = step.get("extra") or {}
    source_uuids = extra.get("source_uuids") or []
    uuid = source_uuids[0] if source_uuids and isinstance(source_uuids[0], str) else None
    source = str(step.get("source") or "")
    role = "assistant" if source == "agent" else (source or "unknown")

    tool_calls: list[tuple[str, str]] = []
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("function_name") or "")
        args = call.get("arguments")
        args_json = json.dumps(args, ensure_ascii=False, default=str) if args is not None else ""
        tool_calls.append((name, args_json))

    tool_results: list[tuple[str, str]] = []
    has_error = False
    observation = step.get("observation") or {}
    for res in observation.get("results") or []:
        if not isinstance(res, dict):
            continue
        call_id = str(res.get("source_call_id") or "")
        content = res.get("content")
        content_str = content if isinstance(content, str) else json.dumps(content, default=str)
        tool_results.append((call_id, content_str or ""))
        res_extra = res.get("extra") or {}
        meta = res_extra.get("tool_result_metadata") or {}
        if meta.get("is_error") is True:
            has_error = True

    return StepEvent(
        ts=str(step.get("timestamp") or ""),
        role=role,
        text=_flatten_message(step.get("message")),
        uuid=uuid,
        is_sidechain=bool(extra.get("is_sidechain") or False),
        is_compact_summary=bool(extra.get("is_compact_summary") or False),
        has_error_result=has_error,
        tool_calls=tool_calls,
        tool_results=tool_results,
    )


class CorpusReader:
    """Read-only seam over ``<corpus_root>/sessions/<id>/`` artifact dirs.

    Loaded steps are memoized in a bounded LRU so the repeated reads WITHIN
    one session's admission (eligibility probe, transcript render, uuid
    re-walk) each pay one parse. The bound is the point: a trajectory can
    decode to tens of MB, one reader is shared by every stage, and an
    unbounded memo would hold the whole admitted batch resident for the run.
    A stage that revisits a session evicted from the window re-parses it.
    """

    def __init__(
        self,
        corpus_root: Path,
        *,
        caps: TranscriptCaps | None = None,
        steps_cache_size: int = DEFAULT_STEPS_CACHE_SIZE,
    ) -> None:
        self._root = corpus_root
        self._caps = caps if caps is not None else TranscriptCaps()
        self._steps_cache: OrderedDict[str, list[StepEvent]] = OrderedDict()
        self._steps_cache_size = max(1, steps_cache_size)

    @property
    def sessions_dir(self) -> Path:
        """``<corpus_root>/sessions/``."""
        return self._root / "sessions"

    # ------------------------------------------------------------------
    # Session enumeration
    # ------------------------------------------------------------------

    def _complete_session_dirs(self) -> list[Path]:
        """Session dirs carrying meta.json + trajectory.json (torn-set gate)."""
        if not self.sessions_dir.is_dir():
            return []
        out: list[Path] = []
        for d in sorted(self.sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            if not (d / META_FILENAME).exists():
                logger.warning("Skipping incomplete session dir {} (no meta.json)", d)
                continue
            if (d / TRAJECTORY_FILENAME).exists():
                out.append(d)
        return out

    def _last_edge_ts(self, session_dir: Path) -> datetime | None:
        """Newest record timestamp from one session's ``edges.jsonl``.

        The edges file is one small JSON object per raw record — no message
        bodies, no tool_result content — so this is the cheap bound.
        atif-converter sorts edges by ``(ts, uuid)``, but the scan takes the
        max rather than the last line so a hand-written or re-ordered file
        still yields the true newest timestamp.
        """
        path = session_dir / EDGES_FILENAME
        newest: datetime | None = None
        try:
            with path.open() as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(rec.get("ts")) if isinstance(rec, dict) else None
                    if ts is not None and (newest is None or ts > newest):
                        newest = ts
        except OSError as exc:
            logger.warning(
                "corpus_reader: unreadable edges for {} ({}); no last_ts bound",
                session_dir.name,
                exc,
            )
        return newest

    def session_bounds(
        self, *, since_days: int | None = None, limit: int | None = None
    ) -> dict[str, tuple[datetime | None, datetime | None]]:
        """``{session_id: (last_record_ts, trajectory_mtime)}``, newest-first.

        Drives the mtime-based checkpoint skip: either bound advancing
        re-admits the session. ``since_days`` filters on the last record
        timestamp; ``limit`` caps the newest-first list.

        Both bounds come from cheap reads — ``edges.jsonl`` line scan plus
        one ``stat`` — so enumerating a whole corpus never parses a
        trajectory. That matters because this runs once per stage per tick
        over EVERY session in the window, including the ones the checkpoint
        is about to skip.
        """
        rows: list[tuple[str, datetime | None, datetime | None]] = []
        for d in self._complete_session_dirs():
            sid = d.name
            last_ts = self._last_edge_ts(d)
            mtime: datetime | None = None
            try:
                st = (d / TRAJECTORY_FILENAME).stat()
                mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            except OSError:
                mtime = None
            rows.append((sid, last_ts, mtime))

        if since_days is not None:
            cutoff = datetime.now(UTC).timestamp() - since_days * 86_400
            rows = [r for r in rows if r[1] is not None and r[1].timestamp() >= cutoff]
        epoch = datetime.min.replace(tzinfo=UTC)
        rows.sort(key=lambda r: (r[1] is not None, r[1] or epoch), reverse=True)
        if limit is not None:
            rows = rows[: int(limit)]
        return {sid: (last_ts, mtime) for sid, last_ts, mtime in rows}

    def session_ids(self, *, since_days: int | None = None, limit: int | None = None) -> list[str]:
        """Newest-first session ids matching the window."""
        return list(self.session_bounds(since_days=since_days, limit=limit))

    # ------------------------------------------------------------------
    # Per-session artifacts
    # ------------------------------------------------------------------

    def _memoize_steps(self, session_id: str, steps: list[StepEvent]) -> list[StepEvent]:
        """Insert into the LRU, evicting the least-recently-used entry."""
        self._steps_cache[session_id] = steps
        self._steps_cache.move_to_end(session_id)
        while len(self._steps_cache) > self._steps_cache_size:
            self._steps_cache.popitem(last=False)
        return steps

    def load_steps(self, session_id: str) -> list[StepEvent]:
        """Parse one session's trajectory steps, memoized in the bounded LRU."""
        cached = self._steps_cache.get(session_id)
        if cached is not None:
            self._steps_cache.move_to_end(session_id)
            return cached
        path = self.sessions_dir / session_id / TRAJECTORY_FILENAME
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("corpus_reader: unreadable trajectory for {} ({})", session_id, exc)
            return self._memoize_steps(session_id, [])
        steps = [_step_event(s) for s in (doc.get("steps") or []) if isinstance(s, dict)]
        return self._memoize_steps(session_id, steps)

    def session_text(self, session_id: str, *, include_uuids: bool = False) -> str:
        """One assembled transcript per the documented byte-shape contract."""
        return render_session_text(
            self.load_steps(session_id),
            total_max_chars=self._caps.session_text_total_max_chars,
            tool_result_max_chars=self._caps.session_text_tool_result_max_chars,
            include_uuids=include_uuids,
        )

    def text_windows(
        self, session_id: str
    ) -> list[tuple[str, str | None, str, str | None, str, str | None, str]]:
        """Adjacent text-step pairs (the turn_window analogue) for one session."""
        return text_windows(self.load_steps(session_id), session_id)

    def edges_uuids(self, session_id: str) -> set[str] | None:
        """Non-null raw-record uuids from ``edges.jsonl``, or ``None`` if unreadable.

        The conflicts pipeline validates the model's returned
        ``turn_*_uuid`` values against this set. ``None`` and an empty set
        are DIFFERENT answers and callers must keep them apart: ``None``
        means the uuid universe is unknown, so no returned uuid can be
        confirmed or refuted, while an empty set means the session
        genuinely has no raw-record uuids. Neither can validate anything,
        so neither may be read as "the guard passes" — a model-returned
        uuid that reaches the parquet unverified is indistinguishable from
        a real one to every downstream view.
        """
        path = self.sessions_dir / session_id / EDGES_FILENAME
        out: set[str] = set()
        try:
            with path.open() as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    uuid = rec.get("uuid")
                    if isinstance(uuid, str) and uuid:
                        out.add(uuid)
        except OSError as exc:
            logger.warning("corpus_reader: unreadable edges for {} ({})", session_id, exc)
            return None
        return out


__all__ = ["CorpusReader", "parse_ts"]
