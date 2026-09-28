# SPDX-License-Identifier: Apache-2.0

"""The corpora's DuckLake: write it, rebuild it, verify it, compact it, read it.

Tables, partitions and schema identity are declared in
:mod:`atif_duck.domain.lake`; this module does the I/O.

Layout
------
One lake root (default ``~/.atif-sql/lake/``, see
:class:`atif_duck.infrastructure.lake_settings.LakeSettings`) holds::

    catalog.duckdb          the writer's DuckLake catalog (DuckDB file)
    catalog.reader.duckdb   the published copy every reader attaches
    data/                   the parquet data and delete files

plus a sibling lock file, ``<root>.lock``, held by whichever process writes.

Why readers attach a published copy rather than the catalog itself
------------------------------------------------------------------
The query sandbox runs SQL an agent composed, so the reader may only load
extensions whose table functions respect ``enable_external_access``. A SQLite
catalog needs the ``sqlite`` extension in the reader, and its
``sqlite_scan`` / ``sqlite_attach`` read any SQLite file on the host after
lockdown (measured on DuckDB 1.5.5: both ignore the allowlist), which is the
arbitrary-file read the sandbox exists to refuse. A DuckDB-file catalog needs
nothing but ``ducklake``, but DuckDB locks the file while a writer holds it,
so a reader could not attach during a write. The writer therefore keeps
``catalog.duckdb`` to itself, and after every commit (still under the lock,
catalog detached so it is checkpointed) copies it to a temporary name and
renames that over ``catalog.reader.duckdb``. A reader attaches the copy
``READ_ONLY``: it never waits on a writer, sees the last committed state, and
a query in flight keeps the file it opened when the next copy lands.

Why readers get per-file grants
-------------------------------
DuckDB grants a directory read-write. With ``data/`` granted,
``ducklake_cleanup_old_files`` run from caller SQL on a READ_ONLY attach
deleted the files scheduled for deletion before it failed on the metadata
write (measured). Granting each data and delete file of the current snapshot
through ``allowed_paths`` instead lets every scan open what it needs and
refuses every delete, rewrite and merge at the filesystem layer. ``COPY`` to a
granted path is the statement gate's to refuse, as for the per-session parquet.

The writer
----------
:class:`DuckLakeSessionSink` is the adapter atif-cli plugs into atif-corpus's
``SessionSink`` port. materialize calls it in the PARENT process, after the
sessions' directories were swapped into place, one batch of session ids at a
time: one transaction deletes the batch's rows from every table and inserts
them again from the registry's raw relations over the published artifacts
(:func:`atif_duck.infrastructure.registry.register_raw` with a session
filter), so the lake holds exactly what the per-session path would read.

A missing lake is not an error: the sink does nothing until
``atif-sql lake rebuild`` creates one. A lake whose recorded schema differs
from this code's is rebuilt by the first sink call that finds it so. A corpus
the lake does not hold yet is loaded whole by the first sink call for it.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_duck.domain.lake import (
    AGENT_COLUMN,
    ARTIFACT_HASH_SQL,
    BOOTSTRAP_CREATE_SQL,
    CORPORA_TABLE,
    CREATE_TABLE_SQL,
    DELETE_CORPUS_SQL,
    DELETE_SESSIONS_SQL,
    INSERT_SQL,
    LAKE_ALIAS,
    LAKE_HASH_SQL,
    LAKE_INFO_TABLE,
    LAKE_METADATA_ALIAS,
    LAKE_TABLES,
    PARTITION_SQL,
    READER_SELECT_SQL,
    expected_lake_info,
    lake_info_mismatches,
)
from atif_duck.domain.raw_readers import CORPUS_COLUMN
from atif_duck.domain.session_id import session_id_rejection
from atif_duck.domain.sql_literal import SqlFragment, sql_literal

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    import duckdb

    from atif_duck.infrastructure.registry import RawSources

#: The DuckDB extension that reads and writes the lake.
DUCKLAKE_EXTENSION: str = "ducklake"

#: The agent a corpus holds when its ``meta.json`` predates the ``agent`` key:
#: no corpus from before Codex support can hold Codex sessions.
DEFAULT_AGENT: str = "claude-code"

#: Sessions per transaction when materialize hands the sink its published
#: sessions. Measured on the copied corpora (see the PR): per-batch cost is
#: dominated by the fixed attach/commit/publish overhead up to about this size.
DEFAULT_SYNC_BATCH_SIZE: int = 64

#: Sessions per transaction when a whole corpus is loaded (rebuild, or a
#: corpus the lake does not hold yet). Bounds the TEMP tables the registry
#: builds per batch (edges, loss reports, JSON-path trajectories).
DEFAULT_LOAD_BATCH_SIZE: int = 512

#: How long a writer waits for another writer's lock before giving up. A
#: materialize tick that gives up records its sessions as pending and the
#: next tick retries them.
DEFAULT_LOCK_TIMEOUT_SECONDS: float = 600.0

#: Files ``lake compact`` scheduled for deletion, or found orphaned, are
#: removed only once they are this old, so a reader that opened the previous
#: published catalog a moment ago still finds every file it names.
CLEANUP_GRACE_HOURS: int = 1

#: The writer's DuckDB memory cap (lowered further by the caller's host- and
#: cgroup-derived cap). Measured on the copied corpora: loading every corpus
#: peaks at 3.5 GB RSS under a 2 GiB cap against 6.3 GB under 4 GiB, in about
#: the same time; spilling covers the rest.
DEFAULT_WRITER_MEMORY_BYTES: int = 2 * 1024**3

#: DuckDB threads for the writer's connection.
_WRITER_THREADS: int = 4

#: DuckDB threads while DuckLake merges or rewrites files. Each thread holds
#: its own share of the files being merged, and ``tool_results`` rows carry
#: whole tool outputs: under the 2 GiB cap, merging the copied corpora ran out
#: of memory at two threads and four, and finished at one (1.9 GB RSS).
_MAINTENANCE_THREADS: int = 1

#: Who the lake's snapshots name as their author.
_COMMIT_AUTHOR: str = "atif-sql"

_LOCK_POLL_SECONDS: float = 0.2


class LakeError(RuntimeError):
    """The lake could not be written or read as asked."""


class LakeExtensionMissingError(LakeError):
    """The ``ducklake`` DuckDB extension is not installed (``atif-sql lake rebuild`` installs it)."""


class LakeLockTimeoutError(LakeError):
    """Another process held the lake's writer lock for longer than the timeout."""


