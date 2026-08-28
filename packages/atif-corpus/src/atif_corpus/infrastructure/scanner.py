# SPDX-License-Identifier: Apache-2.0

"""Filesystem discovery: sessions + side-files with their ``st_mtime_ns``.

The one place atif-corpus stats the source corpus. Discovery follows
CONTRACT.md exactly:

* main transcripts are project-level ``<source_root>/*/*.jsonl``;
* side-files are found by ``rglob`` over the session dir (the directory
  named after the session stem) filtered to ``*.jsonl`` — the rglob
  deliberately does NOT hardcode ``subagents/`` vs
  ``subagents/workflows/wf_*/`` so any deeper future nesting is still
  watermarked.

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

from atif_corpus.domain.sessions import SessionSource

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


def _session_mtimes(main_jsonl: Path) -> dict[str, int] | None:
    """``{path: mtime_ns}`` for one session, or ``None`` when its main file vanished.

    A vanished SIDE-file is simply omitted: the session is still part of the
    scan, and the missing entry is exactly the staleness signal that makes it
    re-materialize. Raises ``OSError`` when any source could not be stat'd.
    """
    main_mtime = _mtime_ns(main_jsonl)
    if main_mtime is None:
        return None
    mtimes = {str(main_jsonl): main_mtime}

    side_dir = main_jsonl.parent / main_jsonl.stem
    if side_dir.is_dir():
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            side_mtime = _mtime_ns(side_file)
            if side_mtime is not None:
                mtimes[str(side_file)] = side_mtime
    return mtimes


def _project_transcripts(project_dir: Path) -> list[Path] | None:
    """``*.jsonl`` directly under one project dir, or ``None`` if unlistable.

    ``Path.glob`` swallows a ``PermissionError`` on a directory it cannot
    open and simply yields nothing, which is indistinguishable from an empty
    project — and "empty" makes every session under it a ghost. Listing each
    project dir explicitly is what keeps the two apart.
    """
    try:
        entries = sorted(project_dir.iterdir())
    except FileNotFoundError:
        logger.debug("scan: project dir {} vanished mid-scan; skipping", project_dir)
        return []
    except OSError as error:
        logger.warning(
            "scan: cannot list project dir {} ({}); its sessions are unreadable "
            "this pass and are NOT treated as deleted",
            project_dir,
            error,
        )
        return None
    return [entry for entry in entries if entry.suffix == ".jsonl"]


def scan_sources(source_root: Path) -> SourceScan:
    """Discover every session under ``source_root``, separating unreadable ones.

    Sessions come back sorted by session id so the scan itself is
    deterministic input to :func:`atif_corpus.domain.sessions.build_plan`.
    An absent or empty root yields an empty scan — a fresh machine is not an
    error condition.

    Absence is separated from unreadability at BOTH levels: a session whose
    ``stat`` failed lands in :attr:`SourceScan.unreadable`, and a whole
    project directory that could not be LISTED lands in
    :attr:`SourceScan.unlistable_dirs` — its sessions were never discovered,
    so only the caller's watermark knows which ids they were.
    """
    if not source_root.is_dir():
        logger.debug("scan: source root {} does not exist", source_root)
        return SourceScan()

    try:
        project_dirs = sorted(p for p in source_root.iterdir() if p.is_dir())
    except OSError as error:
        logger.warning("scan: cannot list source root {} ({})", source_root, error)
        return SourceScan(unlistable_dirs=(str(source_root),))

    sessions: list[SessionSource] = []
    unreadable: dict[str, str] = {}
    unlistable: list[str] = []
    for project_dir in project_dirs:
        transcripts = _project_transcripts(project_dir)
        if transcripts is None:
            unlistable.append(str(project_dir))
            continue
        for main_jsonl in transcripts:
            try:
                mtimes = _session_mtimes(main_jsonl)
            except OSError as error:
                logger.warning(
                    "scan: cannot stat sources of session {} ({}); skipping this pass "
                    "and NOT treating it as deleted",
                    main_jsonl.stem,
                    error,
                )
                unreadable[main_jsonl.stem] = str(main_jsonl)
                continue
            if mtimes is None:
                continue
            sessions.append(
                SessionSource(
                    session_id=main_jsonl.stem,
                    session_jsonl=str(main_jsonl),
                    source_mtimes=mtimes,
                )
            )
    sessions.sort(key=lambda s: s.session_id)
    logger.debug(
        "scan: {} sessions ({} unreadable, {} unlistable dirs) under {}",
        len(sessions),
        len(unreadable),
        len(unlistable),
        source_root,
    )
    return SourceScan(
        sessions=tuple(sessions),
        unreadable=MappingProxyType(dict(unreadable)),
        unlistable_dirs=tuple(unlistable),
    )


def scan_source_root(source_root: Path) -> tuple[SessionSource, ...]:
    """The sessions found under ``source_root`` — :func:`scan_sources` without diagnostics.

    For read-only callers (``atif-sql status``) that plan but never delete:
    only ghost removal needs ``unreadable_session_ids``, and a caller that
    deletes must reach for :func:`scan_sources` and handle it. Dropping the
    diagnostics loses no operator signal here — :func:`scan_sources` logs each
    unreadable session and unlistable directory at WARNING, and the CLI's sink
    is WARNING-and-up on stderr, so an unreadable source still announces
    itself alongside the status output.
    """
    return scan_sources(source_root).sessions
