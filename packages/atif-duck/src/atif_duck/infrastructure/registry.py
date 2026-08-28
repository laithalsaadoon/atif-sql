# SPDX-License-Identifier: Apache-2.0

"""DuckDB view and macro registry over the materialized ATIF corpus.

Wires a DuckDB connection to a ``<corpus_root>/sessions/<id>/`` tree (per
docs/CONTRACT.md: ``trajectory.json``, ``edges.jsonl``, ``loss_report.json``,
``meta.json``) and exposes it as a stable set of SQL views and analytical
macros. Every view reads ATIF-v1.7, never raw Claude Code JSONL, so a
column's meaning comes from the ATIF schema rather than from a transcript
field that happens to share its name.

Design notes
------------
* Raw readers are ``CREATE TEMP TABLE`` over ``read_json`` with an explicit
  ``columns={...}`` projection. In DuckDB 1.5+ the dict is a *strict
  projection filter*: every field any downstream view touches must appear in
  the dict or it silently disappears (the strict-filter lesson). Listing the
  fields also skips JSON schema inference — the dominant cost on a large
  corpus, otherwise paid on every ``DESCRIBE``/view bind.
* ``filename=true`` + a path regexp derive ``session_id_path`` from the
  ``sessions/<id>/`` directory, which is the canonical session key (the
  in-document ``session_id`` is advisory; the directory name is what
  atif-corpus keyed the materialization on).
* ``trajectory.json`` is ONE document per file (``format='auto'``, compact
  JSON), NOT newline-delimited; ``steps`` is projected as a ``JSON[]``
  column so views unnest it lazily at query time. ``edges.jsonl`` is
  ``format='newline_delimited'``.
* All views use ``CREATE OR REPLACE`` so callers may safely re-register.
* Globs are inlined into DDL via :func:`~atif_duck.domain.sql_literal.sql_literal` (DuckDB rejects prepared
  parameters as table-function arguments).
* register-or-fail-loud: every registration function logs via
  ``logger.exception`` and re-raises on any DDL failure.

Semantics a transcript-shaped reader gets wrong (documented per-view below)
--------------------------------------------------------------------------
* ``steps``, not ``messages``, is the primary per-turn surface: one row
  per ATIF step (harbor bundles all events sharing an assistant
  ``message.id`` into one step, so step count < raw message count by design).
* ``messages`` is a uuid-keyed view reconstructed from ``edges.jsonl``,
  which carries the raw-record identity the trajectory itself cannot
  provide (fidelity gap 7: UUID_NOT_PRESERVED).
* Token columns follow ATIF ``Metrics`` semantics: ``prompt_tokens`` is the
  TOTAL input (non-cached + cache_read + cache_creation), ``cached_tokens``
  is the cache-read subset, and ``cache_creation`` is recovered from
  ``metrics.extra`` (fidelity gap 6: CACHE_SPLIT_PARTIAL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from atif_duck.domain.catalog import DEFAULT_PRICING
from atif_duck.domain.embedding_guard import ensure_store_matches
from atif_duck.domain.sql_literal import sql_literal

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

# ---------------------------------------------------------------------------
# Raw-reader object names
# ---------------------------------------------------------------------------
#: The four materialized raw readers. Named constants rather than inline
#: literals so a DDL interpolation site cannot typo a second table into
#: existence.
#: Deliberately absent from :data:`atif_duck.domain.catalog.VIEW_NAMES`:
#: they describe corpus files rather than being queryable business surface.
_RAW_TRAJECTORIES_TABLE: str = "v_raw_trajectories"
_RAW_EDGES_TABLE: str = "v_raw_edges"
_RAW_LOSS_REPORTS_TABLE: str = "v_raw_loss_reports"
_RAW_META_TABLE: str = "v_raw_meta"

#: Inlined ``read_json`` upper bound. Live trajectory.json files reach 436 MB
#: because harbor inlines subagent sidechains and tool outputs, so 1 GiB is
#: roughly 2.3x headroom over the largest observed document — not a knob to
#: trim. Lowering it does not reduce registration memory: measured peak RSS is
#: unchanged at 512 MiB and rises at 128 MiB, because this bounds the parse
#: buffer rather than preallocating per thread.
_MAX_OBJECT_SIZE: int = 1_073_741_824

# Explicit projection for ``v_raw_trajectories``.
#
# ATIF-v1.7 ``Trajectory`` is ``extra='forbid'``, so the top level is a
# closed set; we project exactly what the views touch. ``steps`` stays
# ``JSON[]`` (not a deep STRUCT) because Step.message is a union
# (str | ContentPart[]) and Step.extra / metrics.extra are free-form —
# a STRUCT projection would force one shape and silently null the other.
_TRAJECTORY_COLUMNS: dict[str, str] = {
    "schema_version": "VARCHAR",
    "session_id": "VARCHAR",
    "trajectory_id": "VARCHAR",
    "agent": "STRUCT(name VARCHAR, version VARCHAR, model_name VARCHAR, extra JSON)",
    "steps": "JSON[]",
    "final_metrics": (
        "STRUCT(total_prompt_tokens BIGINT, total_completion_tokens BIGINT, "
        "total_cached_tokens BIGINT, total_cost_usd DOUBLE, total_steps BIGINT, "
        "extra JSON)"
    ),
    "extra": "JSON",
}

# Explicit projection for ``v_raw_edges``: one line per RAW transcript record
# per docs/CONTRACT.md. ``parent_uuid`` is declared VARCHAR outright: a root
# record leaves it null, so inferred typing resolves it as a NULL-vs-string
# JSON union and every downstream view then needs its own CAST. Declaring the
# type in the one explicit-columns reader pays that cost once.
_EDGE_COLUMNS: dict[str, str] = {
    "uuid": "VARCHAR",
    "parent_uuid": "VARCHAR",
    "message_id": "VARCHAR",
    "type": "VARCHAR",
    "ts": "TIMESTAMP",
    "is_sidechain": "BOOLEAN",
    "is_compact_summary": "BOOLEAN",
    "source_file": "VARCHAR",
    "tool_use_ids": "JSON",
}

# Explicit projection for ``v_raw_loss_reports``: atif_converter
# ``LossReport.to_json()`` shape (record_counts / gaps_observed stay JSON —
# enum-keyed dict and list respectively).
_LOSS_REPORT_COLUMNS: dict[str, str] = {
    "record_counts": "JSON",
    "records_total": "BIGINT",
    "records_converted": "BIGINT",
    "records_dropped": "BIGINT",
    "gaps_observed": "JSON",
    "subagent_files_found": "BIGINT",
    "subagent_files_convertible": "BIGINT",
    "workflow_subagent_files_found": "BIGINT",
}

# Explicit projection for ``v_raw_meta`` per docs/CONTRACT.md.
_META_COLUMNS: dict[str, str] = {
    "session_id": "VARCHAR",
    "source_mtime_ns": "BIGINT",
    "source_files": "JSON",
    "harbor_version": "VARCHAR",
    "converter_version": "VARCHAR",
    "materialized_at": "VARCHAR",
}


def _render_columns_clause(columns: dict[str, str]) -> str:
    """Render a ``columns={...}`` clause body for ``read_json``.

    Keys are bare DuckDB identifiers (no quoting); values are SQL type
    strings wrapped in single quotes. Both halves come from code-side
    constants — never user input — so escaping is defensive only.
    """
    return ", ".join(f"{name}: {sql_literal(typ)}" for name, typ in columns.items())


# ---------------------------------------------------------------------------
# Raw readers
# ---------------------------------------------------------------------------


def _warn_incomplete_session_dirs(con: duckdb.DuckDBPyConnection, sessions_dir: Path) -> None:
    """Log every session dir excluded by the meta gate as skipped-incomplete.

    The gate itself keeps torn dirs out of every view; this makes the
    exclusion OBSERVABLE — a session silently missing from ``sessions`` is
    much harder to diagnose than a logged skip.
    """
    if not sessions_dir.is_dir():
        return
    # The one interpolation is the module constant _RAW_META_TABLE.
    rows = con.execute(f"SELECT session_id_path FROM {_RAW_META_TABLE}").fetchall()  # noqa: S608
    with_meta = {row[0] for row in rows}
    for session_dir in sorted(sessions_dir.iterdir()):
        if session_dir.is_dir() and session_dir.name not in with_meta:
            logger.warning(
                "Skipping incomplete session dir {} (no meta.json — "
                "crashed writer or partial cleanup); excluded from all views",
                session_dir,
            )


def register_raw(con: duckdb.DuckDBPyConnection, corpus_root: Path) -> None:
    """Create the four raw readers as TEMP TABLEs over ``corpus_root``.

    ``v_raw_trajectories`` / ``v_raw_edges`` / ``v_raw_loss_reports`` /
    ``v_raw_meta`` each read one file kind from the CONTRACT corpus layout
    ``<corpus_root>/sessions/<session_id>/...``, with ``session_id_path``
    derived from the directory component via regexp over ``filename``.

    TORN-SET GUARD: ``meta.json`` is written last by the corpus writer, so
    its presence marks a session dir as complete. The trajectory / edges /
    loss readers are restricted to session dirs where ``v_raw_meta`` has a
    row — a session dir missing its meta (crashed writer) contributes
    nothing to any view instead of a partial artifact set.

    Parameters
    ----------
    con
        Open DuckDB connection.
    corpus_root
        Materialized corpus root (the directory containing ``sessions/``).

    Raises
    ------
    duckdb.Error
        If any DDL fails (including an empty/absent corpus — ``read_json``
        errors on a glob with zero matches, which is the honest failure
        mode for "nothing materialized yet"). Logged via
        ``logger.exception`` before re-raise.
    """
    sessions_dir = corpus_root / "sessions"
    trajectory_glob = sql_literal(str(sessions_dir / "*" / "trajectory.json"))
    edges_glob = sql_literal(str(sessions_dir / "*" / "edges.jsonl"))
    loss_glob = sql_literal(str(sessions_dir / "*" / "loss_report.json"))
    meta_glob = sql_literal(str(sessions_dir / "*" / "meta.json"))

    # meta.json is written LAST by atif-corpus — its presence marks a
    # session dir as COMPLETE. Every other raw reader is restricted to
    # meta-bearing session dirs via this semi-join predicate, so a torn
    # session dir (crashed writer, partial cleanup) is invisible to every
    # view rather than surfacing a partial artifact set.
    # The one interpolation is the module constant _RAW_META_TABLE.
    meta_gate = f"session_id_path IN (SELECT session_id_path FROM {_RAW_META_TABLE})"  # noqa: S608

    try:
        # meta FIRST: the other three readers gate on it.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_META_TABLE} AS
            SELECT *,
                   filename AS meta_path,
                   regexp_extract(filename, '/sessions/([^/]+)/meta\\.json$', 1)
                       AS session_id_path
            FROM read_json(
                {meta_glob},
                format='auto',
                filename=true,
                columns={{{_render_columns_clause(_META_COLUMNS)}}}
            );
            """  # noqa: S608 — meta glob escaped by sql_literal; table and columns are constants
        )
        logger.debug("Registered {} from glob {}", _RAW_META_TABLE, meta_glob)
        _warn_incomplete_session_dirs(con, sessions_dir)

        # One trajectory document per file -> format='auto' (NOT NDJSON).
        # The explicit projection keeps `steps` as a lazy JSON[] column.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_TRAJECTORIES_TABLE} AS
            SELECT * FROM (
                SELECT *,
                       filename AS trajectory_path,
                       regexp_extract(filename, '/sessions/([^/]+)/trajectory\\.json$', 1)
                           AS session_id_path
                FROM read_json(
                    {trajectory_glob},
                    format='auto',
                    filename=true,
                    columns={{{_render_columns_clause(_TRAJECTORY_COLUMNS)}}},
                    maximum_object_size={_MAX_OBJECT_SIZE}
                )
            ) WHERE {meta_gate};
            """  # noqa: S608 — trajectory glob escaped by sql_literal; table/columns/cap are constants
        )
        logger.debug("Registered {} from glob {}", _RAW_TRAJECTORIES_TABLE, trajectory_glob)

        # edges.jsonl is one line per RAW record -> newline_delimited.
        # The record's own `source_file` (the raw transcript path) is kept;
        # the edges.jsonl path itself is aliased to `edges_path`.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_EDGES_TABLE} AS
            SELECT * FROM (
                SELECT *,
                       filename AS edges_path,
                       regexp_extract(filename, '/sessions/([^/]+)/edges\\.jsonl$', 1)
                           AS session_id_path
                FROM read_json(
                    {edges_glob},
                    format='newline_delimited',
                    filename=true,
                    columns={{{_render_columns_clause(_EDGE_COLUMNS)}}},
                    maximum_object_size={_MAX_OBJECT_SIZE}
                )
            ) WHERE {meta_gate};
            """  # noqa: S608 — edges glob escaped by sql_literal; table/columns/gate are constants
        )
        logger.debug("Registered {} from glob {}", _RAW_EDGES_TABLE, edges_glob)

        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE {_RAW_LOSS_REPORTS_TABLE} AS
            SELECT * FROM (
                SELECT *,
                       filename AS report_path,
                       regexp_extract(filename, '/sessions/([^/]+)/loss_report\\.json$', 1)
                           AS session_id_path
                FROM read_json(
                    {loss_glob},
                    format='auto',
                    filename=true,
                    columns={{{_render_columns_clause(_LOSS_REPORT_COLUMNS)}}}
                )
            ) WHERE {meta_gate};
            """  # noqa: S608 — loss glob escaped by sql_literal; table/columns/gate are constants
        )
        logger.debug("Registered {} from glob {}", _RAW_LOSS_REPORTS_TABLE, loss_glob)
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register raw readers over {}", corpus_root)
        raise


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def register_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create the business-level views on top of the raw readers.

    Must be called after :func:`register_raw`. Creates, in dependency order:
    ``steps``, ``sessions``, ``messages``, ``tool_calls``, ``tool_results``,
    ``todo_events``, ``todo_state_current``, ``subagent_spawns``,
    ``task_creations``, ``task_updates``, ``tasks_state_current``,
    ``skill_invocations``, ``skill_usage``, ``subagent_steps``,
    ``loss_reports``.

    Parameters
    ----------
    con
        Open DuckDB connection with raw readers already registered.

    Raises
    ------
    duckdb.Error
        If any view DDL fails. Logged via ``logger.exception`` before
        re-raise.
    """
    try:
        # One row per ATIF step, and the per-turn surface the contract names
        # ("messages-parity view name: steps"). Column semantics that a
        # transcript-shaped reader would misread:
        # * ``message`` is flattened text: ATIF Step.message is
        #   str | ContentPart[]; the ARRAY branch joins the parts' ``text``
        #   fields with blank lines (mirrors harbor's own text bundling).
        # * ``prompt_tokens`` is the ATIF TOTAL (input + cache_read +
        #   cache_creation); ``cached_tokens`` is the cache-read subset;
        #   ``cache_creation`` is dug out of ``metrics.extra`` because
        #   harbor only preserves the creation split there (fidelity gap 6).
        # * ``is_sidechain`` / ``is_compact_summary`` / ``source_uuids``
        #   come from ``step.extra`` per the CONTRACT enrichment pass.
        con.execute(
            """
            CREATE OR REPLACE VIEW steps AS
            SELECT
                t.session_id_path                                    AS session_id,
                json_extract(step, '$.step_id')::BIGINT              AS step_id,
                json_extract_string(step, '$.timestamp')::TIMESTAMP  AS ts,
                json_extract_string(step, '$.source')                AS source,
                json_extract_string(step, '$.model_name')            AS model_name,
                CASE WHEN json_type(step, '$.message') = 'ARRAY'
                     THEN array_to_string(
                              list_transform(
                                  json_extract(step, '$.message[*].text'),
                                  part -> json_extract_string(part, '$')
                              ),
                              '\n\n'
                          )
                     ELSE json_extract_string(step, '$.message')
                END                                                  AS message,
                coalesce(
                    json_extract(step, '$.extra.is_sidechain')::BOOLEAN, false
                )                                                    AS is_sidechain,
                coalesce(
                    json_extract(step, '$.extra.is_compact_summary')::BOOLEAN, false
                )                                                    AS is_compact_summary,
                json_extract(step, '$.metrics.prompt_tokens')::BIGINT
                                                                     AS prompt_tokens,
                json_extract(step, '$.metrics.completion_tokens')::BIGINT
                                                                     AS completion_tokens,
                json_extract(step, '$.metrics.cached_tokens')::BIGINT
                                                                     AS cached_tokens,
                json_extract(step, '$.metrics.extra.cache_creation_input_tokens')::BIGINT
                                                                     AS cache_creation,
                json_extract(step, '$.llm_call_count')::BIGINT       AS llm_call_count,
                json_extract(step, '$.extra.source_uuids')           AS source_uuids
            FROM v_raw_trajectories t,
                 UNNEST(t.steps) AS s(step);
            """
        )
        logger.debug("Registered view: steps")

        # One row per materialized session. cwd / git_branch come from the
        # harbor adapter's ``agent.extra`` sets (cwds / git_branches); the
        # first element is representative because a session rarely spans more
        # than one cwd or branch. A session that does spans them silently: the
        # column reports one, so treat it as indicative, not exhaustive.
        con.execute(
            """
            CREATE OR REPLACE VIEW sessions AS
            SELECT
                t.session_id_path                                     AS session_id,
                json_extract_string(t.agent.extra, '$.cwds[0]')       AS cwd,
                json_extract_string(t.agent.extra, '$.git_branches[0]')
                                                                      AS git_branch,
                s.started_at,
                s.ended_at,
                s.agent_steps,
                s.step_count,
                t.agent.model_name                                    AS model_name,
                t.final_metrics.total_cost_usd                        AS total_cost_usd,
                t.trajectory_path
            FROM v_raw_trajectories t
            LEFT JOIN (
                SELECT
                    session_id,
                    min(ts)                                    AS started_at,
                    max(ts)                                    AS ended_at,
                    count(*) FILTER (WHERE source = 'agent')   AS agent_steps,
                    count(*)                                   AS step_count
                FROM steps
                GROUP BY session_id
            ) s ON s.session_id = t.session_id_path;
            """
        )
        logger.debug("Registered view: sessions")

        # COMPAT view: uuid-keyed raw-record identity reconstructed from
        # edges.jsonl. The trajectory cannot provide this (fidelity gap 7:
        # harbor never lands the event uuid in Step.extra), so parent-chain
        # walks and per-record counts run against the census-derived edges.
        # ``type`` carries the raw record role (user/assistant/system/...);
        # a separate role column would be redundant with it.
        con.execute(
            """
            CREATE OR REPLACE VIEW messages AS
            SELECT
                uuid,
                parent_uuid,
                session_id_path AS session_id,
                ts,
                type,
                is_sidechain,
                coalesce(is_compact_summary, false) AS is_compact_summary,
                message_id,
                source_file
            FROM v_raw_edges;
            """
        )
        logger.debug("Registered view: messages")

        # One row per ToolCall in any step. The columns are renamed off the
        # ATIF field names: function_name -> tool_name, tool_call_id ->
        # tool_use_id, arguments -> tool_input. ``tool_input`` is an already
        # parsed dict, NOT a JSON string, so a query must not json_extract it.
        #
        # The CTE must narrow each step to (ids, ts, calls list) BEFORE the
        # second UNNEST: a step's JSON string is megabytes on harbor
        # trajectories that inline subagent sidechains, and unnesting it
        # directly replicates that string once per tool call.
        con.execute(
            """
            CREATE OR REPLACE VIEW tool_calls AS
            WITH step_calls AS (
                SELECT
                    t.session_id_path                                    AS session_id,
                    json_extract(step, '$.step_id')::BIGINT              AS step_id,
                    json_extract_string(step, '$.timestamp')::TIMESTAMP  AS ts,
                    json_extract(step, '$.tool_calls[*]')                AS calls
                FROM v_raw_trajectories t,
                     UNNEST(t.steps) AS s(step)
                WHERE json_extract(step, '$.tool_calls') IS NOT NULL
            )
            SELECT
                session_id,
                step_id,
                ts,
                json_extract_string(call, '$.function_name')         AS tool_name,
                json_extract_string(call, '$.tool_call_id')          AS tool_use_id,
                json_extract(call, '$.arguments')                    AS tool_input
            FROM step_calls,
                 UNNEST(calls) AS c(call);
            """
        )
        logger.debug("Registered view: tool_calls")

        # One row per ObservationResult. ``source_call_id`` is ATIF's join
        # key back to the tool_calls array, surfaced under the SAME name
        # ``tool_use_id`` that ``tool_calls`` exposes, so the two views join
        # with `USING (tool_use_id)` and neither side needs a rename.
        # Same early-projection CTE as tool_calls (see comment there): the
        # full step string must be dropped before the second UNNEST.
        con.execute(
            """
            CREATE OR REPLACE VIEW tool_results AS
            WITH step_results AS (
                SELECT
                    t.session_id_path                                    AS session_id,
                    json_extract(step, '$.step_id')::BIGINT              AS step_id,
                    json_extract_string(step, '$.timestamp')::TIMESTAMP  AS ts,
                    json_extract(step, '$.observation.results[*]')       AS results
                FROM v_raw_trajectories t,
                     UNNEST(t.steps) AS s(step)
                WHERE json_extract(step, '$.observation.results') IS NOT NULL
            )
            SELECT
                session_id,
                step_id,
                ts,
                json_extract_string(res, '$.source_call_id')         AS tool_use_id,
                json_extract(res, '$.content')                       AS content
            FROM step_results,
                 UNNEST(results) AS r(res);
            """
        )
        logger.debug("Registered view: tool_results")

        # DuckDB's UNNEST requires a LIST, and the ``$.todos[*]`` wildcard
        # path yields JSON[] that UNNEST accepts natively — a bare
        # ``$.todos`` yields a scalar JSON value that it rejects.
        # ``step_id`` is the snapshot tie-break: steps are unique and ordered
        # per session, so no uuid column is needed to sequence the snapshots.
        con.execute(
            """
            CREATE OR REPLACE VIEW todo_events AS
            SELECT
                tc.session_id,
                tc.ts                                      AS written_at,
                tc.step_id,
                json_extract_string(todo, '$.content')     AS subject,
                json_extract_string(todo, '$.status')      AS status,
                json_extract_string(todo, '$.activeForm')  AS active_form,
                row_number() OVER (
                    PARTITION BY tc.session_id
                    ORDER BY tc.ts, tc.step_id
                ) AS snapshot_ix
            FROM tool_calls tc,
                 UNNEST(json_extract(tc.tool_input, '$.todos[*]')) AS t(todo)
            WHERE tc.tool_name = 'TodoWrite';
            """
        )
        logger.debug("Registered view: todo_events")

        # Latest snapshot wins per (session_id, subject) — TodoWrite always
        # rewrites the full list, so the newest row is the current state.
        con.execute(
            """
            CREATE OR REPLACE VIEW todo_state_current AS
            SELECT session_id, subject, status, active_form, written_at
            FROM (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY session_id, subject
                           ORDER BY snapshot_ix DESC
                       ) AS rn
                FROM todo_events
            )
            WHERE rn = 1;
            """
        )
        logger.debug("Registered view: todo_state_current")

        # Subagent launchers: ``Task`` (pre-v2.1.63) and ``Agent`` (v2.1.63+).
        # Input shape: {subagent_type, description, prompt, run_in_background?}.
        con.execute(
            """
            CREATE OR REPLACE VIEW subagent_spawns AS
            SELECT
                session_id,
                ts AS spawned_at,
                step_id,
                tool_use_id,
                tool_name AS spawn_tool,
                json_extract_string(tool_input, '$.subagent_type')      AS subagent_type,
                json_extract_string(tool_input, '$.description')        AS description,
                json_extract_string(tool_input, '$.prompt')             AS prompt,
                json_extract_string(tool_input, '$.run_in_background')  AS run_in_background
            FROM tool_calls
            WHERE tool_name IN ('Task', 'Agent');
            """
        )
        logger.debug("Registered view: subagent_spawns")

        # Persistent task creation: ``TaskCreate`` (Claude Code v2.1.16+) and
        # the SDK-py mirror ``mcp__tasks__task_create``. Distinct from
        # subagent_spawns — no subagent_type / prompt fields.
        con.execute(
            """
            CREATE OR REPLACE VIEW task_creations AS
            SELECT
                session_id,
                ts AS created_at,
                step_id,
                tool_use_id,
                tool_name                                              AS create_tool,
                json_extract_string(tool_input, '$.subject')           AS subject,
                json_extract_string(tool_input, '$.description')       AS description,
                json_extract_string(tool_input, '$.activeForm')        AS active_form,
                json_extract(tool_input, '$.metadata')                 AS metadata
            FROM tool_calls
            WHERE tool_name IN ('TaskCreate', 'mcp__tasks__task_create');
            """
        )
        logger.debug("Registered view: task_creations")

        # Task lifecycle updates. The native tool spells the key ``taskId``
        # (camel) and the mcp variant spells it ``id``, so both are extracted
        # and COALESCEd: reading only one silently drops half the updates.
        con.execute(
            """
            CREATE OR REPLACE VIEW task_updates AS
            SELECT
                session_id,
                ts AS updated_at,
                step_id,
                tool_use_id,
                tool_name AS update_tool,
                COALESCE(
                    json_extract_string(tool_input, '$.taskId'),
                    json_extract_string(tool_input, '$.id')
                )                                                       AS task_id,
                json_extract_string(tool_input, '$.status')             AS status,
                json_extract(tool_input, '$.addBlockedBy')              AS add_blocked_by,
                json_extract_string(tool_input, '$.owner')              AS owner
            FROM tool_calls
            WHERE tool_name IN ('TaskUpdate', 'mcp__tasks__task_update');
            """
        )
        logger.debug("Registered view: task_updates")

        # Latest status per (session_id, task_id). The runtime assigns the
        # task id and returns it only in the tool RESULT, so we recover it
        # from ``tool_results`` via the source_call_id join, parsing the
        # "Task #N" / {taskId} shapes the result text carries, and falling
        # back to per-session creation order when neither shape matches.
        #
        # NULLIF is required because DuckDB's regexp_extract returns '' (not
        # NULL) on no-match, and '' satisfies COALESCE — without it every later
        # fallback branch is unreachable and non-"Task #N" results all collapse
        # onto the same empty task_id, colliding in the latest_status join.
        con.execute(
            """
            CREATE OR REPLACE VIEW tasks_state_current AS
            WITH creates AS (
                SELECT
                    tc.session_id,
                    tc.created_at,
                    tc.subject,
                    tc.active_form,
                    tc.tool_use_id,
                    COALESCE(
                        NULLIF(
                            regexp_extract(
                                CAST(tr.content AS VARCHAR), 'Task #(\\d+)', 1
                            ),
                            ''
                        ),
                        json_extract_string(tr.content, '$.taskId'),
                        CAST(row_number() OVER (
                            PARTITION BY tc.session_id ORDER BY tc.created_at
                        ) AS VARCHAR)
                    ) AS task_id
                FROM task_creations tc
                LEFT JOIN tool_results tr USING (tool_use_id)
            ),
            latest_status AS (
                SELECT session_id, task_id, status, updated_at,
                       row_number() OVER (
                           PARTITION BY session_id, task_id
                           ORDER BY updated_at DESC
                       ) AS rn
                FROM task_updates
                WHERE task_id IS NOT NULL
            )
            SELECT
                c.session_id,
                c.task_id,
                c.subject,
                c.active_form,
                COALESCE(ls.status, 'pending') AS status,
                c.created_at,
                ls.updated_at AS last_updated_at
            FROM creates c
            LEFT JOIN latest_status ls
              ON ls.session_id = c.session_id
             AND ls.task_id = c.task_id
             AND ls.rn = 1;
            """
        )
        logger.debug("Registered view: tasks_state_current")

        # Every Skill / slash-command invocation, unioned across both shapes:
        # * ``tool`` — the assistant invokes the built-in ``Skill`` tool with
        #   ``arguments.skill = '<name>'``. Lives in ``tool_calls`` already.
        # * ``slash_command`` — the user types ``/<name>``, which Claude Code
        #   serializes into the message text as
        #   ``<command-name>/<name></command-name>`` (sometimes with
        #   ``<command-args>``). ATIF flattens user content to one text
        #   message per step, so ONE regex over ``steps.message`` covers both
        #   raw serializations (list-typed content blocks and bare VARCHAR
        #   content) without branching on the content type.
        # ``skill_id`` is the raw identifier, NOT a normalized name:
        # ``erpaval`` and ``personal-plugins:erpaval`` are distinct rows, so a
        # per-skill aggregate has to decide for itself whether to fold them.
        cmd_name_re = "<command-name>/([A-Za-z0-9_:.-]+)</command-name>"
        args_re = "<command-args>([^<]*)</command-args>"
        con.execute(
            f"""
            CREATE OR REPLACE VIEW skill_invocations AS
            SELECT
                tc.session_id,
                tc.ts,
                tc.step_id,
                'tool'                                         AS source,
                json_extract_string(tc.tool_input, '$.skill')  AS skill_id,
                json_extract_string(tc.tool_input, '$.args')   AS args,
                tc.tool_use_id
            FROM tool_calls tc
            WHERE tc.tool_name = 'Skill'
              AND json_extract_string(tc.tool_input, '$.skill') IS NOT NULL
            UNION ALL
            SELECT
                s.session_id,
                s.ts,
                s.step_id,
                'slash_command'                                     AS source,
                regexp_extract(s.message, '{cmd_name_re}', 1)       AS skill_id,
                NULLIF(regexp_extract(s.message, '{args_re}', 1), '') AS args,
                NULL                                                AS tool_use_id
            FROM steps s
            WHERE s.source = 'user'
              AND s.message LIKE '%<command-name>/%'
              AND regexp_extract(s.message, '{cmd_name_re}', 1) != '';
            """  # noqa: S608 — the two interpolated regexes are local literals defined above
        )
        logger.debug("Registered view: skill_invocations")

        # Labels derive from the skill_id string alone — there is no skills
        # catalog in the corpus to join against. The heuristic: a skill_id
        # WITHOUT a ':' has no plugin namespace and is labelled builtin
        # (`erpaval`, `review`, ...); a `plugin:skill` id splits into plugin +
        # skill_name and is not builtin. ``is_builtin`` therefore OVER-marks a
        # user-local non-plugin skill as builtin. That is tolerable only
        # because the rank/mix macros use the flag to damp slash-only noise;
        # a query that treats it as ground truth about provenance is wrong.
        con.execute(
            """
            CREATE OR REPLACE VIEW skill_usage AS
            SELECT
                si.session_id,
                si.ts,
                si.step_id,
                si.source,
                si.skill_id,
                si.args,
                si.tool_use_id,
                CASE WHEN strpos(si.skill_id, ':') > 0
                     THEN substr(si.skill_id, strpos(si.skill_id, ':') + 1)
                     ELSE si.skill_id
                END                                            AS skill_name,
                CASE WHEN strpos(si.skill_id, ':') > 0
                     THEN split_part(si.skill_id, ':', 1)
                     ELSE NULL
                END                                            AS plugin,
                strpos(si.skill_id, ':') = 0                   AS is_builtin
            FROM skill_invocations si;
            """
        )
        logger.debug("Registered view: skill_usage")

        # Harbor INLINES subagent transcripts into the flat step list
        # (fidelity gap 4), marking them only via ``extra.is_sidechain`` — so
        # the subagent surface is a filter over ``steps`` and there is no
        # separate subagent raw reader to read. A consequence worth knowing:
        # a session-level aggregate over ``steps`` already includes subagent
        # work unless it filters ``is_sidechain`` out.
        con.execute(
            """
            CREATE OR REPLACE VIEW subagent_steps AS
            SELECT *
            FROM steps
            WHERE is_sidechain;
            """
        )
        logger.debug("Registered view: subagent_steps")

        # Per-session conversion loss accounting from atif-converter's
        # LossReport: record counts by raw type plus the fidelity gaps this
        # session actually exhibits. Any count taken from the other views is
        # only as complete as this view says the conversion was, so a
        # discrepancy between a raw record count and a step count is explained
        # here rather than being a defect in the view that reports it.
        con.execute(
            """
            CREATE OR REPLACE VIEW loss_reports AS
            SELECT
                session_id_path AS session_id,
                records_total,
                records_converted,
                records_dropped,
                gaps_observed,
                record_counts,
                subagent_files_found,
                subagent_files_convertible,
                workflow_subagent_files_found,
                report_path
            FROM v_raw_loss_reports;
            """
        )
        logger.debug("Registered view: loss_reports")
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register derived views")
        raise


