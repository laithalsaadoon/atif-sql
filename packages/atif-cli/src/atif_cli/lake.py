# SPDX-License-Identifier: Apache-2.0

"""The ``atif-sql lake`` subcommand group: rebuild, verify, status, compact.

The lake is the one DuckLake every corpus is queried through
(:mod:`atif_duck.infrastructure.lake`). ``materialize`` keeps it current one
batch of published sessions at a time; these commands build it, check it,
report on it, and keep its file count down.

Lean by construction like :mod:`atif_cli.cron`: this module loads eagerly with
``atif_cli.app`` (sub-apps must exist at registration time), so everything
beyond cyclopts and the output helpers is imported inside the command bodies.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import cyclopts

from atif_cli.errors import EXIT_CODES, ClassifiedError
from atif_cli.output import OutputFormat, emit_error, emit_json, resolve_format

if TYPE_CHECKING:
    from atif_duck.infrastructure.lake import LakeCorpus, LakeLayout
    from atif_duck.infrastructure.lake_settings import LakeSettings

lake_app = cyclopts.App(
    name="lake",
    help="Build, check and maintain the DuckLake every corpus is queried through.",
)


def _layout(lake_root: Path | None) -> tuple[LakeLayout, LakeSettings]:
    from atif_duck.infrastructure.lake import LakeLayout
    from atif_duck.infrastructure.lake_settings import LakeSettings

    settings = LakeSettings()
    return LakeLayout(lake_root if lake_root is not None else settings.lake_root), settings


def _memory_limit() -> int:
    """The same host- and cgroup-derived cap ``query`` runs under."""
    from atif_cli.app import query_memory_limit_bytes

    return query_memory_limit_bytes()


def _fail(kind: str, message: str, hint: str, fmt: OutputFormat) -> None:
    err = ClassifiedError(kind=kind, exit_code=EXIT_CODES[kind], message=message, hint=hint)
    emit_error(err, fmt)
    raise SystemExit(err.exit_code)


def _discover(
    layout: LakeLayout, settings: LakeSettings, roots: list[Path] | None
) -> list[LakeCorpus]:
    """The corpora to load: the named roots, else what the lake holds plus every corpus under the base."""
    from atif_duck.infrastructure.lake import LakeCorpus, corpus_agent, registered_corpora

    if roots:
        return [LakeCorpus(root=root, agent=corpus_agent(root)) for root in roots]
    found: dict[Path, LakeCorpus] = {
        corpus.root: corpus
        for corpus in registered_corpora(layout).values()
        if (corpus.root / "sessions").is_dir()
    }
    base = settings.corpus_base
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if (child / "sessions").is_dir() and child not in found:
                found[child] = LakeCorpus(root=child, agent=corpus_agent(child))
    return sorted(found.values(), key=lambda corpus: corpus.name)


@lake_app.command
def rebuild(
    *,
    corpus_root: Annotated[list[Path] | None, cyclopts.Parameter(consume_multiple=False)] = None,
    lake_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Load every corpus's per-session artifacts into a fresh lake, then swap it into place.

    Parameters
    ----------
    corpus_root
        A corpus to load; repeat for more. Default: every corpus the current
        lake holds, plus every directory under ``ATIF_SQL_CORPUS_BASE``
        (default ``~/.atif-sql/corpus``) that holds ``sessions/``.
    lake_root
        The lake to rebuild (default ``ATIF_SQL_LAKE_ROOT``, else
        ``~/.atif-sql/lake``).
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.

    Installs the ``ducklake`` DuckDB extension first when it is missing (the
    one network fetch the lake needs; ``query`` never installs it). The new
    lake is built beside the old one under the writer lock and renamed into
    place when complete, so a reader sees one lake or the other.
    """
    from atif_duck.infrastructure.lake import (
        LakeCorpusConflictError,
        install_ducklake_extension,
        rebuild_lake,
    )

    layout, settings = _layout(lake_root)
    corpora = _discover(layout, settings, corpus_root)
    if not corpora:
        _fail(
            "invalid_input",
            "no corpus to load",
            "materialize a corpus first, or pass --corpus-root",
            fmt,
        )
    install_ducklake_extension()
    try:
        report = rebuild_lake(
            layout,
            corpora,
            batch_size=settings.lake_load_batch_size,
            lock_timeout_seconds=settings.lake_lock_timeout_seconds,
            memory_limit_bytes=_memory_limit(),
        )
    except LakeCorpusConflictError as exc:
        _fail("invalid_input", str(exc), "give each corpus a distinct directory name", fmt)
        return
    payload = {
        "lake_root": str(report.root),
        "corpora": [
            {"corpus": name, "agent": agent, "sessions": sessions}
            for name, agent, sessions in report.corpora
        ],
        "data_files": report.data_files,
        "seconds": round(report.seconds, 3),
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        for name, agent, sessions in report.corpora:
            print(f"loaded {name} ({agent}): {sessions} sessions")
        print(f"lake: {report.root}  data files: {report.data_files}  in {report.seconds:.1f}s")
    else:
        emit_json(payload, fmt)


@lake_app.command
def verify(
    *,
    corpus_root: Annotated[list[Path] | None, cyclopts.Parameter(consume_multiple=False)] = None,
    lake_root: Path | None = None,
    limit: int = 20,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Compare every session's lake rows with its per-session artifacts; exit 65 on a difference.

    Per table and session: the row count and an order-free content hash of
    the lake's rows against the same over what the per-session path reads.
    Exits 0 when all match, 65 (``lake_mismatch``) when any differs, 78
    (``lake_unavailable``) when there is no lake or its schema is stale.

    Parameters
    ----------
    corpus_root
        Verify only this corpus; repeat for more. Default: every corpus the lake holds.
    lake_root
        The lake to verify (default ``ATIF_SQL_LAKE_ROOT``).
    limit
        How many differences to list (all are counted).
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.
    """
    from atif_duck.infrastructure.lake import (
        LakeCorpus,
        LakeError,
        corpus_agent,
        verify_lake,
    )

    layout, _ = _layout(lake_root)
    corpora = (
        [LakeCorpus(root=root, agent=corpus_agent(root)) for root in corpus_root]
        if corpus_root
        else None
    )
    try:
        report = verify_lake(layout, corpora, memory_limit_bytes=_memory_limit())
    except LakeError as exc:
        _fail("lake_unavailable", str(exc), "run `atif-sql lake rebuild`", fmt)
        return
    if report.stale:
        _fail(
            "lake_unavailable",
            f"cannot verify {layout.root}: {', '.join(report.stale)}",
            "run `atif-sql lake rebuild`",
            fmt,
        )
    sessions = sorted({(m.corpus, m.session_id) for m in report.mismatches})
    payload = {
        "lake_root": str(layout.root),
        "clean": report.clean,
        "corpora": [{"corpus": name, "sessions": n} for name, n in report.corpora],
        "mismatched_sessions": len(sessions),
        "mismatches": [
            {
                "corpus": m.corpus,
                "table": m.table,
                "session_id": m.session_id,
                "artifact_rows": m.artifact_rows,
                "lake_rows": m.lake_rows,
            }
            for m in report.mismatches[: max(0, limit)]
        ],
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        for name, n in report.corpora:
            print(f"{name}: {n} sessions checked")
        for m in report.mismatches[: max(0, limit)]:
            print(
                f"  DIFFERS {m.corpus}/{m.session_id} {m.table}: "
                f"{m.artifact_rows} artifact row(s), {m.lake_rows} lake row(s)",
                file=sys.stderr,
            )
        print("clean" if report.clean else f"{len(sessions)} session(s) differ")
    else:
        emit_json(payload, fmt)
    if not report.clean:
        raise SystemExit(EXIT_CODES["lake_mismatch"])


@lake_app.command
def status(
    *,
    lake_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Report the lake: present, schema current, corpora, snapshots, files, last write.

    Read from the published reader catalog, so it never waits on a writer.

    Parameters
    ----------
    lake_root
        The lake to report on (default ``ATIF_SQL_LAKE_ROOT``).
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.
    """
    from atif_duck.infrastructure.lake import lake_status

    layout, _ = _layout(lake_root)
    state = lake_status(layout).as_dict()
    if resolve_format(fmt) is OutputFormat.TABLE:
        print(f"lake root:    {state['root']}")
        print(
            f"present:      {state['present']}  (ducklake extension: {state['extension_installed']})"
        )
        if state["present"]:
            print(
                f"schema:       {'current' if state['schema_current'] else 'STALE ' + ', '.join(state['stale'])}"
            )
            for corpus in state["corpora"]:
                print(
                    f"corpus:       {corpus['corpus']} ({corpus['agent']}): {corpus['sessions']} sessions"
                )
            print(f"snapshots:    {state['snapshots']}")
            print(
                f"data files:   {state['data_files']} ({state['data_bytes']:,} bytes), {state['delete_files']} delete files"
            )
            print(f"last write:   {state['last_write']}")
    else:
        emit_json(state, fmt)


@lake_app.command
def compact(
    *,
    expire_older_than_days: int | None = None,
    memory_limit: str | None = None,
    lake_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Merge small files, expire old snapshots, and remove the files nothing references.

    Parameters
    ----------
    expire_older_than_days
        Expire snapshots older than this (default ``ATIF_SQL_LAKE_EXPIRE_DAYS``,
        else 30). Files only expired snapshots referenced are then removed,
        once an hour old, so a reader in flight keeps every file it names.
    memory_limit
        DuckDB's memory budget for the run, as a size (``2GiB``, ``1500MB``),
        instead of the one derived from the host and cgroup. The writer's own
        2 GiB ceiling still applies. The nightly refresh lane passes one that
        fits its memory scope, so a sizing mistake can't shrink the budget
        until the merge runs out of memory.
    lake_root
        The lake to compact (default ``ATIF_SQL_LAKE_ROOT``).
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.
    """
    from atif_duck.infrastructure.lake import (
        DEFAULT_WRITER_MEMORY_BYTES,
        LakeError,
        compact_lake,
    )

    layout, settings = _layout(lake_root)
    days = (
        expire_older_than_days if expire_older_than_days is not None else settings.lake_expire_days
    )
    if days < 0:
        _fail("invalid_input", "--expire-older-than-days must be >= 0", "pass 0 or more", fmt)
    budget = _memory_limit()
    if memory_limit is not None:
        from atif_cli.app import parse_size

        try:
            budget = parse_size(memory_limit)
        except ValueError as exc:
            _fail("invalid_input", str(exc), "pass a size such as 2GiB or 1500MB", fmt)
        if budget <= 0:
            _fail("invalid_input", "--memory-limit must be a positive size", "e.g. 2GiB", fmt)
    import duckdb

    try:
        report = compact_lake(
            layout,
            expire_older_than_days=days,
            lock_timeout_seconds=settings.lake_lock_timeout_seconds,
            memory_limit_bytes=budget,
        )
    except LakeError as exc:
        _fail("lake_unavailable", str(exc), "run `atif-sql lake rebuild`", fmt)
        return
    except duckdb.Error as exc:
        # An out-of-memory merge is the likely one. Nothing was published, so
        # readers keep the previous catalog; the next run starts over.
        _fail(
            "runtime_error",
            str(exc).splitlines()[0],
            "raise --memory-limit (the writer's ceiling is 2GiB) or run it again",
            fmt,
        )
        return
    payload = {
        "lake_root": str(layout.root),
        "data_files_before": report.files_before,
        "data_files_after": report.files_after,
        "snapshots_before": report.snapshots_before,
        "snapshots_after": report.snapshots_after,
        "memory_limit_bytes": min(budget, DEFAULT_WRITER_MEMORY_BYTES),
        "seconds": round(report.seconds, 3),
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        print(
            f"data files: {report.files_before} -> {report.files_after}  "
            f"snapshots: {report.snapshots_before} -> {report.snapshots_after}  "
            f"in {report.seconds:.1f}s"
        )
    else:
        emit_json(payload, fmt)


__all__ = ["lake_app"]
