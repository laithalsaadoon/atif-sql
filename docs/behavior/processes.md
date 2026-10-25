# atif-sql · Processes

Every process in this system starts as a CLI invocation. There is one console
script, `atif-sql = "atif_cli.app:main"`
(`pyproject.toml:75`), and its commands are the complete
initiator set: the nine `@app.command` sites plus the `cron`, `lake`, and `corpus` sub-apps registered at
`packages/atif-cli/src/atif_cli/app.py:104-109`. No HTTP route, RPC tool, message
handler, or job queue exists to initiate anything else; the only scheduled
initiators are the crontab lines, one per lane, into `scripts/atif-sql-refresh.sh`. Long-running
work is in-process async under `anyio` and `asyncio.run`, not a worker.

## materialize: sync the materialized corpus

Entry point: `packages/atif-cli/src/atif_cli/app.py:878`

1. Resolve `CorpusSettings` and wire the pass: `--source-root` given without
   `--corpus-root` re-derives the corpus root from the overridden source's slug
   so re-pointing the source cannot overwrite another corpus, and the CLI hands
   in the wall clock plus the harbor and converter version pins alongside
   `RealConverter` behind `ConverterPort` (`:639-640`, `:973-989`,
   `packages/atif-cli/src/atif_cli/converter_adapter.py:80`).
2. Sweep `.staging/`: an entry whose `tmp-<pid>` owner is a dead pid is crash
   debris and is removed, while a live pid belongs to a concurrent pass and is
   left alone
   (`packages/atif-corpus/src/atif_corpus/application/materialize.py:971`).
3. Read `watermark.json` as a path-to-mtime map; an unreadable or wrongly
   shaped file degrades to empty, costing one full re-materialization rather
   than refusing to sync: `packages/atif-corpus/src/atif_corpus/application/materialize.py:424`.