# ---------------------------------------------------------------------------
# VSS
# ---------------------------------------------------------------------------


def _lance_table_present(con: duckdb.DuckDBPyConnection) -> bool:
    """True iff the ATTACHed ``lance_store`` catalog exposes ``embeddings``.

    Probed via ``duckdb_tables()`` rather than a speculative SELECT: a Lance
    directory that exists with metadata but no embeddings table (legitimate
    intermediate state after a ``lancedb.connect`` that never created a
    table) ATTACHes cleanly but then blows up at view-bind time with a
    catalog error. The right gate is "is the table actually there?".
    """
    row = con.execute(
        """
        SELECT count(*)
        FROM duckdb_tables()
        WHERE database_name = 'lance_store' AND table_name = 'embeddings'
        """
    ).fetchone()
    return row is not None and int(row[0]) > 0


def register_vss(
    con: duckdb.DuckDBPyConnection,
    *,
    lance_uri: Path,
    expected_model: str | None = None,
    expected_dim: int | None = None,
    dim: int = 1024,
) -> bool:
    """Bind ``message_embeddings`` over a LanceDB local dataset.

    LanceDB stores embeddings + its IVF_HNSW_SQ index in one place (written
    by atif-embed's backfill); reads come back through DuckDB via the lance
    core extension (``INSTALL lance; LOAD lance; ATTACH (TYPE LANCE)``).
    The store probe runs through DuckDB itself. atif-duck declares no lancedb
    dependency: lancedb belongs to atif-embed, which writes the store, and the
    independence contract forbids an import edge between the two packages — so
    the lance extension is the only thing atif-duck may read the store with.

    Parameters
    ----------
    con
        Open DuckDB connection.
    lance_uri
        Local LanceDB dataset directory.
    expected_model
        When supplied (by the composition root), the active embedder's
        ``model_id``. Read against the store's stamped ``model`` column via
        the fail-loud provider guard: a mismatch raises
        :class:`atif_duck.domain.embedding_guard.EmbeddingProviderMismatch`
        rather than letting a cross-provider query silently return garbage
        cosine scores (guard-before-bind).
    expected_dim
        The active embedder's dimension, checked alongside
        ``expected_model``. ``None`` trusts ``model_id`` alone.
    dim
        Fixed-length embedding dimension for the empty-fallback table. When a
        populated Lance store is present its own stamped ``dim`` drives the
        ``CAST(embedding AS FLOAT[dim])`` view instead, so a store written by
        a different-width provider binds correctly regardless of this
        argument.

    Returns
    -------
    bool
        ``True`` when the Lance table is reachable through the
        ``message_embeddings`` view; ``False`` when no embeddings exist yet
        (the name is created as an empty TABLE with the right schema so a
        downstream ``CREATE MACRO semantic_search`` can still bind).
    """
    dim_i = int(dim)
    con.execute("INSTALL lance;")
    con.execute("LOAD lance;")

    import duckdb as _duckdb

    attached = False
    if lance_uri.is_dir():
        try:
            con.execute(
                f"ATTACH IF NOT EXISTS {sql_literal(str(lance_uri))} AS lance_store (TYPE LANCE);"
            )
            attached = True
        except _duckdb.Error:
            # register-or-fail-loud applies to DDL over a healthy store; a
            # directory the lance extension cannot ATTACH is equivalent to
            # "no store yet" and falls through to the empty-fallback table.
            logger.exception("Lance ATTACH failed for {}; treating as empty store", lance_uri)

    if not attached or not _lance_table_present(con):
        logger.warning(
            "No Lance embeddings table at {}; creating empty message_embeddings "
            "table so semantic_search binds. Run `atif-sql embed --all --no-dry-run` "
            "to backfill.",
            lance_uri,
        )
        con.execute(
            f"""
            CREATE OR REPLACE TABLE message_embeddings (
                uuid        VARCHAR PRIMARY KEY,
                model       VARCHAR,
                dim         INTEGER,
                embedding   FLOAT[{dim_i}],
                embedded_at TIMESTAMPTZ
            );
            """
        )
        return False

    # Fail-loud provider/dimension guard: the store stamps ``(model, dim)``
    # on every row; read it back and refuse to bind a view over vectors
    # written by a different provider (even at matching width, cross-model
    # vectors live in incompatible spaces and produce numerically valid but
    # garbage cosine scores). The stored width also drives the CAST below.
    row = con.execute("SELECT model, dim FROM lance_store.main.embeddings LIMIT 1;").fetchone()
    if row is not None:
        stored_model, stored_dim = str(row[0]), int(row[1])
        if expected_model is not None:
            ensure_store_matches(
                stored_model=stored_model,
                stored_dim=stored_dim,
                expected_model=expected_model,
                expected_dim=expected_dim,
            )
        dim_i = stored_dim

    # Project the embeddings table as a top-level view named
    # ``message_embeddings``, casting the embedding column to FLOAT[dim] so
    # the fixed-size ARRAY type is what downstream vector functions see.
    con.execute(
        f"""
        CREATE OR REPLACE VIEW message_embeddings AS
        SELECT
            uuid,
            model,
            dim,
            CAST(embedding AS FLOAT[{dim_i}]) AS embedding,
            embedded_at
        FROM lance_store.main.embeddings;
        """  # noqa: S608 — the only interpolation is the width, through int() coercion
    )
    count_row = con.execute("SELECT count(*) FROM message_embeddings;").fetchone()
    count = int(count_row[0]) if count_row else 0
    logger.debug("Bound message_embeddings over Lance ({} rows, dim={})", count, dim_i)
    return True


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------


