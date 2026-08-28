# SPDX-License-Identifier: Apache-2.0

"""Use case: sync the materialized corpus with the raw transcript corpus.

One pass = sweep → scan → plan → convert → write → advance watermark:

0. Sweep ``.staging/``: it holds nothing durable, only scratch dirs for the
   in-flight directory swap, so residue whose owning pid is GONE is crash
   debris and is removed. An entry whose ``tmp-<pid>`` owner is still running
   belongs to a concurrent pass and is left alone — the refresh script locks
   per lane and a manual run locks nothing. Without this sweep,
   ``*.tmp-<oldpid>`` and ``*.old-<pid>`` accumulate on the corpus
   filesystem forever.
1. Scan the source root (main transcripts + every side-file, per contract).
2. Build a :class:`~atif_corpus.domain.sessions.MaterializationPlan` from
   quiescence + the recorded watermark — the pure decision. A session the
   watermark calls current but whose artifact dir is ABSENT is force-replanned:
   the only way to reach that state is a kill inside the swap window, and the
   watermark cannot express it because it records source mtimes and knows
   nothing about the corpus dir.
3. For each planned session, convert through the injected
   :class:`~atif_corpus.domain.ports.ConverterPort` and write the four
   artifacts into a TEMP session dir, then swap the whole dir into place
   (directory rename) — readers can never observe a torn artifact SET,
   only the complete old generation or the complete new one. Within the
   temp dir, ``meta.json`` is still written LAST as belt-and-braces — meta
   is the "this session's artifacts are complete" marker, and atif-duck
   gates its readers on meta presence, so it must never exist beside a
   partial artifact set.
4. Remove ghost sessions: a corpus session dir whose main JSONL vanished
   from the source scan is DELETED, not tombstoned — the raw source is
   gone, so a retained artifact could never be re-derived or checked
   against anything. Removals are
   reported (``sessions_removed`` / ``removed_session_ids``). Two guards: a
   session the scan could not STAT (EIO, ESTALE, a permission blip) is never
   a ghost — only a genuine ``FileNotFoundError`` counts, and such a session
   is reported in ``unreadable_session_ids`` because it lands in no other
   counter; and when the scan found ZERO sessions while the corpus holds
   some, that smells like a wrong ``source_root`` — the pass fails loud
   (:class:`SuspiciousEmptyScanError`) instead of silently deleting the
   whole corpus.
5. Advance ``watermark.json``: only sessions that materialized successfully
   move their entries forward, so a failed session stays stale and is
   retried next pass instead of being silently forgotten.

A failing session is recorded in the report and skipped — one broken
transcript must never abort a corpus sync.

Clock discipline: ``materialized_at`` / ``harbor_version`` /
``converter_version`` are passed IN by the caller (atif-cli owns the wall
clock and the version pins); the only clocks read here are ``time.time_ns``
for the quiescence "now" when the caller does not supply one, and
``time.perf_counter`` for durations. The domain reads no clock at all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

from atif_corpus.domain.layout import (
    EDGES_FILENAME,
    LOSS_REPORT_FILENAME,
    META_FILENAME,
    TRAJECTORY_FILENAME,
    CorpusLayout,
)
from atif_corpus.domain.sessions import (
    MaterializationPlan,
    QuiescencePolicy,
    build_plan,
    owns_path,
)
from atif_corpus.infrastructure.atomic import (
    replace_dir_atomic,
    write_json_atomic,
    write_text_atomic,
)
from atif_corpus.infrastructure.scanner import scan_sources

if TYPE_CHECKING:
    from atif_corpus.domain.ports import ConverterPort
    from atif_corpus.domain.sessions import SessionSource
    from atif_corpus.infrastructure.scanner import SourceScan


class SuspiciousEmptyScanError(RuntimeError):
    """The scan found zero sessions while the corpus holds materialized ones.

    Almost always a wrong ``source_root`` (typo, unmounted disk, env var
    pointing elsewhere) — proceeding would garbage-collect the ENTIRE
    corpus as "ghosts". Fail loud instead; an operator who really emptied
    the source tree can delete the corpus dir explicitly.
    """


@dataclass(frozen=True, slots=True)
class MaterializationFailure:
    """One session that failed to convert or write during a pass."""

    #: The session that failed.
    session_id: str
    #: ``repr``-ish one-line description of what went wrong.
    error: str


@dataclass(frozen=True, slots=True)
class MaterializationReport:
    """What one materialization pass did, for logs and the CLI status line."""

    #: Sessions whose artifacts were (re)written this pass.
    materialized_count: int
    #: Sessions already current with their sources; untouched.
    up_to_date_count: int
    #: Sessions still being written; deferred to a later pass.
    skipped_live_count: int
    #: Sessions that raised during convert/write; watermark NOT advanced.
    failures: tuple[MaterializationFailure, ...]
    #: Wall seconds for the whole pass (scan + plan + convert + write).
    total_seconds: float
    #: Wall seconds spent inside ``ConverterPort.convert`` calls.
    convert_seconds: float
    #: Ghost session dirs removed (source vanished from the scan).
    removed_session_ids: tuple[str, ...] = ()
    #: Sessions the scan could not stat, so they appear in no other counter.
    #:
    #: A transient stat error resolves itself next pass. A PERMANENT one (a
    #: side-file left at mode 000) starves the session forever: it never
    #: materializes, is never up_to_date, is never skipped_live, and is
    #: deliberately never ghosted. Without this field the pass reports all
    #: zeroes and an operator sees a corpus that looks idle and complete.
    unreadable_session_ids: tuple[str, ...] = ()

    @property
    def failed_count(self) -> int:
        """Number of sessions that failed this pass."""
        return len(self.failures)

    @property
    def sessions_removed(self) -> int:
        """Number of ghost session dirs removed this pass."""
        return len(self.removed_session_ids)

    @property
    def unreadable_count(self) -> int:
        """Number of sessions the scan could not read this pass."""
        return len(self.unreadable_session_ids)


def read_watermark(path: Path) -> dict[str, int]:
    """Load ``watermark.json`` (``{path: mtime_ns}``); empty on first run.

    A corrupt watermark degrades to empty — the cost is one full
    re-materialization pass, which is always safe, versus refusing to sync.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("watermark {} unreadable; treating corpus as unmaterialized", path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("watermark {} has wrong shape; treating corpus as unmaterialized", path)
        return {}
    try:
        return {str(key): int(value) for key, value in raw.items()}
    except (TypeError, ValueError):
        # A non-numeric mtime is corruption of the same kind as unparseable
        # JSON and takes the same exit: degrade, do not raise. Inside the
        # comprehension the coercion is what `json.JSONDecodeError` above
        # cannot see, and an uncaught ValueError here would kill both
        # `materialize` and the read-only `status` that shares this reader.
        logger.warning(
            "watermark {} holds a non-numeric mtime; treating corpus as unmaterialized", path
        )
        return {}


def _write_session(
    layout: CorpusLayout,
    session: SessionSource,
    converter: ConverterPort,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
) -> float:
    """Convert one session and write its four artifacts; returns convert seconds.

    All four artifacts are written into a staging dir under
    ``<corpus_root>/.staging/`` (outside every reader glob), then the whole
    dir is swapped into ``sessions/<id>/`` via
    :func:`~atif_corpus.infrastructure.atomic.replace_dir_atomic` — a crash
    at any point before the swap leaves the previous generation fully intact
    and internally consistent; readers never see a torn artifact set. A kill
    INSIDE the swap window leaves the session dir missing rather than torn,
    and :func:`_unmaterialized_session_ids` replans it next pass. Within
    staging the write order stays trajectory → loss_report → edges → meta,
    ``meta.json`` last as belt-and-braces (atif-duck gates its readers on
    meta presence), and each artifact is fsynced before its rename so the
    ordering holds across power loss too.
    """
    convert_started = time.perf_counter()
    output = converter.convert(Path(session.session_jsonl))
    convert_elapsed = time.perf_counter() - convert_started

    staging = layout.staging_dir / f"{session.session_id}.tmp-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        write_json_atomic(staging / TRAJECTORY_FILENAME, output.trajectory_dict, compact=True)
        write_json_atomic(staging / LOSS_REPORT_FILENAME, output.loss_report_dict)
        write_text_atomic(
            staging / EDGES_FILENAME,
            "".join(f"{line}\n" for line in output.edges_lines),
        )
        write_json_atomic(
            staging / META_FILENAME,
            {
                "session_id": session.session_id,
                "source_mtime_ns": session.newest_mtime_ns,
                "source_files": list(session.source_files),
                "harbor_version": harbor_version,
                "converter_version": converter_version,
                "materialized_at": materialized_at,
            },
        )
        layout.sessions_dir.mkdir(parents=True, exist_ok=True)
        replace_dir_atomic(staging, layout.session_dir(session.session_id))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return convert_elapsed


#: Every ``.tmp-<pid>`` / ``.old-<pid>`` marker a staging entry name carries.
_STAGING_PID_RE = re.compile(r"\.(?:tmp|old)-(\d+)")


def _staging_owner_pids(name: str) -> frozenset[int]:
    """Pids named by a staging entry (``<sid>.tmp-<pid>[.old-<pid>]``)."""
    return frozenset(int(match) for match in _STAGING_PID_RE.findall(name))


def _is_live_pid(pid: int) -> bool:
    """True when ``pid`` names a process that currently exists.

    Only ``ProcessLookupError`` proves the pid is gone. Every other outcome —
    signal delivered, or ``PermissionError`` because the process belongs to
    another user — means it exists, and an unexpected ``OSError`` means the
    probe learned nothing. All three answer "do not delete", which is the
    direction that cannot destroy a running pass's scratch.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sweep_staging(layout: CorpusLayout) -> None:
    """Remove ``.staging/`` entries owned by pids that no longer exist.

    ``.staging/`` is exclusively scratch for the in-flight directory swap —
    nothing durable lives there and no reader globs it — so an entry whose
    ``tmp-<pid>``/``old-<pid>`` owner is GONE is debris from a process that
    died mid-swap. The per-session cleanup in :func:`_write_session` only
    matches the current pid, so without this sweep ``*.tmp-<oldpid>`` and
    ``*.old-<pid>`` grow without bound on the corpus filesystem.

    A single writer is NOT the precondition. ``scripts/atif-sql-refresh.sh``
    takes ``flock -n`` on a PER-LANE lock, and a manual
    ``atif-sql materialize`` takes no lock at all, so a second pass can be
    mid-swap while this one starts. Deleting its ``*.tmp-<pid>`` destroys live
    work, so the pid probe gates every removal.

    A pid can be recycled onto an unrelated process, which only makes the
    sweep skip debris it could have removed — the next pass whose pid table
    has moved on collects it. Erring that direction is deliberate.
    """
    staging = layout.staging_dir
    if not staging.is_dir():
        return
    for entry in sorted(staging.iterdir()):
        live = sorted(pid for pid in _staging_owner_pids(entry.name) if _is_live_pid(pid))
        if live:
            logger.debug(
                "materialize: keeping staging entry {} — pid(s) {} still running",
                entry,
                live,
            )
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        logger.debug("materialize: swept crash residue {}", entry)


def _unmaterialized_session_ids(
    layout: CorpusLayout,
    watermark: Mapping[str, int],
    sessions: Collection[SessionSource],
) -> frozenset[str]:
    """Scanned sessions the watermark calls current but whose artifact dir is gone.

    Reachable one way: a pass was killed inside
    :func:`~atif_corpus.infrastructure.atomic.replace_dir_atomic`'s swap
    window, after the previous generation was renamed aside and before the
    new one landed. The watermark records SOURCE mtimes only, so on a
    ``--force`` pass over a non-stale session it still matches and every
    later pass reports ``up_to_date`` while the session dir stays missing.
    Force-replanning these is the recovery.

    Operand order is the whole cost of this function. The dir check is one
    ``stat``; the watermark check is a linear scan of every watermark entry,
    so testing it first makes the pass O(sessions x watermark_entries) rather
    than O(sessions), every pass, to flag nothing. The missing dir is the rare
    condition, so it gates the scan.
    """
    return frozenset(
        session.session_id
        for session in sessions
        if not layout.session_dir(session.session_id).is_dir()
        and any(owns_path(session.session_jsonl, path) for path in watermark)
    )


def _sessions_under_unlistable_dirs(
    scan: SourceScan,
    watermark: Mapping[str, int],
    *,
    source_root: Path,
) -> dict[str, str]:
    """``{session_id: main_jsonl}`` for sessions inside a dir that would not list.

    A project dir at mode 000 discovers NOTHING, so its sessions cannot reach
    :attr:`SourceScan.unreadable` — that map is keyed by a session the scan
    actually saw. Without this resolution every session under such a dir looks
    deleted and its corpus dir is removed, which is the same data loss the
    per-session stat guard exists to prevent, one level up.

    The watermark is the only record of which sessions lived there, so it
    supplies the ids. Position under the SOURCE ROOT names the session, never
    position under the unlistable dir: the contract puts a main transcript at
    ``<source_root>/<project>/<session>.jsonl`` and its side-files under
    ``<source_root>/<project>/<session>/``, so the second segment is the
    session id whichever level failed to list. Stripping the unlistable prefix
    instead yields the PROJECT name when the unlistable dir is the root, and
    that value reaches operators through
    :attr:`MaterializationReport.unreadable_session_ids`.
    """
    if not scan.unlistable_dirs:
        return {}
    root_prefix = f"{str(source_root).rstrip('/')}/"
    resolved: dict[str, str] = {}
    for directory in scan.unlistable_dirs:
        prefix = f"{directory.rstrip('/')}/"
        for path in watermark:
            if not path.startswith(prefix) or not path.startswith(root_prefix):
                continue
            segments = path[len(root_prefix) :].split("/")
            if len(segments) < 2:  # noqa: PLR2004 — <project>/<session>.jsonl is two
                continue
            project, session_id = segments[0], segments[1].removesuffix(".jsonl")
            resolved[session_id] = f"{root_prefix}{project}/{session_id}.jsonl"
    if resolved:
        logger.warning(
            "materialize: {} session(s) live under a directory that could not be "
            "listed this pass; keeping their artifacts and retaining their "
            "watermark entries",
            len(resolved),
        )
    return resolved


def _remove_ghost_sessions(
    layout: CorpusLayout,
    scanned_session_ids: Collection[str],
    unreadable_session_ids: Collection[str],
) -> tuple[str, ...]:
    """Delete corpus session dirs whose source vanished; return removed ids.

    The raw source is authoritative: a materialized session with no main
    JSONL in the scan is a ghost and is REMOVED outright, with no tombstone
    left behind. A session in ``unreadable_session_ids`` is NOT a ghost —
    the scan failed to stat it (EIO, ESTALE, a permission blip) rather than
    finding it deleted, and deleting a live session's artifacts over a
    transient error is unrecoverable from here. Callers must run the
    suspicious-empty-scan guard first.
    """
    sessions_dir = layout.sessions_dir
    if not sessions_dir.is_dir():
        return ()
    removed: list[str] = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        if session_id in scanned_session_ids:
            continue
        if session_id in unreadable_session_ids:
            logger.warning(
                "materialize: keeping session {} — its sources could not be read this "
                "pass, which is not evidence they are gone",
                session_id,
            )
            continue
        shutil.rmtree(session_dir)
        removed.append(session_id)
        logger.info("materialize: removed ghost session {} (source vanished)", session_id)
    return tuple(removed)


def _advance_watermark(
    previous: dict[str, int],
    scan: SourceScan,
    succeeded: tuple[SessionSource, ...],
) -> dict[str, int]:
    """The next ``watermark.json`` contents.

    ONE retention rule for every entry, whether or not the pass was
    filtered: a path still on disk keeps its entry, and a path that VANISHED
    keeps its entry too unless the session owning it succeeded this pass or
    left the scan entirely. Succeeded sessions then advance their entries;
    everything else is byte-identical to the previous pass.

    Retaining a vanished path is what makes the retry work. Staleness is
    "recorded set != scanned set"
    (:func:`atif_corpus.domain.sessions._is_stale`), so the retained entry IS
    the signal. Drop it for a session that merely FAILED — or that a
    ``--sessions`` filter never planned — and the next pass sees recorded
    equal to scanned, classifies the session ``up_to_date`` forever, and the
    corpus serves a trajectory embedding the deleted file's records with no
    retry ever scheduled.

    Two sessions sit outside the "still in the scan" test. One genuinely
    LEFT the scan: ghost removal already deleted its corpus dir, so there is
    nothing to retry and its entries drop. The other could not be READ (a
    stat failure, which is not evidence about what its sources are): its
    entries are retained wholesale, exactly as if it had failed.
    """
    current_paths = {path for session in scan.sessions for path in session.source_mtimes}
    succeeded_ids = {session.session_id for session in succeeded}
    unresolved_mains = [
        session.session_jsonl
        for session in scan.sessions
        if session.session_id not in succeeded_ids
    ]
    unresolved_mains.extend(scan.unreadable.values())

    def _retain(path: str) -> bool:
        # The membership test carries almost every entry; only a genuinely
        # vanished path reaches the per-session scope scan.
        return path in current_paths or any(owns_path(main, path) for main in unresolved_mains)

    advanced = {path: mtime for path, mtime in previous.items() if _retain(path)}
    for session in succeeded:
        advanced.update(session.source_mtimes)
    return advanced


def materialize(
    *,
    source_root: Path,
    corpus_root: Path,
    converter: ConverterPort,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    quiesce_seconds: int = 300,
    force: bool = False,
    now_ns: int | None = None,
    session_ids: Collection[str] | None = None,
) -> MaterializationReport:
    """Run one materialization pass; see the module docstring for the shape.

    Parameters
    ----------
    source_root
        Raw transcript corpus (``<config>/projects``).
    corpus_root
        Materialized corpus root (CONTRACT.md layout).
    converter
        The conversion port; atif-cli injects the real adapter, tests inject
        :class:`~atif_corpus.infrastructure.fake_converter.FakeConverter`.
    materialized_at
        ISO-8601 UTC instant to stamp into every ``meta.json`` this pass.
    harbor_version, converter_version
        Version pins for provenance; the caller owns them.
    quiesce_seconds
        Source-silence threshold; contract default 300.
    force
        Re-materialize every quiescent session regardless of the watermark.
    now_ns
        Epoch-ns "now" for the quiescence check; defaults to ``time.time_ns()``.
        Pass explicitly in tests to pin the decision.
    session_ids
        Optional session-id filter (contract-compatible extension for
        ``atif-sql materialize --sessions``): when given, only these
        sessions are PLANNED this pass. No special watermark accounting is
        needed — an unplanned session simply never succeeds, and the one
        retention rule in :func:`_advance_watermark` already keeps every
        entry of every session that did not succeed. Ghost removal keys off
        the FULL scan, so an unfiltered session is never removed just
        because it wasn't planned.

    Raises
    ------
    SuspiciousEmptyScanError
        The scan found zero sessions while the corpus holds materialized
        ones — almost always a wrong ``source_root``. Nothing is removed.
    """
    pass_started = time.perf_counter()
    effective_now_ns = time.time_ns() if now_ns is None else now_ns

    layout = CorpusLayout(corpus_root=corpus_root)
    _sweep_staging(layout)
    previous_watermark = read_watermark(layout.watermark_path)
    scan = scan_sources(source_root)
    # A dir that would not list discovered no sessions, so its ids come from
    # the watermark; folding them in makes every downstream unreadable check
    # (ghost removal, retention, the report) read one set.
    scan = scan.with_unreadable(
        _sessions_under_unlistable_dirs(scan, previous_watermark, source_root=source_root)
    )
    sessions = scan.sessions

    # Ghost GC guard: an empty scan over a non-empty corpus smells like a
    # wrong source_root — removing everything would be the silent-no-op
    # footgun inverted into a data-loss footgun. Fail loud instead, UNLESS an
    # unlistable directory already explains the emptiness: that is a diagnosed
    # permission problem, and failing the pass every ten minutes over it adds
    # nothing the warning did not already say.
    existing_session_dirs = (
        sorted(p.name for p in layout.sessions_dir.iterdir() if p.is_dir())
        if layout.sessions_dir.is_dir()
        else []
    )
    if not sessions and existing_session_dirs and not scan.unlistable_dirs:
        msg = (
            f"scan of {source_root} found 0 sessions but the corpus at "
            f"{corpus_root} holds {len(existing_session_dirs)} materialized "
            f"session(s) — refusing to remove them; check source_root"
        )
        raise SuspiciousEmptyScanError(msg)
    if scan.unlistable_dirs:
        # Ghost removal needs a complete picture of what EXISTS, and a dir it
        # could not open means it does not have one. Watermark resolution names
        # the sessions recorded there, never one materialized before the
        # watermark covered it, so removing anything this pass risks deleting a
        # session whose sources are merely behind a closed door.
        logger.warning(
            "materialize: skipping ghost removal — {} source director(ies) could "
            "not be listed, so absence is not evidence of deletion",
            len(scan.unlistable_dirs),
        )
        removed_ids: tuple[str, ...] = ()
    else:
        removed_ids = _remove_ghost_sessions(
            layout,
            {s.session_id for s in sessions},
            scan.unreadable_session_ids,
        )

    planned_sessions = sessions
    if session_ids is not None:
        wanted = set(session_ids)
        planned_sessions = tuple(s for s in sessions if s.session_id in wanted)
    plan: MaterializationPlan = build_plan(
        planned_sessions,
        watermark=previous_watermark,
        policy=QuiescencePolicy(quiesce_seconds=quiesce_seconds),
        now_ns=effective_now_ns,
        force=force,
        unmaterialized_session_ids=_unmaterialized_session_ids(
            layout, previous_watermark, planned_sessions
        ),
    )

    succeeded: list[SessionSource] = []
    failures: list[MaterializationFailure] = []
    convert_seconds = 0.0
    for session in plan.to_materialize:
        try:
            convert_seconds += _write_session(
                layout,
                session,
                converter,
                materialized_at=materialized_at,
                harbor_version=harbor_version,
                converter_version=converter_version,
            )
        except Exception as error:  # noqa: BLE001 — one bad session must not abort the sync
            logger.warning("materialize: session {} failed: {}", session.session_id, error)
            failures.append(
                MaterializationFailure(
                    session_id=session.session_id,
                    error=f"{type(error).__name__}: {error}",
                )
            )
        else:
            succeeded.append(session)

    layout.corpus_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        layout.watermark_path,
        _advance_watermark(previous_watermark, scan, tuple(succeeded)),
    )

    report = MaterializationReport(
        materialized_count=len(succeeded),
        up_to_date_count=len(plan.up_to_date),
        skipped_live_count=len(plan.skipped_live),
        failures=tuple(failures),
        total_seconds=time.perf_counter() - pass_started,
        convert_seconds=convert_seconds,
        removed_session_ids=removed_ids,
        unreadable_session_ids=tuple(sorted(scan.unreadable)),
    )
    logger.info(
        "materialize: {} written, {} current, {} live, {} failed, {} removed, "
        "{} unreadable in {:.2f}s",
        report.materialized_count,
        report.up_to_date_count,
        report.skipped_live_count,
        report.failed_count,
        report.sessions_removed,
        report.unreadable_count,
        report.total_seconds,
    )
    return report
