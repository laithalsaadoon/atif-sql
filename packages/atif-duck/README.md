# atif-duck

DuckDB views and macros over the materialized ATIF corpus
(`<corpus_root>/sessions/<id>/{trajectory.json, edges.jsonl,
loss_report.json, meta.json}` per `docs/CONTRACT.md`, plus the optional typed
columnar artifacts `session.parquet`, `steps.parquet`, `tool_calls.parquet`,
`tool_results.parquet`).

`atif_duck.register(con, corpus_root)` wires a connection to the corpus and
exposes the whole query surface: the core views and macros, the vector-search
view, and the v2 analytics views and macros. It returns a `RawSources` naming
which sessions were read from their parquet artifacts and which from
`trajectory.json`; the views union the two and return the same rows either way.

## Columnar artifacts

`ColumnarArtifactProducer` implements atif-corpus's `ArtifactProducer` port
(atif-cli plugs it into `materialize`). Given a session's trajectory it writes
the four parquet files with the views' own projection expressions
(`atif_duck.infrastructure.projections`), so a query over them is exactly the
query over the JSON, with no JSON parsed at query time. The file names and the
`columnar_schema` version they're claimed under live in
`atif_duck.domain.columnar`; `columnar_coverage(corpus_root)` is what
`atif-sql status` reports as the query path.

## SQL text and the session id boundary

The registry builds its statements from f-strings, and every placeholder in
them is one of three things: a module or catalog constant, a projection
expression from `atif_duck.infrastructure.projections`, or `sql_literal(...)`.
Those three producers return `SqlFragment` (a `NewType` over `str` in
`atif_duck.domain.sql_literal`), which is the type every SQL-building helper
takes and returns. Anything that came from outside the process is a plain
`str` and reaches DuckDB another way:

- corpus globs and file lists go to `read_json(?)` as bound parameters;
- parquet file lists go through `con.read_parquet(files)`, and that relation
  is registered as a view (`CREATE VIEW` can't take a parameter), so the view
  the union reads from holds no path text;
- the Lance store's `ATTACH` and the producer's one-row session projection
  are the two statements DuckDB won't prepare, so they still pass through
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

## Core surface (16 views, 9 macros)

- **Views**: `sessions`, `steps` (one row per ATIF step — the
  messages-parity view), `messages` (uuid-keyed COMPAT view from
  `edges.jsonl`), `tool_calls`, `tool_results`, `todo_events`,
  `todo_state_current`, `subagent_spawns`, `subagent_steps`,
  `task_creations`, `task_updates`, `tasks_state_current`,
  `skill_invocations`, `skill_usage`, `loss_reports`, and
  `message_embeddings` (the VSS view; empty until `atif-sql embed --all
  --no-dry-run` runs).
- **Macros**: `ago`, `model_used`, `cost_estimate`, `tool_rank`,
  `todo_velocity`, `subagent_fanout`, `skill_rank`, `skill_source_mix`, and
  `semantic_search(query_vec, k)` (kNN over `message_embeddings`; skipped
  when `register(..., skip_vss=True)`).

## v2 analytics surface (12 views, 13 macros)

Bound from the parquet artifacts atif-analytics writes under
`<corpus_root>/analytics/`, so each view registers only once its backing
parquet is populated — run `atif-sql analyze` first.

- **Views**: `session_classifications`, `session_goals`,
  `message_trajectory`, `session_conflicts`, `conflicts_summary`,
  `user_friction`, `perceived_errors`, `perceived_summary`,
  `message_clusters`, `cluster_terms`, `session_communities`,
  `community_profile`.
- **Macros**: `work_mix`, `autonomy_trend`, `success_rate_by_work`,
  `sentiment_arc`, `conflicts_over_time`, `friction_rate`,
  `friction_counts`, `friction_examples`, `perceived_rate`,
  `perceived_counts`, `perceived_examples`, `cluster_top_terms`,
  `community_top_topics`.

The static catalogs (`VIEW_NAMES`, `VIEW_SCHEMA`, `MACRO_NAMES`,
`MACRO_SIGNATURES`, `ANALYTICS_VIEW_NAMES`, `ANALYTICS_MACRO_SIGNATURES`)
live in `atif_duck.domain.catalog`; drift against the actual DDL is gated by
CI tests.
