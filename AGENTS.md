# atif-sql — operating manual

ATIF-native analytics over agent trajectories from Claude Code
(`~/.claude/projects/**/*.jsonl`) and Codex CLI
(`~/.codex/sessions/**/rollout-*.jsonl`).
Instead of SQL views over raw JSONL, this stack converts sessions to ATIF
(Harbor's Agent Trajectory Interchange Format), materializes a corpus of ATIF
documents, and layers DuckDB views on top. One corpus holds one agent, and
`sessions.agent` names it.

## Workspace layout

uv WORKSPACE (virtual root, members under `packages/*`):

- `packages/atif-converter` — converts Claude Code and Codex transcripts to
  ATIF (ported from harbor's converters, on the ATIF models vendored in
  `atif_converter.domain.atif`); owns the fidelity policy per agent (the known Claude Code
  gaps in `atif_converter.domain.fidelity`, the Codex ones in
  `atif_converter.domain.codex_fidelity`, whose values are namespaced
  `codex_*` so one `gaps_observed` array carries both). `domain.agents`
  holds the `AgentSource` enum, whose VALUES are the wire contract for the
  `--agent` flag, harbor's `Trajectory.agent.name`, and `meta.agent`.
  `domain.pricing` prices each step (`total_cost_usd`, Codex `cost_usd`) from
  the vendored `domain/model_prices.json` with litellm's arithmetic; a model
  it can't price is NULL. Layered: `application` > `infrastructure` > `domain`.
- `packages/atif-corpus` — corpus materialization: discovery, watermarks,
  quiescence, atomic artifact writes (the bulk JSON artifacts zstd-compressed),
  in-place compression of an old-layout session for `corpus slim`, the
  `ArtifactProducer` port that lets a composition root add per-session files
  to the same atomic swap (atif-cli wires none now), and the `SessionSink` port that hands
  every published session to a store kept beside the corpus (atif-duck's
  DuckLake writer). Per-agent discovery lives in
  `domain.source_layout` (`transcript_depth` 1 for Claude Code, 3 for
  Codex's `<YYYY>/<MM>/<DD>` nesting), and `domain.agents` is an AST-pinned
  twin of the converter's enum, because the two packages may not import each
  other. `domain.session_id` is the boundary a transcript's id must pass
  before it becomes a corpus path (rejected ids are reported, never
  written); atif-duck carries the twin. Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-duck` — DuckDB views + macros over the materialized corpus:
  core views and macros plus the analytics views and macros, all declared in
  a static drift-tested catalog. Who wrote a user
  step is decided ONCE, in `domain.authorship` (a prefix rule table rendered
  into the `step_author` macro and the `user_steps` / `human_turns` /
  `session_outcomes` views); atif-analytics carries an AST-pinned twin of the
  table, and every reader treats only `author = 'human'` as the user
  speaking. Also the
  `ColumnarArtifactProducer`, which types one session's trajectory into
  parquet in bounded memory (the lake loader stages each session through it),
  and the registry, which reads a session's own parquet when an old-layout
  session still has current ones and its stored trajectory otherwise. And the
  DuckLake every corpus is queried through (`domain.lake` declares the tables,
  `infrastructure.lake` writes, reads, verifies and compacts it; see
  "Storage" below). Layered: `infrastructure` > `domain`.
- `packages/atif-models` — model alias registry + structured-output LLM
  client. No other package hardcodes a Bedrock model id. Layered:
  `infrastructure` > `domain`.
- `packages/atif-analytics` — the LLM pipelines that spend money at
  Bedrock and are checkpointed per session (classify, conflicts, friction,
  perceived — see `PIPELINE_NAMES` in
  `infrastructure/sqlite_state/checkpointer.py`). They read human turns only
  (`domain.authorship`), and classify and conflicts skip automated review
  (`turn_audit`) and one-shot-job sessions. Session data comes through the
  `SessionSource` port (`domain.ports`): the default reads each session's
  `trajectory.json` and `edges.jsonl`, and atif-cli plugs in the lake's
  (`atif_cli.lake_sessions`). Both build steps through
  `domain.transcript.step_event`, so the projection rules are written once.
  The trajectory pipeline and the structural
  cluster/terms/community pipelines were cut on 2026-09-27; outcome is the
  deterministic `session_outcomes` view now. Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-embed` — Cohere Embed v4 on Bedrock + LanceDB store + the
  backfill and orphan-prune use cases. Discovery reads the lake's `steps`
  through the `LakeStepsPort` port (atif-cli implements it over atif-duck's
  `infrastructure.lake_steps`), and falls back to the per-session reader.
  Layered: `application` > `infrastructure` > `domain`.
- `packages/atif-cli` — cyclopts CLI composing the rest into commands
  (`convert`, `materialize`, `status`, `query`, `analyze`, `embed`,
  `search`, `examples`, `schema`, `cron`, `lake`, `corpus`).

Rules of the road:

- Independence contract (import-linter, `pyproject.toml`): converter /
  corpus / duck / models / embed may NEVER import each other. atif-cli is
  the composition root and may import them all. atif-analytics is the one
  exception — it may import atif-models and nothing else among our
  packages (a `forbidden` contract pins its other edges shut).
- Inter-package deps: declare in the member's `[project.dependencies]` AND
  `[tool.uv.sources] <pkg> = { workspace = true }`.
- harbor and litellm are DEV dependencies only (`[dependency-groups] dev`);
  `src/` imports nothing from either, and
  `packages/atif-converter/tests/test_dev_only_imports_guard.py` fails on any
  import, in any member. The ATIF data classes and the validator are harbor
  0.24.0's, vendored under Apache-2.0 in `atif_converter.domain.atif` (each
  file byte for byte upstream apart from its header and import path; ruff and
  ty skip the directory so it stays that way). The raw-JSONL → `Trajectory`
  conversion is ours (`atif_converter.domain.claude_code_conversion`,
  `domain.codex_conversion`, ported from harbor and held to parity with
  0.24.0). harbor survives in the tests
  as two oracles: its private converters in
  `packages/atif-converter/tests/harbor_oracle.py` (frozen goldens under
  `tests/goldens/`, plus a live-corpus parity test), and
  `tests/test_vendored_atif.py`, which compares the vendored files to the
  installed harbor's source and round-trips the goldens through both sets of
  models and validators. A harbor bump is a lockfile edit plus reading those
  failures as upstream-behavior reports (see CONTRIBUTING).
- Pricing reads `atif_converter/domain/model_prices.json`, a filtered copy of
  litellm's public price data (MIT, notice in its `meta` block) holding the
  Claude and OpenAI text models our transcripts name, plus a few local
  overrides with cited sources. `scripts/update_prices.py` regenerates it by
  hand (`uv run scripts/update_prices.py --ref <litellm tag>`); never edit the
  JSON directly. `domain.pricing` repeats `litellm.cost_per_token`'s arithmetic
  in the same float order, and `tests/test_pricing_identity.py` proves it bit
  for bit against the dev litellm over every vendored entry whose data the
  installed litellm shares; `tests/test_pricing_policy.py` pins frozen values
  that run without litellm. A model the table doesn't price is NULL, never $0.
- The converter reads each transcript file ONCE. `raw_records.load_session`
  fingerprints and parses a file in the same pass, and the converter, the
  census, the edges emitter and the enrichment pass all consume that one list
  of records; the fingerprints are re-checked (stat and digest) after the
  artifacts are built. Per file that's two opens, one parse and two hash
  passes, pinned by `test_snapshot_and_drift.py::TestSinglePass` for both
  agents, and by `test_source_archive.py::TestArchivingCostsNoRead` with the
  source archive on. Don't add a reader that opens the session again; take the
  records from the `LoadedSession`, and take raw bytes from the re-check pass.
- loguru only, never stdlib logging (ruff banned-api enforces it).
- SQL text is constants only. No corpus path may be spliced into a statement:
  `read_json(?)` takes globs and file lists as bound parameters, parquet file
  lists go through `con.read_parquet(files).create_view(...)`, and the
  statements DuckDB won't prepare (`ATTACH`, the producer's session row) go
  through `sql_literal`. Helpers that build SQL take and return `SqlFragment`
  (`atif_duck.domain.sql_literal`). Every site that still interpolates
  carries `# noqa: S608  # nosec B608 - <what it interpolates>` (ruff reads
  the first marker, Bandit only the second; never blanket-skip B608 in
  `[tool.bandit]`), and `packages/atif-duck/tests/test_sql_text_boundaries.py`
  runs an AST audit over `registry.py`, `columnar.py`, `analytics.py`,
  `authorship.py`, `lake.py` (both layers of each), `lake_steps.py` and atif-embed's
  `corpus_text_rows.py` that fails on any placeholder that isn't a constant, a
  projection call, or `sql_literal(...)`. A statement built per table is a
  module-level constant (a dict comprehension over the table specs), which the
  audit reads like any other constant. Session ids are the
  one outside text that becomes a path; they're validated at the boundary
  (`domain.session_id` in atif-corpus and atif-duck, twinned) rather than
  escaped downstream.
