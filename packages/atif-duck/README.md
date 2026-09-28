# atif-duck

DuckDB views and macros over the materialized ATIF corpus
(`<corpus_root>/sessions/<id>/{trajectory.json.zst, edges.jsonl.zst,
session_events.jsonl.zst, loss_report.json, meta.json}` per `docs/CONTRACT.md`).
A corpus written by an earlier version holds the plain `trajectory.json`,
`edges.jsonl` and `session_events.jsonl` and the typed columnar artifacts
`session.parquet`, `steps.parquet`, `tool_calls.parquet`, `tool_results.parquet`
and `session_events.parquet` until `atif-sql corpus slim` converts it; every
reader takes either layout, a session at a time
(`atif_duck.infrastructure.stored_artifacts`).

`atif_duck.register(con, corpus_root)` wires a connection to the corpus and
exposes the whole query surface: the core views and macros, the vector-search
view, and the v2 analytics views and macros. It returns a `RawSources` naming
which sessions were read from parquet and which from their trajectory; the
views union the two and return the same rows either way. DuckDB decompresses a
`.zst` file itself, and the path columns name the logical `trajectory.json` and
`edges.jsonl` whichever file is on disk.

## Columnar artifacts

`ColumnarArtifactProducer` writes a session's parquet files with the views'
own projection expressions (`atif_duck.infrastructure.projections`), so a query
over them is exactly the query over the JSON. materialize no longer runs it.
The lake loader does: it decodes each trajectory in Python, stages the parquet
in a scratch directory beside the lake, loads it, and removes it. DuckDB's JSON
reader needs several times a large document's size in memory, and the staged
path doesn't. `ATIF_SQL_LAKE_STAGE_WORKERS` sets how many processes stage in
parallel. The file names and the `columnar_schema` version live in
`atif_duck.domain.columnar`.

## SQL text and the session id boundary

The registry builds its statements from f-strings, and every placeholder in
them is one of these: a module or catalog constant, a projection
expression from `atif_duck.infrastructure.projections`, or `sql_literal(...)`.
Those producers return `SqlFragment` (a `NewType` over `str` in
`atif_duck.domain.sql_literal`), which is the type every SQL-building helper
takes and returns. Anything that came from outside the process is a plain
`str` and reaches DuckDB another way:

- corpus globs and file lists go to `read_json(?)` as bound parameters;
- parquet file lists go through `con.read_parquet(files)`, and that relation
  is registered as a view (`CREATE VIEW` can't take a parameter), so the view
  the union reads from holds no path text;
- the Lance store's `ATTACH` and the producer's one-row session projection
  are the statements DuckDB won't prepare, so they still pass through
  `sql_literal`, and a test each fails when that wrapping is removed.

`packages/atif-duck/tests/test_sql_text_boundaries.py` pins all of this: a
corpus under a root named `o'brien ?; --$1` whose sessions carry SQL text in
messages, tool arguments and results registers as data on both the JSON and
the columnar path; the AST audit in `sql_text_audit.py` fails on a planted
`{user_input}`; and the remaining `# noqa: S608  # nosec B608 - <reason>`
markers each name what they interpolate.

The one piece of outside text that becomes a path is the session id, and it
is checked at the boundary instead of escaped downstream. A session dir whose
name fails `atif_duck.domain.session_id` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, at
most 255 characters) registers nothing, is logged once, and is reported in
`RawSources.rejected_session_ids`; `columnar_coverage` skips it the same way.
The module is a deliberate twin of `atif_corpus.domain.session_id`, pinned by
a test on each side, because the corpus writer applies the same rule before
it writes.

## Core surface

- **Views**: `sessions`, `steps` (one row per ATIF step — the
  messages-parity view), `messages` (uuid-keyed COMPAT view from
  `edges.jsonl`), `tool_calls`, `tool_results`, `todo_events`,
  `todo_state_current`, `subagent_spawns`, `subagent_steps`,
  `task_creations`, `task_updates`, `tasks_state_current`,
  `skill_invocations`, `skill_usage`, `loss_reports`,
  `message_embeddings` (the VSS view; empty until `atif-sql embed --all
  --no-dry-run` runs), and the three authorship views `user_steps`,
  `human_turns`, `session_outcomes` (below).
- **Macros**: `ago`, `model_used`, `cost_estimate`, `tool_rank`,
  `todo_velocity`, `subagent_fanout`, `skill_rank`, `skill_source_mix`,
  `semantic_search(query_vec, k)` (kNN over `message_embeddings`; skipped
  when `register(..., skip_vss=True)`), and `step_author(src, msg)`.

## Authorship: who wrote a user step

Claude Code and Codex put a lot of machine-written text in the user role:
Stop hook feedback, task notifications, retry nudges, skill bodies, image
metadata, turn-audit prompts. The authorship surface tells those apart from
what a human typed. It reads only `steps`, so it registers on every corpus
(`requires: core`), before any analytics pipeline has run.

- `step_author(src, msg)` returns `human`, `stop_hook`,
  `task_notification`, `harness`, or `audit_prompt`, and NULL for a step
  that isn't a user step. It's a prefix match after stripping leading
  space, tab, CR and LF; the first rule that matches wins.
- `user_steps` has one row per user step, sidechains included:
  `session_id`, `step_id`, `ts`, `uuid`, `is_sidechain`,
  `is_compact_summary`, `author`, `message`. A compaction summary is
  `harness` whatever its text says.
- `human_turns` is the main-chain rows of `user_steps` with
  `author = 'human'`: `session_id`, `step_id`, `ts`, `uuid`, `message`.
  The analytics rate macros count their denominators from it.
- `session_outcomes` has one row per session: `kind` (`interactive`,
  `one_shot_job`, `turn_audit`), `outcome` (`pass`, `block`,
  `reviewer_blocked`, `interrupted`, `clean_end`), and the counts
  `human_turns`, `interrupts`, `reviewer_blocks`.

The rule table lives in `atif_duck.domain.authorship` and the DDL in
`atif_duck.infrastructure.authorship.register_authorship`, which `register`
calls after the core macros and before the analytics surface. atif-analytics
can't import atif-duck, so `atif_analytics.domain.authorship` carries a twin
of the table; `packages/atif-duck/tests/test_authorship_twin_pin.py` fails on
any difference, and `packages/atif-cli/tests/test_authorship_parity.py` runs
the SQL and the Python side by side over the same steps.

## v2 analytics surface

Bound from the parquet artifacts atif-analytics writes under
`<corpus_root>/analytics/`, so each view registers only once its backing
parquet is populated — run `atif-sql analyze` first.

- **Views**: `session_classifications`, `session_goals`,
  `session_conflicts`, `conflicts_summary`, `user_friction`,
  `perceived_errors`, `perceived_summary`. In `session_classifications`,
  `category` is an alias of `work_category`, and `autonomy_tier` and
  `success` are compatibility columns that are always NULL.
- **Macros**: `work_mix`, `friction_counts`, `friction_rate`,
  `friction_examples`, `conflicts_over_time`, `perceived_counts`,
  `perceived_rate`, `perceived_examples`. `friction_rate` and
  `perceived_rate` count their denominators from `human_turns`.

The static catalogs (`VIEW_NAMES`, `VIEW_SCHEMA`, `MACRO_NAMES`,
`MACRO_SIGNATURES`, `ANALYTICS_VIEW_NAMES`, `ANALYTICS_MACRO_SIGNATURES`)
live in `atif_duck.domain.catalog`; drift against the actual DDL is gated by
CI tests.
