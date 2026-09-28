# SPDX-License-Identifier: Apache-2.0

"""The corpus reader the pipelines share, and the per-session file source under it.

:class:`CorpusReader` is what every pipeline walks: the window filter, the
newest-first order, the bounded memos and the transcript renderer, over one
:class:`~atif_analytics.domain.ports.SessionSource`. The default source is
:class:`TrajectoryFileSource`, which parses each session directory's
``trajectory.json`` steps and scans its ``edges.jsonl`` (CONTRACT-V2: the
pipelines read the MATERIALIZED corpus, not raw JSONL). atif-cli plugs in the
lake source instead when the lake holds the corpus.

Torn-set guard: like atif-duck, a session directory is visible only when
``meta.json`` exists (the corpus writer lands it last), so a crashed
writer's partial dir contributes nothing.

Stored names: materialize stores ``trajectory.json`` and ``edges.jsonl``
zstd-compressed, as ``<name>.zst``, and a corpus written before that keeps
the plain files until ``atif-sql corpus slim`` compresses them. Every reader
here resolves the stored file per session (:func:`stored_file`, compressed
first) and reads the same bytes either way. ``corpus slim`` keeps the plain
file's mtime on the compressed one, so the mtime bound doesn't move.

Session enumeration reads timestamps from ``edges.jsonl``, never from the
parsed steps: :meth:`CorpusReader.session_bounds` runs over every session
in the window on every tick, and parsing a trajectory (tool_result bodies
included) to learn one timestamp makes the cheap checkpoint skip cost as
much as the work it avoids.

Semantic notes (mirrors atif-duck's ``steps`` view; the shared projection is
:func:`~atif_analytics.domain.transcript.step_event`):

* ``Step.message`` is ``str | ContentPart[]``; the list branch joins the
  parts' ``text`` fields with blank lines.
* the step uuid is ``extra.source_uuids[0]`` — the FIRST source uuid, the
  documented primary raw-record key (same choice the VSS branch embeds on).
* ATIF ``source`` maps ``agent`` → ``assistant`` for the prompt surface, and
  every user step carries its ``author``
  (:mod:`atif_analytics.domain.authorship`).
* a result is an error when the converter's typed ``extra.is_error`` says so
  (the flag the lake and the ``tool_results`` view carry), or when harbor's
  ``extra.tool_result_metadata.is_error`` does (the raw Claude Code flag, the
  one this reader read before the typed flag existed; the two agree on every
  Claude Code result, and Codex results carry only the typed one).
"""

from __future__ import annotations