- Settings via pydantic-settings, env prefix `ATIF_SQL_`. `agent` is applied
  at CONSTRUCTION, not copied in afterwards: both default roots derive from
  it, and only roots absent from `model_fields_set` re-derive, so an explicit
  `ATIF_SQL_SOURCE_ROOT` or `ATIF_SQL_CORPUS_ROOT` still wins. That override is
  also the one way to aim a pass at the other agent's corpus, which
  `meta.agent` catches: materialize refuses (exit 78) rather than marking the
  other agent's sessions as source-removed.
- The corpus never deletes a session. When a source transcript disappears
  (Claude Code and Codex expire old ones), materialize keeps the session's
  artifacts and rewrites its `meta.json` once with `source_present: false` and
  `source_removed_at`. The report's `removed` counts sessions newly marked that
  pass and `retained` counts every session kept without a source. A source that
  comes back is re-converted from the live file, which clears the mark.
- Every live conversion writes a raw source archive into the same staged swap:
  `sessions/<id>/source/<path>.zst`, one zstd file per source file (the main
  transcript, side-file transcripts, and every other file under the session's
  side dir), listed in `meta.source_archive` with sizes and sha256s. The
  transcript bytes come from the converter's verifying re-read (a
  `SourceArchiveWriter` handed to `mutated_files`), so archiving adds no open
  and the archive is exactly what was parsed. `restore_session_sources` in
  `atif_corpus.infrastructure.source_archive` rebuilds the tree, and a
  source-removed session whose generation is stale re-converts from it.