def _pricing_values_clause(pricing: dict[str, tuple[float, float]]) -> str:
    """Render a pricing dict as an inline SQL ``VALUES`` row list.

    Parameters
    ----------
    pricing
        Mapping of ``model_name -> (input_rate, output_rate)`` per 1M tokens.

    Returns
    -------
    str
        Comma-separated ``('model', in, out)`` rows. Emits a sentinel row
        that matches no real model if ``pricing`` is empty (DuckDB rejects
        empty ``VALUES`` lists).
    """
    if not pricing:
        return f"({sql_literal('__no_pricing__')}, 0.0, 0.0)"
    # The model NAME is escaped and the two rates are coerced with `float()`
    # rather than interpolated as they arrive. Both are the declared type of the
    # `pricing` parameter, and atif-cli never passes one — but this is a library
    # meant to be embedded in-process, so a caller that reaches DuckDB with a
    # `str` rate past the type checker gets a ValueError here instead of an
    # injected VALUES row.
    rows = [
        f"({sql_literal(model)}, {float(in_rate)}, {float(out_rate)})"
        for model, (in_rate, out_rate) in sorted(pricing.items())
    ]
    return ", ".join(rows)


def register_macros(
    con: duckdb.DuckDBPyConnection,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    skip_vss: bool = False,
) -> None:
    """Create the SQL macros declared in the static catalog.

    Every signature here must match its ``MACRO_SIGNATURES`` entry in
    :mod:`atif_duck.domain.catalog`: the catalog is what ``atif-sql schema``
    prints and what the derived examples are built from, and the drift test
    fails on any divergence.

    Must be called after :func:`register_views` — DuckDB binds macro bodies
    at CREATE time, so every referenced view must already exist. The
    ``semantic_search`` macro additionally requires ``message_embeddings``
    (view or empty fallback table from :func:`register_vss`) to be bound
    first; ``skip_vss=True`` skips it for connections that never call
    :func:`register_vss`.

    Parameters
    ----------
    con
        Open DuckDB connection with views already registered.
    pricing
        Optional pricing override; falls back to
        :data:`atif_duck.domain.catalog.DEFAULT_PRICING`.
    skip_vss
        When ``True``, skip the ``semantic_search`` macro registration.

    Raises
    ------
    duckdb.Error
        If any macro DDL fails. Logged via ``logger.exception`` before
        re-raise.
    """
    pricing_rows = _pricing_values_clause(pricing if pricing is not None else DEFAULT_PRICING)

    try:
        # ``ago('14 days')`` -> ``current_timestamp - INTERVAL 14 DAY``.
        # The CAST handles every interval-unit shape DuckDB recognizes.
        con.execute(
            """
            CREATE OR REPLACE MACRO ago(interval_text) AS (
                current_timestamp - CAST(interval_text AS INTERVAL)
            );
            """
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO model_used(sid) AS (
                SELECT any_value(model_name)
                FROM steps
                WHERE session_id = sid AND model_name IS NOT NULL
            );
            """
        )

        # COORDINATE SPACE: ATIF's ``prompt_tokens`` is the TOTAL input
        # (non-cached + cache_read + cache_creation), so it is NOT the
        # billable uncached base. The charged base here is
        # (prompt_tokens - cached_tokens) = non-cached + cache_creation.
        # Billing cache_creation at the input rate and leaving cache reads
        # uncharged is an approximation: it over-charges a cache write and
        # under-charges a cache read relative to a provider's real rate card.
        # The prefix match strips dated model suffixes
        # (``claude-haiku-4-5-20251001`` -> ``claude-haiku-4-5``).
        #
        # LEFT JOIN plus ``unpriced_steps`` is the honest-accounting shape: an
        # inner join silently drops steps whose model has no pricing row, so a
        # session mixing priced and unpriced models would return a PARTIAL
        # number indistinguishable from a complete one. ``est_cost_usd``
        # covers the priced steps only and is meaningful ONLY when
        # ``unpriced_steps = 0``.
        #
        # Both counters filter on ``model_name IS NOT NULL``: USER steps carry
        # no model and cost nothing, so counting them as unpriced would put
        # every session with a user turn above zero and make a real pricing
        # gap indistinguishable from an ordinary conversation.
        con.execute(
            f"""
            CREATE OR REPLACE MACRO cost_estimate(sid) AS TABLE (
                SELECT sum(
                           (coalesce(s.prompt_tokens, 0) - coalesce(s.cached_tokens, 0))
                               * p.in_rate
                           + coalesce(s.completion_tokens, 0) * p.out_rate
                       ) / 1e6                                   AS est_cost_usd,
                       count(*) FILTER (
                           WHERE s.model_name IS NOT NULL AND p.model IS NOT NULL
                       )                                          AS priced_steps,
                       count(*) FILTER (
                           WHERE s.model_name IS NOT NULL AND p.model IS NULL
                       )                                          AS unpriced_steps
                FROM steps s
                LEFT JOIN (VALUES {pricing_rows}) p(model, in_rate, out_rate)
                  ON regexp_replace(s.model_name, '-\\d{{8}}$', '') = p.model
                WHERE s.session_id = sid
            );
            """  # noqa: S608 — pricing model names escaped by sql_literal; the rates are floats
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO tool_rank(last_n_days) AS TABLE (
                SELECT tool_name, count(*) AS n
                FROM tool_calls
                WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                  AND tool_name IS NOT NULL
                GROUP BY 1
                ORDER BY n DESC
            );
            """
        )

        con.execute(
            """
            CREATE OR REPLACE MACRO todo_velocity(sid) AS (
                SELECT count(*) FILTER (WHERE status = 'completed')::DOUBLE
                     / NULLIF(count(DISTINCT subject), 0)
                FROM todo_state_current
                WHERE session_id = sid
            );
            """
        )

        # SCOPE: fan-out counts spawn INTENT — the number of Task/Agent
        # launches the session issued — not the number of subagent transcripts
        # that resulted. ATIF inlines sidechains, so there is no per-subagent
        # file to census, and a launch that produced no steps still counts
        # here. Read this as an upper bound on subagents realized.
        con.execute(
            """
            CREATE OR REPLACE MACRO subagent_fanout(sid) AS (
                SELECT count(*)
                FROM subagent_spawns
                WHERE session_id = sid
            );
            """
        )

        # Semantic top-k nearest neighbors over ``message_embeddings``.
        # ``query_vec`` must have the bound view's width.
        #
        # COSINE, not L2, throughout: stored document vectors are int8-cast
        # and un-normalized (observed norms 1206-1419 on the live store) while
        # ``embed_query`` returns unit-norm probes, so L2 ranks partly by
        # MAGNITUDE and puts lower-similarity rows above higher ones. ``sim``
        # and ``distance`` are the two halves of the SAME metric
        # (distance = 1 - sim); mixing cosine ``sim`` with L2 ``distance``
        # would make the two columns disagree about which hit is nearest.
        #
        # ``list_cosine_*`` rather than ``array_cosine_*``: the list variants
        # accept a fixed-size ARRAY and a variable-length LIST on either side,
        # so a caller-computed probe binds without knowing the store's width
        # at DDL time. Neither variant pushes down into the Lance index (both
        # plan a full __LANCE_TABLE_SCAN), so this costs no index lookup.
        if not skip_vss:
            con.execute(
                """
                CREATE OR REPLACE MACRO semantic_search(query_vec, k) AS TABLE (
                    SELECT me.uuid,
                           list_cosine_similarity(me.embedding, query_vec) AS sim,
                           list_cosine_distance(me.embedding, query_vec)   AS distance
                    FROM message_embeddings me
                    ORDER BY list_cosine_distance(me.embedding, query_vec)
                    LIMIT k
                );
                """
            )
        else:
            logger.debug("Skipped semantic_search macro (skip_vss=True)")

        # Skill / slash-command leaderboard over the last N days.
        con.execute(
            """
            CREATE OR REPLACE MACRO skill_rank(last_n_days) AS TABLE (
                SELECT skill_id,
                       skill_name,
                       plugin,
                       is_builtin,
                       count(*)                   AS n,
                       count(DISTINCT session_id) AS sessions
                  FROM skill_usage
                 WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                 GROUP BY 1, 2, 3, 4
                 ORDER BY n DESC
            );
            """
        )

        # How is each skill invoked? Built-ins excluded — they're almost
        # always slash-only and would drown everything else out.
        con.execute(
            """
            CREATE OR REPLACE MACRO skill_source_mix(last_n_days) AS TABLE (
                SELECT skill_id,
                       skill_name,
                       count(*) FILTER (WHERE source = 'tool')          AS n_tool,
                       count(*) FILTER (WHERE source = 'slash_command') AS n_slash,
                       count(*)                                         AS n_total
                  FROM skill_usage
                 WHERE ts >= current_timestamp - (last_n_days * INTERVAL 1 DAY)
                   AND NOT is_builtin
                 GROUP BY 1, 2
                 ORDER BY n_total DESC
            );
            """
        )

        _semantic_search_part = "" if skip_vss else "semantic_search, "
        logger.debug(
            "Registered macros: ago, model_used, cost_estimate, tool_rank, "
            f"todo_velocity, subagent_fanout, {_semantic_search_part}skill_rank, "
            "skill_source_mix"
        )
    except Exception:
        # register-or-fail-loud — any DuckDB error must surface to the caller.
        logger.exception("Failed to register macros")
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register(
    con: duckdb.DuckDBPyConnection,
    corpus_root: Path,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    skip_vss: bool = False,
    lance_uri: Path | None = None,
    expected_model: str | None = None,
    expected_dim: int | None = None,
) -> None:
    """Register raw readers, views, VSS, and macros over ``corpus_root``, in order.

    Every call re-scans and re-parses the whole corpus into TEMP tables, so
    cost is O(corpus) per connection: reuse one connection per process rather
    than registering per query.

    Order matters: raw TEMP tables first (views bind against them at CREATE
    time), then views, then VSS (``semantic_search``'s body binds against
    ``message_embeddings`` at CREATE time), then macros (macro bodies bind
    against views at CREATE time), then the v2 analytics views + macros
    (which bind against both the analytics parquets AND the base views —
    ``autonomy_trend`` joins ``sessions``, ``sentiment_arc`` joins
    ``messages``).

    Parameters
    ----------
    con
        Open DuckDB connection.
    corpus_root
        Materialized corpus root (the directory containing ``sessions/``).
    pricing
        Optional pricing override for :func:`register_macros`.
    skip_vss
        When ``True``, neither :func:`register_vss` nor the
        ``semantic_search`` macro registers. The embed backfill needs this:
        it WRITES the store that ``message_embeddings`` reads, so a
        connection opened to run the backfill cannot bind that view first.
        Default ``False`` — an empty or absent store degrades to the empty
        fallback table, so registration is otherwise always safe.
    lance_uri
        Local LanceDB dataset directory; defaults to
        ``<corpus_root>/embeddings_lance`` (atif-embed's per-corpus default).
    expected_model, expected_dim
        Active embedder identity for :func:`register_vss`'s fail-loud guard.

    Raises
    ------
    duckdb.Error
        If any registration step fails (register-or-fail-loud).
    """
    from atif_duck.infrastructure.analytics import (
        register_analytics,
        register_analytics_macros,
    )

    register_raw(con, corpus_root)
    register_views(con)
    if not skip_vss:
        register_vss(
            con,
            lance_uri=lance_uri if lance_uri is not None else corpus_root / "embeddings_lance",
            expected_model=expected_model,
            expected_dim=expected_dim,
        )
    register_macros(con, pricing=pricing, skip_vss=skip_vss)
    registered = register_analytics(con, corpus_root)
    register_analytics_macros(con, registered)
