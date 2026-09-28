# SPDX-License-Identifier: Apache-2.0

"""The ``atif-sql corpus`` subcommand group: ``slim``, and the storage-layout report ``status`` prints.

materialize stores each session's ``trajectory.json``, ``edges.jsonl`` and
``session_events.jsonl`` zstd-compressed and no longer writes the five
per-session parquet files: the lake holds the queryable rows, and it loads a
session from its compressed trajectory. A corpus written before that keeps its
plain files and its parquet, and keeps working, until ``corpus slim`` converts
it. Nothing converts a corpus on its own: deploying this version leaves every
existing artifact as it is.

``corpus slim`` per corpus, in this order:

1. compress every plain artifact in place
   (:func:`atif_corpus.infrastructure.compress_artifacts.compress_in_place`:
   the compressed copy is read back and compared before the plain file goes,
   and it keeps the plain file's mtime);
2. if any session still has parquet, verify the lake against the corpus read
   WITHOUT the parquet (``verify_lake(use_session_parquet=False)``, the corpus
   as it will be once they're gone);
3. only when that verify is clean, delete the parquet files.

A corpus the lake doesn't hold, or whose sessions differ from their lake rows
(a pending lake write, say), keeps its parquet; the report says why and the
command exits 65 or 78 after finishing every corpus. ``meta.json`` is never
rewritten: the lake's ``session_meta`` rows stay equal to it, and a session
whose parquet is gone is read from its trajectory whatever its meta says.

Dry run by default: step 1 compresses into a byte counter instead of a file,
so the report's numbers are exact, and steps 2 and 3 are only described.

Lean at import like :mod:`atif_cli.lake`: every heavy import is deferred into
the command body.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import cyclopts

from atif_cli.errors import EXIT_CODES, ClassifiedError
from atif_cli.output import OutputFormat, emit_error, emit_json, resolve_format

if TYPE_CHECKING:
    from atif_duck.infrastructure.lake import LakeCorpus, LakeLayout

corpus_app = cyclopts.App(
    name="corpus",
    help="Maintain the materialized corpora: `slim` converts one to the compressed layout.",
)


# ---------------------------------------------------------------------------
# The storage-layout scan (also printed by `atif-sql status`)
# ---------------------------------------------------------------------------


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@dataclass(frozen=True, slots=True)
class SessionLayout:
    """What one complete session still stores the old way."""

    session_id: str
    session_dir: Path
    #: Plain artifacts that materialize now stores compressed.
    plain: tuple[Path, ...]
    #: Per-session parquet files present.
    parquet: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """How a corpus's complete sessions are stored."""

    sessions: tuple[SessionLayout, ...]

    @property
    def legacy_sessions(self) -> int:
        """Sessions holding a plain artifact or a parquet file."""
        return sum(1 for s in self.sessions if s.plain or s.parquet)

    @property
    def plain_bytes(self) -> int:
        """Bytes of plain artifacts still to compress."""
        return sum(_size(p) for s in self.sessions for p in s.plain)

    @property
    def parquet_bytes(self) -> int:
        """Bytes of per-session parquet still on disk."""
        return sum(_size(p) for s in self.sessions for p in s.parquet)

    @property
    def summary(self) -> str:
        """One line for ``atif-sql status``."""
        total = len(self.sessions)
        if not self.legacy_sessions:
            return f"compressed ({total} complete sessions)"
        return (
            f"{self.legacy_sessions} of {total} complete sessions in the old layout "
            f"({self.plain_bytes:,} bytes of plain JSON, {self.parquet_bytes:,} bytes of "
            "per-session parquet); `atif-sql corpus slim` converts them"
        )

    def as_dict(self) -> dict[str, Any]:
        """The JSON block ``atif-sql status`` emits."""
        return {
            "sessions": len(self.sessions),
            "legacy_sessions": self.legacy_sessions,
            "plain_bytes": self.plain_bytes,
            "parquet_bytes": self.parquet_bytes,
        }


def storage_layout(corpus_root: Path) -> StorageLayout:
    """Scan ``corpus_root``'s complete sessions for old-layout files (stat calls only)."""
    from atif_corpus.infrastructure.compress_artifacts import plain_artifacts
    from atif_duck.domain.columnar import columnar_paths
    from atif_duck.infrastructure.lake import corpus_session_ids

    sessions_dir = corpus_root / "sessions"
    rows: list[SessionLayout] = []
    for session_id in corpus_session_ids(corpus_root):
        session_dir = sessions_dir / session_id
        rows.append(
            SessionLayout(
                session_id=session_id,
                session_dir=session_dir,
                plain=tuple(plain_artifacts(session_dir)),
                parquet=tuple(p for p in columnar_paths(session_dir) if p.is_file()),
            )
        )
    return StorageLayout(sessions=tuple(rows))


