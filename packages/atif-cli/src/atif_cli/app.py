# SPDX-License-Identifier: Apache-2.0

"""The atif-sql CLI (cyclopts) — the workspace's composition root.

The ONLY package that may import another workspace member, and it imports
five: atif-converter, atif-corpus, atif-duck, atif-embed, and atif-analytics.
Those five may never import each other, so every cross-package seam (the
ConverterPort adapter, the clock, version pins, the DuckDB connection) is
wired here, per docs/CONTRACT.md §CLI.

The ten registered commands — nine ``@app.command`` functions plus the
``cron`` sub-app:

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

A command that reaches Bedrock is marked above because its spend is not
recoverable. Each one guards itself differently: ``analyze`` defaults to a DRY
RUN that only plans and estimates, a bare ``embed`` exits 64 rather than
choosing a scope for the caller, and ``search`` embeds exactly one query
string per invocation.

Two agents
----------
``convert``, ``materialize`` and ``status`` take ``--agent claude-code|codex``.
The flag picks three things at once and they must move together: which harbor
adapter converts a transcript, which discovery layout finds one, and which
source root and corpus slug the settings default to. One corpus root therefore
holds exactly one agent's sessions, which is what lets everything downstream —
the DuckDB views, the analytics pipelines, the embedding store — stay unaware
that a second agent exists.

Agent-friendly defaults
-----------------------
* ``--format auto`` emits a human table on a TTY and JSON on a pipe.
* DuckDB errors classify into parse/catalog/runtime -> exit 64/65/70 with a
  JSON error envelope on non-TTY (see :mod:`atif_cli.errors`).

Heavy imports (duckdb, harbor via atif_converter, pydantic via atif_corpus)
are DEFERRED into the command bodies that use them so the fast path
(``schema`` / ``--help`` / ``--version``) stays on a lean import graph —
pinned by the fresh-interpreter lean-import test in ``tests/test_lean_import``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import cyclopts

from atif_cli.cron import cron_app
from atif_cli.errors import EXIT_CODES, ClassifiedError
from atif_cli.output import (
    OutputFormat,
    emit_cursor,
    emit_error,
    emit_json,
    emit_rows,
    resolve_format,
)

if TYPE_CHECKING:
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


#: Lower bound for ``query``'s DuckDB memory cap. Registration alone parses
#: the corpus through 1 GiB-per-object JSON buffers on every thread, and the
#: base views (``tool_calls``, ``tool_rank``) need several GiB more on a
#: multi-GB corpus — a tighter cap turns working queries into OutOfMemory.
_QUERY_MEMORY_FLOOR_BYTES = 8 * 1024**3


def _query_memory_limit_bytes() -> int:
    """Bytes to cap ``query``'s DuckDB heap at.

    Half of physical RAM, but never below :data:`_QUERY_MEMORY_FLOOR_BYTES`
    and never above DuckDB's own 80%-of-RAM default — on a small host the
    default is already the tighter of the two, and raising it would make an
    unbounded query worse rather than better.
    """
    import os

    physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return min(int(physical * 0.8), max(int(physical * 0.5), _QUERY_MEMORY_FLOOR_BYTES))


#: Config options caller SQL may still change after the sandbox locks. Only
#: ``TimeZone``: rendering timestamps in the reader's zone is an ordinary
#: analytics need and grants no filesystem or network reach. Everything else
#: (``threads``, ``errors_as_json``, ``memory_limit``, the allowlists) stays
#: frozen, because a widenable allowlist is not an allowlist.
_QUERY_UNLOCKED_CONFIGS: tuple[str, ...] = ("TimeZone",)


def _lazy_read_paths(corpus_root: Path) -> list[Path]:
    """Corpus files a registered view reads LAZILY, at caller-query time.

    ``register_raw`` materializes the session artifacts into TEMP TABLEs, and
    ``register_vss`` holds the Lance store open through an ATTACH, so both
    survive the sandbox with no allowlist entry. The analytics views are
    ``read_parquet`` over files on disk, bound but not read until the caller
    selects from them — those paths are the ones that must stay reachable.
    """
    return sorted(p for p in (corpus_root / "analytics").rglob("*.parquet") if p.is_file())


def _harden_query_connection(
    con: Any,
    *,
    corpus_root: Path,
    temp_dir: Path,
) -> None:
    """Sandbox a fully-registered connection before it runs caller SQL.

    ``query`` executes SQL an agent composed while reading third-party text
    out of the transcript corpus, so the statement is untrusted: without this
    it reaches ``read_text`` on any file the user can read, ``COPY`` to any
    path, ``ATTACH`` of unrelated databases, and ``INSTALL httpfs`` for
    network egress.

    Reach is granted at two granularities, and the split is what keeps
    injected SQL from overwriting the corpus it was read from:

    * ``allowed_directories`` holds ONLY ``temp_dir`` — the spill area, which
      must accept writes for a query that exceeds ``memory_limit`` to
      complete at all. It sits inside the corpus root but holds no corpus
      data.
    * ``allowed_paths`` holds the individual analytics parquets from
      :func:`_lazy_read_paths`. A file grant lets ``read_parquet`` open that
      exact path; because ``COPY`` writes through a sibling ``tmp_<name>``
      file, which is a different path, the grant does not carry a write.
      Nothing else under the corpus is named, so ``COPY`` over a
      ``trajectory.json`` or into a new ``pwned.csv`` is refused.

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
    con.execute(f"SET temp_directory={_sql_str(str(temp_dir))}")
    con.execute(f"SET memory_limit='{_query_memory_limit_bytes()}B'")
    con.execute(f"SET allowed_directories=[{_sql_str(str(temp_dir))}]")
    lazy_paths = ", ".join(_sql_str(str(path)) for path in _lazy_read_paths(corpus_root))
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
    a DEFERRED import (it comes from atif-converter, which drags harbor), and
    the lean-import test pins that harbor stays out of the fast path. So the
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
    up-to-date, not skipped-live, not failed, and never ghosted — so a pass
    that could only stat nothing prints all zeroes and reads as an idle,
    complete corpus unless ``unreadable`` is shown alongside them.
    """
    payload = {
        "materialized": report.materialized_count,
        "up_to_date": report.up_to_date_count,
        "skipped_live": report.skipped_live_count,
        "failed": report.failed_count,
        "failures": [{"session_id": f.session_id, "error": f.error} for f in report.failures],
        "removed": report.sessions_removed,
        "removed_session_ids": list(report.removed_session_ids),
        "unreadable": report.unreadable_count,
        "unreadable_session_ids": list(report.unreadable_session_ids),
        "total_seconds": round(report.total_seconds, 3),
        "convert_seconds": round(report.convert_seconds, 3),
        "workers": report.workers,
    }
    if resolve_format(fmt) is OutputFormat.TABLE:
        print(
            f"materialized: {report.materialized_count}  "
            f"up-to-date: {report.up_to_date_count}  "
            f"skipped-live: {report.skipped_live_count}  "
            f"failed: {report.failed_count}  "
            f"removed: {report.sessions_removed}  "
            f"unreadable: {report.unreadable_count}"
        )
        print(
            f"total: {report.total_seconds:.2f}s  "
            f"(convert: {report.convert_seconds:.2f}s summed over {report.workers} worker(s))"
        )
        for failure in report.failures:
            print(f"  FAILED {failure.session_id}: {failure.error}", file=sys.stderr)
        for session_id in report.unreadable_session_ids:
            print(f"  UNREADABLE {session_id}", file=sys.stderr)
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
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Sync the materialized corpus with the raw transcript corpus.

    Runs one scan → plan → convert → write pass through atif-corpus's
    materialize use case, with atif-converter's real converter adapted
    behind the ConverterPort (see :mod:`atif_cli.converter_adapter`). The
    CLI owns the wall clock and the version pins stamped into ``meta.json``.

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
    fmt
        Report format; ``auto`` = human lines on TTY, JSON on a pipe.
    """
    from atif_cli.converter_adapter import RealConverter
    from atif_corpus.application.materialize import (
        CorpusAgentMismatchError,
        SuspiciousEmptyScanError,
        materialize as materialize_use_case,
    )
    from atif_corpus.domain.source_layout import layout_for

    settings = _corpus_settings(source_root, corpus_root, agent)
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
            harbor_version=_version_of("harbor"),
            converter_version=_version_of("atif-converter"),
            quiesce_seconds=(
                quiesce_seconds if quiesce_seconds is not None else settings.quiesce_seconds
            ),
            force=force,
            session_ids=session_filter,
            workers=worker_count,
            worker_setup=_materialize_worker_setup,
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

    Read-only and fast: scans source mtimes and replays the same pure
    planning decision ``materialize`` would make (quiescence + watermark),
    without converting anything. The staleness summary is therefore exactly
    "what would a materialize pass do right now".

    ``--agent`` selects which corpus is reported, resolving the same way
    ``materialize`` resolves it, so the two commands always describe the same
    pair of roots. The JSON payload carries the resolved agent, which is what
    lets an unattended lane confirm it ticked the corpus it meant to.
    """
    import time

    from atif_corpus.application.materialize import read_watermark
    from atif_corpus.domain.layout import CorpusLayout
    from atif_corpus.domain.sessions import QuiescencePolicy, build_plan
    from atif_corpus.domain.source_layout import layout_for
    from atif_corpus.infrastructure.scanner import scan_source_root

    settings = _corpus_settings(source_root, corpus_root, agent)
    layout = CorpusLayout(corpus_root=settings.corpus_root)
    quiesce = quiesce_seconds if quiesce_seconds is not None else settings.quiesce_seconds

    now_ns = time.time_ns()
    watermark = read_watermark(layout.watermark_path)
    watermark_age_seconds: float | None = None
    if layout.watermark_path.exists():
        watermark_age_seconds = round((now_ns - layout.watermark_path.stat().st_mtime_ns) / 1e9, 1)

    source_sessions = scan_source_root(settings.source_root, layout_for(settings.agent))
    plan = build_plan(
        source_sessions,
        watermark=watermark,
        policy=QuiescencePolicy(quiesce_seconds=quiesce),
        now_ns=now_ns,
    )
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
    }
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
            f"staleness:    {staleness['stale']} stale, "
            f"{staleness['up_to_date']} up-to-date, {staleness['live']} live"
        )
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
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Run one SQL statement against the atif-duck catalog and emit results.

    Opens an in-memory DuckDB connection, registers the corpus views +
    macros via ``atif_duck.register``, executes, and emits: TTY -> plain
    table, pipe -> JSON array of row objects.

    ``--examples`` short-circuits to the ``examples`` listing (same output,
    same ``--category`` / ``--requires`` filters) without opening DuckDB —
    the natural discovery path when an agent is already composing a query.

    Sandbox
    -------
    The statement runs against a hardened connection (see
    :func:`_harden_query_connection`). Reads reach the registered views and
    nothing else; the only writable path is the query engine's own spill
    directory ``<corpus_root>/.duckdb_tmp``. ``read_text`` outside the
    corpus, ``COPY`` anywhere in it (including over a ``trajectory.json``),
    ``ATTACH`` of unrelated databases, and extension installs all fail with
    exit 70 rather than reading credentials, corrupting the corpus, or
    reaching the network. The embedding-store guard binds here exactly as it
    does for ``search``, so a store written by a different provider refuses
    to bind (exit 65) instead of scoring garbage.

    Two residual holes remain, both over recomputable derived data rather
    than the transcript artifacts:

    * ``COPY ... TO '<an existing analytics parquet>' (USE_TMP_FILE false)``
      overwrites it. A plain ``COPY`` to the same path is refused, because
      DuckDB stages it through a sibling ``tmp_<name>`` that carries no
      grant; ``USE_TMP_FILE false`` writes the granted path directly. Re-run
      ``atif-sql analyze`` to rebuild.
    * ``DELETE``/``INSERT`` against ``lance_store.main.embeddings`` reach the
      ATTACHed store, which the filesystem allowlist does not cover. Re-run
      ``atif-sql embed --all --no-dry-run`` to rebuild.

    ``lock_configuration`` freezes the connection's settings, so ``SET`` is
    refused for everything except ``TimeZone``, which stays open for
    timestamp rendering. ``SET threads`` and ``SET errors_as_json`` fail with
    exit 70 — deliberately: an unfreezable option is one injected SQL can
    turn back off.

    Exit codes
    ----------
    * 64  parse_error   malformed SQL (or no SQL and no --examples)
    * 65  catalog_error unknown view/macro/column (try ``atif-sql schema``)
    * 65  embedding_mismatch the Lance store was written by another provider
    * 70  runtime_error everything else (an unmaterialized corpus, or SQL
      the sandbox refused)

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

    import duckdb

    from atif_cli.duck_errors import REGISTRATION_ERRORS, classify_registration_error
    from atif_duck.infrastructure.registry import register
    from atif_embed.infrastructure.settings import EmbedSettings

    settings = _corpus_settings(None, corpus_root, agent)
    embed_settings = EmbedSettings()
    expected_model, expected_dim = embed_settings.expected_embedding_identity()
    lance_uri = embed_settings.resolve_lance_uri(settings.corpus_root)

    con = duckdb.connect()
    try:
        try:
            register(
                con,
                settings.corpus_root,
                lance_uri=lance_uri,
                expected_model=expected_model,
                expected_dim=expected_dim,
            )
            _harden_query_connection(
                con,
                corpus_root=settings.corpus_root,
                temp_dir=settings.corpus_root / ".duckdb_tmp",
            )
            cursor = con.execute(sql)
        except REGISTRATION_ERRORS as exc:
            err = classify_registration_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc
        try:
            emit_cursor(cursor, fmt)
        except REGISTRATION_ERRORS as exc:
            err = classify_registration_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc
    finally:
        con.close()


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
    structural_only: bool = False,
    llm_only: bool = False,
    skip_cluster: bool = False,
    skip_terms: bool = False,
    skip_community: bool = False,
    skip_classify: bool = False,
    skip_trajectory: bool = False,
    skip_conflicts: bool = False,
    skip_friction: bool = False,
    skip_perceived: bool = False,
    force_cluster: bool = False,
    force_community: bool = False,
    corpus_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Run the analytics pipelines: cluster/terms/community + LLM classify/trajectory/conflicts/friction/perceived.

    Defaults to a DRY RUN (plan dicts + cost estimates, zero LLM spend);
    pass ``--no-dry-run`` to execute the LLM stages. ``--structural-only`` /
    ``--llm-only`` select the cron lanes; ``--skip-<stage>`` subtracts
    individual stages.

    Parameters
    ----------
    since_days
        Restrict LLM stages to sessions whose last step is within N days
        (default 30; structural stages always run over the full store).
    limit
        Cap the number of sessions (newest-first) per LLM stage.
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
    structural_only
        Run only cluster/terms/community (the :17 cron lane).
    llm_only
        Run only classify/trajectory/conflicts/friction/perceived (the
        nightly lane).
    skip_cluster, skip_terms, skip_community, skip_classify,
    skip_trajectory, skip_conflicts, skip_friction, skip_perceived
        Opt out of one stage.
    force_cluster, force_community
        Recompute even when the mtime sidecar says the input is unchanged.
    corpus_root
        Override the materialized corpus root.
    fmt
        Summary format; ``auto`` = JSON on a pipe.
    """
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

    summary = run_analyze(
        settings,
        since_days=since_days,
        limit=limit,
        dry_run=not no_dry_run,
        structural_only=structural_only,
        llm_only=llm_only,
        skip_cluster=skip_cluster,
        skip_terms=skip_terms,
        skip_community=skip_community,
        skip_classify=skip_classify,
        skip_trajectory=skip_trajectory,
        skip_conflicts=skip_conflicts,
        skip_friction=skip_friction,
        skip_perceived=skip_perceived,
        force_cluster=force_cluster,
        force_community=force_community,
    )
    emit_json(summary, fmt)


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------


