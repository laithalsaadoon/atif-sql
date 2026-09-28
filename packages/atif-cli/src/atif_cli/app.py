# SPDX-License-Identifier: Apache-2.0

"""The atif-sql CLI (cyclopts) — the workspace's composition root.

The ONLY package that may import another workspace member, and it imports
five: atif-converter, atif-corpus, atif-duck, atif-embed, and atif-analytics.
Those five may never import each other, so every cross-package seam (the
ConverterPort adapter, the clock, version pins, the DuckDB connection) is
wired here, per docs/CONTRACT.md §CLI.

The twelve registered commands — nine ``@app.command`` functions plus the
``cron``, ``lake`` and ``corpus`` sub-apps:

* ``convert``      one-shot convert+audit for a single session JSONL
* ``materialize``  sync the materialized corpus (RealConverter behind the port)
* ``status``       corpus freshness — read-only, fast
* ``query``        SQL over the atif-duck views/macros with classified errors
* ``analyze``      run the analytics pipelines — CALLS BEDROCK, spends money
* ``embed``        backfill step embeddings into LanceDB — CALLS BEDROCK
* ``search``       embed one query string, then kNN over the store — CALLS BEDROCK
* ``examples``     derived, test-executed example queries (also ``query --examples``)
* ``schema``       static catalog dump, no DuckDB bind, <50ms
* ``cron``         sub-app: ``install`` (prints, never writes) and ``status``
* ``lake``         sub-app: ``rebuild``, ``verify``, ``status``, ``compact`` for
  the DuckLake every corpus is queried through
* ``corpus``       sub-app: ``slim`` converts a corpus to the compressed layout

A command that reaches Bedrock is marked above because its spend is not
recoverable. Each one guards itself differently: ``analyze`` defaults to a DRY
RUN that only plans and estimates, a bare ``embed`` exits 64 rather than
choosing a scope for the caller, and ``search`` embeds exactly one query
string per invocation.

Two agents
----------
``convert``, ``materialize`` and ``status`` take ``--agent claude-code|codex``.
The flag picks three things at once and they must move together: which
converter reads a transcript, which discovery layout finds one, and which
source root and corpus slug the settings default to. One corpus root therefore
holds exactly one agent's sessions, which is what lets everything downstream —
the DuckDB views, the analytics pipelines, the embedding store — stay unaware
that a second agent exists.

Agent-friendly defaults
-----------------------
* ``--format auto`` emits a human table on a TTY and JSON on a pipe.
* DuckDB errors classify into parse/catalog/runtime -> exit 64/65/70 with a
  JSON error envelope on non-TTY (see :mod:`atif_cli.errors`).

Heavy imports (duckdb, the ATIF models via atif_converter, pydantic via atif_corpus)
are DEFERRED into the command bodies that use them so the fast path
(``schema`` / ``--help`` / ``--version``) stays on a lean import graph —
pinned by the fresh-interpreter lean-import test in ``tests/test_lean_import``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import cyclopts

from atif_cli.corpus import corpus_app
from atif_cli.cron import cron_app
from atif_cli.errors import EXIT_CODES, ClassifiedError
from atif_cli.lake import lake_app
from atif_cli.output import (
    OutputFormat,
    emit_cursor,
    emit_error,
    emit_json,
    emit_rows,
    resolve_format,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from atif_corpus.application.materialize import MaterializationReport

app = cyclopts.App(
    name="atif-sql",
    help="ATIF-native analytics over agent trajectories.",
    # The DISTRIBUTION, which is not the module. cyclopts resolves a default
    # `--version` by looking the calling module's name up in the installed metadata;
    # the module is `atif_cli` and the distribution is `atif-sql`, so the default
    # lookup raises PackageNotFoundError and cyclopts answers `0.0.0`. A callable
    # also keeps `importlib.metadata` out of the import path until `--version` is
    # actually asked for, which is what the lean-import test pins.
    version=lambda: _version_of("atif-sql"),
)

# The `cron` subcommand group (install / status) — lean stdlib+cyclopts module,
# safe to register eagerly (see atif_cli.cron's module docstring).
app.command(cron_app)
# The `lake` subcommand group (rebuild / verify / status / compact): lean at
# import like `cron`, every heavy import deferred into its command bodies.
app.command(lake_app)
# The `corpus` subcommand group (slim): lean at import, like `lake`.
app.command(corpus_app)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC instant — the CLI layer owns the wall clock."""
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


def _version_of(distribution: str) -> str:
    """Installed version of ``distribution``, or ``"unknown"`` off-venv."""
    import importlib.metadata

    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _sql_str(value: str) -> str:
    """Escape a Python string as a single-quoted SQL literal."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


#: Env override for the DuckDB memory cap ``query`` runs under: any size
#: literal DuckDB's ``SET memory_limit`` accepts (``6GB``, ``512MiB``, ``2000000000B``).
QUERY_MEMORY_LIMIT_ENV = "ATIF_SQL_QUERY_MEMORY_LIMIT"

#: Env override for the DuckDB thread count ``query`` runs under.
QUERY_THREADS_ENV = "ATIF_SQL_QUERY_THREADS"

#: Set to ``1`` to let ``query``, ``search`` and ``analyze`` run as uid 0.
ALLOW_ROOT_ENV = "ATIF_SQL_ALLOW_ROOT"

#: Target for ``query``'s DuckDB memory cap when the host can afford it: the
#: base views (``tool_calls``, ``tool_rank``) need several GiB on a multi-GB
#: corpus, and a tighter cap turns working queries into OutOfMemory. A
#: target, not a floor: it never exceeds what the host has (the old 8 GiB
#: floor set a "limit" above physical RAM on an 8 GiB guest).
_QUERY_MEMORY_TARGET_BYTES = 8 * 1024**3

#: Never cap below this. DuckDB needs room to open the readers at all, and a
#: host with less available than this fails to register whatever the cap says.
_QUERY_MEMORY_MIN_BYTES = 512 * 1024**2

#: Cap budgeted per DuckDB thread when deriving the thread count. The JSON
#: readers reserve about twice their ``maximum_object_size`` per thread, and
#: that bound is now sized from the largest file present (the largest
#: trajectory.json seen is 436 MB), so 2 GiB a thread keeps a corpus on the
#: JSON path registering under the cap.
_QUERY_BYTES_PER_THREAD = 2 * 1024**3


#: Where this process's cgroup v2 membership is listed (``0::/<path>``).
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
#: The cgroup v2 unified hierarchy's mount point.
_CGROUP_ROOT = Path("/sys/fs/cgroup")
#: /proc/meminfo, read for ``MemAvailable``.
_PROC_MEMINFO = Path("/proc/meminfo")


def _read_cgroup_int(path: Path) -> int | None:
    """An integer cgroup file (``memory.max``, ``memory.current``); ``None`` for ``max`` or unreadable."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _reclaimable_file_bytes(level: Path) -> int:
    """Page cache the kernel can reclaim from this cgroup: ``active_file`` plus ``inactive_file``.

    Read from ``memory.stat``; 0 when the file is missing or unreadable, which
    leaves ``memory.current`` as the (conservative) usage.
    """
    try:
        text = (level / "memory.stat").read_text(encoding="ascii")
    except OSError:
        return 0
    total = 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in {"active_file", "inactive_file"} and value.strip().isdigit():
            total += int(value)
    return total


def _cgroup_memory() -> tuple[int | None, int | None]:
    """``(limit, headroom)`` bytes from this process's cgroup v2 chain; ``None`` when unlimited.

    A limit can sit on any ancestor (``systemd-run --scope -p MemoryMax=``
    sets it on the scope; a container runtime on a parent), so every level
    from the process's own cgroup up to the root is read: the limit is the
    smallest ``memory.max``, and the headroom the smallest ``memory.max`` minus
    what that level holds and cannot give back. ``memory.current`` counts page
    cache, which the kernel reclaims under pressure, so the level's
    ``active_file`` and ``inactive_file`` from ``memory.stat`` are subtracted
    first, the same way ``MemAvailable`` counts reclaimable cache as free on
    the host. Without that, a slice full of cache reports almost no room and
    every query and lake command drops to the floor. A host without cgroup v2
    (macOS, a v1 host) has no ``0::`` line and answers ``(None, None)``.
    """
    try:
        lines = _PROC_SELF_CGROUP.read_text(encoding="ascii").splitlines()
    except OSError:
        return None, None
    relative = next((line[3:] for line in lines if line.startswith("0::")), None)
    if relative is None:
        return None, None
    level = _CGROUP_ROOT / relative.lstrip("/")
    limit: int | None = None
    headroom: int | None = None
    while True:
        level_max = _read_cgroup_int(level / "memory.max")
        if level_max is not None:
            limit = level_max if limit is None else min(limit, level_max)
            used = _read_cgroup_int(level / "memory.current") or 0
            used = max(0, used - _reclaimable_file_bytes(level))
            room = max(0, level_max - used)
            headroom = room if headroom is None else min(headroom, room)
        if level == _CGROUP_ROOT or _CGROUP_ROOT not in level.parents:
            break
        level = level.parent
    return limit, headroom