4. Scan the source root for main transcripts and every side-file, then fold
   sessions living under a directory that would not list into the unreadable
   set using the watermark as the only record of what lived there:
   `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:206`,
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:1092`.
5. Guard, then retain sourceless sessions: a corpus whose `meta.json` names
   another agent raises `CorpusAgentMismatchError` before anything is touched,
   and zero scanned sessions over a
   non-empty corpus raises `SuspiciousEmptyScanError` instead of marking
   everything source-removed. Otherwise every corpus session with no source in
   the scan is KEPT and marked `source_present: false` once; marking is skipped
   entirely when any source directory failed to list, because absence is then
   not evidence of deletion (`_retain_sourceless_sessions`).
6. Build the pure `MaterializationPlan` from quiescence, the watermark, and
   each session's recorded generation, force-replanning sessions the watermark calls current whose artifact
   directory is missing (the one state reachable by a kill inside the swap
   window): `packages/atif-corpus/src/atif_corpus/domain/sessions.py:169`,
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:1017`.
7. Per planned session: convert (the converter writes the raw source archive
   while it verifies its input), store the attachments in the shared blob
   store, write trajectory, loss report, edges, and session events, and `meta.json` last into a staging directory, then rename the whole directory
   into `sessions/<id>/` so a reader observes only a complete generation; a
   session that raises is recorded and the pass continues (`packages/atif-corpus/src/atif_corpus/application/materialize.py:612`,
   `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:197`).
   A retained session whose recorded generation is stale re-converts from its
   restored source archive in the same stage.
   With `--workers` above 1 this step runs on a spawn-context process pool;
   each worker runs the same per-session function under its own pid, outcomes
   are folded back in plan order, and a single planned session runs inline:
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:874`.
8. Hand every session published or marked this pass, plus any an earlier pass
   left in `sink_pending.json`, to the DuckLake session sink in batches
   (`--no-lake` skips it); the first batch that raises, and every batch after
   it, stays pending for the next pass:
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:384`.
9. Advance the watermark for succeeded sessions only, retaining every entry of
   a failed, unplanned, or unreadable session so staleness still signals a
   retry, and write it atomically before emitting the report:
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:1310`,
   `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:72`.

### Related

- `packages/atif-corpus/src/atif_corpus/domain/ports.py:101`
- `packages/atif-corpus/src/atif_corpus/domain/layout.py:65`
- `packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:88`
- `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:51`
- `packages/atif-cli/src/atif_cli/app.py:780`

## convert: one session to ATIF plus a loss audit

Entry point: `packages/atif-cli/src/atif_cli/app.py:690`

1. Reject a path that is not an existing `.jsonl` file before the fingerprint
   snapshot reads anything, which the CLI maps to exit 64:
   `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:71-73`.
2. Read every source file once: the main transcript and each discovered
   side-file under `<stem>/`, workflow-nested ones included. Each file is
   stat'ed, then streamed through a sha256 digest into the JSON parser, so the
   fingerprint recorded is the fingerprint of the bytes parsed:
   `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py`
   (`load_session`), reached through `harbor_adapter.read_session`, which
   classifies a non-UTF-8 file as `ConversionError`.
3. Replace every inline base64 attachment with a placeholder and collect its
   bytes, then convert the records with our ported converter (a port of harbor
   0.24.0's, built on the ATIF models vendored from harbor,
   `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:2`) and dump the result to a
   JSON-mode dict; a `None` return means no convertible events and raises
   `EmptySessionError`:
   `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py`
   (`convert_loaded_session`),
   `packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py`
   (`convert_loaded_claude_code_session`).
4. Count the same records into the census and derive the `edges.jsonl` lines
   from them, then enrich the
   trajectory in place with `source_uuids`, `is_compact_summary`, and
   `cache_creation_total` (the harbor trajectory has no other consumer, so no
   deep copy is taken): `packages/atif-converter/src/atif_converter/domain/edges.py:111`,
   `packages/atif-converter/src/atif_converter/domain/enrichment.py:198`.
5. Build the loss report: one `FidelityGap` member, `PARENT_CHAIN_FLATTENED`,
   is structural for every conversion, and the rest are added from what the
   census found and from what enrichment didn't repair:
   `packages/atif-converter/src/atif_converter/infrastructure/census.py:57`,
   `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:137`.
6. Re-check the fingerprints, stat and digest both. Any movement since the
   read raises `SourceMutatedDuringConversion` rather than publishing
   artifacts that describe bytes the session no longer holds:
   `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:199`,
   `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py`
   (`mutated_files`).
7. Re-validate the enriched trajectory, and write it plus `edges.jsonl`
   beside it or stream the trajectory to stdout; a validation error exits 65:
   `packages/atif-cli/src/atif_cli/app.py:752-772`.

Per file that is two opens, one parse and two hash passes; the converter and
the audit never read the session separately.

### Related

- `packages/atif-converter/src/atif_converter/infrastructure/census.py:57`
- `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:76`
- `packages/atif-converter/src/atif_converter/domain/errors.py:47`
- `packages/atif-converter/src/atif_converter/domain/fidelity.py:55`
- `packages/atif-cli/src/atif_cli/errors.py:30`

## analyze: orchestrate the analytics stages

Entry point: `packages/atif-cli/src/atif_cli/app.py:1656`

1. Resolve `AnalyticsSettings`, reusing the corpus-root resolution the other
   commands share, then let `--max-sessions` and `--max-cost-usd` override the
   env ceilings so a crontab line carries its spend cap visibly (`:1733-1736`).
   `--llm-only` is accepted and dropped, since every stage is an LLM stage.
2. Open the lake's session source when the lake holds the corpus, else warn
   once and read the files (`--no-lake` forces the files):
   `packages/atif-cli/src/atif_cli/app.py:1760`. Build one `CorpusReader` over
   it, shared by every stage: the steps memo is the expensive part and every
   stage walks the same sessions
   (`packages/atif-analytics/src/atif_analytics/application/analyze.py:62`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:314`).
3. Construct the run-wide `RunBudget` from `llm_max_cost_usd_per_run`, priced
   from the providers' running actual usage rather than estimates:
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:97`.
4. Walk the stages (classify, conflicts, friction, perceived) in
   declaration order. A stage entered with the budget already exhausted is
   skipped with nothing stamped, its consecutive-skip streak persisted, and the
   log escalates to ERROR at three consecutive runs:
   `packages/atif-analytics/src/atif_analytics/application/analyze.py:109-128`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:218`.