- Staleness has two halves. The watermark says whether the SOURCE moved; the
  generation says whether the CODE did: a session whose `meta.converter_schema`
  differs from the running value is stale, and one from before the key existed
  is stale too. `converter_schema`
  is `atif_converter.domain.schema_version.CONVERTER_SCHEMA_VERSION`;
  `meta.converter_version` is the `atif-sql` release and is provenance only
  (the bundled wheel has no `atif-converter` distribution, so the old lookup
  answered `"unknown"`). THE RULE: bump `CONVERTER_SCHEMA_VERSION` in the same
  commit as any change that can alter a byte of any artifact for any input, and
  a bump re-converts the whole corpus (source-removed sessions from their
  archive). `packages/atif-converter/tests/test_converter_schema_version.py`
  pins a digest of the converter's code (docstrings and comments excluded)
  beside the version, so any converter code change fails until you decide: bump
  if output can change, then re-pin the digest either way. A harbor or litellm
  bump can change output with no converter code change, so it needs the same
  decision by hand. atif-duck's `COLUMNAR_SCHEMA_VERSION` no longer makes a
  session stale, because materialize writes no per-session parquet: a bump
  changes the lake's recorded identity, so the next materialize rebuilds the
  lake, and an old-layout session whose `meta.columnar_schema` no longer
  matches is read from its trajectory.