@app.command
def embed(
    *,
    limit: int | None = None,
    all_steps: Annotated[bool, cyclopts.Parameter(name="--all")] = False,
    dry_run: bool = False,
    corpus_root: Path | None = None,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Embed unembedded corpus steps with Cohere Embed v4 and append to LanceDB.

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
    --corpus-root   Override the materialized corpus root.

    Output
    ------
    Dry run: the plan JSON ``{pipeline, candidates, batches, batch_size,
    concurrency, model, limit, dry_run}``. Real run:
    ``{"pipeline": "embed", "rows_processed": N, "dry_run": false}``.

    Exit codes: 0 success, 64 missing --limit/--all, 70 runtime
    (Bedrock / DuckDB / Lance failure — transient, safe to retry), 78 terminal
    state (the store or its config requires operator action; retrying without
    intervention cannot succeed, so unattended lanes suppress retries on 78).
    """
    import asyncio

    from atif_embed.application.embed import run_backfill
    from atif_embed.domain.errors import DomainError
    from atif_embed.infrastructure.settings import EmbedSettings

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
    try:
        result = asyncio.run(
            run_backfill(
                corpus_root=settings.corpus_root,
                settings=embed_settings,
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

    Output columns
    --------------
    uuid, session_id, sim (cosine similarity ∈ [-1, 1]), snippet.
    Sorted by cosine distance ascending — highest sim first.

    Exit codes: 0 success, 2 no_embeddings, 65 embedding_mismatch (the store
    was written by another provider), 70 runtime.
    """
    import duckdb

    from atif_cli.duck_errors import (
        REGISTRATION_ERRORS,
        classify_duckdb_error,
        classify_registration_error,
    )
    from atif_duck.infrastructure.registry import register
    from atif_embed.application.embed import embed_query
    from atif_embed.infrastructure.settings import EmbedSettings

    settings = _corpus_settings(None, corpus_root)
    embed_settings = EmbedSettings()
    expected_model, expected_dim = embed_settings.expected_embedding_identity()
    lance_uri = embed_settings.resolve_lance_uri(settings.corpus_root)

    con = duckdb.connect(":memory:")
    try:
        try:
            register(
                con,
                settings.corpus_root,
                lance_uri=lance_uri,
                expected_model=expected_model,
                expected_dim=expected_dim,
            )
        except REGISTRATION_ERRORS as exc:
            err = classify_registration_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc

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
        dim = len(qv)
        params: list[object] = [qv]
        session_filter = ""
        if session_id is not None:
            session_filter = "WHERE s.session_id = ?"
            params.append(session_id)
        params.append(k)
        # Rank by cosine similarity descending: ORDER BY array_cosine_distance
        # (== 1 - sim) ASC is what triggers the cosine HNSW index lookup.
        # Using array_distance here (L2) would silently bypass the index AND
        # give wrong ranks: the raw int8-cast-to-float document vectors have
        # magnitudes in the thousands while the query vector is
        # unit-normalized — only cosine is magnitude-invariant.
        sql = f"""
            WITH qv AS (SELECT CAST(? AS FLOAT[{dim}]) AS v)
            SELECT me.uuid                                                     AS uuid,
                   s.session_id                                                AS session_id,
                   substr(s.message, 1, 200)                                   AS snippet,
                   array_cosine_similarity(me.embedding, (SELECT v FROM qv))   AS sim
            FROM message_embeddings me
            JOIN steps s
              ON json_extract_string(s.source_uuids, '$[0]') = me.uuid
            {session_filter}
            ORDER BY array_cosine_distance(me.embedding, (SELECT v FROM qv)) ASC
            LIMIT ?
        """  # noqa: S608 — dim is len(vector); session_id, k and the vector are ?-bound
        try:
            cursor = con.execute(sql, params)
            columns = [d[0] for d in cursor.description or ()]
            rows = cursor.fetchall()
        except duckdb.Error as exc:
            err = classify_duckdb_error(exc)
            emit_error(err, fmt)
            raise SystemExit(err.exit_code) from exc
        emit_rows(columns, rows, fmt)
    finally:
        con.close()


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
    """List every registered view (with columns) and every macro signature.

    The canonical catalog for composing ``query`` calls. Answers from the
    static :data:`atif_duck.domain.catalog.VIEW_SCHEMA` /
    :data:`~atif_duck.domain.catalog.MACRO_SIGNATURES` dicts — no DuckDB
    import, no connection, no view registration; sub-50ms by construction
    (drift against the real DDL is caught by atif-duck's CI tests).
    """
    from atif_duck.domain.catalog import MACRO_SIGNATURES, VIEW_SCHEMA

    examples_hint = "tested example queries: atif-sql examples (or: atif-sql query --examples)"
    if resolve_format(fmt) is OutputFormat.TABLE:
        for name, cols in VIEW_SCHEMA.items():
            print(f"\n{name} ({len(cols)} cols)")
            for col, col_type in cols:
                print(f"  {col:<28} {col_type}")
        print(f"\nMacros ({len(MACRO_SIGNATURES)})")
        for macro, params in MACRO_SIGNATURES.items():
            print(f"  {macro}({', '.join(params)})")
        print(f"\n{examples_hint}")
        return
    emit_json(
        {
            "views": {
                name: [{"column": c, "type": t} for c, t in cols]
                for name, cols in VIEW_SCHEMA.items()
            },
            "macros": [{"name": n, "params": list(p)} for n, p in MACRO_SIGNATURES.items()],
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
