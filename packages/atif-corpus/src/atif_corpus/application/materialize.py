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

   With ``workers > 1`` this stage runs across a
   :class:`concurrent.futures.ProcessPoolExecutor`: each worker builds its
   own copy of the converter once (the injected instance is pickled into the
   pool initializer) and runs the SAME :func:`_write_session` the serial
   path runs, so the per-session staging dir and atomic swap stay the
   crash-safety unit, only now the staging name carries the worker's pid.
   Every worker outcome is folded back in PLAN order, so the report reads
   the same regardless of which worker finished first. ``workers == 1`` is
   the reference path and runs everything inline in this process.
   partial artifact set. An optional
   :class:`~atif_corpus.domain.ports.ArtifactProducer` runs between the
   three JSON artifacts and ``meta.json`` and may add files to the same
   temp dir (atif-cli plugs in atif-duck's typed columnar writer here), so
   its files publish in the same swap and under the same marker; the keys
   it returns land in ``meta.json`` beside the contract's own.
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
import multiprocessing
import os
import re
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

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
from atif_corpus.domain.source_layout import CLAUDE_CODE_LAYOUT, SourceLayout
from atif_corpus.infrastructure.atomic import (
    replace_dir_atomic,
    write_json_atomic,
    write_text_atomic,
)
from atif_corpus.infrastructure.scanner import scan_sources

if TYPE_CHECKING:
    from typing import Any

    from atif_corpus.domain.ports import ArtifactProducer, ConverterPort
    from atif_corpus.domain.sessions import SessionSource
    from atif_corpus.infrastructure.scanner import SourceScan


class SuspiciousEmptyScanError(RuntimeError):
    """The scan found zero sessions while the corpus holds materialized ones.

    Almost always a wrong ``source_root`` (typo, unmounted disk, env var
    pointing elsewhere) — proceeding would garbage-collect the ENTIRE
    corpus as "ghosts". Fail loud instead; an operator who really emptied
    the source tree can delete the corpus dir explicitly.
    """


class CorpusAgentMismatchError(RuntimeError):
    """The corpus at this root was materialized from a DIFFERENT agent.

    One corpus holds one agent's sessions (``docs/CONTRACT.md``), and the
    per-agent default roots keep that true without anyone thinking about it.
    An EXPLICIT root defeats them: ``--corpus-root <claude corpus> --agent
    codex``, or ``ATIF_SQL_CORPUS_ROOT`` left pointing at one corpus while the
    agent moves, aims a Codex pass at a corpus full of Claude Code sessions.
    Every one of them is then a ghost — no Codex scan will ever name them — so
    the pass would delete the lot and report it as "source vanished".

    So the corpus's own ``meta.agent`` is a DISCRIMINATOR, not just provenance:
    it is read before ghost removal and before any write, and a disagreement
    fails the pass with nothing removed.
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
    #: Seconds spent inside ``ConverterPort.convert`` calls, SUMMED over
    #: sessions. With one worker this is a slice of ``total_seconds``; with
    #: several it is the work the pool did in parallel and can exceed the wall
    #: clock, which is the point of the pool.
    convert_seconds: float
    #: Ghost session dirs removed (source vanished from the scan).
    removed_session_ids: tuple[str, ...] = ()
    #: How many processes converted this pass: 1 for the inline reference
    #: path, otherwise the pool size actually used (never more than the number
    #: of sessions planned).
    workers: int = 1
    #: Wall seconds spent inside ``ArtifactProducer.produce`` calls (0.0 when
    #: no producer was injected). Reported separately from ``convert_seconds``
    #: because the extra artifacts are a cost the operator opted into and may
    #: want to see on its own.
    artifact_seconds: float = 0.0
    #: Sessions the scan could not stat, so they appear in no other counter.
    #:
    #: A transient stat error resolves itself next pass. A PERMANENT one (a
    #: side-file left at mode 000) starves the session forever: it never
    #: materializes, is never up_to_date, is never skipped_live, and is
    #: deliberately never ghosted. Without this field the pass reports all
    #: zeroes and an operator sees a corpus that looks idle and complete.
    unreadable_session_ids: tuple[str, ...] = ()
    #: Transcript names the scan REJECTED because the session id they carry
    #: fails the boundary in :mod:`atif_corpus.domain.session_id`. The same
    #: kind of silence as ``unreadable``: such a session is never materialized,
    #: never up_to_date, never skipped_live, never failed, and never ghosted,
    #: so it has to be reported here or it vanishes without a trace.
    rejected_session_ids: tuple[str, ...] = ()

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

    @property
    def rejected_count(self) -> int:
        """Number of transcript names the scan rejected this pass."""
        return len(self.rejected_session_ids)


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


#: The keys ``meta.json`` always carries. An artifact producer may add keys
#: but never these: a producer that could overwrite ``session_id`` or
#: ``agent`` could silently relabel a session, so a collision is an error.
_META_CONTRACT_KEYS: frozenset[str] = frozenset(
    {
        "session_id",
        "source_mtime_ns",
        "source_files",
        "harbor_version",
        "converter_version",
        "materialized_at",
        "agent",
    }
)


def _produce_extra_artifacts(
    producer: ArtifactProducer | None,
    staging: Path,
    session_id: str,
    trajectory: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    """Run the optional producer in ``staging``; return (meta extras, seconds).

    The extras are checked against :data:`_META_CONTRACT_KEYS` here, before
    ``meta.json`` is assembled, so a colliding producer fails the session
    with a clear message instead of publishing a relabeled one.
    """
    if producer is None:
        return {}, 0.0
    started = time.perf_counter()
    extras = dict(producer.produce(staging, session_id=session_id, trajectory=trajectory))
    elapsed = time.perf_counter() - started
    collisions = sorted(_META_CONTRACT_KEYS & extras.keys())
    if collisions:
        msg = f"artifact producer returned meta keys reserved by the contract: {collisions}"
        raise ValueError(msg)
    return extras, elapsed


def _write_session(
    layout: CorpusLayout,
    session: SessionSource,
    converter: ConverterPort,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    agent: str,
    artifact_producer: ArtifactProducer | None = None,
) -> tuple[float, float]:
    """Convert one session and write its artifacts; returns (convert, artifact) seconds.

    All artifacts are written into a staging dir under
    ``<corpus_root>/.staging/`` (outside every reader glob), then the whole
    dir is swapped into ``sessions/<id>/`` via
    :func:`~atif_corpus.infrastructure.atomic.replace_dir_atomic` — a crash
    at any point before the swap leaves the previous generation fully intact
    and internally consistent; readers never see a torn artifact set. A kill
    INSIDE the swap window leaves the session dir missing rather than torn,
    and :func:`_unmaterialized_session_ids` replans it next pass. Within
    staging the write order stays trajectory → loss_report → edges →
    (producer's extra artifacts) → meta, ``meta.json`` last as
    belt-and-braces (atif-duck gates its readers on meta presence), and each
    artifact is fsynced before its rename so the ordering holds across power
    loss too. The producer sees the staged ``trajectory.json`` already on
    disk and the same dict in memory; whatever it writes rides the same swap.
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
        extras, artifact_elapsed = _produce_extra_artifacts(
            artifact_producer, staging, session.session_id, output.trajectory_dict
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
                # WHICH agent wrote the transcript this artifact set came from.
                # A corpus root holds one agent's sessions by construction (the
                # slug derives from the source root), so this is provenance
                # rather than a discriminator — but a corpus copied out of place
                # keeps saying what it is, and an operator reading one
                # meta.json does not have to infer the agent from a path shape.
                "agent": agent,
                **extras,
            },
        )
        layout.sessions_dir.mkdir(parents=True, exist_ok=True)
        replace_dir_atomic(staging, layout.session_dir(session.session_id))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return convert_elapsed, artifact_elapsed


@dataclass(frozen=True, slots=True)
class _SessionOutcome:
    """What one convert+write attempt produced, shaped to cross a process boundary.

    A worker never lets the converter's exception escape: the executor would
    pickle it, and an exception class whose ``__init__`` signature differs
    from its ``args`` (``TrajectoryValidationError(list)`` is one) fails to
    rebuild in the parent and surfaces as a pickling error in its place. The
    worker formats the failure line exactly as the serial path does, so the
    report carries the same text either way.
    """

    session_id: str
    convert_seconds: float
    error: str | None
    artifact_seconds: float = 0.0


def _attempt_session(
    layout: CorpusLayout,
    session: SessionSource,
    converter: ConverterPort,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    agent: str,
    artifact_producer: ArtifactProducer | None = None,
) -> _SessionOutcome:
    """Run :func:`_write_session` and fold any exception into the outcome."""
    try:
        elapsed, produced = _write_session(
            layout,
            session,
            converter,
            materialized_at=materialized_at,
            harbor_version=harbor_version,
            converter_version=converter_version,
            agent=agent,
            artifact_producer=artifact_producer,
        )
    except Exception as error:  # noqa: BLE001 — one bad session must not abort the sync
        return _SessionOutcome(
            session_id=session.session_id,
            convert_seconds=0.0,
            error=f"{type(error).__name__}: {error}",
        )
    return _SessionOutcome(
        session_id=session.session_id,
        convert_seconds=elapsed,
        error=None,
        artifact_seconds=produced,
    )


#: The converter a pool worker builds once in :func:`_worker_init` and reuses
#: for every session it is handed. Module state because the executor gives a
#: task function nothing else to reach it through.
_worker_converter: ConverterPort | None = None
#: The artifact producer handed to the same worker, or ``None`` when none was injected.
_worker_artifact_producer: ArtifactProducer | None = None


def _worker_init(
    converter: ConverterPort,
    setup: Callable[[], None] | None,
    artifact_producer: ArtifactProducer | None = None,
) -> None:
    """Pool initializer: run the caller's process setup, then adopt the converter.

    ``converter`` arrives pickled from the parent — for the real adapter that
    is a small spec (agent, flags) whose unpickling imports the converter
    stack once per worker. ``setup`` is the composition root's hook for
    per-process concerns the use case must not know about, such as
    installing the same log sink the parent runs.
    """
    global _worker_converter, _worker_artifact_producer  # noqa: PLW0603 — the executor offers no other channel
    if setup is not None:
        setup()
    _worker_converter = converter
    _worker_artifact_producer = artifact_producer


def _worker_attempt(
    layout: CorpusLayout,
    session: SessionSource,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    agent: str,
) -> _SessionOutcome:
    """Pool task: convert and write one session with this worker's converter."""
    if _worker_converter is None:
        msg = "pool worker used before its initializer ran"
        raise RuntimeError(msg)
    return _attempt_session(
        layout,
        session,
        _worker_converter,
        materialized_at=materialized_at,
        harbor_version=harbor_version,
        converter_version=converter_version,
        agent=agent,
        artifact_producer=_worker_artifact_producer,
    )


def _attempt_sessions(
    layout: CorpusLayout,
    sessions: Sequence[SessionSource],
    converter: ConverterPort,
    *,
    workers: int,
    worker_setup: Callable[[], None] | None,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    agent: str,
    artifact_producer: ArtifactProducer | None = None,
) -> tuple[list[_SessionOutcome], int]:
    """Convert and write every planned session; return outcomes in PLAN order.

    ``workers == 1`` is the reference path: every session runs inline, in
    order, in this process. Above that a spawn-context
    :class:`~concurrent.futures.ProcessPoolExecutor` of
    ``min(workers, len(sessions))`` processes does the same work, and the
    outcomes are read back in submission order so completion order can never
    reorder the report. A pass with a single planned session runs inline
    whatever ``workers`` says: a pool of one buys nothing and costs a process
    start plus a converter import.

    ``spawn`` rather than the platform default, deliberately: a forked child
    inherits whatever threads and locks the parent holds, which is the class
    of bug that shows up once a month and never under a test. Each spawned
    worker imports the converter once, and that cost is paid once per worker,
    not per session.

    The second value is the worker count actually used, for the report.
    """
    if workers < 1:
        msg = f"workers must be >= 1, got {workers}"
        raise ValueError(msg)
    provenance = {
        "materialized_at": materialized_at,
        "harbor_version": harbor_version,
        "converter_version": converter_version,
        "agent": agent,
    }
    pool_size = min(workers, len(sessions))
    if pool_size <= 1:
        return [
            _attempt_session(
                layout, session, converter, artifact_producer=artifact_producer, **provenance
            )
            for session in sessions
        ], 1
    with ProcessPoolExecutor(
        max_workers=pool_size,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
        initargs=(converter, worker_setup, artifact_producer),
    ) as pool:
        futures = [
            pool.submit(_worker_attempt, layout, session, **provenance) for session in sessions
        ]
        return [future.result() for future in futures], pool_size


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

    Pool workers change nothing here. A worker stages under ITS pid, so a
    concurrent pass's in-flight entries name pids that are alive and are kept,
    and a pass that died takes its workers with it (a spawned worker exits
    when the parent's queue closes), so its entries name pids that are gone
    and are swept — the same two answers the single-process pass gets.
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
    source_layout: SourceLayout,
) -> dict[str, str]:
    """``{session_id: main_jsonl}`` for sessions inside a dir that would not list.

    A directory at mode 000 discovers NOTHING, so its sessions cannot reach
    :attr:`SourceScan.unreadable` — that map is keyed by a session the scan
    actually saw. Without this resolution every session under such a dir looks
    deleted and its corpus dir is removed, which is the same data loss the
    per-session stat guard exists to prevent, one level up.

    The watermark is the only record of which sessions lived there, so it
    supplies the ids, and
    :meth:`~atif_corpus.domain.source_layout.SourceLayout.session_from_watermark_path`
    resolves each recorded path by POSITION under the source root — never by
    stripping the unlistable prefix, which would yield a project or a date
    component when the unlistable dir is an intermediate one. That resolved id
    reaches operators through
    :attr:`MaterializationReport.unreadable_session_ids`.
    """
    if not scan.unlistable_dirs:
        return {}
    resolved: dict[str, str] = {}
    for directory in scan.unlistable_dirs:
        prefix = f"{directory.rstrip('/')}/"
        for path in watermark:
            if not path.startswith(prefix):
                continue
            session = source_layout.session_from_watermark_path(str(source_root), path)
            if session is None:
                continue
            session_id, main_jsonl = session
            resolved[session_id] = main_jsonl
    if resolved:
        logger.warning(
            "materialize: {} session(s) live under a directory that could not be "
            "listed this pass; keeping their artifacts and retaining their "
            "watermark entries",
            len(resolved),
        )
    return resolved


#: How many existing session dirs to open looking for the corpus's agent. One
#: is normally enough; a handful covers a corpus whose first dirs are mid-swap
#: or unreadable, and the cap keeps the probe O(1) on a 3,700-session corpus.
_AGENT_PROBE_LIMIT = 8


def _corpus_agent(layout: CorpusLayout, session_dir_names: Sequence[str]) -> str | None:
    """The agent a materialized corpus says it holds, or ``None`` if it cannot say.

    Reads at most :data:`_AGENT_PROBE_LIMIT` ``meta.json`` files and returns the
    first answer. A ``meta.json`` with NO ``agent`` key still answers
    ``claude-code``: the key landed with Codex support, and this workspace could
    not read a Codex rollout before then, so a corpus written without it holds
    Claude Code sessions. Unreadable or unparseable meta files are skipped
    rather than treated as an answer — a permission blip must not be able to
    relabel a corpus.
    """
    for name in list(session_dir_names)[:_AGENT_PROBE_LIMIT]:
        try:
            meta = json.loads(layout.meta_path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        agent = meta.get("agent")
        if isinstance(agent, str) and agent:
            return agent
        return CLAUDE_CODE_LAYOUT.agent.value
    return None


def _remove_ghost_sessions(
    layout: CorpusLayout,
    scanned_session_ids: Collection[str],
    unreadable_session_ids: Collection[str],
    rejected_session_ids: Collection[str] = (),
) -> tuple[str, ...]:
    """Delete corpus session dirs whose source vanished; return removed ids.

    The raw source is authoritative: a materialized session with no main
    JSONL in the scan is a ghost and is REMOVED outright, with no tombstone
    left behind. A session in ``unreadable_session_ids`` is NOT a ghost —
    the scan failed to stat it (EIO, ESTALE, a permission blip) rather than
    finding it deleted, and deleting a live session's artifacts over a
    transient error is unrecoverable from here. A session in
    ``rejected_session_ids`` is not a ghost either: its source is present,
    the scan declined it by policy, and a corpus dir under that name (one an
    older version wrote) is left for the operator rather than removed by a
    rule change. Callers must run the suspicious-empty-scan guard first.
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
        if session_id in rejected_session_ids:
            logger.warning(
                "materialize: keeping session dir {!r} — its name fails the session id "
                "boundary, so this version neither writes nor reads it; remove it by hand",
                session_id,
            )
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
    source_layout: SourceLayout = CLAUDE_CODE_LAYOUT,
    workers: int = 1,
    worker_setup: Callable[[], None] | None = None,
    artifact_producer: ArtifactProducer | None = None,
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
    artifact_producer
        Optional :class:`~atif_corpus.domain.ports.ArtifactProducer` run per
        session inside the staged dir, between the three JSON artifacts and
        ``meta.json``; its extra files publish in the same directory swap and
        its returned keys land in ``meta.json``. ``None`` (the default) writes
        exactly the four contract artifacts.
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
    source_layout
        Which agent's transcript arrangement to discover under
        ``source_root``, and the agent name stamped into every ``meta.json``
        this pass writes. Defaults to Claude Code's shape, which is what every
        caller predating Codex support meant.
    session_ids
        Optional session-id filter (contract-compatible extension for
        ``atif-sql materialize --sessions``): when given, only these
        sessions are PLANNED this pass. No special watermark accounting is
        needed — an unplanned session simply never succeeds, and the one
        retention rule in :func:`_advance_watermark` already keeps every
        entry of every session that did not succeed. Ghost removal keys off
        the FULL scan, so an unfiltered session is never removed just
        because it wasn't planned.
    workers
        Processes for the convert+write stage. ``1`` (the default) is the
        inline reference path. Above ``1``, a spawn-context process pool of
        at most this many workers converts the planned sessions, each worker
        holding its own copy of ``converter`` (which must therefore be
        picklable) and writing through the same per-session staging dir and
        atomic swap. Artifacts are byte-identical to the inline path; the
        report's ``convert_seconds`` is the per-session sum either way.
    worker_setup
        Optional picklable callable each pool worker runs once before it
        builds its converter — the composition root's hook for per-process
        setup such as installing the log sink the parent uses. Ignored on
        the inline path.

    Raises
    ------
    SuspiciousEmptyScanError
        The scan found zero sessions while the corpus holds materialized
        ones — almost always a wrong ``source_root``. Nothing is removed.
    ValueError
        ``workers`` is below ``1``.
    """
    pass_started = time.perf_counter()
    effective_now_ns = time.time_ns() if now_ns is None else now_ns

    layout = CorpusLayout(corpus_root=corpus_root)
    _sweep_staging(layout)
    previous_watermark = read_watermark(layout.watermark_path)
    scan = scan_sources(source_root, source_layout)
    # A dir that would not list discovered no sessions, so its ids come from
    # the watermark; folding them in makes every downstream unreadable check
    # (ghost removal, retention, the report) read one set.
    scan = scan.with_unreadable(
        _sessions_under_unlistable_dirs(
            scan,
            previous_watermark,
            source_root=source_root,
            source_layout=source_layout,
        )
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
    # Agent check FIRST: it is the one condition under which every existing
    # session dir is a ghost by construction, so it has to run before the
    # emptiness tripwire and before the first write.
    corpus_agent = _corpus_agent(layout, existing_session_dirs)
    if corpus_agent is not None and corpus_agent != source_layout.agent.value:
        msg = (
            f"the corpus at {corpus_root} holds {corpus_agent} sessions but this "
            f"pass is materializing {source_layout.agent.value} from {source_root} "
            f"— refusing: one corpus holds one agent, and continuing would delete "
            f"all {len(existing_session_dirs)} of them as ghosts"
        )
        raise CorpusAgentMismatchError(msg)
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
            scan.rejected_session_ids,
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

    outcomes, workers_used = _attempt_sessions(
        layout,
        plan.to_materialize,
        converter,
        workers=workers,
        worker_setup=worker_setup,
        materialized_at=materialized_at,
        harbor_version=harbor_version,
        converter_version=converter_version,
        agent=source_layout.agent.value,
        artifact_producer=artifact_producer,
    )
    succeeded: list[SessionSource] = []
    failures: list[MaterializationFailure] = []
    convert_seconds = 0.0
    artifact_seconds = 0.0
    for session, outcome in zip(plan.to_materialize, outcomes, strict=True):
        if outcome.error is not None:
            logger.warning("materialize: session {} failed: {}", session.session_id, outcome.error)
            failures.append(
                MaterializationFailure(session_id=session.session_id, error=outcome.error)
            )
        else:
            convert_seconds += outcome.convert_seconds
            artifact_seconds += outcome.artifact_seconds
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
        artifact_seconds=artifact_seconds,
        removed_session_ids=removed_ids,
        unreadable_session_ids=tuple(sorted(scan.unreadable)),
        rejected_session_ids=scan.rejected_session_ids,
        workers=workers_used,
    )
    logger.info(
        "materialize: {} written, {} current, {} live, {} failed, {} removed, "
        "{} unreadable, {} rejected in {:.2f}s",
        report.materialized_count,
        report.up_to_date_count,
        report.skipped_live_count,
        report.failed_count,
        report.sessions_removed,
        report.unreadable_count,
        report.rejected_count,
        report.total_seconds,
    )
    return report