- A transcript with nothing to convert (the converter's `EmptySessionError`,
  translated to the port's `EmptySourceError`) is recorded in
  `<corpus>/empty_sessions.json` with the generation it was checked under, its
  watermark advances, and it's reported under `empty`, never `failed`. A write to
  it or a `CONVERTER_SCHEMA_VERSION` bump tries it again.
- `materialize` runs its convert+write stage on a spawn-context process pool
  (`--workers N` / `ATIF_SQL_MATERIALIZE_WORKERS`, default `min(8, cpu_count)`).
  `--workers 1` is the single-process reference path and must stay
  byte-identical to the pool; the pool tests in
  `packages/atif-corpus/tests/test_materialize_parallel.py` pin that, and
  read worker pids back off disk so a pool that silently ran inline fails.
  The `ConverterPort` instance is pickled into each worker, so an adapter
  has to stay picklable.

## Storage

The per-session artifacts under `<corpus>/sessions/<id>/` are the source of
truth. Beside them sits one DuckLake that holds every corpus, at
`ATIF_SQL_LAKE_ROOT` (default `~/.atif-sql/lake/`), and the lake is the one
copy of the queryable rows:

- Per-session artifacts: materialize writes `trajectory.json.zst` (the ATIF
  document, the interchange copy and what the lake loads from),
  `edges.jsonl.zst`, `session_events.jsonl.zst`, a plain `loss_report.json`
  and `meta.json`, and the raw source archive under `source/`. Each `.zst` is
  one zstd frame whose content size is in its header; decompressed, it's byte
  for byte the plain file materialize used to write
  (`atif_corpus.infrastructure.atomic.write_json_zstd_atomic`, which
  serializes twice so a large document is never held as one string). The
  names live in `atif_corpus.domain.layout` and are twinned in
  `atif_duck.domain.artifacts`, `atif_analytics.infrastructure.corpus_reader`
  and `atif_embed.infrastructure.corpus_text_rows`, pinned by
  `packages/atif-cli/tests/test_artifact_names_twin_pin.py`. Every reader
  resolves a session's stored file per session, compressed first
  (`stored_names`), so a corpus written before this (plain JSON plus five
  per-session parquet files) keeps working unchanged. Path columns
  (`sessions.trajectory_path`, the edges reader's `edges_path`) name the
  logical `trajectory.json` / `edges.jsonl` either way, so a lake row doesn't
  change when a session is compressed.
- `atif-sql corpus slim` converts an old-layout corpus, and nothing else does:
  deploying a new version never rewrites or deletes an artifact. It's a dry
  run unless `--no-dry-run`, and the dry run compresses into a byte counter,
  so its report is exact. For real, it compresses each plain artifact in place
  (`atif_corpus.infrastructure.compress_artifacts`: the copy is read back and
  compared with the plain file before the plain file is removed, and it keeps
  the plain file's mtime, because analyze bounds each session by its
  trajectory's mtime), then runs `lake verify` for that corpus reading every
  session from its trajectory and ignoring the parquet
  (`use_session_parquet=False`), and deletes the parquet only when that comes
  back clean. A corpus the lake doesn't hold or that differs keeps its
  parquet, and slim exits 78 or 65 once every corpus is done. It never
  rewrites `meta.json`, so the lake's `session_meta` rows stay equal to it.

- Layout: `catalog.duckdb` is the writer's catalog, `catalog.reader.duckdb`
  is a read-only copy the writer publishes after every write, `data/` holds
  the parquet files, and `<root>.lock` beside the root is the writer's flock.
  The catalog is a DuckDB file, not SQLite, on purpose: a reader with the
  sqlite extension loaded can `sqlite_scan` any SQLite file on the host
  whatever `enable_external_access` says, so the query path never loads it.
- Tables: one per artifact kind (`sessions`, `steps`, `tool_calls`,
  `tool_results`, `session_events`, `edges`, `loss_reports`, `session_meta`),
  each the registry's raw reader shape (`atif_duck.domain.raw_readers`, which
  derives from the catalog and the columnar schema) with `corpus`, `agent` and
  `session_id` in front. A source column that collides with an identity name is
  stored as `src_<name>`. Nothing is declared twice: `domain.lake` builds every
  table and statement from those shapes.
- Partitioning: `agent`, `corpus`, then year and month of the row's time
  (`ts`, or the first step's time for `sessions`). `loss_reports` and
  `session_meta` have no time column and stop at `agent`, `corpus`. The
  module docstring says why.
- Schema identity: the lake records `LAKE_SCHEMA_VERSION`, a digest of the
  table definitions and the `COLUMNAR_SCHEMA_VERSION` it was loaded under.
  Any of them differing from the running code makes the lake stale: the next
  materialize rebuilds it and `query` falls back meanwhile.
  `packages/atif-duck/tests/test_lake.py` pins the version and digest together,
  so a change to any shape the lake derives from fails until you decide whether
  it needs a version bump, then re-pin.
- Writer: only the materialize PARENT writes, through the `SessionSink` port
  (`atif_corpus.domain.ports`), which atif-duck implements as
  `DuckLakeSessionSink` and atif-cli wires in. After the swaps, materialize
  hands it every session published or marked source-removed this pass, in plan
  order, in batches (`ATIF_SQL_LAKE_SYNC_BATCH_SIZE`); each batch is one
  transaction that deletes and re-inserts those sessions' rows in every table.
  Pool workers never touch the lake, so a pool and `--workers 1` write the same
  rows. Before anything publishes, the pass records the sessions it's about to
  hand over in `<corpus>/sink_pending.json`; a sink failure or a killed pass
  leaves them there and the next pass hands them over again, source change or
  not. A failed lake write never fails the pass. With no lake, the sink does
  nothing, so `atif-sql lake rebuild` is what turns it on. A corpus the lake
  doesn't hold yet is loaded whole on first sync.
- Loading a batch (sync, first load, rebuild, and verify's reading of the
  artifacts): a session with current parquet of its own (an old-layout
  session) is read from it. Every other session is staged: its stored
  trajectory is decoded in Python and typed by the `ColumnarArtifactProducer`
  into five parquet files under `<lake root>.load-<pid>/<id>/`, which the
  registry reads (`register_raw(stage_columnar=...)`), and the batch deletes
  them when it's done. DuckDB's own JSON reader can't be the load path: on the
  largest sessions it needs more than the writer's whole memory cap. The
  staged rows are the ones the JSON reader yields (the columnar parity tests,
  and `test_compressed_layout.py::TestStagedLoad`). `ATIF_SQL_LAKE_STAGE_WORKERS`
  stages a batch on a spawn-context process pool; the files, and so the
  inserted rows, don't depend on it. A dead process's `.load-<pid>` is swept
  like `.rebuild-<pid>`.
- Reader: `query` loads ducklake (never installs it) and attaches
  `catalog.reader.duckdb` READ_ONLY before the sandbox locks the connection,
  then binds the raw relations as views over the lake tables, scoped to the
  requested corpus (`--all-corpora` spans every corpus, and `sessions.corpus`
  tells them apart). Its file grants name each live data and delete file, never
  the data directory: with the directory granted, a caller's
  `ducklake_cleanup_old_files` deletes files. With no lake, a stale one, or a
  corpus the lake doesn't hold, query prints one warning and reads the
  per-session artifacts as before (`--no-lake` forces that). That fallback
  parses every trajectory on each run and holds each one in memory, so it's
  slow on a large corpus, and a full scan of `tool_calls` or `tool_results`
  there can run out of query memory. That error's hint points at `lake
  rebuild`. `search` reads the same way. Each corpus keeps its own embeddings store in its directory,
  and under `--all-corpora` `message_embeddings` is the union of every
  corpus's store (one store for all when `ATIF_SQL_LANCE_URI` pins it). The
  analytics views read each corpus's `analytics/` parquets on either path,
  and with `--all-corpora` they read every lake corpus's (granted file by
  file like the rest). Those rows carry no corpus column, so a session id
  held by two corpora (a copied corpus) matches both corpora's analytics
  rows there.
- Embed discovery: `atif-sql embed` reads the steps to embed from the lake's
  `steps` table (primary uuid = `source_uuids[0]`, text = `message`, the
  store's existing key and text), in the per-session reader's order, so the
  row set and every stored vector stay as they were. Only rows whose uuid a
  lake snapshot inserted or deleted since the last complete run are read:
  `lake_watermark.json` inside the store directory records that run's lake
  position (the corpus's `registered_at` as the lineage, plus the snapshot
  id), the text floor, and the store's row count. A rebuilt lake, expired
  snapshots, a changed floor or a store count that moved outside a run all
  force a full lake read; no usable lake falls back to the per-session
  reader. The watermark only moves after a run that read to the end (not to
  a `--limit`) and embedded every row. `embed --prune-orphans` counts stored
  rows whose uuid is no lake step's (dry run unless `--no-dry-run`, refused
  while sessions are pending a lake write).
- Analytics: `analyze` reads session data from the lake when the lake holds
  the corpus (`--no-lake` reads the files). A session is read from the lake
  only when its `session_meta` row carries the `materialized_at` and
  `source_mtime_ns` its `meta.json` carries now; any other session (not
  loaded, left behind by a failed or skipped lake write) is read from its
  files, so a lagging lake never hands a pipeline an older transcript. Session
  enumeration and the stored trajectory's mtime bound come from the session
  directories either way, so the checkpoint sees the same bounds (`corpus
  slim` keeps each file's mtime for this reason). A lake
  read that fails mid-run (a rebuild swapped the lake out, or compact removed
  a file the open attach still names) warns once, and the rest of the run
  reads files. The reader drops a memoized session whenever its bounds move
  between stages. The outputs stay parquet under `analytics/`, not lake tables: they're a few
  small shard directories per corpus that one process appends to, so the lake
  would add a second writer to lock against materialize and a pending ledger
  for a failed sync, and buy no fewer files.
- Row order and layout: a step's tool calls and results have no ordinal, so
  their order is insertion order (`rowid`), and the writer inserts the
  step-level tables (`SOURCE_ORDERED_TABLES`) on one thread, since a parallel
  partitioned insert interleaves rows. Files are written in small row groups
  (`LAKE_ROW_GROUP_SIZE`), so reading one session touches only the groups
  holding it rather than a month of a corpus. Both are part of the schema
  digest, so a lake written otherwise is stale and the next materialize
  rebuilds it.
- Commands: `atif-sql lake rebuild` builds a fresh lake from every registered
  corpus plus every corpus under `ATIF_SQL_CORPUS_BASE` (or the
  `--corpus-root`s given) beside the old one and swaps the directory in.
  `lake verify` compares every session's row count and content hash, table by
  table, between the lake and its artifacts and exits 65 on a difference (78
  with no usable lake). `lake status` (also folded into `atif-sql status`)
  reports the state, snapshots, files and pending sessions. `lake compact`
  merges small files, rewrites delete-heavy ones, expires snapshots older than
  `--expire-older-than-days` (default 30) and removes unreferenced files once
  they're an hour old, so a reader on the previous catalog copy still finds
  its files. `--memory-limit SIZE` replaces the host-derived DuckDB budget
  (the writer's ceiling still applies). The refresh script's nightly
  `compact` lane runs it with an explicit budget under its own lock and 4G
  cap, holding the materialize lane's lock too, so it never overlaps the
  lake's writer. A DuckDB error (an out-of-memory merge) exits 70 and
  publishes nothing.
- Memory: the writer caps DuckDB at 2 GiB (lower when the host or cgroup is),
  and runs DuckLake's file merges and rewrites on one thread, because merging
  `tool_results` at more threads outgrew that cap. Each staging worker holds
  one decoded trajectory at a time, outside that cap. `query` sizes its own cap
  from the cgroup v2 `memory.max` (the tightest one up the cgroup tree) when
  it's lower than what `/proc/meminfo` reports.

## Agent query workflow

For an LLM agent driving `atif-sql`, the discovery loop is this:

1. `atif-sql schema` — every view (with columns) and macro signature, core
   and analytics, each with what it `requires`, from the static catalog in
   <50 ms. Its output ends with an `examples_hint`.
2. `atif-sql examples` (alias: `atif-sql query --examples`) — runnable
   example queries for every view and macro, DERIVED from the catalog (never
   hardcoded per object) and each one EXECUTED by
   `packages/atif-duck/tests/test_examples.py` against a fixture corpus —
   the header's "test-executed against this version" is literal. Piped
   output is JSON `{note, examples: [{name, sql, description, requires,
   category}]}`; filter with `--requires core|analytics|vss` and
   `--category view|table-macro|scalar-macro`.
3. `atif-sql query '<sql>'` — run it. `--agent codex` points it at the Codex
   corpus without spelling the path, and `--all-corpora` runs it over every
   corpus the lake holds (`sessions.corpus` names each row's corpus). Copy an example verbatim (the `sid`
   exemplar is a subquery over `sessions`, so it works on any corpus) or
   adapt it. `requires: analytics` needs `atif-sql analyze` to have run;
   `requires: vss` needs `atif-sql embed --all --no-dry-run` (a bare `embed`
   exits 64: a real run needs an explicit scope). For semantic search over TEXT,
   prefer `atif-sql search 'query'` — it embeds the text first, then runs
   the same `semantic_search` kNN.

   What the query sandbox will and won't do: `SELECT`, `EXPLAIN`, `SET
   TimeZone`, and in-memory DDL/DML run; `COPY`, `EXPORT`, `ATTACH`,
   `DETACH`, `INSTALL`, `LOAD`, `PREPARE` and `EXECUTE` exit 70 with kind
   `sandbox_refused` before anything executes, so emit results on stdout
   rather than writing files. Running as uid 0 exits 77 (`root_refused`)
   unless `ATIF_SQL_ALLOW_ROOT=1`. The connection is sized to the host
   before registration (`ATIF_SQL_QUERY_MEMORY_LIMIT` / `ATIF_SQL_QUERY_THREADS`
   override), and no extension is ever installed at query time: `atif-sql
   status` reports `vector_search` as `ready`, `no_store`, or
   `extension_missing` (fix the last with `atif-sql embed --install-extension`).

Adding a view/macro? The drift tests force: a `DESCRIPTIONS` entry in
`atif_duck/domain/catalog.py`, an `ARG_EXEMPLARS` entry for any new
parameter name, `TABLE_MACRO_NAMES` membership if the DDL is `AS TABLE`, and
the derived example must execute — or a documented `EXCLUSIONS` entry.

## Definition of done

`mise run check` fully green (lint + fmt + typecheck + lint:imports + lint:workflows +
lint:hooks + lint:pnpm-lock + security:vex:check + docs:prose + test). `docs:prose` is Vale over
the published prose (docs/, site/authored/, README.md, AGENTS.md, CONTRIBUTING.md) at error level,
with a file floor per glob so a shrunken scope is red, and a per-rule warning ratchet against `.vale-baseline.json`: reword the sentence it flags, or add a real term to `.vale/styles/config/vocabularies/atif-sql/accept.txt`. When a count falls, run `mise run docs:prose:baseline` and commit the lower file; never raise it. `mise run security` is the report-only tier beside it: findings do not fail
it, a scanner that produced no usable SARIF does.
First-time setup: `mise trust && mise install && mise run install`.

## Experiments

`experiments/` holds numbered experiment protocols (README per experiment).
Outputs go to `experiments/**/out/` (gitignored). No experiment is wired
into a gate — `mise run check` and pytest's `testpaths` both skip the
directory.

Migration parity oracles are not part of this repo. Parity is not a standing
gate: re-running one would need a reference implementation this workspace does
not depend on.