5. Emit the per-stage summary carrying `budget_exhausted` and, on a real run,
   `llm_spent_usd`:
   `packages/atif-analytics/src/atif_analytics/application/analyze.py:150-152`,
   `packages/atif-cli/src/atif_cli/app.py:1757`.

### Related

- `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:60`
- `packages/atif-analytics/src/atif_analytics/domain/layout.py:45`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:36`
- `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:90`

## classify: the LLM analytics stage shape

Entry point: `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:374`

`conflicts`, `friction`, and `perceived` are entered the same way from the
stages table and share this shape through `_shared.py`. classify and conflicts
admit only `interactive` sessions; friction and perceived read only human
turns, the main-chain user steps whose author is `human`.

1. Resolve the layout, the reader, and the sharded parquet cache, then resolve
   the model through the atif-models registry by pipeline size (no pipeline
   writes down a model id) and build the adapter atif-models wires for the
   spec's family, OpenAI chat-completions by default or Anthropic Messages:
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:374`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:49`,
   `packages/atif-models/src/atif_models/infrastructure/providers.py:20`.
2. Under `dry_run`, the default, return a plan dict whose input tokens are
   measured from the actually rendered transcripts at the resolved model's
   `chars_per_token` ratio and whose session count is the same newest-first
   cap the real run applies:
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:311`.
3. Anti-join the cache for session ids already written, then drop sessions
   whose last-step timestamp and mtime bounds are unchanged since the last run (`:115`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:137`).
4. Drain the retry queue back into the admission set, then subtract units with
   a live entry (inside backoff or past the attempt cap) because the retry
   queue is the single re-admission gate and the checkpoint path would re-bill
   them:
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:145`,
   `:176`.
5. Walk newest-first and stop admitting at `llm_max_sessions_per_run` so a
   deferred session is never rendered; a session that renders to nothing, or
   whose kind isn't `interactive`, is checkpointed at current bounds instead of
   re-rendered every tick:
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:147-178`.
   The rendered transcript carries each tool result as a size stub,
   `[tool_result <id> <ts>] (N chars omitted)`, unless
   `ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES` turns the bodies on:
   `packages/atif-analytics/src/atif_analytics/domain/transcript.py:308-311`.
6. Dispatch each write chunk in budget-checked sub-batches of
   `BUDGET_CHECK_BATCH`, advancing the cursor by what was actually sent so a
   mid-chunk budget stop leaves the remainder unstamped rather than silently
   skipped. Each call sends the stage's system prompt and a user message
   holding the fenced transcript followed by the one-line request:
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:160`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:218`,
   `packages/atif-analytics/src/atif_analytics/application/prompts.py:742`,
   `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:149`,
   `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:185`.
7. Route each result: a `RefusalError` is terminal and writes the documented
   `goal='[refused]'` sentinel row so the refusal is queryable and never
   re-billed, while any other exception enqueues a retry
   (`packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:247`).
8. Write the chunk as a parquet part, checkpoint the completed sessions at
   current bounds, and clear them from the retry queue:
   `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:160`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:288-294`.

### Related

- `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:354`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:501`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:361`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:196`
- `packages/atif-models/src/atif_models/domain/ports.py:131`

## embed: backfill step embeddings into LanceDB

Entry point: `packages/atif-cli/src/atif_cli/app.py:1823`

1. Refuse a real run with no scope: without `--limit`, `--all`, or
   `--dry-run` the command exits 64, so a mistyped invocation cannot start a
   full backfill (`:1930`).
2. Resolve the Lance URI from `EmbedSettings`, defaulting to
   `<corpus_root>/embeddings_lance` (`<corpus_root>/embeddings_lance_gemma`
   under the `gemma` provider)
   (`packages/atif-embed/src/atif_embed/infrastructure/settings.py:133`).
3. Read the store's uuid-to-text-hash map once, then stream candidates whose
   hash is absent or stale through `TextRowsPort`; the staleness comparison
   happens before the limit cap so `--limit N` always makes N rows of progress
   (`packages/atif-embed/src/atif_embed/domain/discovery.py`). With a lake
   that holds the corpus, the rows come from its `steps` table
   (`packages/atif-embed/src/atif_embed/infrastructure/lake_text_rows.py`,
   over `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py`),
   and only the uuids a lake snapshot touched since the watermark in the
   store directory are read; otherwise every session's `trajectory.json` is
   read (`packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py`).
4. Under `--dry-run`, count candidates and return the plan dict, returning
   before boto3 is ever imported:
   `packages/atif-embed/src/atif_embed/application/embed.py:124`.
5. Build the provider `ATIF_SQL_EMBED_PROVIDER` selects once (Cohere on
   Bedrock by default, or EmbeddingGemma 2 on this machine), then refuse to append when the
   store's stamped model and dimension differ from the live embedder's, because
   mixing vector spaces corrupts kNN silently (`:169-183`,
   `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38`).
6. Per chunk, a multiple of the batch size so a checkpoint boundary never
   splits a provider batch: embed the texts and drop rows the provider failed,
   leaving them pending for the next run
   (`packages/atif-embed/src/atif_embed/application/embed.py:153`,
   `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:310`).
7. Delete stale rows under the same uuid before appending the fixed-size
   float32 array frame, so text that changed cannot leave two rows fanning the
   kNN join out: `packages/atif-embed/src/atif_embed/application/embed.py:213-218`,
   `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:402`.
8. Compact every eight chunks to bound fragment count, then optimize and
   ensure the HNSW index on the way out so `search` pays no brute-force scan:
   `packages/atif-embed/src/atif_embed/application/embed.py:252`,
   `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414`.
9. When no row failed, hand the store's row count to `TextRowsPort.commit`:
   the lake reader moves its watermark to the snapshot it read, unless the
   run stopped at `--limit` (`packages/atif-embed/src/atif_embed/application/embed.py`).

`embed --prune-orphans` is a separate path: it compares the store's uuids with
every step's primary uuid in the lake and reports (or, with `--no-dry-run`,
deletes) the ones no step names (`packages/atif-embed/src/atif_embed/application/prune.py`).

### Related

- `packages/atif-embed/src/atif_embed/domain/ports.py:37`
- `packages/atif-embed/src/atif_embed/domain/ports.py:65`
- `packages/atif-embed/src/atif_embed/domain/ports.py:93`
- `packages/atif-embed/src/atif_embed/domain/errors.py:29`
- `packages/atif-cli/src/atif_cli/app.py:1967`

## query: one SQL statement over the catalog

Entry point: `packages/atif-cli/src/atif_cli/app.py:1320`

1. Short-circuit `--examples` to the examples listing without opening DuckDB;
   a missing statement emits a classified parse error and exits 64 (`:1445-1459`).
2. Resolve the corpus root and the expected embedder identity, then open an
   in-memory DuckDB connection (`:1480-1495`).
3. Attach the lake when it exists, its schema is current, and it holds the
   corpus: every raw relation is then a view over the lake's tables scoped to
   that corpus, and nothing loads before the statement runs
   (`packages/atif-cli/src/atif_cli/app.py:1602`,
   `packages/atif-duck/src/atif_duck/infrastructure/lake.py:1037`). Otherwise
   warn once and register the raw readers over the contract layout (trajectory rows come
   from `read_parquet` for sessions with current columnar artifacts and from
   `read_json` for the rest, unioned per surface),
   gating trajectory, edges, and loss on `meta.json` presence so a torn session
   directory contributes nothing to any view:
   `packages/atif-duck/src/atif_duck/infrastructure/registry.py:631`.
4. Create the core views, then bind `message_embeddings` over the Lance store
   guard-before-bind: a store written by another provider raises rather than
   returning numerically valid garbage cosine scores (`:1018`, `:1635`).
5. Create the macros and the authorship views (`user_steps`, `human_turns`,
   `session_outcomes`), then the analytics views and analytics macros over the
   analytics parquets, which bind against both the parquets and the base views
   (`:1890`, `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:119`).
6. Harden the connection in a fixed order (the memory cap, thread count and
   private spill directory were set before step 3, since registration is what
   needs them): a directory allowlist holding only the spill area, a file
   allowlist of the parquets the views read lazily, the config exemption
   list, then `enable_external_access=false` and `lock_configuration=true`
   last (`packages/atif-cli/src/atif_cli/app.py`, `_harden_query_connection`).
7. Check the statement's kinds with DuckDB's parser and refuse `COPY`,
   `EXPORT`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `PREPARE`, `EXECUTE`,
   `PRAGMA` and a `PIVOT` without an `IN` list before anything runs (exit 70, `sandbox_refused`); then execute the
   caller's statement and stream the cursor: a plain table on a TTY, a JSON
   array of row objects on a pipe (`packages/atif-cli/src/atif_cli/output.py:158`).
   The spill directory is removed on exit.
8. Classify any DuckDB failure into parse, catalog, or runtime (or an
   embedding-provider mismatch) and exit 64, 65, or 70 with a JSON error
   envelope: `packages/atif-cli/src/atif_cli/duck_errors.py:35`, `:67`.

### Related

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2129`
- `packages/atif-duck/src/atif_duck/domain/catalog.py:59`
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:51`
- `packages/atif-cli/src/atif_cli/output.py:206`
- `packages/atif-cli/src/atif_cli/errors.py:30`

## search: semantic top-k over step embeddings

Entry point: `packages/atif-cli/src/atif_cli/app.py:2040`

1. Resolve the corpus root, the Lance URI, and the expected model and
   dimension from `EmbedSettings` (`:2122-2125`,
   `packages/atif-embed/src/atif_embed/infrastructure/settings.py:139`).
2. Attach the lake as `query` does (or fall back to the per-session
   artifacts with a warning), then register the full catalog on a fresh
   in-memory connection; a registration failure or a provider mismatch leaves
   through the classified-error path (a provider mismatch exits 65):
   `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2129`,
   `packages/atif-cli/src/atif_cli/duck_errors.py:67`. `--all-corpora` scopes
   the steps to every corpus the lake holds and binds `message_embeddings`
   over each corpus's store (`register_vss_stores`).
3. Count `message_embeddings` before embedding anything: an empty or absent store exits 2 with the
   backfill hint instead of returning an empty result that reads like "no
   matches" (`packages/atif-cli/src/atif_cli/app.py:2167`). A store on disk
   that no installed lance extension can read exits 78 before the count
   (`:2152-2165`).
4. Embed the query text with the selected provider: Cohere `search_query`
   float mode by default (Cohere forces float for queries even when documents
   were stored int8), or EmbeddingGemma 2's `SearchQuery` prompt:
   `packages/atif-embed/src/atif_embed/application/embed.py:280`,
   `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:401`.
5. Bind the query vector, the optional session filter, and `k` as parameters
   rather than interpolating them: `packages/atif-cli/src/atif_cli/app.py:2236-2239`.
6. Order by `array_cosine_distance` ascending: that is what triggers the cosine
   HNSW index lookup, and cosine is the only magnitude-invariant choice against
   int8-cast document vectors whose magnitudes run into the thousands (`:2218`).
7. Join back to `steps` on the first entry of `source_uuids` for a
   200-character snippet, selecting cosine similarity as the reported score
   (`:2210-2216`).
8. Emit uuid, session id, snippet, and similarity (plus `corpus` under
   `--all-corpora`); a DuckDB error classifies to
   its own exit code (`:2192-2196`,
   `packages/atif-cli/src/atif_cli/output.py:132`).

### Related

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1635`
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:51`
- `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414`
- `packages/atif-cli/src/atif_cli/errors.py:30`

## refresh lane: the scheduled driver

Entry point: `scripts/atif-sql-refresh.sh:160`

1. Pin the identity trio once from the invoking environment and unset
   `AWS_PROFILE` and `AWS_DEFAULT_PROFILE`, because an inherited profile name
   with no matching config entry makes botocore raise at credential resolution
   before Bedrock is reached (`:135-140`, `:147`).
2. Normalize the lane argument before it names a lock file, so two spellings of
   one lane cannot take two locks; an unknown mode exits 64 rather than
   defaulting to a plane. The removed `structural` lane (and `struct`) still
   parses: it logs one "lane removed" line and exits 0 (`:160-168`, `:164-167`).
3. Read the rotated Bedrock bearer token at run time and export it only when
   non-empty, because botocore treats an empty value as a real broken
   credential and skips the default chain (`:238-241`).
4. Resolve the corpus list (the primary config directory first, then each
   `ATIF_SQL_EXTRA_CONFIG_DIRS` entry) and export both `CLAUDE_CONFIG_DIR` and
   `ATIF_SQL_SOURCE_ROOT` per corpus so the source root and the corpus slug
   stay coherent (`:250-260`, `:497-498`).
5. Resolve the CLI through the override, then a user install, then the
   workspace venv script, and take a nonblocking per-lane `flock`: a busy lane
   skips this tick rather than queueing behind it (`:267-273`, `:279-285`).
6. On the `llm` lane, probe `atif-sql --help` for `analyze` and exit 0 when it
   is absent, so an armed crontab line against an older CLI is a no-op instead
   of a nightly error (`:324-329`).
7. Run the lane per corpus: `materialize` plus a bounded `embed --limit 500`
   piggyback, or
   `analyze --no-dry-run --llm-only --max-sessions 1000000 --max-cost-usd 1000000`,
   the only spending line in the file, with both caps set out of reach on
   purpose (`:394`, `:446`). The `materialize` lane then runs one extra
   `materialize --agent codex` pass over the Codex transcripts, with no embed
   piggyback (`:453`).
8. On embed exit 78, write a terminal marker keyed on the store path and its
   mtime and suppress further embeds for that corpus until the mtime changes;
   every other nonzero exit stays transient and retries next tick (`:398-403`,
   `:380-390`).
9. The `compact` lane runs `atif-sql lake compact --memory-limit 2GiB` once
   (not per corpus; `ATIF_SQL_REFRESH_COMPACT_MEMORY_LIMIT` changes the
   budget) under a 4G scope, after taking the materialize lane's lock too (waiting up to
   `ATIF_SQL_REFRESH_COMPACT_WAIT_SECONDS`), so no materialize tick writes the
   lake while it runs; a CLI without `lake` skips with one line.

### Related

- `scripts/atif-sql-refresh-selftest.sh:121`
- `scripts/atif-sql-refresh-selftest.sh:134`
- `packages/atif-cli/src/atif_cli/cron.py:47`
- `packages/atif-cli/src/atif_cli/app.py:104`

## Minor flows

- status: entry at `packages/atif-cli/src/atif_cli/app.py:1162`. Read-only
  freshness report: scans source mtimes and replays the same pure plan
  `materialize` would build, generation staleness included, without converting anything, so its stale,
  up-to-date, and live counts are exactly what a pass would do
  (`packages/atif-corpus/src/atif_corpus/application/materialize.py:1433`).
- examples: entry at `packages/atif-cli/src/atif_cli/app.py:2265`. Derives one
  runnable example per view and macro from the static catalog and filters by
  `--category` and `--requires`; an unknown value exits 64
  (`packages/atif-duck/src/atif_duck/domain/examples.py:191`).
- schema: entry at `packages/atif-cli/src/atif_cli/app.py:2357`. Dumps the
  core and analytics view schemas and macro signatures, each with its
  `requires` value, from the static catalog with no DuckDB import and no
  connection (`packages/atif-duck/src/atif_duck/domain/catalog.py:59`).
- cron install: entry at `packages/atif-cli/src/atif_cli/cron.py:186`. Renders
  the crontab block, plus one comment per removed lane, for a human to paste and never writes the
  crontab itself (`:85`).
- cron status: entry at `packages/atif-cli/src/atif_cli/cron.py:207`. Probes
  each lane's flock nonblocking, so the probe perturbs nothing, and parses the
  last completion and last skip per lane out of the refresh log (`:133`, `:97`).
- main: entry at `packages/atif-cli/src/atif_cli/app.py:2431`. Replaces
  loguru's default DEBUG sink with WARNING-and-up (`ATIF_SQL_LOG_LEVEL` widens
  it) so routine reads keep stderr quiet, then hands control to cyclopts.

## See also

- [module map](../architecture/module-map.md)
- [business logic](../insights/business-logic.md)
- [contract map](../insights/contract-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [debugging guide](../insights/debugging-guide.md)
