# SPDX-License-Identifier: Apache-2.0

"""Filesystem discovery: sessions + side-files with their ``st_mtime_ns``.

The one place atif-corpus stats the source corpus. WHERE it looks comes from a
:class:`~atif_corpus.domain.source_layout.SourceLayout` — one per agent, so
this module walks one algorithm instead of carrying a branch per transcript
shape. Discovery follows CONTRACT.md exactly:

* transcripts sit ``layout.transcript_depth`` directory levels under the source
  root: ``<source_root>/<project>/<session>.jsonl`` for Claude Code,
  ``<source_root>/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`` for Codex;
* the session id comes from the FILENAME via ``layout.session_id`` — the whole
  stem for Claude Code, the trailing uuid for a Codex rollout — and must pass
  :func:`~atif_corpus.domain.session_id.session_id_rejection` before it is
  used for anything: it becomes a directory name in the corpus and, from
  there, part of the paths atif-duck opens. A name that fails is REJECTED:
  skipped this pass, logged with the reason, and reported in
  :attr:`SourceScan.rejected_session_ids` so the skip is visible;
* side-files are found by ``rglob`` over the session dir (the directory
  named after the session stem) filtered to ``*.jsonl`` — the rglob
  deliberately does NOT hardcode ``subagents/`` vs
  ``subagents/workflows/wf_*/`` so any deeper future nesting is still
  watermarked. Only layouts that HAVE side-files are walked for them: a Codex
  sub-agent writes its own rollout under its own session id, so a rollout is
  always alone and looking for siblings would be a stat per session for a
  directory that cannot exist.

A scan reports two kinds of absence and they must never be conflated. A
session whose main transcript is genuinely GONE is ghost-eligible: its
corpus dir gets deleted. A session whose ``stat`` merely FAILED (EIO,
ESTALE, an NFS or permission blip) is only unreadable this pass and its
source may well still be there — deleting its artifacts would destroy a live
session over a transient error. :class:`SourceScan` keeps the two in
separate fields so a caller cannot accidentally treat one as the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from loguru import logger

from atif_corpus.domain.session_id import session_id_rejection
from atif_corpus.domain.sessions import SessionSource
from atif_corpus.domain.source_layout import CLAUDE_CODE_LAYOUT, SourceLayout

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

#: Shared empty default; a frozen dataclass must not hand out a mutable dict.
_NO_UNREADABLE: Mapping[str, str] = MappingProxyType[str, str]({})


@dataclass(frozen=True, slots=True)
class SourceScan:
    """One scan of the source root: sessions found, plus sessions it could not read."""

    #: Sessions discovered this pass, sorted by session id.
    sessions: tuple[SessionSource, ...] = ()
    #: ``{session_id: main_jsonl}`` for sessions whose sources raised a
    #: non-``FileNotFoundError`` ``OSError``. Absent from ``sessions`` (nothing
    #: converts without an mtime), so callers MUST exclude them from ghost
    #: removal and retain their watermark entries — the main path is carried
    #: because retention scopes entries by it. Wrapped read-only so the frozen
    #: dataclass is actually frozen rather than frozen-except-this-field.
    unreadable: Mapping[str, str] = field(default_factory=lambda: _NO_UNREADABLE)
    #: Directories whose CONTENTS could not be listed at all (a project dir at
    #: mode 000). Their sessions cannot appear in ``unreadable`` — that map is
    #: keyed by discovered session, and nothing was discovered — so the
    #: directory is reported instead and the caller resolves it to session ids
    #: through the watermark.
    unlistable_dirs: tuple[str, ...] = ()
    #: Transcript names whose derived session id failed the boundary check
    #: (:mod:`atif_corpus.domain.session_id`), sorted. Nothing from them is
    #: materialized, they are never ghosted, and they are reported so an
    #: operator can see why a transcript is missing from the corpus.
    rejected_session_ids: tuple[str, ...] = ()

    @property
    def unreadable_session_ids(self) -> frozenset[str]:
        """Ids of the sessions this scan could not read."""
        return frozenset(self.unreadable)

    def with_unreadable(self, extra: Mapping[str, str]) -> SourceScan:
        """A copy of this scan carrying more unreadable ``{session_id: main_jsonl}``.

        Lets a caller that resolved :attr:`unlistable_dirs` to session ids
        fold them in, so ghost removal, watermark retention and the pass
        report all read one unreadable set instead of three.
        """
        if not extra:
            return self
        return SourceScan(
            sessions=self.sessions,
            unreadable=MappingProxyType({**self.unreadable, **extra}),
            unlistable_dirs=self.unlistable_dirs,
            rejected_session_ids=self.rejected_session_ids,
        )


def _mtime_ns(path: Path) -> int | None:
    """``st_mtime_ns`` for ``path``; ``None`` when it vanished mid-scan.

    Every other ``OSError`` propagates so the caller can tell "deleted" from
    "could not read".
    """
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        # A transcript deleted between glob and stat is a normal race on a
        # live corpus, not an error: it simply isn't part of this scan.
        logger.debug("scan: {} vanished mid-scan; skipping", path)
        return None


def _session_mtimes(main_jsonl: Path, layout: SourceLayout) -> dict[str, int] | None:
    """``{path: mtime_ns}`` for one session, or ``None`` when its main file vanished.

    A vanished SIDE-file is simply omitted: the session is still part of the
    scan, and the missing entry is exactly the staleness signal that makes it
    re-materialize. Raises ``OSError`` when any source could not be stat'd.
    """
    main_mtime = _mtime_ns(main_jsonl)
    if main_mtime is None:
        return None
    mtimes = {str(main_jsonl): main_mtime}

    if not layout.has_side_files:
        return mtimes
    side_dir = main_jsonl.parent / main_jsonl.stem
    if side_dir.is_dir():
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            side_mtime = _mtime_ns(side_file)
            if side_mtime is not None:
                mtimes[str(side_file)] = side_mtime
    return mtimes


def _list_dir(directory: Path) -> list[Path] | None:
    """Sorted entries of ``directory``; ``[]`` if it vanished, ``None`` if unlistable.

    ``Path.glob`` swallows a ``PermissionError`` on a directory it cannot
    open and simply yields nothing, which is indistinguishable from an empty
    directory — and "empty" makes every session under it a ghost. Listing each
    level explicitly is what keeps the two apart.
    """
    try:
        return sorted(directory.iterdir())
    except FileNotFoundError:
        logger.debug("scan: directory {} vanished mid-scan; skipping", directory)
        return []
    except OSError as error:
        logger.warning(
            "scan: cannot list directory {} ({}); its sessions are unreadable "
            "this pass and are NOT treated as deleted",
            directory,
            error,
        )
        return None


def _transcript_dirs(source_root: Path, layout: SourceLayout) -> tuple[list[Path], list[str]]:
    """The directories that hold transcripts, plus every dir that would not list.

    Descends exactly ``layout.transcript_depth`` levels, listing each one, so an
    unopenable directory at ANY level is reported as unlistable instead of
    reading as empty. Claude Code stops at the project dirs; Codex walks
    year -> month -> day.
    """
    level = [source_root]
    unlistable: list[str] = []
    for _ in range(layout.transcript_depth):
        next_level: list[Path] = []
        for directory in level:
            entries = _list_dir(directory)
            if entries is None:
                unlistable.append(str(directory))
                continue
            next_level.extend(entry for entry in entries if entry.is_dir())
        level = sorted(next_level)
    return level, unlistable


def _transcripts(transcript_dir: Path, layout: SourceLayout) -> list[Path] | None:
    """List one dir's transcript files for this agent, or ``None`` if unlistable."""
    entries = _list_dir(transcript_dir)
    if entries is None:
        return None
    return [entry for entry in entries if layout.is_transcript(entry.name)]


