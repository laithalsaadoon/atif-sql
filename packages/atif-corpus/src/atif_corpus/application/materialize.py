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
4. Retain source-removed sessions: a corpus session dir whose main JSONL
   vanished from the source scan is KEPT, forever. Claude Code and Codex
   expire old transcripts, and the corpus is the only copy of those sessions
   left, so deleting it would be deleting history. Its ``meta.json`` is
   rewritten once with ``source_present: false`` and ``source_removed_at``
   (this pass's ``materialized_at``); a session marked this pass is reported
   in ``removed_session_ids``, and ``retained_count`` counts every session
   kept without a source. If the source comes back, the session is stale
   again (its watermark entries were dropped) and the next quiescent pass
   re-converts it from the live file, which clears the mark. Three guards
   keep the mark honest, the same three that used to guard deletion: a
   session the scan could not STAT is never marked, only a genuine
   ``FileNotFoundError`` counts, and such a session is reported in
   ``unreadable_session_ids``; a pass that could not list a source directory
   marks nothing; and when the scan found ZERO sessions while the corpus holds
   some, that smells like a wrong ``source_root`` and the pass fails loud
   (:class:`SuspiciousEmptyScanError`) instead of flagging the whole corpus.
5. Advance ``watermark.json``: only sessions that materialized successfully
   (or converted to nothing, see below) move their entries forward, so a
   failed session stays stale and is retried next pass instead of being
   silently forgotten.

Three more rules decide what a pass converts:

* Raw source archive. Every live conversion is handed ``<staging>/source/``
  and writes a zstd copy of every source file there from the bytes it parsed
  (the converter's verifying re-read, so no extra open of a transcript).
  ``meta.json`` lists it under ``source_archive``, and it publishes in the same
  directory swap as the artifacts it produced.
* Generation staleness. A session whose ``meta.json`` records a different
  ``converter_schema`` (the converter-owned output version; the caller passes
  it) or ``columnar_schema`` (when the caller expects one) than this pass would
  stamp is stale even though no source byte moved. ``converter_version`` is the
  release version and is provenance only, so a release that changes no output
  re-converts nothing. A caller that passes no ``converter_schema`` gets
  ``converter_version`` compared instead. A source-removed session that is
  stale this way, and has an archive, is re-converted from its restored
  archive; the archive itself carries over.
* Empty sessions. A converter that raises
  :class:`~atif_corpus.domain.ports.EmptySourceError` has found a well-formed
  transcript with nothing to convert. That is recorded in
  ``empty_sessions.json`` with the generation it was checked under, its
  watermark entries advance, and it is reported under ``empty_session_ids``
  rather than ``failures``. A later write to the transcript (the watermark
  moves) or a new converter schema (the generation moves) tries it again.

A failing session is recorded in the report and skipped — one broken
transcript must never abort a corpus sync.

Clock discipline: ``materialized_at`` / ``harbor_version`` /
``converter_version`` are passed IN by the caller (atif-cli owns the wall
clock and the version pins); the only clocks read here are ``time.time_ns``
for the quiescence "now" when the caller does not supply one, and
``time.perf_counter`` for durations. The domain reads no clock at all. "Now"
is read AFTER the scan: read before it, a transcript written while a long
scan ran carries an mtime later than "now" and trips the future-mtime clock
warning for a host whose clock is fine.
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
from typing import TYPE_CHECKING, Any, TypedDict

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

from atif_corpus.domain.generation import generation_matches
from atif_corpus.domain.layout import (
    EDGES_FILENAME,
    LOSS_REPORT_FILENAME,
    META_FILENAME,
    SESSION_EVENTS_FILENAME,
    SOURCE_ARCHIVE_DIRNAME,
    TRAJECTORY_FILENAME,
    CorpusLayout,
)
from atif_corpus.domain.ports import EmptySourceError
from atif_corpus.domain.sessions import (
    MaterializationPlan,
    QuiescencePolicy,
    build_plan,
    owns_path,
)
from atif_corpus.domain.source_layout import CLAUDE_CODE_LAYOUT, SourceLayout
from atif_corpus.infrastructure.atomic import (
    replace_dir_atomic,
    write_blob_atomic,
    write_json_atomic,
    write_text_atomic,
)
from atif_corpus.infrastructure.scanner import scan_sources
from atif_corpus.infrastructure.source_archive import (
    META_SOURCE_ARCHIVE_KEY,
    archive_manifest,
    restore_session_sources,
    verify_archive_on_disk,
)

if TYPE_CHECKING:
    from atif_corpus.domain.ports import ArtifactProducer, BlobOutput, ConverterPort
    from atif_corpus.domain.sessions import SessionSource
    from atif_corpus.infrastructure.scanner import SourceScan


class SuspiciousEmptyScanError(RuntimeError):
    """The scan found zero sessions while the corpus holds materialized ones.

    Almost always a wrong ``source_root`` (typo, unmounted disk, env var
    pointing elsewhere) — proceeding would mark the ENTIRE corpus as
    source-removed. Nothing is deleted any more, but a mass flip of every
    session's ``source_present`` is still the wrong answer to a typo, so the
    pass fails loud instead; an operator who really emptied the source tree
    can point ``source_root`` at an empty directory on purpose and delete the
    corpus by hand if that is what they want.
    """


class CorpusAgentMismatchError(RuntimeError):
    """The corpus at this root was materialized from a DIFFERENT agent.

    One corpus holds one agent's sessions (``docs/CONTRACT.md``), and the
    per-agent default roots keep that true without anyone thinking about it.
    An EXPLICIT root defeats them: ``--corpus-root <claude corpus> --agent
    codex``, or ``ATIF_SQL_CORPUS_ROOT`` left pointing at one corpus while the
    agent moves, aims a Codex pass at a corpus full of Claude Code sessions.
    No Codex scan will ever name any of them, so the pass would mark every one
    source-removed and then start writing Codex sessions beside them.

    So the corpus's own ``meta.agent`` is a DISCRIMINATOR, not just provenance:
    it is read before any session is marked and before any write, and a
    disagreement fails the pass with nothing touched.
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

    #: Sessions whose artifacts were (re)written this pass, from a live source
    #: or from a restored archive (the latter also in ``archive_session_ids``).
    materialized_count: int
    #: Sessions already current with their sources and generation; untouched.
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
    #: Sessions whose source vanished THIS pass. Their artifacts are KEPT and
    #: their ``meta.json`` now says ``source_present: false``; nothing is
    #: deleted. A session marked on an earlier pass is not repeated here; it is
    #: counted in ``retained_count``.
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
    #: deliberately never marked source-removed. Without this field the pass
    #: reports all zeroes and an operator sees a corpus that looks idle and
    #: complete.
    unreadable_session_ids: tuple[str, ...] = ()
    #: Transcript names the scan REJECTED because the session id they carry
    #: fails the boundary in :mod:`atif_corpus.domain.session_id`. The same
    #: kind of silence as ``unreadable``: such a session is never materialized,
    #: never up_to_date, never skipped_live, never failed, and never marked,
    #: so it has to be reported here or it vanishes without a trace.
    rejected_session_ids: tuple[str, ...] = ()
    #: Sessions the converter found EMPTY this pass (no user or assistant
    #: record). Not failures: they are recorded in ``empty_sessions.json`` and
    #: their watermark advances, so they are tried again only once their
    #: source moves or the converter version changes.
    empty_session_ids: tuple[str, ...] = ()
    #: Every session the corpus keeps without a source, after this pass
    #: (``removed_session_ids`` included).
    retained_count: int = 0
    #: Source-removed sessions re-converted from their raw source archive this
    #: pass because their recorded generation was stale (or ``force``).
    archive_session_ids: tuple[str, ...] = ()

    @property
    def failed_count(self) -> int:
        """Number of sessions that failed this pass."""
        return len(self.failures)

    @property
    def sessions_removed(self) -> int:
        """Number of sessions whose source vanished this pass (artifacts kept)."""
        return len(self.removed_session_ids)

    @property
    def unreadable_count(self) -> int:
        """Number of sessions the scan could not read this pass."""
        return len(self.unreadable_session_ids)

    @property
    def rejected_count(self) -> int:
        """Number of transcript names the scan rejected this pass."""
        return len(self.rejected_session_ids)

    @property
    def empty_count(self) -> int:
        """Number of sessions the converter found empty this pass."""
        return len(self.empty_session_ids)

    @property
    def archive_count(self) -> int:
        """Number of sessions re-converted from their source archive this pass."""
        return len(self.archive_session_ids)


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


def read_empty_sessions(path: Path) -> dict[str, dict[str, Any]]:
    """Load ``empty_sessions.json`` (``{session_id: generation}``); empty when absent.

    A corrupt file degrades to empty, like the watermark: the cost is one more
    conversion attempt per empty session, never a lost session.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("empty-session record {} unreadable; re-checking every empty session", path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("empty-session record {} has wrong shape; re-checking them", path)
        return {}
    return {
        str(session_id): dict(generation)
        for session_id, generation in raw.items()
        if isinstance(generation, dict)
    }


#: The keys ``meta.json`` always carries (or may carry, for the last three),
#: owned by this use case. An artifact producer may add keys but never these:
#: a producer that could overwrite ``session_id`` or ``agent`` could silently
#: relabel a session, so a collision is an error.
_META_CONTRACT_KEYS: frozenset[str] = frozenset(
    {
        "session_id",
        "source_mtime_ns",
        "source_files",
        "harbor_version",
        "converter_version",
        "converter_schema",
        "materialized_at",
        "agent",
        "source_present",
        "source_removed_at",
        META_SOURCE_ARCHIVE_KEY,
    }
)

#: The generation key this use case stamps when the caller names the converter's
#: output version, and the one it falls back to comparing when it doesn't.
_CONVERTER_SCHEMA_KEY = "converter_schema"
_CONVERTER_VERSION_KEY = "converter_version"


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


@dataclass(frozen=True, slots=True)
class _Job:
    """One session to convert this pass, shaped to cross a process boundary.

    A LIVE job converts ``session_jsonl`` from the source tree. An ARCHIVE job
    (``session_jsonl is None``) restores the session's raw source archive into
    staging and converts that; it exists for a source-removed session whose
    generation is stale. ``source_meta`` holds the ``meta.json`` keys that
    describe the source (mtime, files, presence), copied verbatim into the new
    ``meta.json`` so an archive re-conversion keeps saying where the session
    originally came from. ``source_dir`` is the main transcript's parent,
    relative to the source root, recorded in the archive manifest.
    """

    session_id: str
    source_meta: dict[str, Any]
    source_dir: str
    session_jsonl: str | None = None
    #: The previous archive manifest, carried forward by an archive job. Plain
    #: dicts rather than read-only mappings because a job is pickled into the
    #: pool, and ``MappingProxyType`` does not pickle.
    previous_archive: dict[str, Any] | None = None


def _live_job(session: SessionSource, source_root: Path) -> _Job:
    main = Path(session.session_jsonl)
    try:
        source_dir = main.parent.relative_to(source_root).as_posix()
    except ValueError:
        # Only reachable with a scanner that yields paths outside its root;
        # fall back to the one component the converters read.
        source_dir = main.parent.name or "."
    return _Job(
        session_id=session.session_id,
        session_jsonl=session.session_jsonl,
        source_dir=source_dir,
        source_meta={
            "source_mtime_ns": session.newest_mtime_ns,
            "source_files": list(session.source_files),
            "source_present": True,
        },
    )


def _link_or_copy_tree(src: Path, dst: Path) -> None:
    """Copy an immutable archive tree, hard-linking where the filesystem allows.

    The staging dir and the live session dir share a filesystem by
    construction, so a link costs no bytes. The previous generation still owns
    its links until the swap, so a failed pass leaves it whole.
    """

    def _link(source: str, destination: str) -> None:
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    shutil.copytree(src, dst, copy_function=_link)


def _store_blobs(layout: CorpusLayout, blobs: Sequence[BlobOutput]) -> int:
    """Write a session's attachments into the shared blob store; return how many were new.

    WHY A SHARED STORE, NOT THE SESSION DIR. A blob is named by its content
    hash, so writing one is idempotent and order-free: two sessions (or two
    pool workers) holding the same screenshot store it once, and nothing
    about a blob ever needs the per-session swap's all-or-nothing guarantee.
    Putting blobs in the session dir would store every shared image once per
    session and copy it again on every re-conversion.

    WHY BEFORE THE SWAP. Every blob a session references is on disk before
    the session that references it publishes, so a reader can never hold a
    placeholder whose bytes are missing. A pass that dies between the two
    leaves only unreferenced blobs, which cost space and nothing else.
    """
    written = 0
    for blob in blobs:
        if write_blob_atomic(layout.blob_path(blob.sha256, blob.extension), blob.data):
            written += 1
    if blobs:
        logger.debug("materialize: {} blob(s) referenced, {} newly stored", len(blobs), written)
    return written


def _write_session(
    layout: CorpusLayout,
    job: _Job,
    converter: ConverterPort,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    converter_schema: int | None = None,
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
    staging the write order stays source archive (written by the converter
    while it verifies its input) → trajectory → loss_report → edges →
    session_events → (producer's extra artifacts) → meta, ``meta.json`` last as
    belt-and-braces (atif-duck gates its readers on meta presence), and each
    artifact is fsynced before its rename so the ordering holds across power
    loss too. The producer sees the staged ``trajectory.json`` already on
    disk and the same dict in memory; whatever it writes rides the same swap.
    The session's attachments go to the shared blob store (outside the
    staging dir, see :func:`_store_blobs`) before any of that, so every blob
    a published session references is already on disk.

    An archive job restores the previous archive into
    ``.staging/<id>.src-<pid>/`` (swept like any other staging residue if the
    pass dies), converts the restored main transcript, and carries the
    previous archive forward by hard link rather than trusting a re-archive
    of its own restore.
    """
    pid = os.getpid()
    staging = layout.staging_dir / f"{job.session_id}.tmp-{pid}"
    restore_root = layout.staging_dir / f"{job.session_id}.src-{pid}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    archive_dir = staging / SOURCE_ARCHIVE_DIRNAME
    try:
        archive_meta: dict[str, Any]
        if job.session_jsonl is None:
            shutil.rmtree(restore_root, ignore_errors=True)
            session_jsonl = restore_session_sources(
                layout.session_dir(job.session_id), restore_root
            )
            _link_or_copy_tree(layout.source_archive_dir(job.session_id), archive_dir)
            convert_started = time.perf_counter()
            output = converter.convert(session_jsonl)
            convert_elapsed = time.perf_counter() - convert_started
            archive_meta = {META_SOURCE_ARCHIVE_KEY: dict(job.previous_archive or {})}
        else:
            session_jsonl = Path(job.session_jsonl)
            archive_dir.mkdir()
            convert_started = time.perf_counter()
            output = converter.convert(session_jsonl, archive_dir=archive_dir)
            convert_elapsed = time.perf_counter() - convert_started
            if output.source_archive:
                verify_archive_on_disk(archive_dir, output.source_archive)
                archive_meta = {
                    META_SOURCE_ARCHIVE_KEY: archive_manifest(
                        output.source_archive, source_dir=job.source_dir, main=session_jsonl.name
                    )
                }
            else:
                # An adapter that archives nothing leaves no half-promise on disk.
                shutil.rmtree(archive_dir, ignore_errors=True)
                archive_meta = {}
        _store_blobs(layout, output.blobs)
        write_json_atomic(staging / TRAJECTORY_FILENAME, output.trajectory_dict, compact=True)
        write_json_atomic(staging / LOSS_REPORT_FILENAME, output.loss_report_dict)
        write_text_atomic(
            staging / EDGES_FILENAME,
            "".join(f"{line}\n" for line in output.edges_lines),
        )
        write_text_atomic(
            staging / SESSION_EVENTS_FILENAME,
            "".join(f"{line}\n" for line in output.events_lines),
        )
        extras, artifact_elapsed = _produce_extra_artifacts(
            artifact_producer, staging, job.session_id, output.trajectory_dict
        )
        write_json_atomic(
            staging / META_FILENAME,
            {
                "session_id": job.session_id,
                "source_mtime_ns": job.source_meta.get("source_mtime_ns"),
                "source_files": job.source_meta.get("source_files", []),
                "harbor_version": harbor_version,
                "converter_version": converter_version,
                **({} if converter_schema is None else {_CONVERTER_SCHEMA_KEY: converter_schema}),
                "materialized_at": materialized_at,
                # WHICH agent wrote the transcript this artifact set came from.
                # A corpus root holds one agent's sessions by construction (the
                # slug derives from the source root), so this is provenance
                # rather than a discriminator — but a corpus copied out of place
                # keeps saying what it is, and an operator reading one
                # meta.json does not have to infer the agent from a path shape.
                "agent": agent,
                **{
                    key: job.source_meta[key]
                    for key in ("source_present", "source_removed_at")
                    if key in job.source_meta
                },
                **archive_meta,
                **extras,
            },
        )
        layout.sessions_dir.mkdir(parents=True, exist_ok=True)
        replace_dir_atomic(staging, layout.session_dir(job.session_id))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)
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
    #: The converter raised :class:`EmptySourceError`: nothing to convert.
    empty: bool = False


def _attempt_session(
    layout: CorpusLayout,
    job: _Job,
    converter: ConverterPort,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    converter_schema: int | None = None,
    agent: str,
    artifact_producer: ArtifactProducer | None = None,
) -> _SessionOutcome:
    """Run :func:`_write_session` and fold any exception into the outcome."""
    try:
        elapsed, produced = _write_session(
            layout,
            job,
            converter,
            materialized_at=materialized_at,
            harbor_version=harbor_version,
            converter_version=converter_version,
            converter_schema=converter_schema,
            agent=agent,
            artifact_producer=artifact_producer,
        )
    except EmptySourceError:
        return _SessionOutcome(
            session_id=job.session_id, convert_seconds=0.0, error=None, empty=True
        )
    except Exception as error:  # noqa: BLE001 — one bad session must not abort the sync
        return _SessionOutcome(
            session_id=job.session_id,
            convert_seconds=0.0,
            error=f"{type(error).__name__}: {error}",
        )
    return _SessionOutcome(
        session_id=job.session_id,
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
    job: _Job,
    *,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    converter_schema: int | None = None,
    agent: str,
) -> _SessionOutcome:
    """Pool task: convert and write one session with this worker's converter."""
    if _worker_converter is None:
        msg = "pool worker used before its initializer ran"
        raise RuntimeError(msg)
    return _attempt_session(
        layout,
        job,
        _worker_converter,
        materialized_at=materialized_at,
        harbor_version=harbor_version,
        converter_version=converter_version,
        converter_schema=converter_schema,
        agent=agent,
        artifact_producer=_worker_artifact_producer,
    )


class _Provenance(TypedDict):
    """What every session of one pass is stamped with, typed per key for ``**`` forwarding."""

    materialized_at: str
    harbor_version: str
    converter_version: str
    converter_schema: int | None
    agent: str


def _attempt_sessions(
    layout: CorpusLayout,
    jobs: Sequence[_Job],
    converter: ConverterPort,
    *,
    workers: int,
    worker_setup: Callable[[], None] | None,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    converter_schema: int | None = None,
    agent: str,
    artifact_producer: ArtifactProducer | None = None,
) -> tuple[list[_SessionOutcome], int]:
    """Convert and write every planned session; return outcomes in PLAN order.

    ``workers == 1`` is the reference path: every session runs inline, in
    order, in this process. Above that a spawn-context
    :class:`~concurrent.futures.ProcessPoolExecutor` of
    ``min(workers, len(jobs))`` processes does the same work, and the
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
    provenance: _Provenance = {
        "materialized_at": materialized_at,
        "harbor_version": harbor_version,
        "converter_version": converter_version,
        "converter_schema": converter_schema,
        "agent": agent,
    }
    pool_size = min(workers, len(jobs))
    if pool_size <= 1:
        return [
            _attempt_session(
                layout, job, converter, artifact_producer=artifact_producer, **provenance
            )
            for job in jobs
        ], 1
    with ProcessPoolExecutor(
        max_workers=pool_size,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
        initargs=(converter, worker_setup, artifact_producer),
    ) as pool:
        futures = [pool.submit(_worker_attempt, layout, job, **provenance) for job in jobs]
        return [future.result() for future in futures], pool_size


#: Every ``.tmp-<pid>`` / ``.old-<pid>`` / ``.src-<pid>`` marker a staging entry name carries.
_STAGING_PID_RE = re.compile(r"\.(?:tmp|old|src)-(\d+)")


def _staging_owner_pids(name: str) -> frozenset[int]:
    """Pids named by a staging entry (``<sid>.tmp-<pid>[.old-<pid>]``, ``<sid>.src-<pid>``)."""
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

    ``.staging/`` is exclusively scratch for the in-flight directory swap and
    for archive restores — nothing durable lives there and no reader globs it
    — so an entry whose ``tmp-<pid>``/``old-<pid>``/``src-<pid>`` owner is GONE
    is debris from a process that died mid-swap. The per-session cleanup in
    :func:`_write_session` only matches the current pid, so without this sweep
    ``*.tmp-<oldpid>`` and ``*.old-<pid>`` grow without bound on the corpus
    filesystem.

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
    known_empty: Collection[str] = (),
) -> frozenset[str]:
    """Scanned sessions the watermark calls current but whose artifact dir is gone.

    Reachable one way: a pass was killed inside
    :func:`~atif_corpus.infrastructure.atomic.replace_dir_atomic`'s swap
    window, after the previous generation was renamed aside and before the
    new one landed. The watermark records SOURCE mtimes only, so on a
    ``--force`` pass over a non-stale session it still matches and every
    later pass reports ``up_to_date`` while the session dir stays missing.
    Force-replanning these is the recovery.

    A session in ``known_empty`` has a current watermark and no dir BY DESIGN
    (it converted to nothing), so it is not one of these; without the
    exclusion every empty session would be retried every pass, which is the
    loop the empty record exists to stop.

    Operand order is the whole cost of this function. The dir check is one
    ``stat``; the watermark check is a linear scan of every watermark entry,
    so testing it first makes the pass O(sessions x watermark_entries) rather
    than O(sessions), every pass, to flag nothing. The missing dir is the rare
    condition, so it gates the scan.
    """
    empty = set(known_empty)
    return frozenset(
        session.session_id
        for session in sessions
        if session.session_id not in empty
        and not layout.session_dir(session.session_id).is_dir()
        and any(owns_path(session.session_jsonl, path) for path in watermark)
    )


def _read_meta(layout: CorpusLayout, session_id: str) -> dict[str, Any] | None:
    """One session's ``meta.json`` as a dict, or ``None`` if it cannot be read as one."""
    try:
        meta = json.loads(layout.meta_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _generation_stale_session_ids(
    layout: CorpusLayout,
    sessions: Collection[SessionSource],
    expected: Mapping[str, object],
    known_empty: Mapping[str, Mapping[str, object]],
) -> frozenset[str]:
    """Scanned sessions whose recorded generation differs from ``expected``.

    One ``meta.json`` read per scanned session with an artifact dir. A meta
    that cannot be read is stale too: re-converting is what repairs it. A
    known-empty session compares its empty record instead, so a converter
    upgrade gets a chance at the transcripts the old one found empty. A
    session with neither is the watermark's business, not this function's.
    """
    stale: set[str] = set()
    for session in sessions:
        session_id = session.session_id
        if session_id in known_empty:
            if not generation_matches(known_empty[session_id], expected):
                stale.add(session_id)
            continue
        if not layout.session_dir(session_id).is_dir():
            continue
        meta = _read_meta(layout, session_id)
        if meta is None or not generation_matches(meta, expected):
            stale.add(session_id)
    return frozenset(stale)


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
    deleted and is marked source-removed, which is the same false verdict the
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


def _sourceless_session_ids(
    layout: CorpusLayout,
    scanned_session_ids: Collection[str],
    unreadable_session_ids: Collection[str],
    rejected_session_ids: Collection[str] = (),
) -> tuple[str, ...]:
    """Corpus session dirs with no source in this scan, excluding the two false alarms.

    A session in ``unreadable_session_ids`` is NOT sourceless — the scan
    failed to stat it (EIO, ESTALE, a permission blip) rather than finding it
    deleted. A session in ``rejected_session_ids`` is not either: its source
    is present, the scan declined it by policy, and a corpus dir under that
    name (one an older version wrote) is left for the operator. Callers must
    run the suspicious-empty-scan guard first.
    """
    sessions_dir = layout.sessions_dir
    if not sessions_dir.is_dir():
        return ()
    sourceless: list[str] = []
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
        sourceless.append(session_id)
    return tuple(sourceless)


def _retain_sourceless_sessions(
    layout: CorpusLayout,
    sourceless_ids: Sequence[str],
    *,
    removed_at: str,
    mark: bool,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """Keep every sourceless session; mark the ones not yet marked. Nothing is deleted.

    Returns ``(newly_marked_ids, retained)`` where ``retained`` maps every
    session now recorded ``source_present: false`` to its ``meta.json``. The
    mark is a ``meta.json`` rewrite through the same atomic replace every
    artifact uses, so a reader sees the old meta or the new one and the
    session's other artifacts never move. With ``mark=False`` (a pass that
    could not list part of the source tree, so absence proves nothing) no meta
    is rewritten and only sessions marked on an earlier pass are returned.

    A session whose ``meta.json`` cannot be read is left exactly as it is: no
    mark, and it is not reported as retained, because its provenance cannot be
    trusted enough to rewrite.
    """
    newly_marked: list[str] = []
    retained: dict[str, dict[str, Any]] = {}
    for session_id in sourceless_ids:
        meta = _read_meta(layout, session_id)
        if meta is None:
            logger.warning(
                "materialize: session {} has no source and an unreadable meta.json; "
                "leaving it untouched",
                session_id,
            )
            continue
        if meta.get("source_present") is not False:
            if not mark:
                continue
            meta = {**meta, "source_present": False, "source_removed_at": removed_at}
            write_json_atomic(layout.meta_path(session_id), meta)
            newly_marked.append(session_id)
            logger.info(
                "materialize: source of session {} vanished; artifacts kept, marked "
                "source_present=false",
                session_id,
            )
        retained[session_id] = meta
    return tuple(newly_marked), retained


def _archive_jobs(
    retained: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, object],
    *,
    force: bool,
    wanted: Collection[str] | None,
) -> tuple[_Job, ...]:
    """Archive re-conversions for retained sessions whose generation is stale.

    Only a session that HAS an archive can be re-converted; one retained
    before archives existed keeps its old artifacts, which is still better
    than the deletion it used to get. ``force`` re-converts every archived
    retained session, the same meaning it has for live ones.
    """
    jobs: list[_Job] = []
    for session_id in sorted(retained):
        if wanted is not None and session_id not in wanted:
            continue
        meta = retained[session_id]
        manifest = meta.get(META_SOURCE_ARCHIVE_KEY)
        if not isinstance(manifest, dict):
            continue
        if not force and generation_matches(meta, expected):
            continue
        source_meta = {
            key: meta[key]
            for key in ("source_mtime_ns", "source_files", "source_present", "source_removed_at")
            if key in meta
        }
        jobs.append(
            _Job(
                session_id=session_id,
                source_meta=source_meta,
                source_dir=str(manifest.get("source_dir", ".")),
                previous_archive=dict(manifest),
            )
        )
    return tuple(jobs)


def _advance_watermark(
    previous: dict[str, int],
    scan: SourceScan,
    succeeded: tuple[SessionSource, ...],
) -> dict[str, int]:
    """The next ``watermark.json`` contents.

    ONE retention rule for every entry, whether or not the pass was
    filtered: a path still on disk keeps its entry, and a path that VANISHED
    keeps its entry too unless the session owning it succeeded this pass or
    left the scan entirely. Succeeded sessions (empty ones included) then
    advance their entries; everything else is byte-identical to the previous
    pass.

    Retaining a vanished path is what makes the retry work. Staleness is
    "recorded set != scanned set"
    (:func:`atif_corpus.domain.sessions._is_stale`), so the retained entry IS
    the signal. Drop it for a session that merely FAILED — or that a
    ``--sessions`` filter never planned — and the next pass sees recorded
    equal to scanned, classifies the session ``up_to_date`` forever, and the
    corpus serves a trajectory embedding the deleted file's records with no
    retry ever scheduled.

    Two sessions sit outside the "still in the scan" test. One genuinely
    LEFT the scan: its artifacts are retained and marked source-removed, and
    its entries drop, so that if the source ever comes back the session reads
    as stale and is converted again from the live file. The other could not
    be READ (a stat failure, which is not evidence about what its sources
    are): its entries are retained wholesale, exactly as if it had failed.
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


def _next_empty_record(
    previous: Mapping[str, Mapping[str, object]],
    *,
    keep_ids: Collection[str],
    attempted_ids: Collection[str],
    newly_empty: Collection[str],
    expected: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """The next ``empty_sessions.json``: drop what moved on, add what came back empty.

    A recorded session keeps its entry while it is still around
    (``keep_ids``: scanned or merely unreadable) and was not attempted this
    pass. An attempt replaces the entry: empty again records the current
    generation, anything else (success or a real failure) removes it.
    """
    attempted = set(attempted_ids)
    keep = set(keep_ids)
    record = {
        session_id: dict(generation)
        for session_id, generation in previous.items()
        if session_id in keep and session_id not in attempted
    }
    for session_id in newly_empty:
        record[session_id] = dict(expected)
    return dict(sorted(record.items()))


def expected_generation(
    converter_version: str,
    expected_meta: Mapping[str, object] | None = None,
    *,
    converter_schema: int | None = None,
) -> dict[str, object]:
    """The ``meta.json`` values a current artifact set must carry this pass.

    ``converter_schema`` when the caller names one (atif-cli passes the
    converter's ``CONVERTER_SCHEMA_VERSION``), otherwise ``converter_version``;
    plus whatever the caller adds in ``expected_meta`` (atif-cli adds
    ``columnar_schema`` when the columnar producer runs). ``expected_meta`` may
    not contradict the stamped key: the value stamped and the value compared
    must be the same one.
    """
    extra = dict(expected_meta or {})
    key, value = (
        (_CONVERTER_VERSION_KEY, converter_version)
        if converter_schema is None
        else (_CONVERTER_SCHEMA_KEY, converter_schema)
    )
    if key in extra and extra[key] != value:
        msg = f"expected_meta may not carry a {key} different from the stamped one"
        raise ValueError(msg)
    return {**extra, key: value}


@dataclass(frozen=True, slots=True)
class PassPreview:
    """What a materialize pass would do right now, computed without writing anything.

    ``atif-sql status`` reports this, so its staleness numbers are the ones the
    next pass would act on, generation staleness and archive re-conversions
    included.
    """

    plan: MaterializationPlan
    #: Sessions kept without a source (marked, or about to be marked).
    retained_session_ids: tuple[str, ...] = ()
    #: Of those, the ones the next pass would re-convert from their archive.
    archive_session_ids: tuple[str, ...] = ()
    #: Sessions recorded as empty.
    empty_session_ids: tuple[str, ...] = ()
    #: Sessions whose recorded generation differs from the expected one.
    generation_stale_session_ids: tuple[str, ...] = ()


def preview_pass(
    *,
    source_root: Path,
    corpus_root: Path,
    converter_version: str,
    expected_meta: Mapping[str, object] | None = None,
    converter_schema: int | None = None,
    quiesce_seconds: int = 300,
    source_layout: SourceLayout = CLAUDE_CODE_LAYOUT,
    now_ns: int | None = None,
) -> PassPreview:
    """Replay a pass's planning decisions read-only, for ``status``.

    Same scan, same watermark, same empty record and same generation check as
    :func:`materialize`, with "now" read after the scan the same way. It marks
    nothing and converts nothing; a session the next pass would newly mark
    already counts as retained here.
    """
    layout = CorpusLayout(corpus_root=corpus_root)
    watermark = read_watermark(layout.watermark_path)
    empties = read_empty_sessions(layout.empty_sessions_path)
    scan = scan_sources(source_root, source_layout)
    scan = scan.with_unreadable(
        _sessions_under_unlistable_dirs(
            scan, watermark, source_root=source_root, source_layout=source_layout
        )
    )
    effective_now_ns = time.time_ns() if now_ns is None else now_ns
    expected = expected_generation(
        converter_version, expected_meta, converter_schema=converter_schema
    )
    stale_generation = _generation_stale_session_ids(layout, scan.sessions, expected, empties)
    plan = build_plan(
        scan.sessions,
        watermark=watermark,
        policy=QuiescencePolicy(quiesce_seconds=quiesce_seconds),
        now_ns=effective_now_ns,
        unmaterialized_session_ids=_unmaterialized_session_ids(
            layout, watermark, scan.sessions, empties
        ),
        generation_stale_session_ids=stale_generation,
    )
    sourceless = _sourceless_session_ids(
        layout,
        {s.session_id for s in scan.sessions},
        scan.unreadable_session_ids,
        scan.rejected_session_ids,
    )
    retained: dict[str, dict[str, Any]] = {}
    for session_id in sourceless:
        meta = _read_meta(layout, session_id)
        if meta is not None and (not scan.unlistable_dirs or meta.get("source_present") is False):
            retained[session_id] = meta
    archive = _archive_jobs(retained, expected, force=False, wanted=None)
    scanned_ids = {s.session_id for s in scan.sessions} | scan.unreadable_session_ids
    return PassPreview(
        plan=plan,
        retained_session_ids=tuple(sorted(retained)),
        archive_session_ids=tuple(job.session_id for job in archive),
        empty_session_ids=tuple(sorted(sid for sid in empties if sid in scanned_ids)),
        generation_stale_session_ids=tuple(sorted(stale_generation)),
    )


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
    expected_meta: Mapping[str, object] | None = None,
    converter_schema: int | None = None,
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
        session inside the staged dir, between the contract JSON artifacts and
        ``meta.json``; its extra files publish in the same directory swap and
        its returned keys land in ``meta.json``. ``None`` (the default) writes
        exactly the contract artifacts plus the source archive.
    materialized_at
        ISO-8601 UTC instant to stamp into every ``meta.json`` this pass, and
        the ``source_removed_at`` of every session marked this pass.
    harbor_version, converter_version
        Version pins for provenance; the caller owns them.
    converter_schema
        The converter's own output version, stamped into ``meta.json`` as
        ``converter_schema``. A session recording a different one (or none) is
        stale. When omitted, ``converter_version`` is compared instead, so a
        caller without a schema number still re-converts on an upgrade.
    expected_meta
        Further ``meta.json`` values a current session must carry (atif-cli
        passes the columnar schema version when it injects the columnar
        producer). A session recording anything else for one of these keys,
        or lacking one, is stale.
    quiesce_seconds
        Source-silence threshold; contract default 300.
    force
        Re-materialize every quiescent session regardless of the watermark
        and generation, and re-convert every archived source-removed session.
    now_ns
        Epoch-ns "now" for the quiescence check; defaults to ``time.time_ns()``
        read right AFTER the scan. Pass explicitly in tests to pin the decision.
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
        entry of every session that did not succeed. Source-removal marking
        keys off the FULL scan, so an unfiltered session is never marked just
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
        ones — almost always a wrong ``source_root``. Nothing is touched.
    CorpusAgentMismatchError
        The corpus holds another agent's sessions. Nothing is touched.
    ValueError
        ``workers`` is below ``1``, or ``expected_meta`` contradicts the
        stamped converter key.
    """
    pass_started = time.perf_counter()
    expected = expected_generation(
        converter_version, expected_meta, converter_schema=converter_schema
    )

    layout = CorpusLayout(corpus_root=corpus_root)
    _sweep_staging(layout)
    previous_watermark = read_watermark(layout.watermark_path)
    previous_empty = read_empty_sessions(layout.empty_sessions_path)
    scan = scan_sources(source_root, source_layout)
    # "Now" comes AFTER the scan: a transcript written while the scan ran must
    # not look like it was written in the future.
    effective_now_ns = time.time_ns() if now_ns is None else now_ns
    # A dir that would not list discovered no sessions, so its ids come from
    # the watermark; folding them in makes every downstream unreadable check
    # (source-removal marking, retention, the report) read one set.
    scan = scan.with_unreadable(
        _sessions_under_unlistable_dirs(
            scan,
            previous_watermark,
            source_root=source_root,
            source_layout=source_layout,
        )
    )
    sessions = scan.sessions

    existing_session_dirs = (
        sorted(p.name for p in layout.sessions_dir.iterdir() if p.is_dir())
        if layout.sessions_dir.is_dir()
        else []
    )
    # Agent check FIRST: it is the one condition under which every existing
    # session dir is sourceless by construction, so it has to run before the
    # emptiness tripwire and before the first write.
    corpus_agent = _corpus_agent(layout, existing_session_dirs)
    if corpus_agent is not None and corpus_agent != source_layout.agent.value:
        msg = (
            f"the corpus at {corpus_root} holds {corpus_agent} sessions but this "
            f"pass is materializing {source_layout.agent.value} from {source_root} "
            f"— refusing: one corpus holds one agent, and continuing would mark "
            f"all {len(existing_session_dirs)} of them source-removed"
        )
        raise CorpusAgentMismatchError(msg)
    # Empty-scan guard: an empty scan over a non-empty corpus smells like a
    # wrong source_root. Fail loud instead of flagging every session, UNLESS an
    # unlistable directory already explains the emptiness: that is a diagnosed
    # permission problem, and failing the pass every ten minutes over it adds
    # nothing the warning did not already say.
    if not sessions and existing_session_dirs and not scan.unlistable_dirs:
        msg = (
            f"scan of {source_root} found 0 sessions but the corpus at "
            f"{corpus_root} holds {len(existing_session_dirs)} materialized "
            f"session(s) — refusing to mark them source-removed; check source_root"
        )
        raise SuspiciousEmptyScanError(msg)
    if scan.unlistable_dirs:
        # Marking needs a complete picture of what EXISTS, and a dir it could
        # not open means it does not have one. Watermark resolution names the
        # sessions recorded there, never one materialized before the watermark
        # covered it, so marking anything this pass risks flagging a session
        # whose sources are merely behind a closed door.
        logger.warning(
            "materialize: not marking any session source-removed — {} source "
            "director(ies) could not be listed, so absence is not evidence of deletion",
            len(scan.unlistable_dirs),
        )
    removed_ids, retained = _retain_sourceless_sessions(
        layout,
        _sourceless_session_ids(
            layout,
            {s.session_id for s in sessions},
            scan.unreadable_session_ids,
            scan.rejected_session_ids,
        ),
        removed_at=materialized_at,
        mark=not scan.unlistable_dirs,
    )

    planned_sessions = sessions
    wanted = set(session_ids) if session_ids is not None else None
    if wanted is not None:
        planned_sessions = tuple(s for s in sessions if s.session_id in wanted)
    plan: MaterializationPlan = build_plan(
        planned_sessions,
        watermark=previous_watermark,
        policy=QuiescencePolicy(quiesce_seconds=quiesce_seconds),
        now_ns=effective_now_ns,
        force=force,
        unmaterialized_session_ids=_unmaterialized_session_ids(
            layout, previous_watermark, planned_sessions, previous_empty
        ),
        generation_stale_session_ids=_generation_stale_session_ids(
            layout, planned_sessions, expected, previous_empty
        ),
    )

    live_jobs = tuple(_live_job(session, source_root) for session in plan.to_materialize)
    archive_jobs = _archive_jobs(retained, expected, force=force, wanted=wanted)
    outcomes, workers_used = _attempt_sessions(
        layout,
        (*live_jobs, *archive_jobs),
        converter,
        workers=workers,
        worker_setup=worker_setup,
        materialized_at=materialized_at,
        harbor_version=harbor_version,
        converter_version=converter_version,
        converter_schema=converter_schema,
        agent=source_layout.agent.value,
        artifact_producer=artifact_producer,
    )
    live_outcomes = outcomes[: len(live_jobs)]
    archive_outcomes = outcomes[len(live_jobs) :]

    succeeded: list[SessionSource] = []
    empty: list[str] = []
    failures: list[MaterializationFailure] = []
    archived_ok: list[str] = []
    convert_seconds = 0.0
    artifact_seconds = 0.0
    for session, outcome in zip(plan.to_materialize, live_outcomes, strict=True):
        if outcome.error is not None:
            logger.warning("materialize: session {} failed: {}", session.session_id, outcome.error)
            failures.append(
                MaterializationFailure(session_id=session.session_id, error=outcome.error)
            )
        elif outcome.empty:
            logger.info("materialize: session {} has nothing to convert", session.session_id)
            empty.append(session.session_id)
            succeeded.append(session)
        else:
            convert_seconds += outcome.convert_seconds
            artifact_seconds += outcome.artifact_seconds
            succeeded.append(session)
    for job, outcome in zip(archive_jobs, archive_outcomes, strict=True):
        if outcome.error is not None or outcome.empty:
            error = outcome.error or "EmptySourceError: the restored archive held nothing"
            logger.warning(
                "materialize: re-converting session {} from its archive failed: {}",
                job.session_id,
                error,
            )
            failures.append(MaterializationFailure(session_id=job.session_id, error=error))
        else:
            convert_seconds += outcome.convert_seconds
            artifact_seconds += outcome.artifact_seconds
            archived_ok.append(job.session_id)

    layout.corpus_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        layout.watermark_path,
        _advance_watermark(previous_watermark, scan, tuple(succeeded)),
    )
    next_empty = _next_empty_record(
        previous_empty,
        keep_ids={s.session_id for s in sessions} | scan.unreadable_session_ids,
        attempted_ids={s.session_id for s in plan.to_materialize},
        newly_empty=empty,
        expected=expected,
    )
    if next_empty or layout.empty_sessions_path.exists():
        write_json_atomic(layout.empty_sessions_path, next_empty, indent=2)

    materialized = len(succeeded) - len(empty) + len(archived_ok)
    report = MaterializationReport(
        materialized_count=materialized,
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
        empty_session_ids=tuple(empty),
        retained_count=len(retained),
        archive_session_ids=tuple(archived_ok),
    )
    logger.info(
        "materialize: {} written ({} from archive), {} current, {} live, {} failed, "
        "{} empty, {} newly source-removed ({} retained), {} unreadable, {} rejected in {:.2f}s",
        report.materialized_count,
        report.archive_count,
        report.up_to_date_count,
        report.skipped_live_count,
        report.failed_count,
        report.empty_count,
        report.sessions_removed,
        report.retained_count,
        report.unreadable_count,
        report.rejected_count,
        report.total_seconds,
    )
    return report