import io
import json
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_analytics.domain.config import TranscriptCaps
from atif_analytics.domain.transcript import (
    StepEvent,
    render_session_text,
    session_kind,
    step_event,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from atif_analytics.domain.authorship import SessionKind
    from atif_analytics.domain.ports import SessionBounds, SessionSource

TRAJECTORY_FILENAME = "trajectory.json"
EDGES_FILENAME = "edges.jsonl"
META_FILENAME = "meta.json"
#: The suffix a compressed artifact carries (twin of atif-corpus's, pinned in
#: atif-cli's tests).
COMPRESSED_SUFFIX = ".zst"

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


def parse_trajectory_step(step: dict[str, Any]) -> StepEvent:
    """Project one raw ``trajectory.json`` step dict into a :class:`StepEvent`."""
    extra = step.get("extra") or {}
    calls: list[tuple[str | None, object]] = [
        (call.get("function_name"), call.get("arguments"))
        for call in step.get("tool_calls") or []
        if isinstance(call, dict)
    ]
    results: list[tuple[str | None, object, bool]] = []
    observation = step.get("observation") or {}
    for res in observation.get("results") or []:
        if not isinstance(res, dict):
            continue
        res_extra = res.get("extra") or {}
        meta = res_extra.get("tool_result_metadata") or {}
        is_error = res_extra.get("is_error") is True or meta.get("is_error") is True
        results.append((res.get("source_call_id"), res.get("content"), is_error))
    return step_event(
        ts=parse_ts(str(step.get("timestamp") or "")),
        source=str(step.get("source") or ""),
        text=_flatten_message(step.get("message")),
        source_uuids=extra.get("source_uuids"),
        is_sidechain=bool(extra.get("is_sidechain") or False),
        is_compact_summary=bool(extra.get("is_compact_summary") or False),
        tool_calls=calls,
        tool_results=results,
    )


def stored_file(session_dir: Path, name: str) -> Path | None:
    """The file ``name`` is stored under (``<name>.zst`` first, then ``<name>``), or ``None``."""
    for candidate in (f"{name}{COMPRESSED_SUFFIX}", name):
        path = session_dir / candidate
        if path.is_file():
            return path
    return None


def _read_bytes(path: Path) -> bytes:
    if not path.name.endswith(COMPRESSED_SUFFIX):
        return path.read_bytes()
    import zstandard

    with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
        return reader.readall()


def _lines(path: Path) -> Iterable[str]:
    """``path``'s decompressed text, line by line."""
    with path.open("rb") as raw:
        if path.name.endswith(COMPRESSED_SUFFIX):
            import zstandard

            with (
                zstandard.ZstdDecompressor().stream_reader(raw) as reader,
                io.TextIOWrapper(reader, encoding="utf-8") as text,
            ):
                yield from text
        else:
            with io.TextIOWrapper(raw, encoding="utf-8") as text:
                yield from text


def complete_session_dirs(sessions_dir: Path) -> list[Path]:
    """Session dirs carrying meta.json + a stored trajectory (torn-set gate), sorted."""
    if not sessions_dir.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        if not (d / META_FILENAME).exists():
            logger.warning("Skipping incomplete session dir {} (no meta.json)", d)
            continue
        if stored_file(d, TRAJECTORY_FILENAME) is not None:
            out.append(d)
    return out


def last_edge_ts(session_dir: Path) -> datetime | None:
    """Newest record timestamp from one session's ``edges.jsonl``.

    The edges file is one small JSON object per raw record — no message
    bodies, no tool_result content — so this is the cheap bound.
    atif-converter sorts edges by ``(ts, uuid)``, but the scan takes the
    max rather than the last line so a hand-written or re-ordered file
    still yields the true newest timestamp.
    """
    path = stored_file(session_dir, EDGES_FILENAME) or session_dir / EDGES_FILENAME
    newest: datetime | None = None
    try:
        for raw_line in _lines(path):
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


def trajectory_mtime(session_dir: Path) -> datetime | None:
    """The stored trajectory's mtime: the bound that moves when materialize rewrites a session."""
    path = stored_file(session_dir, TRAJECTORY_FILENAME)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(st.st_mtime, tz=UTC)


def read_trajectory_steps(session_dir: Path) -> list[StepEvent]:
    """Parse one session's stored trajectory steps (empty when unreadable)."""
    path = stored_file(session_dir, TRAJECTORY_FILENAME) or session_dir / TRAJECTORY_FILENAME
    try:
        doc = json.loads(_read_bytes(path))
    except (OSError, ValueError) as exc:
        logger.warning("corpus_reader: unreadable trajectory for {} ({})", session_dir.name, exc)
        return []
    return [parse_trajectory_step(s) for s in (doc.get("steps") or []) if isinstance(s, dict)]


def read_edges_uuids(session_dir: Path) -> set[str] | None:
    """Non-null raw-record uuids from ``edges.jsonl``, or ``None`` if unreadable."""
    path = stored_file(session_dir, EDGES_FILENAME) or session_dir / EDGES_FILENAME
    out: set[str] = set()
    try:
        for raw_line in _lines(path):
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
        logger.warning("corpus_reader: unreadable edges for {} ({})", session_dir.name, exc)
        return None
    return out


class TrajectoryFileSource:
    """The per-session file source: ``trajectory.json`` steps, ``edges.jsonl`` bounds.

    Parsing costs per session, so it never reads ahead (batch size 1), and a
    trajectory parse yields the tool payloads anyway, so :meth:`load_turns`
    is :meth:`load_steps`.
    """

    name: str = "files"
    turns_batch_size: int = 1
    steps_batch_size: int = 1
    #: :meth:`load_turns` already carries the tool payloads, so the reader
    #: keeps one memo for both and parses a session once.
    turns_are_complete: bool = True

    def __init__(self, corpus_root: Path) -> None:
        self._sessions_dir = corpus_root / "sessions"

    def session_bounds(self) -> SessionBounds:
        """``{session_id: (last_record_ts, trajectory_mtime)}`` over every complete dir."""
        return {
            d.name: (last_edge_ts(d), trajectory_mtime(d))
            for d in complete_session_dirs(self._sessions_dir)
        }

    def batchable(self, session_id: str) -> bool:
        """Never: a parse costs per session, so nothing is read ahead."""
        del session_id
        return False

    def load_steps(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        """Parse each session's trajectory."""
        return {sid: read_trajectory_steps(self._sessions_dir / sid) for sid in session_ids}

    def load_turns(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        """The same as :meth:`load_steps` (a parse yields the payloads anyway)."""
        return self.load_steps(session_ids)

    def edges_uuids(self, session_id: str) -> set[str] | None:
        """Non-null raw-record uuids from the session's ``edges.jsonl``."""
        return read_edges_uuids(self._sessions_dir / session_id)


class CorpusReader:
    """The pipelines' view of one corpus, over a :class:`~atif_analytics.domain.ports.SessionSource`.

    Two bounded memos. The steps memo holds sessions with their tool payloads
    (what the renderer reads) so the repeated reads WITHIN one session's
    admission (eligibility probe, transcript render, uuid re-walk) each pay
    one load. The turns memo holds the gate-level steps a source can load
    without the payloads. The bound is the point: a trajectory can decode to
    tens of MB, one reader is shared by every stage, and an unbounded memo
    would hold the whole admitted batch resident for the run. A stage that
    revisits a session evicted from the window loads it again.

    Every :meth:`session_bounds` call drops the memoized sessions whose
    bounds moved since the last one. The stages share one reader, and
    materialize can rewrite a session between two of them, so without this a
    later stage would gate on the old steps and then checkpoint the session
    at its new bounds, never looking at the new ones.

    Read-ahead: a source whose one call costs about the same for many
    sessions as for one (the lake) sets a batch size, and a turns miss then
    loads that many sessions from the miss onward in the newest-first order of
    the last :meth:`session_bounds`, which is the order every gate walks. The
    file source's batch size is 1, so it loads exactly what is asked for.
    Only sessions the source calls :meth:`~atif_analytics.domain.ports.SessionSource.batchable`
    ride along, so a session the lake source would read from its files is
    parsed only when a gate asks for it.
    """

    def __init__(
        self,
        corpus_root: Path,
        *,
        caps: TranscriptCaps | None = None,
        source: SessionSource | None = None,
        steps_cache_size: int = DEFAULT_STEPS_CACHE_SIZE,
    ) -> None:
        self._root = corpus_root
        self._caps = caps if caps is not None else TranscriptCaps()
        self._source: SessionSource = (
            source if source is not None else TrajectoryFileSource(corpus_root)
        )
        self._steps_cache: OrderedDict[str, list[StepEvent]] = OrderedDict()
        self._steps_cache_size = max(1, steps_cache_size, self._source.steps_batch_size)
        self._turns_cache: OrderedDict[str, list[StepEvent]] = OrderedDict()
        self._turns_cache_size = 2 * max(1, self._source.turns_batch_size)
        self._order: dict[str, int] = {}
        self._ordered: list[str] = []
        self._bounds: SessionBounds = {}

    @property
    def source_name(self) -> str:
        """Which source this reader reads (``files`` or ``lake``)."""
        return self._source.name

    # ------------------------------------------------------------------
    # Session enumeration
    # ------------------------------------------------------------------

    def session_bounds(
        self, *, since_days: int | None = None, limit: int | None = None
    ) -> dict[str, tuple[datetime | None, datetime | None]]:
        """``{session_id: (last_record_ts, trajectory_mtime)}``, newest-first.

        Drives the mtime-based checkpoint skip: either bound advancing
        re-admits the session. ``since_days`` filters on the last record
        timestamp; ``limit`` caps the newest-first list.

        Both bounds come from cheap reads (the source never parses a
        trajectory for them), because this runs once per stage per tick over
        EVERY session in the window, including the ones the checkpoint is
        about to skip. The order is also the read-ahead order.
        """
        bounds = self._source.session_bounds()
        self._forget_moved(bounds)
        rows = [(sid, last_ts, mtime) for sid, (last_ts, mtime) in bounds.items()]
        rows.sort(key=lambda r: r[0])
        if since_days is not None:
            cutoff = datetime.now(UTC).timestamp() - since_days * 86_400
            rows = [r for r in rows if r[1] is not None and r[1].timestamp() >= cutoff]
        epoch = datetime.min.replace(tzinfo=UTC)
        rows.sort(key=lambda r: (r[1] is not None, r[1] or epoch), reverse=True)
        if limit is not None:
            rows = rows[: int(limit)]
        self._ordered = [sid for sid, _, _ in rows]
        self._order = {sid: i for i, sid in enumerate(self._ordered)}
        return {sid: (last_ts, mtime) for sid, last_ts, mtime in rows}

    def _forget_moved(self, bounds: SessionBounds) -> None:
        """Drop every memoized session whose bounds differ from the last enumeration's."""
        for cache in (self._turns_cache, self._steps_cache):
            for sid in [sid for sid in cache if bounds.get(sid) != self._bounds.get(sid)]:
                del cache[sid]
        self._bounds = bounds

    def session_ids(self, *, since_days: int | None = None, limit: int | None = None) -> list[str]:
        """Newest-first session ids matching the window."""
        return list(self.session_bounds(since_days=since_days, limit=limit))

    # ------------------------------------------------------------------
    # Per-session artifacts
    # ------------------------------------------------------------------

    @staticmethod
    def _memoize(
        cache: OrderedDict[str, list[StepEvent]], size: int, session_id: str, steps: list[StepEvent]
    ) -> list[StepEvent]:
        """Insert into an LRU, evicting the least-recently-used entry."""
        cache[session_id] = steps
        cache.move_to_end(session_id)
        while len(cache) > size:
            cache.popitem(last=False)
        return steps

    def _full_steps(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        """Each session's steps with tool payloads, through the steps memo."""
        out: dict[str, list[StepEvent]] = {}
        missing: list[str] = []
        for sid in session_ids:
            cached = self._steps_cache.get(sid)
            if cached is None:
                missing.append(sid)
            else:
                self._steps_cache.move_to_end(sid)
                out[sid] = cached
        if missing:
            loaded = self._source.load_steps(missing)
            for sid in missing:
                out[sid] = self._memoize(
                    self._steps_cache, self._steps_cache_size, sid, list(loaded.get(sid, []))
                )
        return out

    def _read_ahead(self, session_id: str) -> list[str]:
        """``session_id`` plus the next uncached sessions in the walk order, up to the batch."""
        batch = [session_id]
        size = self._source.turns_batch_size
        start = self._order.get(session_id)
        if size <= 1 or start is None:
            return batch
        for sid in self._ordered[start + 1 :]:
            if len(batch) >= size:
                break
            if (
                sid not in self._turns_cache
                and sid not in self._steps_cache
                and self._source.batchable(sid)
            ):
                batch.append(sid)
        return batch

    def load_steps(self, session_id: str) -> list[StepEvent]:
        """One session's steps for the gates, memoized.

        Every step with its text, flags, uuid, author and error flag; the
        tool payload lists may be empty (the gates never read them, and the
        lake source leaves them out). :meth:`session_text` loads the payloads.
        """
        if self._source.turns_are_complete:
            return self._full_steps([session_id])[session_id]
        for cache in (self._turns_cache, self._steps_cache):
            cached = cache.get(session_id)
            if cached is not None:
                cache.move_to_end(session_id)
                return cached
        batch = self._read_ahead(session_id)
        loaded = self._source.load_turns(batch)
        for sid in batch:
            self._memoize(self._turns_cache, self._turns_cache_size, sid, list(loaded.get(sid, [])))
        return self._turns_cache[session_id]

    def session_text(self, session_id: str, *, include_uuids: bool = False) -> str:
        """One assembled transcript per the documented byte-shape contract."""
        return self._render(self._full_steps([session_id])[session_id], include_uuids=include_uuids)

    def session_texts(
        self, session_ids: Sequence[str], *, include_uuids: bool = False
    ) -> Iterator[str]:
        """:meth:`session_text` for each session in order, loading a source batch at a time.

        Holds at most one batch of loaded sessions (the steps memo is at
        least that large), so rendering a capped list costs a few source
        calls instead of one per session.
        """
        size = max(1, self._source.steps_batch_size)
        for start in range(0, len(session_ids), size):
            chunk = list(session_ids[start : start + size])
            loaded = self._full_steps(chunk)
            for sid in chunk:
                yield self._render(loaded[sid], include_uuids=include_uuids)

    def _render(self, steps: list[StepEvent], *, include_uuids: bool) -> str:
        return render_session_text(
            steps,
            total_max_chars=self._caps.session_text_total_max_chars,
            tool_result_max_chars=self._caps.session_text_tool_result_max_chars,
            include_uuids=include_uuids,
        )

    def session_kind(self, session_id: str) -> SessionKind:
        """``interactive`` | ``one_shot_job`` | ``turn_audit`` (atif-duck's ``session_outcomes.kind``)."""
        return session_kind(self.load_steps(session_id))

    def edges_uuids(self, session_id: str) -> set[str] | None:
        """Non-null raw-record uuids, or ``None`` if unknown.

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
        return self._source.edges_uuids(session_id)


__all__ = [
    "DEFAULT_STEPS_CACHE_SIZE",
    "EDGES_FILENAME",
    "META_FILENAME",
    "TRAJECTORY_FILENAME",
    "CorpusReader",
    "TrajectoryFileSource",
    "complete_session_dirs",
    "last_edge_ts",
    "parse_trajectory_step",
    "parse_ts",
    "read_edges_uuids",
    "read_trajectory_steps",
    "trajectory_mtime",
]