def scan_sources(
    source_root: Path,
    layout: SourceLayout = CLAUDE_CODE_LAYOUT,
) -> SourceScan:
    """Discover every session under ``source_root``, separating unreadable and rejected ones.

    Sessions come back sorted by session id so the scan itself is
    deterministic input to :func:`atif_corpus.domain.sessions.build_plan`.
    An absent or empty root yields an empty scan — a fresh machine is not an
    error condition.

    Absence is separated from unreadability at EVERY level: a session whose
    ``stat`` failed lands in :attr:`SourceScan.unreadable`, and any directory
    that could not be LISTED — the root, a Claude Code project dir, a Codex
    year/month/day dir — lands in :attr:`SourceScan.unlistable_dirs`. Its
    sessions were never discovered, so only the caller's watermark knows which
    ids they were.

    ``layout`` defaults to Claude Code's shape, which is what every caller
    predating Codex support meant.
    """
    if not source_root.is_dir():
        logger.debug("scan: source root {} does not exist", source_root)
        return SourceScan()

    transcript_dirs, unlistable = _transcript_dirs(source_root, layout)

    sessions: list[SessionSource] = []
    unreadable: dict[str, str] = {}
    rejected: list[str] = []
    for transcript_dir in transcript_dirs:
        transcripts = _transcripts(transcript_dir, layout)
        if transcripts is None:
            unlistable.append(str(transcript_dir))
            continue
        for main_jsonl in transcripts:
            session_id = layout.session_id(main_jsonl.name)
            if session_id is None:
                logger.debug(
                    "scan: {} is not a {} transcript name; skipping",
                    main_jsonl,
                    layout.agent.value,
                )
                continue
            rejection = session_id_rejection(session_id)
            if rejection is not None:
                # The one outside influence on a corpus path. A name that fails
                # here never becomes a directory, a watermark key, or a SQL
                # parameter; it is reported instead of guessed at.
                logger.warning(
                    "scan: rejecting session name {!r} from {} ({}); nothing from it "
                    "is materialized",
                    session_id,
                    main_jsonl,
                    rejection,
                )
                rejected.append(session_id)
                continue
            try:
                mtimes = _session_mtimes(main_jsonl, layout)
            except OSError as error:
                logger.warning(
                    "scan: cannot stat sources of session {} ({}); skipping this pass "
                    "and NOT treating it as deleted",
                    session_id,
                    error,
                )
                unreadable[session_id] = str(main_jsonl)
                continue
            if mtimes is None:
                continue
            sessions.append(
                SessionSource(
                    session_id=session_id,
                    session_jsonl=str(main_jsonl),
                    source_mtimes=mtimes,
                )
            )
    sessions.sort(key=lambda s: s.session_id)
    logger.debug(
        "scan: {} sessions ({} unreadable, {} unlistable dirs, {} rejected names) under {} as {}",
        len(sessions),
        len(unreadable),
        len(unlistable),
        len(rejected),
        source_root,
        layout.agent.value,
    )
    return SourceScan(
        sessions=tuple(sessions),
        unreadable=MappingProxyType(dict(unreadable)),
        unlistable_dirs=tuple(unlistable),
        rejected_session_ids=tuple(sorted(rejected)),
    )


def scan_source_root(
    source_root: Path,
    layout: SourceLayout = CLAUDE_CODE_LAYOUT,
) -> tuple[SessionSource, ...]:
    """The sessions found under ``source_root`` — :func:`scan_sources` without diagnostics.

    For read-only callers (``atif-sql status``) that plan but never delete:
    only ghost removal needs ``unreadable_session_ids``, and a caller that
    deletes must reach for :func:`scan_sources` and handle it. Dropping the
    diagnostics loses no operator signal here — :func:`scan_sources` logs each
    unreadable session and unlistable directory at WARNING, and the CLI's sink
    is WARNING-and-up on stderr, so an unreadable source still announces
    itself alongside the status output.
    """
    return scan_sources(source_root, layout).sessions