class LakeCorpusConflictError(LakeError):
    """Two corpus roots share a name, or a registered corpus moved; the lake keys corpora by name."""


# ---------------------------------------------------------------------------
# Layout, lock, connections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LakeLayout:
    """Every path under one lake root. Pure path arithmetic."""

    root: Path

    @property
    def catalog_path(self) -> Path:
        """The writer's catalog, never opened by a reader."""
        return self.root / "catalog.duckdb"

    @property
    def reader_catalog_path(self) -> Path:
        """The catalog copy readers attach ``READ_ONLY``."""
        return self.root / "catalog.reader.duckdb"

    @property
    def data_dir(self) -> Path:
        """The data and delete files."""
        return self.root / "data"

    @property
    def lock_path(self) -> Path:
        """Beside the root rather than in it, because a rebuild swaps the root."""
        return self.root.with_name(f"{self.root.name}.lock")

    def exists(self) -> bool:
        """True when a lake has been built here (both catalogs present)."""
        return self.catalog_path.is_file() and self.reader_catalog_path.is_file()


@dataclass(frozen=True, slots=True)
class LakeCorpus:
    """One corpus the lake holds: its root and the agent it holds."""

    root: Path
    agent: str

    @property
    def name(self) -> str:
        """The corpus directory's name, which is how the lake keys a corpus."""
        return self.root.name


def corpus_agent(corpus_root: Path, *, probe: int = 8) -> str:
    """The agent a corpus holds, read from its first readable ``meta.json``.

    Mirrors atif-corpus's own probe: a meta without the key answers
    :data:`DEFAULT_AGENT`, and an empty corpus does too.
    """
    import json

    sessions = corpus_root / "sessions"
    if not sessions.is_dir():
        return DEFAULT_AGENT
    for session_dir in sorted(sessions.iterdir())[:probe]:
        try:
            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent = meta.get(AGENT_COLUMN) if isinstance(meta, dict) else None
        return agent if isinstance(agent, str) and agent else DEFAULT_AGENT
    return DEFAULT_AGENT


def corpus_session_ids(corpus_root: Path) -> list[str]:
    """Complete, well-named session dirs of a corpus, sorted (what a load registers)."""
    sessions = corpus_root / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(
        entry.name
        for entry in sessions.iterdir()
        if entry.is_dir()
        and session_id_rejection(entry.name) is None
        and (entry / "meta.json").is_file()
    )