def _host_memory() -> tuple[int, int]:
    """``(physical, available)`` bytes this process can use.

    Physical comes from ``os.sysconf``. Available is Linux's ``MemAvailable``
    from ``/proc/meminfo`` (what a new allocation can really get, reclaimable
    page cache included); where that file is absent (macOS) available is
    taken as physical. A cgroup v2 ``memory.max`` below either one lowers it
    (:func:`_cgroup_memory`): /proc/meminfo describes the host, and a query
    capped by its cgroup that sized itself from the host would be OOM-killed
    instead of spilling.
    """
    physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    available = physical
    try:
        with _PROC_MEMINFO.open(encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    limit, headroom = _cgroup_memory()
    if limit is not None:
        physical = min(physical, limit)
    if headroom is not None:
        available = min(available, headroom)
    return physical, min(physical, available)


def _query_memory_limit_bytes() -> int:
    """Bytes to cap ``query``'s DuckDB heap at, derived from the host.

    Half of physical RAM or :data:`_QUERY_MEMORY_TARGET_BYTES`, whichever is
    larger, but never above 80% of physical RAM (DuckDB's own default) and
    never above 80% of the memory available right now, floored at
    :data:`_QUERY_MEMORY_MIN_BYTES`. On a 124 GiB host that is 62 GiB; on an
    8 GiB guest with 6 GiB free it is 4.8 GiB, where the previous rule said
    8 GiB and DuckDB's default said 6.4 GiB, neither of which the guest had.
    """
    physical, available = _host_memory()
    target = max(int(physical * 0.5), _QUERY_MEMORY_TARGET_BYTES)
    ceiling = min(int(physical * 0.8), int(available * 0.8))
    return max(_QUERY_MEMORY_MIN_BYTES, min(target, ceiling))


def parse_size(text: str) -> int:
    """Bytes for a DuckDB-style size literal; raises ``ValueError`` on anything else."""
    return _parse_size(text)


def query_memory_limit_bytes() -> int:
    """The host- and cgroup-derived DuckDB cap; the ``lake`` commands open their writers under it."""
    return _query_memory_limit_bytes()


def _query_threads(memory_limit_bytes: int) -> int:
    """DuckDB threads for ``query``: one per :data:`_QUERY_BYTES_PER_THREAD` of cap.

    Capped at the CPUs this process may run on (``sched_getaffinity``, which
    sees a container's cpuset where ``cpu_count`` does not) and floored at
    one. DuckDB's own default is the core count, which is what made a 4 vCPU
    guest reserve four readers' worth of memory it did not have.
    """
    affinity = getattr(os, "sched_getaffinity", None)
    cpus = len(affinity(0)) if affinity is not None else (os.cpu_count() or 1)
    return max(1, min(cpus, memory_limit_bytes // _QUERY_BYTES_PER_THREAD))


_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}

_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*")


def _parse_size(text: str) -> int:
    """Bytes for a DuckDB-style size literal (``6GB``, ``512 MiB``, ``123B``)."""
    match = _SIZE_RE.fullmatch(text)
    if match is None:
        problem = f"{text!r} is not a size (expected e.g. 6GB, 512MiB, 2000000000B)"
        raise ValueError(problem)
    number, unit = match.group(1), (match.group(2) or "B").upper()
    if unit not in _SIZE_UNITS:
        problem = f"{text!r} has an unknown unit (use B, KB, MB, GB, TB, KiB, MiB, GiB, TiB)"
        raise ValueError(problem)
    return int(float(number) * _SIZE_UNITS[unit])


@dataclass(frozen=True, slots=True)
class QueryResources:
    """What ``query`` hands DuckDB before it registers anything."""

    memory_limit_bytes: int
    threads: int


def _query_resources() -> QueryResources:
    """Resolve the memory cap and thread count for one ``query`` process.

    :data:`QUERY_MEMORY_LIMIT_ENV` and :data:`QUERY_THREADS_ENV` override the
    host-derived values; the thread default follows whichever cap is in
    force, so a caller who lowers the cap gets fewer threads for free.
    Raises ``ValueError`` on a malformed override; the command turns that
    into exit 64.
    """
    memory_env = os.environ.get(QUERY_MEMORY_LIMIT_ENV, "").strip()
    threads_env = os.environ.get(QUERY_THREADS_ENV, "").strip()
    if memory_env:
        memory = _parse_size(memory_env)
        if memory <= 0:
            problem = f"{QUERY_MEMORY_LIMIT_ENV} must be a positive size"
            raise ValueError(problem)
    else:
        memory = _query_memory_limit_bytes()
    if threads_env:
        threads = int(threads_env)
        if threads < 1:
            problem = f"{QUERY_THREADS_ENV} must be a positive integer"
            raise ValueError(problem)
    else:
        threads = _query_threads(memory)
    return QueryResources(memory_limit_bytes=memory, threads=threads)


def _configure_query_resources(con: Any, resources: QueryResources, spill_dir: Path) -> None:
    """Size the connection to the host BEFORE registration.

    Registration is the heaviest thing ``query`` does (the eager JSON readers
    reserve memory per thread), so the cap and thread count must be in force
    before it runs, not after, which is where they used to be applied. The
    spill directory is set here for the same reason: a registration that
    spills must spill into the private directory. Extension auto-install and
    auto-load are switched off so nothing during registration can reach the
    network; the lance extension is LOADed only when it is already installed
    (:func:`atif_duck.infrastructure.registry.load_lance_extension`).
    """
    con.execute(f"SET threads={int(resources.threads)}")
    con.execute(f"SET memory_limit='{int(resources.memory_limit_bytes)}B'")
    con.execute(f"SET temp_directory={_sql_str(str(spill_dir))}")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")


@contextmanager
def _private_spill_dir() -> Generator[Path]:
    """A per-process spill directory outside the corpus, gone when the process is.

    ``mkdtemp`` creates it mode 0700 under the system temp dir. It is the
    ONLY directory the sandbox grants, so it is where DuckDB spills a query
    that exceeds ``memory_limit``; it used to be ``<corpus_root>/.duckdb_tmp``,
    inside the tree ``materialize`` scans, and files written there by caller
    SQL persisted between runs. Removed on every exit path, error included,
    after the connection is closed.
    """
    path = Path(tempfile.mkdtemp(prefix="atif-sql-query-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _refuse_root(command: str, fmt: OutputFormat) -> None:
    """Exit 77 when running as uid 0, unless :data:`ALLOW_ROOT_ENV` is ``1``.

    The corpus artifacts are written mode 0444, which is what stops a
    ``COPY ... TO`` at a granted parquet from succeeding at the filesystem.
    Root ignores file modes, so as uid 0 that protection is gone and caller
    SQL could overwrite the corpus (measured: probe d2 of the MicroVM review
    rewrote ``steps.parquet`` as a one-row file). The override exists for
    containers that have no other user; it logs a warning so the run is on
    record.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    if os.environ.get(ALLOW_ROOT_ENV, "").strip() == "1":
        from loguru import logger

        logger.warning(
            "atif-sql {} is running as root because {}=1; the 0444 file modes no longer "
            "protect the corpus from the SQL this process runs",
            command,
            ALLOW_ROOT_ENV,
        )
        return
    err = ClassifiedError(
        kind="root_refused",
        exit_code=EXIT_CODES["root_refused"],
        message=f"atif-sql {command} refuses to run as root (uid 0)",
        hint=f"run it as an unprivileged user, or set {ALLOW_ROOT_ENV}=1 to override "
        "(a warning is logged; root can then overwrite corpus files from SQL)",
    )
    emit_error(err, fmt)
    raise SystemExit(err.exit_code)


#: Statement kinds the query sandbox executes. Everything else is refused
#: before execution, by name, using DuckDB's own parser on the hardened
#: connection. The refused kinds are the ones that name a file or defer a
#: statement past this check: COPY and COPY_DATABASE (DuckDB's ``allowed_paths``
#: grants are read-write, so ``COPY ... TO <granted parquet> (USE_TMP_FILE
#: false)`` would overwrite corpus data, and root ignores the 0444 mode that
#: used to stop it), EXPORT, ATTACH, DETACH, LOAD, EXTENSION (INSTALL),
#: PREPARE and EXECUTE (a prepared COPY parses as PREPARE). An allowlist, so a
#: statement kind a future DuckDB adds is refused until someone reads what it
#: does.
_QUERY_STATEMENT_KINDS: frozenset[str] = frozenset(
    {
        "SELECT",
        "EXPLAIN",
        "SET",
        "VARIABLE_SET",
        "CREATE",
        "CREATE_FUNC",
        "DROP",
        "ALTER",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE_INTO",
        "CALL",
        "PRAGMA",
        "TRANSACTION",
        "VACUUM",
        "ANALYZE",
    }
)


def _refused_statement_kinds(con: Any, sql: str) -> list[str]:
    """Statement kinds in ``sql`` outside :data:`_QUERY_STATEMENT_KINDS`, in order, once each.

    ``extract_statements`` is the parser ``execute`` uses, so there is no
    second grammar to disagree with it, and it runs on the hardened
    connection so a PRAGMA that reads a file while parsing meets the same
    allowlist the query would. A parse error propagates as DuckDB's own
    ``ParserException`` (exit 64), exactly as ``execute`` would have raised it.
    """
    refused: list[str] = []
    for statement in con.extract_statements(sql):
        kind = str(statement.type.name)
        if kind not in _QUERY_STATEMENT_KINDS and kind not in refused:
            refused.append(kind)
    return refused


#: Config options caller SQL may still change after the sandbox locks. Only
#: ``TimeZone``: rendering timestamps in the reader's zone is an ordinary
#: analytics need and grants no filesystem or network reach. Everything else
#: (``threads``, ``errors_as_json``, ``memory_limit``, the allowlists) stays
#: frozen, because a widenable allowlist is not an allowlist.
_QUERY_UNLOCKED_CONFIGS: tuple[str, ...] = ("TimeZone",)


def _lazy_read_paths(*corpus_roots: Path) -> list[Path]:
    """Analytics parquets a registered view reads LAZILY, at caller-query time.

    ``register_raw`` materializes the JSON session artifacts into TEMP
    TABLEs, and ``register_vss`` holds the Lance store open through an
    ATTACH, so both survive the sandbox with no allowlist entry. The
    analytics views are ``read_parquet`` over files on disk, bound but not
    read until the caller selects from them — those paths are the ones that
    must stay reachable. The other lazily-read set, the per-session columnar
    parquets, is reported by ``register`` itself (``RawSources.lazy_read_paths``)
    because only the registry knows which sessions it bound that way.
    """
    return sorted(
        p for root in corpus_roots for p in (root / "analytics").rglob("*.parquet") if p.is_file()
    )


def _harden_query_connection(
    con: Any,
    *,
    corpus_root: Path,
    spill_dir: Path,
    columnar_paths: Sequence[Path] = (),
    analytics_roots: Sequence[Path] = (),
) -> None:
    """Sandbox a fully-registered connection before it runs caller SQL.

    ``query`` executes SQL an agent composed while reading third-party text
    out of the transcript corpus, so the statement is untrusted: without this
    it reaches ``read_text`` on any file the user can read, ``COPY`` to any
    path, ``ATTACH`` of unrelated databases, and ``INSTALL httpfs`` for
    network egress.

    The memory cap, thread count and spill directory are NOT set here any
    more: :func:`_configure_query_resources` applies them before
    ``register`` runs, because registration is what needs them. This
    function arms the allowlists and freezes the configuration.

    Reach is granted at two granularities, and the split is what keeps
    injected SQL from overwriting the corpus it was read from:

    * ``allowed_directories`` holds ONLY ``spill_dir``, the per-process
      private directory from :func:`_private_spill_dir`, which must accept
      writes for a query that exceeds ``memory_limit`` to complete at all.
      It lives outside the corpus and is removed when the process exits, so
      nothing written there persists and nothing under the corpus root is
      writable. (DuckDB grants a directory read-write, so a ``COPY`` into it
      would succeed at this layer; the statement gate in :func:`query`
      refuses COPY before it gets here.)
    * ``allowed_paths`` holds the individual files the views read lazily:
      the analytics parquets from :func:`_lazy_read_paths` and the
      per-session columnar parquets in ``columnar_paths`` (what
      ``register`` bound this connection to). A file grant lets
      ``read_parquet`` open that exact path. Nothing else under the corpus
      is named, so ``read_text`` of a ``trajectory.json`` is refused. DuckDB
      1.5.5 has no read-only grant (measured: a ``read_parquet`` relation
      bound before ``enable_external_access=false`` is refused at query time
      without a grant, and ``duckdb_settings()`` lists no write switch), so
      each grant is read-write and ``COPY ... TO <granted parquet>
      (USE_TMP_FILE false)`` would overwrite it. Two things close that: the
      statement gate refuses COPY for any uid, and :func:`_refuse_root`
      keeps the producer's 0444 modes meaningful by refusing uid 0.

    Four ordering constraints, each one required:

    * Both allowlists must be set BEFORE ``enable_external_access``. On their
      own they block nothing; disabling external access is what arms them,
      and DuckDB then refuses to widen either list.
    * Those settings are rejected pre-boot, so they cannot move into
      ``duckdb.connect(config=...)`` — and they must follow ``register`` so
      the corpus globs and the Lance ATTACH still bind.
    * ``allowed_configs`` must precede ``lock_configuration``, which is what
      makes its exemptions mean anything.
    * ``lock_configuration`` must come LAST (it freezes every option above,
      including itself). Without it the memory cap is decorative: caller SQL
      can just ``SET memory_limit`` back up.
    """
    con.execute(f"SET allowed_directories=[{_sql_str(str(spill_dir))}]")
    lazy_paths = ", ".join(
        _sql_str(str(path))
        for path in (*columnar_paths, *_lazy_read_paths(*(analytics_roots or (corpus_root,))))
    )
    con.execute(f"SET allowed_paths=[{lazy_paths}]")
    unlocked = ", ".join(_sql_str(name) for name in _QUERY_UNLOCKED_CONFIGS)
    con.execute(f"SET allowed_configs=[{unlocked}]")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")


def _corpus_settings(
    source_root: Path | None,
    corpus_root: Path | None,
    agent: str | None = None,
) -> Any:
    """Resolve CorpusSettings, letting explicit flags override env/defaults.

    When ``--source-root`` is given without ``--corpus-root``, the corpus
    root re-derives from the OVERRIDDEN source root's slug, so re-pointing
    the source can never write into another source's corpus. An explicit
    ``ATIF_SQL_CORPUS_ROOT`` in the env wins over that re-derivation, because
    a pinned root is a deliberate statement about where artifacts belong.

    ``agent`` is applied at CONSTRUCTION rather than copied in afterwards,
    because it is what the settings' own defaults for both roots derive from:
    a copy would leave a Codex run pointed at the Claude Code source root.
    """
    import os

    from atif_corpus.domain.agents import AgentSource as CorpusAgentSource
    from atif_corpus.domain.slug import corpus_slug
    from atif_corpus.infrastructure.settings import CorpusSettings

    settings = (
        CorpusSettings(agent=_resolve_agent(agent, CorpusAgentSource))
        if agent is not None
        else CorpusSettings()
    )
    if source_root is not None:
        resolved_corpus = corpus_root
        if resolved_corpus is None and "ATIF_SQL_CORPUS_ROOT" not in os.environ:
            resolved_corpus = Path.home() / ".atif-sql" / "corpus" / corpus_slug(source_root)
        settings = settings.model_copy(
            update={
                "source_root": source_root,
                **({"corpus_root": resolved_corpus} if resolved_corpus is not None else {}),
            }
        )
    elif corpus_root is not None:
        settings = settings.model_copy(update={"corpus_root": corpus_root})
    return settings


def _env_agent() -> str:
    """The ``ATIF_SQL_AGENT`` setting, or the default spelling.

    ``materialize`` / ``status`` / ``query`` read this through
    ``CorpusSettings``, which is env-aware for every field. ``convert`` takes no
    corpus settings at all, so it would have ignored the variable and run the
    Claude Code converter over a Codex rollout — which fails with "no
    convertible events", blaming the transcript for the wrong adapter.
    """
    import os

    return os.environ.get("ATIF_SQL_AGENT", "claude-code")


def _resolve_agent(value: str, agent_enum: Any) -> Any:
    """Parse an ``--agent`` value, or exit 64 naming the accepted spellings.

    cyclopts would coerce a StrEnum parameter itself, but the enum lives behind
    a DEFERRED import (it comes from atif-converter, which builds the ATIF
    models), and the lean-import test pins that atif_converter stays out of
    the fast path. So the
    flag is typed ``str`` at the signature and parsed here, inside the command
    body, with the same exit code an unparseable path would get.
    """
    try:
        return agent_enum(value)
    except ValueError:
        accepted = ", ".join(member.value for member in agent_enum)
        print(f"error: unknown --agent {value!r} (expected one of: {accepted})", file=sys.stderr)
        raise SystemExit(EXIT_CODES["invalid_input"]) from None


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


@app.command
def convert(
    session_jsonl: Path,
    *,
    agent: str | None = None,
    include_subagents: Annotated[bool, cyclopts.Parameter(negative="--no-subagents")] = True,
    trajectory_out: Path | None = None,
) -> None:
    """Convert one agent transcript (Claude Code session or Codex rollout) to ATIF.

    One-shot convert-and-audit (CONTRACT §CLI). With ``--trajectory-out``,
    the ENRICHED trajectory is written there, ``edges.jsonl`` lands next to
    it (always — the edges are part of the contract's conversion output),
    and stdout carries the trajectory path plus the loss summary. Without
    it, the trajectory JSON itself goes to stdout (edges are skipped: there
    is no "next to the output" on a stream).

    Parameters
    ----------
    session_jsonl
        Path to the transcript: a Claude Code session
        (``~/.claude/projects/<proj>/<session>.jsonl``) or a Codex rollout
        (``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl``).
    agent
        Which agent wrote it: ``claude-code`` (default) or ``codex``. It
        selects the harbor adapter, the record taxonomy and the fidelity
        policy the loss report is written against.
    include_subagents
        Stage ``<session>/subagents/**.jsonl`` side-files alongside the main
        chain. Claude Code only — a Codex rollout has no side-files, so the
        flag is accepted and ignored there.
    trajectory_out
        Write the trajectory JSON here (plus ``edges.jsonl`` beside it)
        instead of stdout.

    Exit codes: 0 ok, 2 empty session, 64 invalid input or unknown agent,
    65 validation, 70 conversion.
    """
    from atif_converter.application.convert_and_audit import convert_and_audit
    from atif_converter.application.convert_codex import convert_codex_and_audit
    from atif_converter.domain.agents import AgentSource
    from atif_converter.domain.errors import (
        DomainError,
        EmptySessionError,
        InvalidSessionInput,
    )

    resolved_agent = _resolve_agent(agent or _env_agent(), AgentSource)
    try:
        if resolved_agent is AgentSource.CODEX:
            result, report = convert_codex_and_audit(session_jsonl)
        else:
            result, report = convert_and_audit(session_jsonl, include_subagents=include_subagents)
    except InvalidSessionInput as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CODES["invalid_input"]) from exc
    except EmptySessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CODES["empty_session"]) from exc
    except DomainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CODES["runtime_error"]) from exc

    if trajectory_out is not None:
        trajectory_out.parent.mkdir(parents=True, exist_ok=True)
        trajectory_out.write_text(json.dumps(result.trajectory, indent=2, default=str) + "\n")
        edges_out = trajectory_out.parent / "edges.jsonl"
        edges_out.write_text("".join(f"{line}\n" for line in result.edges_lines))
        print(f"trajectory: {trajectory_out}")
        print(f"edges: {edges_out} ({len(result.edges_lines)} lines)")
    else:
        print(json.dumps(result.trajectory, indent=2, default=str))

    print(
        json.dumps(
            {
                "loss_report": report.to_json(),
                "validation_errors": list(result.validation_errors),
            },
            indent=2,
        )
    )
    if result.validation_errors:
        raise SystemExit(EXIT_CODES["validation_error"])


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def _print_report(report: MaterializationReport, fmt: OutputFormat) -> None:
    """Emit a MaterializationReport: human lines on TTY, JSON otherwise.

    An unreadable session lands in no other counter — not materialized, not
    up-to-date, not skipped-live, not failed, and never marked — so a pass
    that could only stat nothing prints all zeroes and reads as an idle,
    complete corpus unless ``unreadable`` is shown alongside them. A rejected
    session (a transcript whose name fails the session id boundary) is the
    same kind of silence and gets the same treatment; its name is printed
    ``repr``-quoted because the whole point is that it carries odd characters.

    ``removed`` counts sessions whose source vanished THIS pass. Nothing is
    deleted any more: their artifacts are kept and marked
    ``source_present: false``, and ``retained`` counts every session kept
    without a source. ``empty`` counts transcripts with nothing to convert,
    which are recorded rather than failed; ``from_archive`` counts
    source-removed sessions re-converted from their raw source archive.
    """
    payload = {
        "materialized": report.materialized_count,
        "up_to_date": report.up_to_date_count,
        "skipped_live": report.skipped_live_count,
        "failed": report.failed_count,
        "failures": [{"session_id": f.session_id, "error": f.error} for f in report.failures],
        "empty": report.empty_count,
        "empty_session_ids": list(report.empty_session_ids),
        "removed": report.sessions_removed,
        "removed_session_ids": list(report.removed_session_ids),
        "retained": report.retained_count,
        "from_archive": report.archive_count,
        "from_archive_session_ids": list(report.archive_session_ids),
        "unreadable": report.unreadable_count,
        "unreadable_session_ids": list(report.unreadable_session_ids),
        "rejected": report.rejected_count,
        "rejected_session_ids": list(report.rejected_session_ids),
        "total_seconds": round(report.total_seconds, 3),
        "convert_seconds": round(report.convert_seconds, 3),
        "workers": report.workers,
        "artifact_seconds": round(report.artifact_seconds, 3),
        "lake_synced": report.sink_synced_count,
        "lake_pending": len(report.sink_pending_session_ids),
        "lake_pending_session_ids": list(report.sink_pending_session_ids),
        "lake_error": report.sink_error,
        "lake_seconds": round(report.sink_seconds, 3),
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        print(
            f"materialized: {report.materialized_count}  "
            f"up-to-date: {report.up_to_date_count}  "
            f"skipped-live: {report.skipped_live_count}  "
            f"failed: {report.failed_count}  "
            f"empty: {report.empty_count}  "
            f"removed: {report.sessions_removed}  "
            f"retained: {report.retained_count}  "
            f"from-archive: {report.archive_count}  "
            f"unreadable: {report.unreadable_count}  "
            f"rejected: {report.rejected_count}"
        )
        print(
            f"total: {report.total_seconds:.2f}s  "
            f"(convert: {report.convert_seconds:.2f}s summed over {report.workers} worker(s), "
            f"lake: {report.sink_synced_count} synced in {report.sink_seconds:.2f}s)"
        )
        if report.sink_error is not None:
            print(
                f"  LAKE PENDING {len(report.sink_pending_session_ids)} session(s): "
                f"{report.sink_error}",
                file=sys.stderr,
            )
        for failure in report.failures:
            print(f"  FAILED {failure.session_id}: {failure.error}", file=sys.stderr)
        for session_id in report.unreadable_session_ids:
            print(f"  UNREADABLE {session_id}", file=sys.stderr)
        for session_name in report.rejected_session_ids:
            print(f"  REJECTED {session_name!r}", file=sys.stderr)
    else:
        emit_json(payload, fmt)


def _materialize_worker_setup() -> None:
    """Give a materialize pool worker the stderr sink :func:`main` gives the parent.

    A spawned worker starts with loguru's default DEBUG sink, so without this
    every debug line the converter or the atomic writer emits in a worker
    would land on the CLI's stderr in a format the parent never uses. Reads
    :data:`LOG_LEVEL_ENV` the same way, because the environment is what a
    spawned child inherits. Module-level so the executor can pickle it.
    """
    from loguru import logger

    logger.remove()
    try:
        logger.add(sys.stderr, level=_stderr_log_level())
    except ValueError:
        logger.add(sys.stderr, level=DEFAULT_LOG_LEVEL)


@app.command
def materialize(
    *,
    agent: str | None = None,
    force: bool = False,
    quiesce_seconds: int | None = None,
    source_root: Path | None = None,
    corpus_root: Path | None = None,
    sessions: str | None = None,
    workers: int | None = None,
    lake: Annotated[bool, cyclopts.Parameter(negative="--no-lake")] = True,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Sync the materialized corpus with the raw transcript corpus.

    Runs one scan → plan → convert → write pass through atif-corpus's
    materialize use case, with atif-converter's real converter adapted
    behind the ConverterPort (see :mod:`atif_cli.converter_adapter`). Each
    session is written as ``trajectory.json.zst``, ``edges.jsonl.zst`` and
    ``session_events.jsonl.zst`` (zstd), ``loss_report.json``, ``meta.json``
    and its raw source archive; the lake holds the queryable rows. The CLI
    owns the wall clock and the version pins stamped into ``meta.json``.

    Parameters
    ----------
    agent
        Which agent's transcripts to discover and convert: ``claude-code``
        (default) or ``codex``. It selects the discovery layout, the converter,
        and — unless a root is given explicitly — both default roots, so
        ``--agent codex`` alone materializes ``<CODEX_HOME>/sessions`` into its
        own corpus without touching the Claude Code one.
    force
        Re-materialize every quiescent session regardless of the watermark.
    quiesce_seconds
        Source-silence threshold; default from settings (contract: 300).
    source_root
        Override the raw transcript root (default: env ``ATIF_SQL_SOURCE_ROOT``,
        or ``<CLAUDE_CONFIG_DIR>/projects`` / ``<CODEX_HOME>/sessions`` per
        ``--agent``).
    corpus_root
        Override the materialized corpus root (default: env or
        ``~/.atif-sql/corpus/<slug>``).
    sessions
        Comma-separated session-id filter — only these sessions are planned
        this pass (contract-compatible extension; other sessions' watermark
        entries are left untouched).
    workers
        Processes for the convert+write stage. Default from
        ``ATIF_SQL_MATERIALIZE_WORKERS``, else ``min(8, cpu_count)``. ``1``
        is the single-process reference path; above that a process pool
        converts sessions in parallel and writes byte-identical artifacts.
        Below ``1`` exits 64.
    lake
        After the sessions are swapped into place, replace their rows in the
        DuckLake at ``ATIF_SQL_LAKE_ROOT`` (default ``~/.atif-sql/lake``), one
        transaction per batch, in this process. Does nothing until ``atif-sql
        lake rebuild`` has created the lake. A failed lake write leaves the
        sessions in ``<corpus>/sink_pending.json`` and the next pass retries
        them; the report's ``lake_pending`` counts them. ``--no-lake`` skips the
        lake and leaves that file alone.
    fmt
        Report format; ``auto`` = human lines on TTY, JSON on a pipe.
    """
    from atif_cli.converter_adapter import RealConverter
    from atif_converter.domain.atif import UPSTREAM_VERSION as ATIF_MODELS_VERSION
    from atif_converter.domain.schema_version import CONVERTER_SCHEMA_VERSION
    from atif_corpus.application.materialize import (
        CorpusAgentMismatchError,
        SuspiciousEmptyScanError,
        materialize as materialize_use_case,
    )
    from atif_corpus.domain.source_layout import layout_for
    from atif_duck.infrastructure.lake import DuckLakeSessionSink, LakeLayout
    from atif_duck.infrastructure.lake_settings import LakeSettings

    settings = _corpus_settings(source_root, corpus_root, agent)
    lake_settings = LakeSettings()
    source_layout = layout_for(settings.agent)
    session_filter = (
        [s for s in (part.strip() for part in sessions.split(",")) if s]
        if sessions is not None
        else None
    )
    worker_count = workers if workers is not None else settings.materialize_workers
    if worker_count < 1:
        emit_error(
            ClassifiedError(
                kind="invalid_input",
                exit_code=EXIT_CODES["invalid_input"],
                message=f"--workers must be >= 1, got {worker_count}",
                hint="pass --workers 1 for the single-process path",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["invalid_input"])
    try:
        report = materialize_use_case(
            source_root=settings.source_root,
            corpus_root=settings.corpus_root,
            converter=RealConverter(agent=settings.agent),
            source_layout=source_layout,
            materialized_at=_now_iso(),
            # The harbor release the vendored ATIF models match; harbor itself
            # isn't installed with atif-sql, so this is provenance, not a lookup.
            harbor_version=ATIF_MODELS_VERSION,
            # The release that did the converting, looked up under the ONE
            # published distribution. `atif-converter` isn't a distribution in the
            # bundled wheel, so looking it up answered "unknown" for every
            # installed run. Provenance only: staleness keys on the schema below.
            converter_version=_version_of("atif-sql"),
            # The converter's own output version. A session recording another
            # one re-converts, from its source archive if the source is gone.
            converter_schema=CONVERTER_SCHEMA_VERSION,
            # No per-session parquet any more, so no columnar schema to
            # expect: a session written with them (before `corpus slim`) is
            # as current as one written without, and the lake loads either.
            quiesce_seconds=(
                quiesce_seconds if quiesce_seconds is not None else settings.quiesce_seconds
            ),
            force=force,
            session_ids=session_filter,
            workers=worker_count,
            worker_setup=_materialize_worker_setup,
            session_sink=(
                DuckLakeSessionSink(
                    LakeLayout(lake_settings.lake_root),
                    lock_timeout_seconds=lake_settings.lake_lock_timeout_seconds,
                    load_batch_size=lake_settings.lake_load_batch_size,
                    memory_limit_bytes=_query_memory_limit_bytes(),
                    stage_workers=lake_settings.lake_stage_workers,
                )
                if lake
                else None
            ),
            sink_batch_size=lake_settings.lake_sync_batch_size,
        )
    except CorpusAgentMismatchError as exc:
        # One corpus holds one agent. Nothing was removed and nothing written —
        # the same posture as a suspicious scan, and the same exit code, because
        # an unattended lane must branch on it rather than retry.
        emit_error(
            ClassifiedError(
                kind="terminal_state",
                exit_code=EXIT_CODES["terminal_state"],
                message=str(exc),
                hint="point --corpus-root / ATIF_SQL_CORPUS_ROOT at this agent's own corpus",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["terminal_state"]) from exc
    except SuspiciousEmptyScanError as exc:
        # The corpus's loudest data-loss tripwire, so it gets a code an
        # unattended lane can branch on. Uncaught it would exit 1, which a
        # caller cannot tell apart from a crash — and the two want opposite
        # responses: a crash is worth retrying, a wrong source_root is not.
        emit_error(
            ClassifiedError(
                kind="suspicious_scan",
                exit_code=EXIT_CODES["suspicious_scan"],
                message=str(exc),
                hint="check --source-root / ATIF_SQL_SOURCE_ROOT; nothing was removed",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["suspicious_scan"]) from exc
    _print_report(report, fmt)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _vector_surface(corpus_root: Path) -> dict[str, Any]:
    """How ``query`` and ``search`` will see the embeddings store, without installing anything.

    The extension check reads ``duckdb_extensions()`` on a throwaway
    connection (the local extension directory, a few milliseconds, no
    network); the store check is the directory ``embed`` writes, resolved the
    way ``query`` and ``search`` resolve it (``ATIF_SQL_LANCE_URI`` wins).
    """
    import duckdb

    from atif_duck.infrastructure.registry import lance_extension_installed
    from atif_embed.infrastructure.settings import EmbedSettings

    store = EmbedSettings().resolve_lance_uri(corpus_root)
    con = duckdb.connect()
    try:
        installed = lance_extension_installed(con)
    finally:
        con.close()
    store_present = store.is_dir()
    if store_present and installed:
        state, note = "ready", f"store at {store}"
    elif store_present:
        state = "extension_missing"
        note = (
            "store present but the lance DuckDB extension is not installed; "
            "run `atif-sql embed --install-extension`"
        )
    else:
        state, note = "no_store", "no embeddings store; run `atif-sql embed --all --no-dry-run`"
    return {
        "lance_extension_installed": installed,
        "embeddings_store_present": store_present,
        "vector_search": state,
        "note": note,
    }


def _lake_surface(corpus_root: Path) -> dict[str, Any]:
    """Whether ``query`` will read this corpus from the lake, and the lake's vitals.

    ``state`` is ``ready`` (query reads the lake), ``absent``, ``stale``,
    ``extension_missing``, or ``corpus_missing`` (the lake does not hold this
    corpus yet; the next materialize loads it). ``pending`` counts this
    corpus's sessions waiting for a lake write.
    """
    from atif_corpus.application.materialize import read_sink_pending
    from atif_corpus.domain.layout import CorpusLayout
    from atif_duck.infrastructure.lake import LakeLayout, lake_status
    from atif_duck.infrastructure.lake_settings import LakeSettings

    state = lake_status(LakeLayout(LakeSettings().lake_root)).as_dict()
    registered = {c["corpus"]: c for c in state["corpora"]}
    mine = registered.get(corpus_root.name)
    if not state["present"]:
        name, note = "absent", "no lake; run `atif-sql lake rebuild`"
    elif not state["extension_installed"]:
        name, note = "extension_missing", "the ducklake extension is not installed"
    elif not state["schema_current"]:
        name, note = "stale", f"schema {', '.join(state['stale']) or state['error']}"
    elif mine is None or Path(mine["corpus_root"]).resolve() != corpus_root.resolve():
        name, note = "corpus_missing", "the lake does not hold this corpus"
    else:
        name, note = "ready", f"{mine['sessions']} sessions, last write {state['last_write']}"
    return {
        "state": name,
        "note": note,
        "root": state["root"],
        "schema": state["schema"],
        "snapshots": state["snapshots"],
        "data_files": state["data_files"],
        "last_write": state["last_write"],
        "sessions": mine["sessions"] if mine else 0,
        "pending": len(read_sink_pending(CorpusLayout(corpus_root=corpus_root).sink_pending_path)),
    }


def _dir_bytes(root: Path) -> int:
    """Total bytes of every file under ``root`` (0 if absent)."""
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


@app.command
def status(
    *,
    agent: str | None = None,
    source_root: Path | None = None,
    corpus_root: Path | None = None,
    quiesce_seconds: int | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Report corpus freshness: watermark age, counts, bytes, staleness.

    Read-only and fast: scans source mtimes and replays the same planning
    decision ``materialize`` would make (quiescence, watermark, and the
    recorded converter schema), without converting
    anything. The staleness summary is therefore exactly "what would a
    materialize pass do right now", and ``stale`` includes sessions a
    converter upgrade made stale (``generation_stale`` counts those alone).

    ``retained`` counts sessions the corpus keeps whose source transcript is
    gone (``meta.source_present`` false, or about to be marked so), and
    ``from_archive`` how many of them the next pass would re-convert from
    their raw source archive. ``empty`` counts transcripts recorded as having
    nothing to convert.

    ``--agent`` selects which corpus is reported, resolving the same way
    ``materialize`` resolves it, so the two commands always describe the same
    pair of roots. The JSON payload carries the resolved agent, which is what
    lets an unattended lane confirm it ticked the corpus it meant to.

    ``layout`` says how many sessions are still stored the old way (plain
    ``trajectory.json`` / ``edges.jsonl`` / ``session_events.jsonl``, or the
    per-session parquet files materialize no longer writes) and how many
    bytes that is; ``atif-sql corpus slim`` converts them. ``query path``
    says how ``query`` reads this corpus when it can't use the lake:
    ``columnar`` when every complete session still carries its parquet,
    ``json`` when none does (every session materialized or slimmed since the
    parquet was dropped), ``mixed`` in between. It applies the same
    per-session predicate the registry does.

    ``vector search`` says whether ``semantic_search`` and ``search`` can
    reach an embeddings store: ``ready`` (store present, lance extension
    installed), ``no_store`` (run ``atif-sql embed``), or
    ``extension_missing`` (a store exists but the DuckDB lance extension is
    not installed; ``query`` binds ``message_embeddings`` empty rather than
    downloading it, so run ``atif-sql embed --install-extension``). Read
    from DuckDB's local extension listing; never installs anything.
    """
    import time

    from atif_cli.corpus import storage_layout
    from atif_converter.domain.schema_version import CONVERTER_SCHEMA_VERSION
    from atif_corpus.application.materialize import preview_pass, read_watermark
    from atif_corpus.domain.layout import CorpusLayout
    from atif_corpus.domain.source_layout import layout_for
    from atif_duck.domain.columnar import COLUMNAR_SCHEMA_VERSION
    from atif_duck.infrastructure.columnar import columnar_coverage

    settings = _corpus_settings(source_root, corpus_root, agent)
    layout = CorpusLayout(corpus_root=settings.corpus_root)
    quiesce = quiesce_seconds if quiesce_seconds is not None else settings.quiesce_seconds

    watermark = read_watermark(layout.watermark_path)
    # Planned as a default `materialize` would plan it.
    preview = preview_pass(
        source_root=settings.source_root,
        corpus_root=settings.corpus_root,
        converter_version=_version_of("atif-sql"),
        converter_schema=CONVERTER_SCHEMA_VERSION,
        quiesce_seconds=quiesce,
        source_layout=layout_for(settings.agent),
    )
    plan = preview.plan
    source_sessions = (*plan.to_materialize, *plan.up_to_date, *plan.skipped_live)
    # Read after the scan, like the pass's own "now".
    now_ns = time.time_ns()
    watermark_age_seconds: float | None = None
    if layout.watermark_path.exists():
        watermark_age_seconds = round((now_ns - layout.watermark_path.stat().st_mtime_ns) / 1e9, 1)
    materialized_dirs = (
        sorted(p.name for p in layout.sessions_dir.iterdir() if p.is_dir())
        if layout.sessions_dir.is_dir()
        else []
    )

    corpus_bytes = _dir_bytes(settings.corpus_root)
    staleness = {
        "stale": len(plan.to_materialize),
        "up_to_date": len(plan.up_to_date),
        "live": len(plan.skipped_live),
        "generation_stale": len(preview.generation_stale_session_ids),
        "from_archive": len(preview.archive_session_ids),
    }
    coverage = columnar_coverage(settings.corpus_root)
    storage = storage_layout(settings.corpus_root)
    vector = _vector_surface(settings.corpus_root)
    lake_block = _lake_surface(settings.corpus_root)
    if resolve_format(fmt) is OutputFormat.TABLE:
        print(f"agent:        {settings.agent.value}")
        print(f"source root:  {settings.source_root}")
        print(f"corpus root:  {settings.corpus_root}")
        age = "never materialized" if watermark_age_seconds is None else f"{watermark_age_seconds}s"
        print(f"watermark:    {age} old  ({len(watermark)} entries)")
        print(
            f"sessions:     {len(source_sessions)} in source, {len(materialized_dirs)} materialized"
        )
        print(f"corpus bytes: {corpus_bytes:,}")
        print(
            f"staleness:    {staleness['stale']} stale "
            f"({staleness['generation_stale']} from a converter or schema change), "
            f"{staleness['up_to_date']} up-to-date, {staleness['live']} live"
        )
        print(
            f"retained:     {len(preview.retained_session_ids)} kept without a source "
            f"({staleness['from_archive']} to re-convert from archive), "
            f"{len(preview.empty_session_ids)} empty"
        )
        print(f"layout:       {storage.summary}")
        print(
            f"query path:   {coverage.query_path} without the lake  "
            f"({coverage.columnar_sessions} of {coverage.total_sessions} complete sessions "
            "still carry per-session parquet)"
        )
        print(f"vector search: {vector['vector_search']}  ({vector['note']})")
        print(f"lake:         {lake_block['state']}  ({lake_block['note']})")
    else:
        emit_json(
            {
                "agent": settings.agent.value,
                "source_root": str(settings.source_root),
                "corpus_root": str(settings.corpus_root),
                "watermark_age_seconds": watermark_age_seconds,
                "watermark_entries": len(watermark),
                "source_sessions": len(source_sessions),
                "materialized_sessions": len(materialized_dirs),
                "corpus_bytes": corpus_bytes,
                "staleness": staleness,
                "retained_sessions": len(preview.retained_session_ids),
                "empty_sessions": len(preview.empty_session_ids),
                "converter_schema": CONVERTER_SCHEMA_VERSION,
                "columnar_schema": COLUMNAR_SCHEMA_VERSION,
                "columnar_sessions": coverage.columnar_sessions,
                "json_sessions": coverage.json_sessions,
                "query_path": coverage.query_path,
                "layout": storage.as_dict(),
                "lance_extension_installed": vector["lance_extension_installed"],
                "embeddings_store_present": vector["embeddings_store_present"],
                "vector_search": vector["vector_search"],
                "lake": lake_block,
            },
            fmt,
        )


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


@app.command
def query(
    sql: str | None = None,
    /,
    *,
    examples_flag: Annotated[bool, cyclopts.Parameter(name="--examples")] = False,
    category: str | None = None,
    requires: str | None = None,
    agent: str | None = None,
    corpus_root: Path | None = None,
    all_corpora: bool = False,
    lake: Annotated[bool, cyclopts.Parameter(negative="--no-lake")] = True,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Run one SQL statement against the atif-duck catalog and emit results.

    Opens an in-memory DuckDB connection, registers the corpus views +
    macros via ``atif_duck.register``, executes, and emits: TTY -> plain
    table, pipe -> JSON array of row objects.

    ``--examples`` short-circuits to the ``examples`` listing (same output,
    same ``--category`` / ``--requires`` filters) without opening DuckDB —
    the natural discovery path when an agent is already composing a query.

    Where the rows come from
    ------------------------
    When the DuckLake at ``ATIF_SQL_LAKE_ROOT`` exists, its schema is current
    and it holds the requested corpus, every view reads the lake, scoped to
    that corpus (``--agent`` / ``--corpus-root`` pick it, as before), and
    nothing is loaded before the statement runs. ``--all-corpora`` scopes the
    views to every corpus the lake holds instead; ``sessions.corpus`` tells
    them apart (the analytics views stay the selected corpus's, and
    ``message_embeddings`` spans every corpus's store unless
    ``ATIF_SQL_LANCE_URI`` pins one). Otherwise, or with ``--no-lake``, ``query`` reads the
    per-session artifacts as it always has, after a one-line warning saying
    why. ``--all-corpora`` has no per-session fallback and exits 78
    (``lake_unavailable``) without a usable lake.

    Sandbox
    -------
    Before anything is registered the connection is sized to the host
    (:func:`_query_resources`: a memory cap derived from available RAM and a
    thread count derived from that cap, overridable with
    ``ATIF_SQL_QUERY_MEMORY_LIMIT`` and ``ATIF_SQL_QUERY_THREADS``), its spill
    directory is a private ``mkdtemp`` (mode 0700) outside the corpus that is
    removed when the process exits, and extension auto-install and auto-load
    are off. The lance extension is loaded only when it is already installed;
    when a store exists and it is not, the vector surface binds empty with a
    warning and ``atif-sql status`` says so. Registration therefore reaches
    neither the network nor the corpus for writing, and it fits an 8 GiB
    guest, where it used to exit 70 out of memory before the cap applied.

    The statement then runs against the hardened connection (see
    :func:`_harden_query_connection`). Reads reach the registered views and
    nothing else. ``read_text`` outside the corpus, ``ATTACH`` of unrelated
    databases and extension installs fail with exit 70 rather than reading
    credentials or reaching the network, and nothing under the corpus root
    is writable. Two layers keep caller SQL from writing the corpus:

    * DuckDB's file grants are read-write, and ``COPY ... TO <granted
      parquet> (USE_TMP_FILE false)`` used to overwrite one, so every
      statement kind that names a file is refused BEFORE execution, for any
      uid, by DuckDB's own parser: COPY, EXPORT, ATTACH, DETACH, INSTALL,
      LOAD, and PREPARE/EXECUTE (which could defer one). Exit 70, kind
      ``sandbox_refused``.
    * A 0444 file mode does not bind root, so ``query`` refuses to run as
      uid 0 (exit 77) unless ``ATIF_SQL_ALLOW_ROOT=1`` is set, which logs a
      warning.

    The embedding-store guard binds here exactly as it does for ``search``,
    so a store written by a different provider refuses to bind (exit 65)
    instead of scoring garbage.

    One residual hole remains, over recomputable derived data rather than
    the transcript artifacts: ``DELETE``/``INSERT`` against
    ``lance_store.main.embeddings`` reach the ATTACHed store, which the
    filesystem allowlist does not cover. Re-run
    ``atif-sql embed --all --no-dry-run`` to rebuild.

    What caller SQL can still see, and why that is accepted:
    ``duckdb_settings()`` and ``current_setting(...)`` return the sandbox's
    own configuration, including the corpus root, the spill directory, the
    memory cap and every granted parquet path, which names every session id.
    DuckDB cannot hide a setting from SQL, and the grants have to be per-file
    for the lazy reads to work at all, so the inventory is a property of the
    design. The caller is the local user, who can list the corpus directory
    and ``SELECT session_id FROM sessions`` anyway; ``query`` is not a
    privilege boundary (SECURITY.md) and must not be exposed to a caller who
    may not know the session ids.

    ``lock_configuration`` freezes the connection's settings, so ``SET`` is
    refused for everything except ``TimeZone``, which stays open for
    timestamp rendering. ``SET threads`` and ``SET errors_as_json`` fail with
    exit 70 — deliberately: an unfreezable option is one injected SQL can
    turn back off.

    Exit codes
    ----------
    * 64  parse_error   malformed SQL (or no SQL and no --examples)
    * 64  invalid_input a malformed ``ATIF_SQL_QUERY_MEMORY_LIMIT`` or
      ``ATIF_SQL_QUERY_THREADS``
    * 65  catalog_error unknown view/macro/column (try ``atif-sql schema``)
    * 65  embedding_mismatch the Lance store was written by another provider
    * 70  sandbox_refused a statement kind the sandbox never runs (COPY,
      EXPORT, ATTACH, INSTALL, LOAD, PREPARE, ...)
    * 70  runtime_error everything else (an unmaterialized corpus, or SQL
      the sandbox refused)
    * 77  root_refused  running as uid 0 without ``ATIF_SQL_ALLOW_ROOT=1``

    Examples
    --------
    ::

        atif-sql query 'SELECT count(*) FROM sessions'
        atif-sql query 'SELECT * FROM tool_rank(14)' --format json
        atif-sql query --examples --requires core
        atif-sql query --agent codex 'SELECT agent, count(*) FROM sessions GROUP BY 1'

    ``--agent codex`` selects the Codex corpus root the way ``materialize``
    selects it, so the corpus a Codex materialize wrote is queryable without
    spelling its path. One corpus holds one agent; ``--corpus-root`` still wins.
    """
    if examples_flag:
        examples(category=category, requires=requires, fmt=fmt)
        return
    if sql is None:
        emit_error(
            ClassifiedError(
                kind="parse_error",
                exit_code=EXIT_CODES["parse_error"],
                message="no SQL given",
                hint="pass a statement (atif-sql query 'SELECT ...') or list "
                "tested templates with: atif-sql examples",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["parse_error"])

    _refuse_root("query", fmt)
    try:
        resources = _query_resources()
    except ValueError as exc:
        err = ClassifiedError(
            kind="invalid_input",
            exit_code=EXIT_CODES["invalid_input"],
            message=str(exc),
            hint=f"{QUERY_MEMORY_LIMIT_ENV} takes a size such as 6GB or 512MiB; "
            f"{QUERY_THREADS_ENV} takes a positive integer",
        )
        emit_error(err, fmt)
        raise SystemExit(err.exit_code) from exc

    import duckdb

    from atif_cli.duck_errors import REGISTRATION_ERRORS, classify_registration_error
    from atif_duck.infrastructure.registry import analytics_roots, register
    from atif_embed.infrastructure.settings import EmbedSettings

    settings = _corpus_settings(None, corpus_root, agent)
    embed_settings = EmbedSettings()
    expected_model, expected_dim = embed_settings.expected_embedding_identity()
    lance_uri = embed_settings.resolve_lance_uri(settings.corpus_root)
    if all_corpora and not lake:
        err = ClassifiedError(
            kind="invalid_input",
            exit_code=EXIT_CODES["invalid_input"],
            message="--all-corpora reads the lake, and --no-lake turns it off",
            hint="drop one of the two flags",
        )
        emit_error(err, fmt)
        raise SystemExit(err.exit_code)

    with _private_spill_dir() as spill_dir:
        con = duckdb.connect()
        lake_reader = None
        try:
            try:
                _configure_query_resources(con, resources, spill_dir)
                lake_reader = (
                    _attach_query_lake(con, settings.corpus_root, all_corpora=all_corpora, fmt=fmt)
                    if lake
                    else None
                )
                sources = register(
                    con,
                    settings.corpus_root,
                    lance_uri=lance_uri,
                    expected_model=expected_model,
                    expected_dim=expected_dim,
                    lake=lake_reader,
                    lance_uris=_embedding_stores(lake_reader, embed_settings),
                )
                _harden_query_connection(
                    con,
                    corpus_root=settings.corpus_root,
                    spill_dir=spill_dir,
                    columnar_paths=sources.lazy_read_paths,
                    analytics_roots=analytics_roots(settings.corpus_root, lake_reader),
                )
                refused = _refused_statement_kinds(con, sql)
                if refused:
                    err = ClassifiedError(
                        kind="sandbox_refused",
                        exit_code=EXIT_CODES["sandbox_refused"],
                        message=f"the query sandbox does not run {', '.join(refused)} statements",
                        hint="query runs SELECT and in-memory DDL/DML only; COPY, EXPORT, "
                        "ATTACH, DETACH, INSTALL, LOAD, PREPARE and EXECUTE are refused "
                        "because they reach files or defer a statement past this check",
                    )
                    emit_error(err, fmt)
                    raise SystemExit(err.exit_code)
                cursor = con.execute(sql)
            except REGISTRATION_ERRORS as exc:
                err = _per_session_memory_hint(classify_registration_error(exc), exc, lake_reader)
                emit_error(err, fmt)
                raise SystemExit(err.exit_code) from exc
            try:
                emit_cursor(cursor, fmt)
            except REGISTRATION_ERRORS as exc:
                err = _per_session_memory_hint(classify_registration_error(exc), exc, lake_reader)
                emit_error(err, fmt)
                raise SystemExit(err.exit_code) from exc
        finally:
            con.close()


#: The hint a query that ran out of memory on the per-session path carries.
PER_SESSION_MEMORY_HINT = (
    "this query read the per-session files because no usable lake holds the corpus, "
    "and that path holds every document it reads in memory; the lake answers it in "
    "bounded memory: run `atif-sql lake rebuild` (or `atif-sql materialize`), or raise "
    f"{QUERY_MEMORY_LIMIT_ENV}"
)


def _per_session_memory_hint(
    err: ClassifiedError, exc: Exception, lake_reader: object | None
) -> ClassifiedError:
    """Point an out-of-memory error on the per-session path at the lake.

    The per-session reader loads each trajectory whole through DuckDB's JSON
    reader, so a full scan of a large corpus can exceed the query cap where
    the lake, reading typed parquet, doesn't.
    """
    import dataclasses

    import duckdb

    if lake_reader is not None or not isinstance(exc, duckdb.OutOfMemoryException):
        return err
    return dataclasses.replace(err, hint=PER_SESSION_MEMORY_HINT)


def _embedding_stores(lake_reader: Any, embed_settings: Any) -> list[Path] | None:
    """Every lake corpus's embeddings store, for a connection scoped to all corpora.

    ``None`` (bind the selected corpus's store, as before) unless the lake
    reader spans every corpus and the stores follow the per-corpus layout;
    ``ATIF_SQL_LANCE_URI`` names one store for all of them, so there is
    nothing to join.
    """
    if (
        lake_reader is None
        or lake_reader.corpus is not None
        or embed_settings.lance_uri is not None
    ):
        return None
    from atif_duck.infrastructure.lake import LakeLayout, registered_corpora
    from atif_duck.infrastructure.lake_settings import LakeSettings

    corpora = registered_corpora(LakeLayout(LakeSettings().lake_root))
    stores: list[Path] = [
        embed_settings.resolve_lance_uri(corpora[name].root) for name in sorted(corpora)
    ]
    return stores or None


def _attach_query_lake(
    con: Any, corpus_root: Path, *, all_corpora: bool, fmt: OutputFormat, command: str = "query"
) -> Any:
    """Attach the lake for ``query``, or warn once and return ``None`` for the per-session path.

    With ``all_corpora`` there is no per-session equivalent, so an unusable
    lake exits 78 instead. A corpus whose last pass left sessions pending
    for the lake still reads the lake, after a warning naming how many.
    """
    from loguru import logger

    from atif_corpus.application.materialize import read_sink_pending
    from atif_corpus.domain.layout import CorpusLayout
    from atif_duck.infrastructure.lake import LakeLayout, LakeReader, attach_lake_for_query
    from atif_duck.infrastructure.lake_settings import LakeSettings

    attached = attach_lake_for_query(
        con,
        LakeLayout(LakeSettings().lake_root),
        corpus_root=corpus_root,
        all_corpora=all_corpora,
    )
    if not isinstance(attached, LakeReader):
        if all_corpora:
            err = ClassifiedError(
                kind="lake_unavailable",
                exit_code=EXIT_CODES["lake_unavailable"],
                message=f"--all-corpora needs the lake: {attached.reason}",
                hint="run `atif-sql lake rebuild`",
            )
            emit_error(err, fmt)
            raise SystemExit(err.exit_code)
        logger.warning(
            "{}: {}; reading the per-session artifacts instead", command, attached.reason
        )
        return None
    if attached.corpus is not None:
        pending = read_sink_pending(CorpusLayout(corpus_root=corpus_root).sink_pending_path)
        if pending:
            logger.warning(
                "{}: {} session(s) of this corpus are pending a lake write; "
                "their rows show the previous materialize until the next pass lands",
                command,
                len(pending),
            )
    return attached


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@app.command
def analyze(
    *,
    since_days: int | None = 30,
    limit: int | None = None,
    max_sessions: Annotated[int | None, cyclopts.Parameter(name="--max-sessions")] = None,
    max_cost_usd: Annotated[float | None, cyclopts.Parameter(name="--max-cost-usd")] = None,
    no_dry_run: Annotated[bool, cyclopts.Parameter(name="--no-dry-run")] = False,
    llm_only: bool = False,
    skip_classify: bool = False,
    skip_conflicts: bool = False,
    skip_friction: bool = False,
    skip_perceived: bool = False,
    corpus_root: Path | None = None,
    lake: Annotated[bool, cyclopts.Parameter(negative="--no-lake")] = True,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Run the four LLM analytics pipelines: classify, conflicts, friction, perceived.

    Defaults to a DRY RUN (plan dicts + cost estimates, zero LLM spend);
    pass ``--no-dry-run`` to execute them. ``--skip-<stage>`` subtracts
    individual stages. The deterministic surfaces (``user_steps``,
    ``human_turns``, ``session_outcomes``) are views, so they need no run.

    Session data comes from the lake when it holds the corpus (a session
    whose lake rows are not its current artifacts is read from its files),
    and from the per-session files otherwise, after one warning. The
    summary's ``session_source`` says which. Outputs still land under the
    corpus's ``analytics/`` directory either way.

    Parameters
    ----------
    since_days
        Restrict the stages to sessions whose last step is within N days
        (default 30).
    limit
        Cap the number of sessions (newest-first) per stage.
    max_sessions
        Hard per-run session ceiling per LLM pipeline (newest-first wins);
        overrides ``ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN`` (default 50).
    max_cost_usd
        Hard per-run dollar ceiling across all LLM pipelines, checked against
        running actual usage; overrides ``ATIF_SQL_LLM_MAX_COST_USD_PER_RUN``
        (default 25.0). When crossed, remaining LLM work aborts cleanly and
        the summary flags ``budget_exhausted``.
    no_dry_run
        Execute the LLM stages for real (costs money).
    llm_only
        Accepted and ignored: since the structural stages were cut
        (2026-09-27) every stage is an LLM stage. Kept so existing crontab
        lines and scripts keep parsing.
    skip_classify, skip_conflicts, skip_friction, skip_perceived
        Opt out of one stage.
    corpus_root
        Override the materialized corpus root.
    lake
        Read session data from the lake (default). ``--no-lake`` reads the
        per-session files.
    fmt
        Summary format; ``auto`` = JSON on a pipe.
    """
    _refuse_root("analyze", fmt)
    del llm_only  # a no-op flag; see the docstring

    from atif_analytics.application.analyze import run_analyze
    from atif_analytics.infrastructure.settings import AnalyticsSettings

    settings = AnalyticsSettings()
    if corpus_root is None:
        # Reuse the corpus-root resolution the other commands share so
        # ATIF_SQL_SOURCE_ROOT/-CORPUS_ROOT behave identically here.
        settings = settings.model_copy(
            update={"corpus_root": _corpus_settings(None, None).corpus_root}
        )
    else:
        settings = settings.model_copy(update={"corpus_root": corpus_root})
    # Budget ceilings: explicit flags beat env/defaults so the crontab line
    # carries the spend cap visibly (CONTRACT-V2 §Cost guards).
    if max_sessions is not None:
        settings = settings.model_copy(update={"llm_max_sessions_per_run": max_sessions})
    if max_cost_usd is not None:
        settings = settings.model_copy(update={"llm_max_cost_usd_per_run": max_cost_usd})

    source = _analyze_lake_source(settings.corpus_root) if lake else None
    try:
        summary = run_analyze(
            settings,
            since_days=since_days,
            limit=limit,
            dry_run=not no_dry_run,
            skip_classify=skip_classify,
            skip_conflicts=skip_conflicts,
            skip_friction=skip_friction,
            skip_perceived=skip_perceived,
            source=source,
        )
    finally:
        if source is not None:
            source.close()
    if source is not None:
        summary["sessions_read_from_files"] = len(source.from_files)
        summary["lake_read_failed"] = source.failed
    emit_json(summary, fmt)


def _analyze_lake_source(corpus_root: Path) -> Any:
    """The lake's session source for ``corpus_root``, or ``None`` (after a warning) to read the files."""
    from loguru import logger

    from atif_cli.lake_sessions import LakeSessionSource
    from atif_duck.infrastructure.lake import LakeLayout
    from atif_duck.infrastructure.lake_settings import LakeSettings

    opened = LakeSessionSource.open(
        LakeLayout(LakeSettings().lake_root),
        corpus_root,
        memory_limit_bytes=query_memory_limit_bytes(),
    )
    if not isinstance(opened, LakeSessionSource):
        logger.warning("analyze: {}; reading the per-session artifacts instead", opened.reason)
        return None
    return opened


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------


def _install_lance_extension(fmt: OutputFormat, *, quiet: bool = False) -> None:
    """``INSTALL lance; LOAD lance`` on a throwaway connection.

    The one network reach outside Bedrock, so it lives behind ``embed``.
    With ``quiet`` (a real embed run) a failure is a warning rather than an
    exit: the backfill itself still runs, and ``status`` will keep saying
    the extension is missing until a later attempt succeeds. Without
    ``quiet`` (``--install-extension``) the result is emitted as JSON and a
    failure exits 70.
    """
    import duckdb

    from atif_cli.duck_errors import classify_duckdb_error
    from atif_duck.infrastructure.registry import install_lance_extension

    con = duckdb.connect()
    try:
        try:
            install_path = install_lance_extension(con)
        except duckdb.Error as exc:
            if quiet:
                from loguru import logger

                logger.warning(
                    "Could not install the lance DuckDB extension ({}); vector search stays "
                    "unavailable until `atif-sql embed --install-extension` succeeds",
                    str(exc).splitlines()[0],
                )
                return
            err = classify_duckdb_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc
    finally:
        con.close()
    if not quiet:
        emit_json({"extension": "lance", "installed": True, "install_path": install_path}, fmt)


@app.command
def embed(
    *,
    limit: int | None = None,
    all_steps: Annotated[bool, cyclopts.Parameter(name="--all")] = False,
    dry_run: bool | None = None,
    install_extension: Annotated[bool, cyclopts.Parameter(name="--install-extension")] = False,
    prune_orphans: Annotated[bool, cyclopts.Parameter(name="--prune-orphans")] = False,
    lake: Annotated[bool, cyclopts.Parameter(negative="--no-lake")] = True,
    corpus_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Embed unembedded corpus steps with Cohere Embed v4 and append to LanceDB.

    Discovery
    ---------
    When the DuckLake at ``ATIF_SQL_LAKE_ROOT`` holds this corpus (current
    schema, same directory), the steps to embed are read from its ``steps``
    table rather than from every session's ``trajectory.json``, and only the
    rows changed since the last complete run: the lake snapshot that run
    reached is kept in ``lake_watermark.json`` inside the store directory.
    A run cut short by ``--limit``, or one where any row failed to embed,
    leaves the watermark where it was. Without a usable lake, or with
    ``--no-lake``, discovery reads the per-session artifacts as before. The
    dry-run plan's ``discovery`` says which path ran (``corpus``,
    ``lake-full``, ``lake-incremental``, ``lake-unchanged``).

    Orphans
    -------
    ``--prune-orphans`` counts the stored rows whose uuid is no step's
    primary uuid in the lake (sessions deleted before the corpus kept them,
    re-keyed steps). It's a dry run unless ``--no-dry-run`` is given too,
    and it never calls Bedrock. It needs the lake (exit 78 without one), and
    it refuses to delete while any session of the corpus is still pending a
    lake write (exit 78), since those sessions' rows would look orphaned.
    Output: ``{pipeline, lake_snapshot, stored, lake_keys, orphans, deleted,
    dry_run}``.

    Extension
    ---------
    ``query`` and ``search`` read the store through DuckDB's lance extension
    and never install it themselves (installing is a 242 MB download from
    the extension repository, and ``query`` runs unattended). It is
    installed here, where the network is already a deliberate act: every
    REAL run installs it first, and ``--install-extension`` installs it and
    exits without touching Bedrock or the store (``{"extension": "lance",
    "installed": true, "install_path": ...}``). ``atif-sql status`` reports
    whether it is present.

    Cost
    ----
    Calls Bedrock (``global.cohere.embed-v4:0``) on every unembedded step
    (main + sidechain text >= 32 chars, keyed by the step's primary
    source uuid). A REAL run requires an explicit scope: either ``--limit N``
    or ``--all`` — a bare ``atif-sql embed`` exits 64 with a hint, so an
    accidental full backfill (potentially every step ever recorded) can't
    happen from a mistyped command. ``--dry-run`` needs no scope: it spends
    nothing and previews the full plan.

    Flags
    -----
    --limit N       Cap the number of steps embedded this run.
    --all           Explicitly embed EVERY unembedded step (full backfill).
    --dry-run       Preview only; emit plan JSON, no embedding calls.
    --install-extension  Install the lance DuckDB extension and exit (no Bedrock).
    --prune-orphans Count (and with --no-dry-run, delete) rows no lake step names.
    --no-lake       Discover from the per-session artifacts even when a lake exists.
    --corpus-root   Override the materialized corpus root.

    Output
    ------
    Dry run: the plan JSON ``{pipeline, discovery, candidates, batches,
    batch_size, concurrency, model, limit, dry_run}``. Real run:
    ``{"pipeline": "embed", "rows_processed": N, "dry_run": false}``.

    Exit codes: 0 success, 64 missing --limit/--all, 70 runtime
    (Bedrock / DuckDB / Lance failure — transient, safe to retry), 78 terminal
    state (the store or its config requires operator action; retrying without
    intervention cannot succeed, so unattended lanes suppress retries on 78).
    """
    if install_extension:
        _install_lance_extension(fmt)
        return
    if prune_orphans:
        _prune_orphans(corpus_root, dry_run=dry_run is not False, fmt=fmt)
        return

    import asyncio

    from atif_embed.application.embed import run_backfill
    from atif_embed.domain.errors import DomainError
    from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows
    from atif_embed.infrastructure.settings import EmbedSettings

    dry_run = bool(dry_run)
    if not dry_run and limit is None and not all_steps:
        emit_error(
            ClassifiedError(
                kind="invalid_input",
                exit_code=EXIT_CODES["invalid_input"],
                message="a real embed run needs an explicit scope",
                hint="pass --limit N (bounded) or --all (full backfill), or preview with --dry-run",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["invalid_input"])

    settings = _corpus_settings(None, corpus_root)
    embed_settings = EmbedSettings()
    if not dry_run:
        _install_lance_extension(fmt, quiet=True)
    text_rows: Any = DuckDbTextRows()
    if lake:
        from atif_cli.embed_lake import lake_steps_port
        from atif_embed.infrastructure.lake_text_rows import LAKE_WATERMARK_FILE, LakeTextRows

        text_rows = LakeTextRows(
            lake=lake_steps_port(),
            watermark_path=embed_settings.resolve_lance_uri(settings.corpus_root)
            / LAKE_WATERMARK_FILE,
            fallback=text_rows,
        )
    try:
        result = asyncio.run(
            run_backfill(
                corpus_root=settings.corpus_root,
                settings=embed_settings,
                text_rows=text_rows,
                limit=limit,
                dry_run=dry_run,
            )
        )
    except DomainError as exc:
        # Terminal vs transient decides the exit code: a terminal state
        # (provider mismatch, unhealable schema) needs an operator, and the
        # refresh lane suppresses retries on 78 instead of re-running an
        # identical failure every tick.
        kind = "terminal_state" if exc.terminal else "runtime_error"
        emit_error(
            ClassifiedError(
                kind=kind,
                exit_code=EXIT_CODES[kind],
                message=str(exc),
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES[kind]) from exc
    if isinstance(result, dict):
        emit_json(result, fmt)
    else:
        emit_json({"pipeline": "embed", "rows_processed": result, "dry_run": False}, fmt)


def _prune_orphans(corpus_root: Path | None, *, dry_run: bool, fmt: OutputFormat) -> None:
    """``embed --prune-orphans``: count, and unless ``dry_run`` delete, the store's orphan rows."""
    from atif_cli.embed_lake import lake_steps_port
    from atif_corpus.application.materialize import read_sink_pending
    from atif_corpus.domain.layout import CorpusLayout
    from atif_embed.application.prune import prune_orphans
    from atif_embed.infrastructure.lance_store import LanceVectorStore
    from atif_embed.infrastructure.settings import EmbedSettings

    settings = _corpus_settings(None, corpus_root)
    embed_settings = EmbedSettings()
    lance_uri = embed_settings.resolve_lance_uri(settings.corpus_root)
    pending = read_sink_pending(CorpusLayout(corpus_root=settings.corpus_root).sink_pending_path)
    if pending and not dry_run:
        emit_error(
            ClassifiedError(
                kind="lake_unavailable",
                exit_code=EXIT_CODES["lake_unavailable"],
                message=f"{len(pending)} session(s) of this corpus are pending a lake write, "
                "so their stored rows would look orphaned",
                hint="let the next materialize pass land them, then prune again",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["lake_unavailable"])
    report = prune_orphans(
        settings.corpus_root,
        lake=lake_steps_port(),
        store=LanceVectorStore(lance_uri, dim=int(embed_settings.output_dimension)),
        dry_run=dry_run,
    )
    if report is None:
        emit_error(
            ClassifiedError(
                kind="lake_unavailable",
                exit_code=EXIT_CODES["lake_unavailable"],
                message="--prune-orphans checks the store against the lake, and there is no "
                "usable lake for this corpus",
                hint="run `atif-sql lake rebuild`",
            ),
            fmt,
        )
        raise SystemExit(EXIT_CODES["lake_unavailable"])
    report["pending_sessions"] = len(pending)
    emit_json(report, fmt)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@app.command
def search(
    query_text: str,
    /,
    *,
    k: Annotated[int, cyclopts.Parameter(name=["-k", "--k"])] = 10,
    session_id: str | None = None,
    corpus_root: Path | None = None,
    all_corpora: bool = False,
    lake: Annotated[bool, cyclopts.Parameter(negative="--no-lake")] = True,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Semantic top-k nearest-neighbor search over step embeddings.

    Pipeline
    --------
    1. Embed ``query_text`` with Cohere Embed v4 ``search_query`` mode (float).
    2. DuckDB cosine-kNN against the Lance-backed ``message_embeddings`` view
       (``register_vss`` guard-before-bind: a store written by a different
       provider raises instead of returning garbage scores).
    3. Join back to ``steps`` via ``source_uuids[0]`` for a 200-char snippet.

    Where the steps come from
    -------------------------
    Like ``query``: the lake's ``steps`` table when the lake holds this
    corpus, else (after a one-line warning, or with ``--no-lake``) the
    per-session artifacts. ``--all-corpora`` searches every corpus the lake
    holds, each through its own store, and adds a ``corpus`` column; it
    needs the lake (exit 78 without one).

    Prereq
    ------
    The Lance store must exist. If it's empty or missing, the command exits
    with code 2 and a hint. Run ``atif-sql embed --all --no-dry-run`` to
    populate.

    Flags
    -----
    --k N            Top-k (default 10).
    --session-id ID  Confine the kNN to one session.
    --corpus-root    Override the materialized corpus root.
    --all-corpora    Search every corpus the lake holds.
    --no-lake        Read the per-session artifacts even when a lake exists.

    Output columns
    --------------
    uuid, session_id, snippet, sim (cosine similarity ∈ [-1, 1]), plus
    corpus under ``--all-corpora``. Sorted by cosine distance ascending —
    highest sim first.

    Exit codes: 0 success, 2 no_embeddings, 64 --all-corpora with --no-lake,
    65 embedding_mismatch (the store was written by another provider), 70
    runtime, 77 root_refused (uid 0 without ``ATIF_SQL_ALLOW_ROOT=1``), 78
    extension_missing (a store exists but the lance DuckDB extension is not
    installed; run ``atif-sql embed --install-extension``) or
    lake_unavailable (``--all-corpora`` without a usable lake).
    """
    _refuse_root("search", fmt)

    import duckdb

    from atif_cli.duck_errors import (
        REGISTRATION_ERRORS,
        classify_duckdb_error,
        classify_registration_error,
    )
    from atif_duck.infrastructure.registry import lance_extension_installed, register
    from atif_embed.application.embed import embed_query
    from atif_embed.infrastructure.settings import EmbedSettings

    if all_corpora and not lake:
        err = ClassifiedError(
            kind="invalid_input",
            exit_code=EXIT_CODES["invalid_input"],
            message="--all-corpora reads the lake, and --no-lake turns it off",
            hint="drop one of the two flags",
        )
        emit_error(err, fmt)
        raise SystemExit(err.exit_code)

    settings = _corpus_settings(None, corpus_root)
    embed_settings = EmbedSettings()
    expected_model, expected_dim = embed_settings.expected_embedding_identity()
    lance_uri = embed_settings.resolve_lance_uri(settings.corpus_root)

    con = duckdb.connect(":memory:")
    try:
        try:
            lake_reader = (
                _attach_query_lake(
                    con, settings.corpus_root, all_corpora=all_corpora, fmt=fmt, command="search"
                )
                if lake
                else None
            )
            stores = _embedding_stores(lake_reader, embed_settings)
            register(
                con,
                settings.corpus_root,
                lance_uri=lance_uri,
                expected_model=expected_model,
                expected_dim=expected_dim,
                lake=lake_reader,
                lance_uris=stores,
            )
        except REGISTRATION_ERRORS as exc:
            err = classify_registration_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc

        if any(uri.is_dir() for uri in stores or [lance_uri]) and not lance_extension_installed(
            con
        ):
            emit_error(
                ClassifiedError(
                    kind="extension_missing",
                    exit_code=EXIT_CODES["extension_missing"],
                    message="the lance DuckDB extension is not installed, so the embeddings "
                    f"store at {lance_uri} cannot be read",
                    hint="run: atif-sql embed --install-extension (a one-time download)",
                ),
                fmt,
            )
            raise SystemExit(EXIT_CODES["extension_missing"])

        row = con.execute("SELECT count(*) FROM message_embeddings").fetchone()
        if not row or int(row[0]) == 0:
            emit_error(
                ClassifiedError(
                    kind="no_embeddings",
                    exit_code=EXIT_CODES["no_embeddings"],
                    message="No embeddings yet.",
                    hint="Run: atif-sql embed --all --no-dry-run (or --limit N)",
                ),
                fmt,
            )
            raise SystemExit(EXIT_CODES["no_embeddings"])

        qv = embed_query(query_text, settings=embed_settings)
        try:
            columns, rows = search_rows(
                con, qv, k=k, session_id=session_id, with_corpus=all_corpora
            )
        except duckdb.Error as exc:
            err = classify_duckdb_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc
        emit_rows(columns, rows, fmt)
    finally:
        con.close()


#: ``search``'s kNN, one text per shape. Rank by cosine similarity
#: descending: ORDER BY array_cosine_distance (== 1 - sim) ASC is what
#: triggers the cosine HNSW index lookup. Using array_distance here (L2) would
#: silently bypass the index AND give wrong ranks: the raw int8-cast-to-float
#: document vectors have magnitudes in the thousands while the query vector is
#: unit-normalized — only cosine is magnitude-invariant. ``{dim}`` is the only
#: placeholder, filled with ``int(len(vector))``.
_SEARCH_SQL = """
    WITH qv AS (SELECT CAST(? AS FLOAT[{dim}]) AS v)
    SELECT me.uuid                                                     AS uuid,
           s.session_id                                                AS session_id,
           substr(s.message, 1, 200)                                   AS snippet,
           array_cosine_similarity(me.embedding, (SELECT v FROM qv))   AS sim{corpus}
    FROM message_embeddings me
    JOIN steps s
      ON json_extract_string(s.source_uuids, '$[0]') = me.uuid{corpus_join}
    {session_filter}
    ORDER BY array_cosine_distance(me.embedding, (SELECT v FROM qv)) ASC
    LIMIT ?
"""


def search_rows(
    con: Any,
    query_vector: list[float],
    *,
    k: int,
    session_id: str | None = None,
    with_corpus: bool = False,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run ``search``'s kNN on a registered connection; return ``(columns, rows)``.

    Separate from the command so the same statement runs over a query vector
    the caller already has (the tests' fixed vectors; no Bedrock call).
    """
    params: list[object] = [query_vector]
    if session_id is not None:
        params.append(session_id)
    params.append(k)
    sql = _SEARCH_SQL.format(
        dim=len(query_vector),
        corpus=",\n           c.corpus                                                    AS corpus"
        if with_corpus
        else "",
        corpus_join="\n    JOIN sessions c ON c.session_id = s.session_id" if with_corpus else "",
        session_filter="WHERE s.session_id = ?" if session_id is not None else "",
    )
    cursor = con.execute(sql, params)
    columns = [d[0] for d in cursor.description or ()]
    return columns, cursor.fetchall()


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------

#: Header line stamped onto the examples output. Honest by construction:
#: packages/atif-duck/tests/test_examples.py executes every one of these
#: statements against the fixture corpus, so a release with a broken example
#: cannot ship.
_EXAMPLES_NOTE = "test-executed against this version"


@app.command
def examples(
    *,
    category: str | None = None,
    requires: str | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """List tested example queries for every view and macro.

    Examples are DERIVED from the static atif-duck catalog (never hardcoded
    per object) and each one is executed by atif-duck's test suite against a
    fixture corpus — the listing is proof-carrying, not documentation that
    can rot. Answers from :mod:`atif_duck.domain` alone: no DuckDB import,
    no connection, lean-import fast like ``schema``.

    Output
    ------
    TTY: a table grouped by ``requires`` (core / analytics / vss). Piped or
    ``--format json``: ``{"note": ..., "examples": [{name, sql, description,
    requires, category}, ...]}``.

    Flags
    -----
    --category   Filter: view | table-macro | scalar-macro.
    --requires   Filter: core | analytics | vss.

    Exit codes: 0 ok, 64 unknown --category/--requires value.
    """
    from atif_duck.domain.examples import (
        CATEGORY_VALUES,
        REQUIRES_VALUES,
        build_examples,
    )

    for flag, value, allowed in (
        ("--category", category, CATEGORY_VALUES),
        ("--requires", requires, REQUIRES_VALUES),
    ):
        if value is not None and value not in allowed:
            emit_error(
                ClassifiedError(
                    kind="invalid_input",
                    exit_code=EXIT_CODES["invalid_input"],
                    message=f"unknown {flag} value: {value!r}",
                    hint=f"one of: {', '.join(allowed)}",
                ),
                fmt,
            )
            raise SystemExit(EXIT_CODES["invalid_input"])

    selected = [
        example
        for example in build_examples()
        if (category is None or example.category == category)
        and (requires is None or example.requires == requires)
    ]

    if resolve_format(fmt) is OutputFormat.TABLE:
        print(f"# {len(selected)} examples — {_EXAMPLES_NOTE}")
        for group in ("core", "analytics", "vss"):
            grouped = [e for e in selected if e.requires == group]
            if not grouped:
                continue
            print(f"\n[{group}]")
            width = max(len(e.name) for e in grouped)
            for example in grouped:
                print(f"  {example.name:<{width}}  {example.sql}")
                print(f"  {'':<{width}}  # {example.description}")
        return
    emit_json(
        {
            "note": _EXAMPLES_NOTE,
            "examples": [
                {
                    "name": e.name,
                    "sql": e.sql,
                    "description": e.description,
                    "requires": e.requires,
                    "category": e.category,
                }
                for e in selected
            ],
        },
        fmt,
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


@app.command
def schema(
    *,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """List every view (with columns) and every macro signature, with what each requires.

    The canonical catalog for composing ``query`` calls. Answers from the
    static :mod:`atif_duck.domain.catalog` dicts (core :data:`VIEW_SCHEMA`
    and :data:`MACRO_SIGNATURES`, then :data:`ANALYTICS_VIEW_SCHEMA` and
    :data:`ANALYTICS_MACRO_SIGNATURES`) with no DuckDB import, no
    connection, no view registration; sub-50ms by construction (drift
    against the real DDL is caught by atif-duck's CI tests).

    Each object carries ``requires``: ``core`` binds on any corpus,
    ``analytics`` once ``atif-sql analyze`` has written its parquet, ``vss``
    once ``atif-sql embed`` has built the store.
    """
    from atif_duck.domain.catalog import (
        ANALYTICS_MACRO_SIGNATURES,
        ANALYTICS_VIEW_SCHEMA,
        MACRO_SIGNATURES,
        VIEW_SCHEMA,
    )
    from atif_duck.domain.examples import object_requires

    views = {**VIEW_SCHEMA, **ANALYTICS_VIEW_SCHEMA}
    macros = {**MACRO_SIGNATURES, **ANALYTICS_MACRO_SIGNATURES}
    examples_hint = "tested example queries: atif-sql examples (or: atif-sql query --examples)"
    if resolve_format(fmt) is OutputFormat.TABLE:
        for name, cols in views.items():
            print(f"\n{name} ({len(cols)} cols, requires: {object_requires(name)})")
            for col, col_type in cols:
                print(f"  {col:<28} {col_type}")
        print(f"\nMacros ({len(macros)})")
        for macro, params in macros.items():
            call = f"{macro}({', '.join(params)})"
            print(f"  {call:<44} requires: {object_requires(macro)}")
        print(f"\n{examples_hint}")
        return
    emit_json(
        {
            "views": {
                name: [{"column": c, "type": t} for c, t in cols] for name, cols in views.items()
            },
            "view_requires": {name: object_requires(name) for name in views},
            "macros": [
                {"name": n, "params": list(p), "requires": object_requires(n)}
                for n, p in macros.items()
            ],
            "examples_hint": examples_hint,
        },
        fmt,
    )


#: Env var selecting the stderr log level. `logger.remove()` below drops the
#: default handler, and with it the `LOGURU_LEVEL` that would have
#: parameterized it, so this is the ONE knob reaching the workspace's INFO and
#: DEBUG surface — measured 2026-08-28 over `packages/*/src`, 110 `logger.info`
#: and 41 `logger.debug` sites that are otherwise unreachable through the CLI.
#: Named to the pydantic-settings prefix every other setting uses.
LOG_LEVEL_ENV: str = "ATIF_SQL_LOG_LEVEL"
DEFAULT_LOG_LEVEL: str = "WARNING"


def _stderr_log_level() -> str:
    """The level :func:`main` gives its stderr sink, read from the environment.

    Upper-cased, and an unset or blank value reads as the default rather than
    as a request for level ``""``.
    """
    return os.environ.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).strip().upper() or DEFAULT_LOG_LEVEL


def main() -> None:
    """Console-script entry point.

    Default stderr stays quiet for routine reads: loguru's default DEBUG sink
    is replaced with WARNING-and-up, so a piped read emits data on stdout and
    nothing on stderr unless something is actually wrong. Set
    :data:`LOG_LEVEL_ENV` to widen it.

    An unknown level falls back to WARNING and says so, rather than replacing
    the caller's command with a loguru traceback — a bad log level is not a
    reason to refuse to run.
    """
    from loguru import logger

    level = _stderr_log_level()
    logger.remove()
    try:
        logger.add(sys.stderr, level=level)
    except ValueError:
        logger.add(sys.stderr, level=DEFAULT_LOG_LEVEL)
        logger.warning(
            "{}={!r} is not a loguru level; using {}", LOG_LEVEL_ENV, level, DEFAULT_LOG_LEVEL
        )
    app()


__all__ = [
    "EXIT_CODES",
    "ClassifiedError",
    "analyze",
    "app",
    "convert",
    "embed",
    "examples",
    "main",
    "materialize",
    "query",
    "schema",
    "search",
    "status",
]