def _dir_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(_size(p) for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# slim
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CorpusSlimReport:
    """What ``corpus slim`` did (or, on a dry run, would do) to one corpus."""

    corpus: str
    root: Path
    sessions: int
    bytes_before: int
    legacy_sessions: int = 0
    plain_files: int = 0
    plain_bytes: int = 0
    compressed_bytes: int = 0
    parquet_files: int = 0
    parquet_bytes: int = 0
    #: ``deleted``, ``would_delete``, ``none`` (nothing to delete), or ``kept``.
    parquet_action: str = "none"
    #: Why the parquet was kept (or what a real run would check first).
    parquet_note: str = ""
    #: ``lake_mismatch`` / ``lake_unavailable`` when the parquet was kept for that reason.
    parquet_blocked: str | None = None
    failures: list[str] = field(default_factory=list)
    bytes_after: int = 0

    @property
    def bytes_freed(self) -> int:
        return self.bytes_before - self.bytes_after

    def as_dict(self) -> dict[str, Any]:
        return {
            "corpus": self.corpus,
            "corpus_root": str(self.root),
            "sessions": self.sessions,
            "legacy_sessions": self.legacy_sessions,
            "plain_files": self.plain_files,
            "plain_bytes": self.plain_bytes,
            "compressed_bytes": self.compressed_bytes,
            "parquet_files": self.parquet_files,
            "parquet_bytes": self.parquet_bytes,
            "parquet_action": self.parquet_action,
            "parquet_note": self.parquet_note,
            "failures": list(self.failures),
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_freed": self.bytes_freed,
        }


def _lake_holds(layout: LakeLayout, corpus: LakeCorpus) -> str | None:
    """Why the lake can't vouch for ``corpus`` (``None`` when it holds it)."""
    from atif_duck.infrastructure.lake import lake_status

    state = lake_status(layout).as_dict()
    if not state["present"]:
        return f"no lake at {layout.root}; run `atif-sql lake rebuild`, then slim again"
    if not state["schema_current"]:
        return "the lake's schema is stale; run `atif-sql lake rebuild`, then slim again"
    for row in state["corpora"]:
        if row["corpus"] == corpus.name:
            if Path(row["corpus_root"]).resolve() != corpus.root.resolve():
                return f"the lake's corpus {corpus.name!r} is {row['corpus_root']}"
            return None
    return f"the lake doesn't hold {corpus.name!r}; run `atif-sql lake rebuild`, then slim again"


def _slim_corpus(
    corpus: LakeCorpus,
    layout: LakeLayout,
    *,
    dry_run: bool,
    batch_size: int,
    stage_workers: int,
    memory_limit_bytes: int,
) -> CorpusSlimReport:
    from atif_corpus.infrastructure.compress_artifacts import (
        CompressionMismatchError,
        compress_in_place,
        measure_compressed,
    )
    from atif_duck.infrastructure.lake import verify_lake

    scan = storage_layout(corpus.root)
    report = CorpusSlimReport(
        corpus=corpus.name,
        root=corpus.root,
        sessions=len(scan.sessions),
        bytes_before=_dir_bytes(corpus.root),
        legacy_sessions=scan.legacy_sessions,
    )
    for session in scan.sessions:
        for plain in session.plain:
            try:
                done = measure_compressed(plain) if dry_run else compress_in_place(plain)
            except (OSError, CompressionMismatchError) as exc:
                # A materialize pass that swapped the session's directory
                # meanwhile left it in the new layout already.
                report.failures.append(f"{session.session_id}/{plain.name}: {exc}")
                continue
            report.plain_files += 1
            report.plain_bytes += done.plain_bytes
            report.compressed_bytes += done.compressed_bytes
    parquet = [path for session in scan.sessions for path in session.parquet]
    report.parquet_files = len(parquet)
    report.parquet_bytes = sum(_size(path) for path in parquet)

    blocked = _lake_holds(layout, corpus) if parquet else None
    if not parquet:
        report.parquet_action = "none"
    elif blocked is not None:
        report.parquet_action = "kept"
        report.parquet_note = blocked
        report.parquet_blocked = "lake_unavailable"
    elif dry_run:
        report.parquet_action = "would_delete"
        report.parquet_note = (
            "a real run deletes them once `lake verify`, reading the corpus without "
            "them, comes back clean"
        )
    else:
        verified = verify_lake(
            layout,
            [corpus],
            memory_limit_bytes=memory_limit_bytes,
            use_session_parquet=False,
            batch_size=batch_size,
            stage_workers=stage_workers,
        )
        if verified.clean:
            for path in parquet:
                path.unlink(missing_ok=True)
            report.parquet_action = "deleted"
            report.parquet_note = "lake verify without them was clean"
        else:
            differing = sorted({m.session_id for m in verified.mismatches})
            report.parquet_action = "kept"
            report.parquet_blocked = "lake_mismatch" if not verified.stale else "lake_unavailable"
            report.parquet_note = (
                f"{len(differing)} session(s) differ from their lake rows "
                f"(first: {', '.join(differing[:3])}); run `atif-sql materialize` to "
                "retry pending lake writes, or `atif-sql lake rebuild`, then slim again"
                if not verified.stale
                else f"the lake can't be verified: {', '.join(verified.stale)}"
            )

    if dry_run:
        freed_parquet = report.parquet_bytes if report.parquet_action == "would_delete" else 0
        report.bytes_after = (
            report.bytes_before - report.plain_bytes + report.compressed_bytes - freed_parquet
        )
    else:
        report.bytes_after = _dir_bytes(corpus.root)
    return report


@corpus_app.command
def slim(
    *,
    corpus_root: Annotated[list[Path] | None, cyclopts.Parameter(consume_multiple=False)] = None,
    lake_root: Path | None = None,
    dry_run: Annotated[bool, cyclopts.Parameter(negative="--no-dry-run")] = True,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Convert corpora to the compressed layout; a dry run (the default) only reports.

    Compresses every plain ``trajectory.json``, ``edges.jsonl`` and
    ``session_events.jsonl`` in place (each read back and compared before the
    plain file is removed), then deletes the per-session parquet once ``lake
    verify``, reading the corpus without it, is clean. Reports the bytes each
    corpus frees. Exits 0 when every corpus is done, 65 (``lake_mismatch``)
    or 78 (``lake_unavailable``) when a corpus kept its parquet for that
    reason, 70 when a file could not be compressed.

    Run it while no materialize pass is running: a pass that replaces a
    session mid-slim leaves that session in the new layout anyway, but the
    lake verify may then report it as differing.

    Parameters
    ----------
    corpus_root
        A corpus to slim; repeat for more. Default: every corpus the lake
        holds, plus every directory under ``ATIF_SQL_CORPUS_BASE`` (default
        ``~/.atif-sql/corpus``) that holds ``sessions/``.
    lake_root
        The lake that vouches for the corpora (default ``ATIF_SQL_LAKE_ROOT``).
    dry_run
        Report what would change (exact byte counts) and change nothing.
        ``--no-dry-run`` acts.
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.
    """
    from atif_cli.lake import discover_corpora, lake_layout, writer_memory_limit

    layout, settings = lake_layout(lake_root)
    corpora = discover_corpora(layout, settings, corpus_root)
    if not corpora:
        err = ClassifiedError(
            kind="invalid_input",
            exit_code=EXIT_CODES["invalid_input"],
            message="no corpus to slim",
            hint="pass --corpus-root",
        )
        emit_error(err, fmt)
        raise SystemExit(err.exit_code)
    memory_limit = writer_memory_limit()
    reports = [
        _slim_corpus(
            corpus,
            layout,
            dry_run=dry_run,
            batch_size=settings.lake_load_batch_size,
            stage_workers=settings.lake_stage_workers,
            memory_limit_bytes=memory_limit,
        )
        for corpus in corpora
    ]
    payload = {
        "dry_run": dry_run,
        "lake_root": str(layout.root),
        "corpora": [r.as_dict() for r in reports],
        "bytes_before": sum(r.bytes_before for r in reports),
        "bytes_after": sum(r.bytes_after for r in reports),
        "bytes_freed": sum(r.bytes_freed for r in reports),
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        verb = "would free" if dry_run else "freed"
        for r in reports:
            print(
                f"{r.corpus}: {r.legacy_sessions} of {r.sessions} sessions in the old layout; "
                f"{r.plain_files} plain file(s) {r.plain_bytes:,} -> {r.compressed_bytes:,} bytes; "
                f"{r.parquet_files} parquet file(s) {r.parquet_bytes:,} bytes {r.parquet_action}; "
                f"{verb} {r.bytes_freed:,} bytes ({r.bytes_before:,} -> {r.bytes_after:,})"
            )
            if r.parquet_note:
                print(f"  parquet: {r.parquet_note}")
            for failure in r.failures:
                print(f"  FAILED {failure}", file=sys.stderr)
        total = payload["bytes_freed"]
        print(
            f"total: {verb} {total:,} bytes" + ("  (dry run; --no-dry-run acts)" if dry_run else "")
        )
    else:
        emit_json(payload, fmt)
    if dry_run:
        return
    if any(r.failures for r in reports):
        raise SystemExit(EXIT_CODES["runtime_error"])
    blocked = [r.parquet_blocked for r in reports if r.parquet_blocked]
    if "lake_mismatch" in blocked:
        raise SystemExit(EXIT_CODES["lake_mismatch"])
    if blocked:
        raise SystemExit(EXIT_CODES["lake_unavailable"])


__all__ = ["StorageLayout", "corpus_app", "storage_layout"]