@contextlib.contextmanager
def writer_lock(layout: LakeLayout, *, timeout_seconds: float) -> Generator[None]:
    """Hold the lake's writer lock (``flock`` on ``<root>.lock``), waiting up to the timeout."""
    layout.lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    msg = f"another process held {layout.lock_path} for over {timeout_seconds:.0f}s"
                    raise LakeLockTimeoutError(msg) from exc
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def ducklake_extension_installed(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the ducklake extension is in the local extension directory (no network)."""
    row = con.execute(
        "SELECT installed FROM duckdb_extensions() WHERE extension_name = ?", [DUCKLAKE_EXTENSION]
    ).fetchone()
    return row is not None and bool(row[0])


def install_ducklake_extension() -> None:
    """Download the ducklake extension if it is absent. The one place the lake reaches the network."""
    import duckdb

    con = duckdb.connect()
    try:
        if not ducklake_extension_installed(con):
            logger.info("lake: installing the {} DuckDB extension", DUCKLAKE_EXTENSION)
            con.execute(f"INSTALL {DUCKLAKE_EXTENSION};")
    finally:
        con.close()


def load_ducklake(con: duckdb.DuckDBPyConnection) -> None:
    """``LOAD ducklake``, never installing it; raise when it is absent."""
    if not ducklake_extension_installed(con):
        msg = (
            f"the {DUCKLAKE_EXTENSION} DuckDB extension is not installed; "
            "`atif-sql lake rebuild` installs it"
        )
        raise LakeExtensionMissingError(msg)
    con.execute(f"LOAD {DUCKLAKE_EXTENSION};")


@contextlib.contextmanager
def _writer_connection(
    memory_limit_bytes: int | None,
) -> Generator[duckdb.DuckDBPyConnection]:
    """An in-memory connection for writing: ducklake loaded, spill dir private and removed after."""
    import duckdb

    spill = Path(tempfile.mkdtemp(prefix="atif-sql-lake-"))
    con = duckdb.connect()
    try:
        con.execute(f"SET threads={int(_WRITER_THREADS)}")
        cap = min(memory_limit_bytes or DEFAULT_WRITER_MEMORY_BYTES, DEFAULT_WRITER_MEMORY_BYTES)
        con.execute(f"SET memory_limit='{int(cap)}B'")
        con.execute(f"SET temp_directory={sql_literal(str(spill))}")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        load_ducklake(con)
        yield con
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)


@contextlib.contextmanager
def _file_maintenance(con: duckdb.DuckDBPyConnection) -> Generator[None]:
    """Run DuckLake's file merges and rewrites at :data:`_MAINTENANCE_THREADS`."""
    con.execute(f"SET threads={int(_MAINTENANCE_THREADS)}")
    try:
        yield
    finally:
        con.execute(f"SET threads={int(_WRITER_THREADS)}")


def _attach(
    con: duckdb.DuckDBPyConnection, catalog: Path, data_dir: Path, *, read_only: bool
) -> None:
    """Attach a lake catalog as :data:`LAKE_ALIAS` with its data path pinned to ``data_dir``.

    ``OVERRIDE_DATA_PATH`` on every attach: the catalog records the data path
    it was created with, and a rebuild creates it under a temporary name
    before swapping the directory into place. File paths are stored relative
    to the data path, so the override is all a moved lake needs. ``ATTACH``
    cannot be prepared, so both paths enter through :func:`sql_literal`.
    """
    if "?" in str(catalog):
        # DuckDB reads everything after a '?' in a database path as options,
        # so the catalog's WAL lands at a different path than the catalog
        # and every commit fails (measured on 1.5.5).
        msg = f"the lake root may not contain '?': {catalog.parent}"
        raise LakeError(msg)
    options = [
        f"DATA_PATH {sql_literal(str(data_dir) + os.sep)}",
        "OVERRIDE_DATA_PATH true",
        "READ_ONLY" if read_only else "DATA_INLINING_ROW_LIMIT 0",
    ]
    con.execute(
        f"ATTACH {sql_literal('ducklake:' + str(catalog))} AS {LAKE_ALIAS} ({', '.join(options)});"
    )


def _detach(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"DETACH {LAKE_ALIAS};")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_reader_catalog(layout: LakeLayout) -> None:
    """Copy the (detached, checkpointed) writer catalog over the reader copy, atomically."""
    wal = layout.catalog_path.with_name(layout.catalog_path.name + ".wal")
    if wal.exists():
        msg = f"refusing to publish {layout.catalog_path}: it still has a WAL ({wal})"
        raise LakeError(msg)
    tmp = layout.root / f".{layout.reader_catalog_path.name}.tmp-{os.getpid()}"
    shutil.copyfile(layout.catalog_path, tmp)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.chmod(0o444)
    tmp.replace(layout.reader_catalog_path)
    _fsync_dir(layout.root)


# ---------------------------------------------------------------------------
# Schema and bookkeeping
# ---------------------------------------------------------------------------


def _create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create every table in a fresh lake and record this code's schema identity."""
    con.execute(f"CALL {LAKE_ALIAS}.set_option('parquet_compression', 'zstd');")
    con.execute(f"CALL {LAKE_ALIAS}.set_option('data_inlining_row_limit', 0);")
    # One transaction, one snapshot: each DDL statement on its own is a
    # catalog commit, which made schema creation most of a small rebuild.
    con.execute("BEGIN TRANSACTION")
    for statement in BOOTSTRAP_CREATE_SQL:
        con.execute(statement)
    for table in LAKE_TABLES:
        con.execute(CREATE_TABLE_SQL[table.name])
        con.execute(PARTITION_SQL[table.name])
    con.executemany(
        f"INSERT INTO {LAKE_ALIAS}.{LAKE_INFO_TABLE} VALUES (?, ?)",  # noqa: S608  # nosec B608 - constants only
        sorted(expected_lake_info().items()),
    )
    con.execute("COMMIT")


def _read_info(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = con.execute(f"SELECT key, value FROM {LAKE_ALIAS}.{LAKE_INFO_TABLE}").fetchall()  # noqa: S608  # nosec B608 - constants only
    return {str(key): str(value) for key, value in rows}


def _read_corpora(con: duckdb.DuckDBPyConnection) -> dict[str, LakeCorpus]:
    rows = con.execute(
        f"SELECT {CORPUS_COLUMN}, {AGENT_COLUMN}, corpus_root FROM {LAKE_ALIAS}.{CORPORA_TABLE}"  # noqa: S608  # nosec B608 - constants only
    ).fetchall()
    return {
        str(name): LakeCorpus(root=Path(str(root)), agent=str(agent)) for name, agent, root in rows
    }


def _register_corpus(
    con: duckdb.DuckDBPyConnection, corpus: LakeCorpus, registered_at: str
) -> None:
    con.execute(
        f"DELETE FROM {LAKE_ALIAS}.{CORPORA_TABLE} WHERE {CORPUS_COLUMN} = ?",  # noqa: S608  # nosec B608 - constants only
        [corpus.name],
    )
    con.execute(
        f"INSERT INTO {LAKE_ALIAS}.{CORPORA_TABLE} VALUES (?, ?, ?, ?)",  # noqa: S608  # nosec B608 - constants only
        [corpus.name, corpus.agent, str(corpus.root), registered_at],
    )


def _commit_message(con: duckdb.DuckDBPyConnection, message: str) -> None:
    con.execute(
        f"CALL ducklake_set_commit_message({sql_literal(LAKE_ALIAS)}, ?, ?)",
        [_COMMIT_AUTHOR, message],
    )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_batch(
    con: duckdb.DuckDBPyConnection,
    corpus: LakeCorpus,
    session_ids: Sequence[str],
    *,
    replace_corpus: bool = False,
    delete: bool = True,
    register_at: str | None = None,
) -> None:
    """Replace ``session_ids``' rows (or, with ``replace_corpus``, the corpus's) in one transaction.

    ``delete=False`` skips the per-session DELETE for a batch the caller
    knows has no rows yet (a whole-corpus load after its first batch cleared
    the corpus); each DELETE costs a scan of the table's files even when it
    matches nothing.

    The registry binds the batch's raw relations first (TEMP tables and
    lazy parquet views in the in-memory catalog): a DuckDB transaction may
    write to one database only, and the lake is that one.
    """
    from atif_duck.infrastructure.registry import register_raw

    register_raw(con, corpus.root, session_ids=session_ids)
    con.execute("BEGIN TRANSACTION")
    try:
        for table in LAKE_TABLES:
            if replace_corpus:
                con.execute(DELETE_CORPUS_SQL[table.name], [corpus.name])
            elif delete:
                con.execute(DELETE_SESSIONS_SQL[table.name], [corpus.name, list(session_ids)])
            con.execute(INSERT_SQL[table.name], [corpus.name, corpus.agent])
        if register_at is not None:
            _register_corpus(con, corpus, register_at)
        _commit_message(con, f"{corpus.name}: {len(session_ids)} session(s)")
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        raise


def _load_corpus(con: duckdb.DuckDBPyConnection, corpus: LakeCorpus, *, batch_size: int) -> int:
    """Load a whole corpus in batches; the corpus is registered by the LAST batch's transaction.

    The first batch also deletes every row the corpus had, so a corpus a
    crashed load left half-written starts clean. Until the last batch
    commits the corpus is unregistered and a reader falls back to the
    per-session path for it.
    """
    ids = corpus_session_ids(corpus.root)
    batches = [ids[i : i + batch_size] for i in range(0, len(ids), batch_size)] or [[]]
    registered_at = _now_iso()
    for index, batch in enumerate(batches):
        _load_batch(
            con,
            corpus,
            batch,
            replace_corpus=index == 0,
            delete=False,
            register_at=registered_at if index == len(batches) - 1 else None,
        )
    logger.info("lake: loaded corpus {} ({} session(s))", corpus.name, len(ids))
    return len(ids)


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What ``lake rebuild`` did."""

    root: Path
    corpora: tuple[tuple[str, str, int], ...]
    seconds: float
    data_files: int


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_siblings(layout: LakeLayout) -> None:
    """Remove ``<root>.rebuild-<pid>`` / ``<root>.old-<pid>`` left by a dead process."""
    parent = layout.root.parent
    if not parent.is_dir():
        return
    for suffix in ("rebuild", "old"):
        for entry in parent.glob(f"{layout.root.name}.{suffix}-*"):
            tail = entry.name.rsplit("-", 1)[-1]
            if tail.isdigit() and not _pid_alive(int(tail)):
                logger.info("lake: removing {} left by a dead process", entry)
                shutil.rmtree(entry, ignore_errors=True)


def _count_live_files(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute(
        f"SELECT count(*) FROM {LAKE_METADATA_ALIAS}.ducklake_data_file WHERE end_snapshot IS NULL"  # noqa: S608  # nosec B608 - constants only
    ).fetchone()
    return int(row[0]) if row else 0


def _check_unique_names(corpora: Sequence[LakeCorpus]) -> None:
    seen: dict[str, Path] = {}
    for corpus in corpora:
        other = seen.get(corpus.name)
        if other is not None and other != corpus.root:
            msg = f"two corpora share the name {corpus.name!r}: {other} and {corpus.root}"
            raise LakeCorpusConflictError(msg)
        seen[corpus.name] = corpus.root


def _rebuild_locked(
    layout: LakeLayout,
    corpora: Sequence[LakeCorpus],
    *,
    batch_size: int,
    memory_limit_bytes: int | None,
) -> RebuildReport:
    """Build a fresh lake beside the old one, then swap it in. Caller holds the lock."""
    started = time.perf_counter()
    unique = {corpus.root: corpus for corpus in corpora}
    ordered = sorted(unique.values(), key=lambda corpus: (corpus.name, str(corpus.root)))
    _check_unique_names(ordered)
    _sweep_siblings(layout)
    build = LakeLayout(layout.root.with_name(f"{layout.root.name}.rebuild-{os.getpid()}"))
    shutil.rmtree(build.root, ignore_errors=True)
    build.data_dir.mkdir(parents=True)
    loaded: list[tuple[str, str, int]] = []
    try:
        with _writer_connection(memory_limit_bytes) as con:
            _attach(con, build.catalog_path, build.data_dir, read_only=False)
            _create_schema(con)
            for corpus in ordered:
                count = _load_corpus(con, corpus, batch_size=batch_size)
                loaded.append((corpus.name, corpus.agent, count))
            # A load in batches leaves several small files per partition.
            with _file_maintenance(con):
                con.execute(f"CALL ducklake_merge_adjacent_files({sql_literal(LAKE_ALIAS)});")
            con.execute(
                f"CALL ducklake_expire_snapshots({sql_literal(LAKE_ALIAS)}, older_than => now());"
            )
            con.execute(
                f"CALL ducklake_cleanup_old_files({sql_literal(LAKE_ALIAS)}, cleanup_all => true);"
            )
            data_files = _count_live_files(con)
            _detach(con)
        _publish_reader_catalog(build)
        old = layout.root.with_name(f"{layout.root.name}.old-{os.getpid()}")
        if layout.root.exists():
            layout.root.replace(old)
        build.root.replace(layout.root)
        _fsync_dir(layout.root.parent)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(build.root, ignore_errors=True)
        raise
    seconds = time.perf_counter() - started
    logger.info("lake: rebuilt {} with {} corpora in {:.1f}s", layout.root, len(loaded), seconds)
    return RebuildReport(
        root=layout.root, corpora=tuple(loaded), seconds=seconds, data_files=data_files
    )


def registered_corpora(layout: LakeLayout) -> dict[str, LakeCorpus]:
    """The corpora the published lake holds (empty when there is no lake or it cannot be read)."""
    import duckdb

    if not layout.exists():
        return {}
    con = duckdb.connect()
    try:
        load_ducklake(con)
        _attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
        return _read_corpora(con)
    except (duckdb.Error, LakeError) as exc:
        logger.warning("lake: cannot read the corpora {} holds: {}", layout.root, exc)
        return {}
    finally:
        con.close()


def rebuild_lake(
    layout: LakeLayout,
    corpora: Sequence[LakeCorpus],
    *,
    batch_size: int = DEFAULT_LOAD_BATCH_SIZE,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    memory_limit_bytes: int | None = None,
) -> RebuildReport:
    """Load every corpus's per-session artifacts into a fresh lake and swap it into place.

    The new lake is built at ``<root>.rebuild-<pid>`` and renamed over the old
    root only once complete, so a reader sees the whole old lake or the whole
    new one (or, for the instant between the two renames, none, and falls
    back to the per-session path). The writer lock is held throughout, so no
    materialize pass writes to the lake being replaced.
    """
    with writer_lock(layout, timeout_seconds=lock_timeout_seconds):
        return _rebuild_locked(
            layout, corpora, batch_size=batch_size, memory_limit_bytes=memory_limit_bytes
        )


# ---------------------------------------------------------------------------
# The materialize sink
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DuckLakeSessionSink:
    """Replace published sessions' rows in the lake: atif-corpus's ``SessionSink``.

    Satisfies the port structurally (the two packages may not import each
    other). Runs in materialize's parent process only; nothing here is
    pickled into a pool worker.
    """

    layout: LakeLayout
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    load_batch_size: int = DEFAULT_LOAD_BATCH_SIZE
    memory_limit_bytes: int | None = None
    #: How many sync calls wrote rows (for tests and the report).
    writes: int = field(default=0)

    def sync_sessions(self, *, corpus_root: Path, agent: str, session_ids: Sequence[str]) -> None:
        """Make the lake's rows for ``session_ids`` equal their published artifacts.

        Returns normally when that holds afterwards, including when there is
        no lake at all (``lake rebuild`` loads everything when one is made).
        Raises on anything else; materialize then records the sessions as
        pending and retries them next pass.
        """
        if not session_ids:
            return
        if not self.layout.exists():
            logger.debug("lake: no lake at {}; nothing to sync", self.layout.root)
            return
        corpus = LakeCorpus(root=corpus_root, agent=agent)
        with writer_lock(self.layout, timeout_seconds=self.lock_timeout_seconds):
            action, known = self._plan(corpus)
            if action == "rebuild":
                logger.warning(
                    "lake: {} was written by another schema; rebuilding it", self.layout.root
                )
                _rebuild_locked(
                    self.layout,
                    [*known.values(), corpus],
                    batch_size=self.load_batch_size,
                    memory_limit_bytes=self.memory_limit_bytes,
                )
                self.writes += 1
                return
            with _writer_connection(self.memory_limit_bytes) as con:
                _attach(con, self.layout.catalog_path, self.layout.data_dir, read_only=False)
                if action == "load_corpus":
                    _load_corpus(con, corpus, batch_size=self.load_batch_size)
                else:
                    _load_batch(con, corpus, sorted(set(session_ids)))
                _detach(con)
            _publish_reader_catalog(self.layout)
            self.writes += 1

    def _plan(self, corpus: LakeCorpus) -> tuple[str, dict[str, LakeCorpus]]:
        """``rebuild`` (stale schema), ``load_corpus`` (not registered yet) or ``sync``."""
        with _writer_connection(self.memory_limit_bytes) as con:
            _attach(con, self.layout.catalog_path, self.layout.data_dir, read_only=False)
            known = _read_corpora(con)
            stale = lake_info_mismatches(_read_info(con))
            _detach(con)
        if stale:
            return "rebuild", known
        current = known.get(corpus.name)
        if current is None:
            return "load_corpus", known
        if current.root.resolve() != corpus.root.resolve():
            msg = (
                f"the lake already holds a corpus named {corpus.name!r} from {current.root}, "
                f"not {corpus.root}; run `atif-sql lake rebuild` with the roots you mean"
            )
            raise LakeCorpusConflictError(msg)
        return "sync", known


# ---------------------------------------------------------------------------
# Reading (the query path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LakeReader:
    """A lake attached to a query connection, and what the sandbox must grant."""

    #: The corpus the views are scoped to, or ``None`` for every corpus.
    corpus: str | None
    #: Every data and delete file of the attached snapshot.
    grants: tuple[Path, ...]
    #: The corpora the lake holds.
    corpora: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LakeUnavailable:
    """Why the query path cannot use the lake (it falls back to the per-session path)."""

    reason: str


def _snapshot_files(con: duckdb.DuckDBPyConnection) -> tuple[Path, ...]:
    paths: set[str] = set()
    for table in (LAKE_INFO_TABLE, CORPORA_TABLE, *(t.name for t in LAKE_TABLES)):
        rows = con.execute(
            "SELECT data_file, delete_file FROM ducklake_list_files(?, ?)", [LAKE_ALIAS, table]
        ).fetchall()
        for data_file, delete_file in rows:
            paths.add(str(data_file))
            if delete_file:
                paths.add(str(delete_file))
    return tuple(Path(path) for path in sorted(paths))


def attach_lake_for_query(
    con: duckdb.DuckDBPyConnection,
    layout: LakeLayout,
    *,
    corpus_root: Path,
    all_corpora: bool,
) -> LakeReader | LakeUnavailable:
    """Attach the published lake ``READ_ONLY`` for ``query``, or say why not.

    Never installs anything: a missing extension is a reason to fall back.
    Must run before the sandbox locks the connection, because ``ATTACH``
    is one of the statements the lockdown refuses.
    """
    import duckdb as _duckdb

    if not layout.exists():
        return LakeUnavailable(f"no lake at {layout.root} (run `atif-sql lake rebuild`)")
    if not ducklake_extension_installed(con):
        return LakeUnavailable(f"the {DUCKLAKE_EXTENSION} extension is not installed")
    try:
        con.execute(f"LOAD {DUCKLAKE_EXTENSION};")
        _attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
    except _duckdb.Error as exc:
        return LakeUnavailable(f"the lake at {layout.root} did not attach: {exc}")
    stale = lake_info_mismatches(_read_info(con))
    known = _read_corpora(con)
    reason: str | None = None
    if stale:
        reason = f"the lake's schema is stale ({', '.join(stale)}); materialize rebuilds it"
    elif not all_corpora:
        current = known.get(corpus_root.name)
        if current is None:
            reason = f"the lake holds no corpus {corpus_root.name!r}"
        elif current.root.resolve() != corpus_root.resolve():
            reason = f"the lake's corpus {corpus_root.name!r} is {current.root}, not {corpus_root}"
    if reason is not None:
        _detach(con)
        return LakeUnavailable(reason)
    return LakeReader(
        corpus=None if all_corpora else corpus_root.name,
        grants=_snapshot_files(con),
        corpora=tuple(sorted(known)),
    )


def register_lake_raw(con: duckdb.DuckDBPyConnection, reader: LakeReader) -> RawSources:
    """Bind every raw relation the views read as a view over its lake table.

    Scoped to ``reader.corpus`` unless it is ``None``. The corpus name enters
    the view text through :func:`sql_literal` (``CREATE VIEW`` cannot be
    prepared). Nothing is loaded eagerly: every view is a lake scan at
    caller-query time.
    """
    from atif_duck.infrastructure.registry import RawSources

    where = (
        SqlFragment("")
        if reader.corpus is None
        else SqlFragment(f" WHERE {CORPUS_COLUMN} = {sql_literal(reader.corpus)}")
    )
    for table in LAKE_TABLES:
        con.execute(
            f"CREATE OR REPLACE VIEW {table.relation} AS {READER_SELECT_SQL[table.name]}{where};"  # nosec B608 - constants; the corpus is a sql_literal
        )
    return RawSources(
        columnar_session_ids=(),
        json_session_ids=(),
        lazy_read_paths=reader.grants,
        lake_corpus=reader.corpus,
        from_lake=True,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LakeMismatch:
    """One (table, session) whose lake rows differ from its artifacts' rows."""

    corpus: str
    table: str
    session_id: str
    artifact_rows: int
    lake_rows: int


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """What ``lake verify`` compared and what differed."""

    stale: tuple[str, ...]
    corpora: tuple[tuple[str, int], ...]
    mismatches: tuple[LakeMismatch, ...]

    @property
    def clean(self) -> bool:
        """True when the schema is current and every session matched."""
        return not self.stale and not self.mismatches


def _hashes(
    con: duckdb.DuckDBPyConnection, sql: SqlFragment, params: list[Any]
) -> dict[str, tuple[int, int]]:
    return {str(k): (int(n), int(h or 0)) for k, n, h in con.execute(sql, params).fetchall()}


def verify_lake(
    layout: LakeLayout,
    corpora: Sequence[LakeCorpus] | None = None,
    *,
    memory_limit_bytes: int | None = None,
) -> VerifyReport:
    """Compare every session's rows, table by table, between the lake and its artifacts.

    Per (table, session): the row count and an order-free content hash
    (``sum(hash(columns))``) of the lake rows against the same over the
    registry's raw relation for that corpus. A session present on one side
    only differs by construction. Reads the published catalog, so it needs
    no lock and can run beside a writer (a write that lands mid-verify can
    show up as a difference; run it again).
    """
    if not layout.exists():
        return VerifyReport(stale=("no lake",), corpora=(), mismatches=())
    from atif_duck.infrastructure.registry import register_raw

    mismatches: list[LakeMismatch] = []
    checked: list[tuple[str, int]] = []
    with _writer_connection(memory_limit_bytes) as con:
        _attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
        stale = lake_info_mismatches(_read_info(con))
        known = _read_corpora(con)
        targets = list(corpora) if corpora is not None else list(known.values())
        if stale:
            return VerifyReport(stale=stale, corpora=(), mismatches=())
        for corpus in sorted(targets, key=lambda c: c.name):
            ids = corpus_session_ids(corpus.root)
            # The whole corpus through its globs (twice as fast as a list),
            # unless it is empty, where a glob with no match is an error.
            register_raw(con, corpus.root, session_ids=None if ids else [])
            sessions: set[str] = set()
            for table in LAKE_TABLES:
                artifact = _hashes(con, ARTIFACT_HASH_SQL[table.name], [])
                lake = _hashes(con, LAKE_HASH_SQL[table.name], [corpus.name])
                sessions.update(artifact)
                sessions.update(lake)
                for session_id in sorted(set(artifact) | set(lake)):
                    left, right = artifact.get(session_id), lake.get(session_id)
                    if left != right:
                        mismatches.append(
                            LakeMismatch(
                                corpus=corpus.name,
                                table=table.name,
                                session_id=session_id,
                                artifact_rows=left[0] if left else 0,
                                lake_rows=right[0] if right else 0,
                            )
                        )
            checked.append((corpus.name, len(sessions)))
    return VerifyReport(stale=(), corpora=tuple(checked), mismatches=tuple(mismatches))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LakeStatus:
    """What ``lake status`` (and the lake block of ``status``) reports."""

    root: Path
    present: bool
    extension_installed: bool
    stale: tuple[str, ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    corpora: tuple[tuple[str, str, str, int], ...] = ()
    snapshots: int = 0
    data_files: int = 0
    delete_files: int = 0
    data_bytes: int = 0
    last_write: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "root": str(self.root),
            "present": self.present,
            "extension_installed": self.extension_installed,
            "schema_current": self.present and not self.stale and self.error is None,
            "stale": list(self.stale),
            "schema": self.schema,
            "corpora": [
                {"corpus": name, "agent": agent, "corpus_root": root, "sessions": sessions}
                for name, agent, root, sessions in self.corpora
            ],
            "snapshots": self.snapshots,
            "data_files": self.data_files,
            "delete_files": self.delete_files,
            "data_bytes": self.data_bytes,
            "last_write": self.last_write,
            "error": self.error,
        }


def lake_status(layout: LakeLayout) -> LakeStatus:
    """Read the published catalog: schema identity, corpora, snapshots, files, last write."""
    import duckdb

    con = duckdb.connect()
    try:
        installed = ducklake_extension_installed(con)
        if not layout.exists() or not installed:
            return LakeStatus(
                root=layout.root, present=layout.exists(), extension_installed=installed
            )
        try:
            con.execute(f"LOAD {DUCKLAKE_EXTENSION};")
            _attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
            info = _read_info(con)
            known = _read_corpora(con)
            counts = dict(
                con.execute(
                    f"SELECT {CORPUS_COLUMN}, count(*) FROM {LAKE_ALIAS}.session_meta GROUP BY 1"  # noqa: S608  # nosec B608 - constants only
                ).fetchall()
            )
            snap = con.execute(
                f"SELECT count(*), max(snapshot_time) FROM ducklake_snapshots({sql_literal(LAKE_ALIAS)})"  # noqa: S608  # nosec B608 - constants only
            ).fetchone()
            files = con.execute(
                f"SELECT count(*), coalesce(sum(file_size_bytes), 0) FROM {LAKE_METADATA_ALIAS}.ducklake_data_file WHERE end_snapshot IS NULL"  # noqa: S608  # nosec B608 - constants only
            ).fetchone()
            deletes = con.execute(
                f"SELECT count(*) FROM {LAKE_METADATA_ALIAS}.ducklake_delete_file WHERE end_snapshot IS NULL"  # noqa: S608  # nosec B608 - constants only
            ).fetchone()
        except duckdb.Error as exc:
            return LakeStatus(
                root=layout.root, present=True, extension_installed=installed, error=str(exc)
            )
        return LakeStatus(
            root=layout.root,
            present=True,
            extension_installed=True,
            stale=lake_info_mismatches(info),
            schema=info,
            corpora=tuple(
                (name, corpus.agent, str(corpus.root), int(counts.get(name, 0)))
                for name, corpus in sorted(known.items())
            ),
            snapshots=int(snap[0]) if snap else 0,
            last_write=snap[1].isoformat() if snap and snap[1] is not None else None,
            data_files=int(files[0]) if files else 0,
            data_bytes=int(files[1]) if files else 0,
            delete_files=int(deletes[0]) if deletes else 0,
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Compact
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompactReport:
    """File and snapshot counts before and after ``lake compact``."""

    files_before: int
    files_after: int
    snapshots_before: int
    snapshots_after: int
    seconds: float


def _snapshot_count(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute(
        f"SELECT count(*) FROM ducklake_snapshots({sql_literal(LAKE_ALIAS)})"  # noqa: S608  # nosec B608 - constants only
    ).fetchone()
    return int(row[0]) if row else 0


def compact_lake(
    layout: LakeLayout,
    *,
    expire_older_than_days: int = 30,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    memory_limit_bytes: int | None = None,
) -> CompactReport:
    """Merge small files, rewrite delete-heavy ones, expire old snapshots, remove unreferenced files.

    Snapshots older than ``expire_older_than_days`` are expired; the files
    only they referenced, and any orphaned file, are then removed once
    :data:`CLEANUP_GRACE_HOURS` old, so a reader holding the previous
    published catalog keeps every file it names.
    """
    if not layout.exists():
        msg = f"no lake at {layout.root}"
        raise LakeError(msg)
    started = time.perf_counter()
    alias = sql_literal(LAKE_ALIAS)
    with writer_lock(layout, timeout_seconds=lock_timeout_seconds):
        with _writer_connection(memory_limit_bytes) as con:
            _attach(con, layout.catalog_path, layout.data_dir, read_only=False)
            files_before, snapshots_before = _count_live_files(con), _snapshot_count(con)
            with _file_maintenance(con):
                con.execute(f"CALL ducklake_merge_adjacent_files({alias});")
                con.execute(f"CALL ducklake_rewrite_data_files({alias});")
                con.execute(f"CALL ducklake_merge_adjacent_files({alias});")
            con.execute(
                f"CALL ducklake_expire_snapshots({alias}, older_than => now() - INTERVAL {int(expire_older_than_days)} DAY);"
            )
            con.execute(
                f"CALL ducklake_cleanup_old_files({alias}, older_than => now() - INTERVAL {int(CLEANUP_GRACE_HOURS)} HOUR);"
            )
            con.execute(
                f"CALL ducklake_delete_orphaned_files({alias}, older_than => now() - INTERVAL {int(CLEANUP_GRACE_HOURS)} HOUR);"
            )
            files_after, snapshots_after = _count_live_files(con), _snapshot_count(con)
            _detach(con)
        _publish_reader_catalog(layout)
    return CompactReport(
        files_before=files_before,
        files_after=files_after,
        snapshots_before=snapshots_before,
        snapshots_after=snapshots_after,
        seconds=time.perf_counter() - started,
    )


__all__ = [
    "CLEANUP_GRACE_HOURS",
    "DEFAULT_LOAD_BATCH_SIZE",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "DEFAULT_SYNC_BATCH_SIZE",
    "DEFAULT_WRITER_MEMORY_BYTES",
    "DUCKLAKE_EXTENSION",
    "CompactReport",
    "DuckLakeSessionSink",
    "LakeCorpus",
    "LakeCorpusConflictError",
    "LakeError",
    "LakeExtensionMissingError",
    "LakeLayout",
    "LakeLockTimeoutError",
    "LakeMismatch",
    "LakeReader",
    "LakeStatus",
    "LakeUnavailable",
    "RebuildReport",
    "VerifyReport",
    "attach_lake_for_query",
    "compact_lake",
    "corpus_agent",
    "corpus_session_ids",
    "ducklake_extension_installed",
    "install_ducklake_extension",
    "lake_status",
    "load_ducklake",
    "rebuild_lake",
    "register_lake_raw",
    "registered_corpora",
    "verify_lake",
    "writer_lock",
]
