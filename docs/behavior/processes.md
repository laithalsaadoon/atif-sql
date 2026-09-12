# atif-sql · Processes

Every process in this system starts as a CLI invocation. There is one console
script, `atif-sql = "atif_cli.app:main"`
(`packages/atif-cli/pyproject.toml:42`), and its ten commands are the complete
initiator set — nine `@app.command` sites plus the `cron` sub-App registered at
`packages/atif-cli/src/atif_cli/app.py:66`. No HTTP route, RPC tool, message
handler, or job queue exists to initiate anything else; the only scheduled
initiator is a crontab line into `scripts/atif-sql-refresh.sh`. Long-running
work is in-process async under `anyio` and `asyncio.run`, not a worker.

## materialize — sync the materialized corpus

Entry point: `packages/atif-cli/src/atif_cli/app.py:352`

1. Resolve `CorpusSettings` and wire the pass: `--source-root` given without
   `--corpus-root` re-derives the corpus root from the overridden source's slug
   so re-pointing the source cannot overwrite another corpus, and the CLI hands
   in the wall clock plus the harbor and converter version pins alongside
   `RealConverter` behind `ConverterPort` — `:190`,
   `packages/atif-cli/src/atif_cli/converter_adapter.py:44`.
2. Sweep `.staging/`: an entry whose `tmp-<pid>` owner is a dead pid is crash
   debris and is removed, while a live pid belongs to a concurrent pass and is
   left alone —
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:274`.
3. Read `watermark.json` as a path-to-mtime map; an unreadable or wrongly
   shaped file degrades to empty, costing one full re-materialization rather
   than refusing to sync — `packages/atif-corpus/src/atif_corpus/application/materialize.py:160`.
4. Scan the source root for main transcripts and every side-file, then fold
   sessions living under a directory that would not list into the unreadable
   set using the watermark as the only record of what lived there —
   `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:143`,
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:342`.
5. Guard, then collect ghosts: zero scanned sessions over a non-empty corpus
   raises `SuspiciousEmptyScanError` instead of deleting everything as ghosts,
   and ghost removal is skipped entirely when any source directory failed to
   list, because absence is then not evidence of deletion — `:542`, `:379`.
6. Build the pure `MaterializationPlan` from quiescence and the watermark,
   force-replanning sessions the watermark calls current whose artifact
   directory is missing — the one state reachable by a kill inside the swap
   window — `packages/atif-corpus/src/atif_corpus/domain/sessions.py:159`,
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:313`.
7. Per planned session: convert, write trajectory, loss report, edges, and
   `meta.json` last into a staging directory, then rename the whole directory
   into `sessions/<id>/` so a reader observes only a complete generation; a
   session that raises is recorded and the pass continues — `packages/atif-corpus/src/atif_corpus/application/materialize.py:228`,
   `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:98`.
   With `--workers` above 1 this step runs on a spawn-context process pool;
   each worker runs the same per-session function under its own pid, outcomes
   are folded back in plan order, and a single planned session runs inline —
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:385`.
8. Advance the watermark for succeeded sessions only, retaining every entry of
   a failed, unplanned, or unreadable session so staleness still signals a
   retry, and write it atomically before emitting the report —
   `packages/atif-corpus/src/atif_corpus/application/materialize.py:428`,
   `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:64`.

### Related

- `packages/atif-corpus/src/atif_corpus/domain/ports.py:41`
- `packages/atif-corpus/src/atif_corpus/domain/layout.py:26`
- `packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:44`
- `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:43`
- `packages/atif-cli/src/atif_cli/app.py:312`

## convert — one session to ATIF plus a loss audit

Entry point: `packages/atif-cli/src/atif_cli/app.py:226`

1. Reject a path that is not an existing `.jsonl` file before any harbor work,
   which the CLI maps to exit 64 —
   `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:163`.
2. Fingerprint every source file — the main transcript and each discovered
   side-file — before harbor reads anything —
   `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:110`.
3. Read the main transcript and discover every side file under `<stem>/`,
   workflow-nested ones included —
   `packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:57`.
4. Convert the records with our ported converter (a parity port of harbor
   0.22.0's, built on the public ATIF models) and dump the result to a
   JSON-mode dict; a `None` return means no convertible events and raises
   `EmptySessionError` —
   `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:83`.
5. Re-check the fingerprints. Any movement raises
   `SourceMutatedDuringConversion` rather than emitting a census, a trajectory,
   and an edges file that describe different bytes of one session —
   `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:93`.
6. Parse the raw records from the snapshot and build the loss report: four
   `FidelityGap` members are structural for every harbor 0.22.0 conversion, and
   the rest are added from what the census found —
   `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:118`,
   `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:62`.
7. Derive the `edges.jsonl` lines from the raw records, then enrich the
   trajectory in place with `source_uuids`, `is_compact_summary`, and
   `cache_creation_total` — the harbor trajectory has no other consumer, so no
   deep copy is taken — `packages/atif-converter/src/atif_converter/domain/edges.py:99`,
   `packages/atif-converter/src/atif_converter/domain/enrichment.py:198`.
8. Re-check the fingerprints a second time, re-validate the enriched
   trajectory, and write it plus `edges.jsonl` beside it or stream the
   trajectory to stdout; a validation error exits 65 —
   `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:148`,
   `packages/atif-cli/src/atif_cli/app.py:272`.

### Related

- `packages/atif-converter/src/atif_converter/infrastructure/census.py:57`
- `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:97`
- `packages/atif-converter/src/atif_converter/domain/errors.py:57`
- `packages/atif-converter/src/atif_converter/domain/fidelity.py:44`
- `packages/atif-cli/src/atif_cli/errors.py:25`

## analyze — orchestrate the eight analytics stages

Entry point: `packages/atif-cli/src/atif_cli/app.py:659`

1. Resolve `AnalyticsSettings`, reusing the corpus-root resolution the other
   commands share, then let `--max-sessions` and `--max-cost-usd` override the
   env ceilings so a crontab line carries its spend cap visibly — `:691`.
2. Reject `--structural-only` together with `--llm-only` and derive the two
   lane booleans —
   `packages/atif-analytics/src/atif_analytics/application/analyze.py:61`.
3. Build one `CorpusReader` shared by every stage: the parsed-steps memo is the
   expensive part and all five LLM stages walk the same sessions — `:72`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:138`.
4. Structural lane, zero LLM cost — cluster the Lance store with UMAP plus
   HDBSCAN, skipping when the mtime sidecar says the input is unchanged and
   `--force-cluster` is absent —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/cluster.py:50`.
5. Label those clusters with c-TF-IDF terms, then detect communities over
   session centroids with Leiden and CPM —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/terms.py:54`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/community.py:94`.
6. Construct the run-wide `RunBudget` from `llm_max_cost_usd_per_run`, priced
   from the providers' running actual usage rather than estimates —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:90`.
7. Walk the five LLM stages in declaration order. A stage entered with the
   budget already exhausted is skipped with nothing stamped, its
   consecutive-skip streak persisted, and the log escalates to ERROR at three
   consecutive runs —
   `packages/atif-analytics/src/atif_analytics/application/analyze.py:161`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:217`.
8. Emit the per-stage summary carrying `budget_exhausted` and, on a real run,
   `llm_spent_usd` —
   `packages/atif-analytics/src/atif_analytics/application/analyze.py:202`,
   `packages/atif-cli/src/atif_cli/app.py:757`.

### Related

- `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:68`
- `packages/atif-analytics/src/atif_analytics/domain/layout.py:45`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:32`
- `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:253`
- `packages/atif-analytics/src/atif_analytics/infrastructure/freshness.py:77`

## classify — the LLM analytics stage shape

Entry point: `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:342`

`trajectory`, `conflicts`, `friction`, and `perceived` are entered the same way
from the stages table and share this shape through `_shared.py`.

1. Resolve the layout, the reader, and the sharded parquet cache, then resolve
   the model through the atif-models registry by pipeline size — no pipeline
   writes down a model id — `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:368`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:32`.
2. Under `dry_run`, the default, return a plan dict whose input tokens are
   measured from the actually rendered transcripts and whose session count is
   the same newest-first cap the real run applies —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:293`.
3. Anti-join the cache for session ids already written, then drop sessions
   whose last-step timestamp and mtime bounds are unchanged since the last run
   — `:74`,
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:136`.
4. Drain the retry queue back into the admission set, then subtract units with
   a live entry — inside backoff or past the attempt cap — because the retry
   queue is the single re-admission gate and the checkpoint path would re-bill
   them —
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:145`,
   `:159`.
5. Walk newest-first and stop admitting at `llm_max_sessions_per_run` so a
   deferred session is never rendered; a session that renders to nothing is
   checkpointed at current bounds instead of re-rendered every tick —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:135`.
6. Dispatch each write chunk in budget-checked sub-batches of
   `BUDGET_CHECK_BATCH`, advancing the cursor by what was actually sent so a
   mid-chunk budget stop leaves the remainder unstamped rather than silently
   skipped —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:155`,
   `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:217`.
7. Route each result: a `RefusalError` is terminal and writes the documented
   `goal='[refused]'` sentinel row so the refusal is queryable and never
   re-billed, while any other exception enqueues a retry —
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:219`.
8. Write the chunk as a parquet part, checkpoint the completed sessions at
   current bounds, and clear them from the retry queue —
   `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:268`,
   `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:272`.

### Related

- `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:421`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:337`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:531`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:357`
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:191`
- `packages/atif-models/src/atif_models/domain/ports.py:116`

## embed — backfill step embeddings into LanceDB

Entry point: `packages/atif-cli/src/atif_cli/app.py:766`

1. Refuse a real run with no scope: without `--limit`, `--all`, or
   `--dry-run` the command exits 64, so a mistyped invocation cannot start a
   full backfill — `:778`.
2. Resolve the Lance URI from `EmbedSettings`, defaulting to
   `<corpus_root>/embeddings_lance` —
   `packages/atif-embed/src/atif_embed/infrastructure/settings.py:51`.
3. Read the store's uuid-to-text-hash map once, then stream candidates whose
   hash is absent or stale through `TextRowsPort`; the staleness comparison
   happens before the limit cap so `--limit N` always makes N rows of progress
   — `packages/atif-embed/src/atif_embed/application/embed.py:43`,
   `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:158`.
4. Under `--dry-run`, count candidates and return the plan dict, returning
   before boto3 is ever imported —
   `packages/atif-embed/src/atif_embed/application/embed.py:120`.
5. Build the Cohere-on-Bedrock provider once, then refuse to append when the
   store's stamped model and dimension differ from the live embedder's, because
   mixing vector spaces corrupts kNN silently — `:157`,
   `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38`.
6. Per chunk, a multiple of the batch size so a checkpoint boundary never
   splits a Bedrock batch: embed the texts and drop rows the provider failed,
   leaving them pending for the next run —
   `packages/atif-embed/src/atif_embed/application/embed.py:147`,
   `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:309`.
7. Delete stale rows under the same uuid before appending the fixed-size
   float32 array frame, so text that changed cannot leave two rows fanning the
   kNN join out — `packages/atif-embed/src/atif_embed/application/embed.py:207`,
   `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:402`.
8. Compact every eight chunks to bound fragment count, then optimize and
   ensure the HNSW index on the way out so `search` pays no brute-force scan —
   `packages/atif-embed/src/atif_embed/application/embed.py:243`,
   `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414`.

### Related

- `packages/atif-embed/src/atif_embed/domain/ports.py:31`
- `packages/atif-embed/src/atif_embed/domain/ports.py:59`
- `packages/atif-embed/src/atif_embed/domain/ports.py:87`
- `packages/atif-embed/src/atif_embed/domain/errors.py:29`
- `packages/atif-cli/src/atif_cli/app.py:833`

## query — one SQL statement over the catalog

Entry point: `packages/atif-cli/src/atif_cli/app.py:529`

1. Short-circuit `--examples` to the examples listing without opening DuckDB;
   a missing statement emits a classified parse error and exits 64 — `:564`.
2. Resolve the corpus root and the expected embedder identity, then open an
   in-memory DuckDB connection — `:586`.
3. Register the four raw readers as TEMP tables over the contract layout,
   gating trajectory, edges, and loss on `meta.json` presence so a torn session
   directory contributes nothing to any view —
   `packages/atif-duck/src/atif_duck/infrastructure/registry.py:180`.
4. Create the core views, then bind `message_embeddings` over the Lance store
   guard-before-bind: a store written by another provider raises rather than
   returning numerically valid garbage cosine scores — `:333`, `:846`.
5. Create the macros, then the analytics views and analytics macros over the
   analytics parquets, which bind against both the parquets and the base views
   — `:1005`, `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:124`.
6. Harden the connection in a fixed order: temp directory, memory cap, a
   directory allowlist holding only the spill area, a file allowlist of the
   analytics parquets, the config exemption list, then
   `enable_external_access=false` and `lock_configuration=true` last —
   `packages/atif-cli/src/atif_cli/app.py:138`.
7. Execute the caller's statement and stream the cursor: a plain table on a
   TTY, a JSON array of row objects on a pipe — `:606`,
   `packages/atif-cli/src/atif_cli/output.py:154`.
8. Classify any DuckDB failure into parse, catalog, or runtime — or an
   embedding-provider mismatch — and exit 64, 65, or 70 with a JSON error
   envelope — `packages/atif-cli/src/atif_cli/duck_errors.py:34`, `:66`.

### Related

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1212`
- `packages/atif-duck/src/atif_duck/domain/catalog.py:51`
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:46`
- `packages/atif-cli/src/atif_cli/output.py:202`
- `packages/atif-cli/src/atif_cli/errors.py:25`

## search — semantic top-k over step embeddings

Entry point: `packages/atif-cli/src/atif_cli/app.py:860`

1. Resolve the corpus root, the Lance URI, and the expected model and
   dimension from `EmbedSettings` — `:878`,
   `packages/atif-embed/src/atif_embed/infrastructure/settings.py:57`.
2. Register the full catalog on a fresh in-memory connection; a registration
   failure or a provider mismatch leaves through the classified-error path with
   exit 65 — `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1212`,
   `packages/atif-cli/src/atif_cli/duck_errors.py:66`.
3. Count `message_embeddings` first: an empty or absent store exits 2 with the
   backfill hint instead of returning an empty result that reads like "no
   matches" — `packages/atif-cli/src/atif_cli/app.py:930`.
4. Embed the query text in Cohere `search_query` float mode — Cohere forces
   float for queries even when documents were stored int8 —
   `packages/atif-embed/src/atif_embed/application/embed.py:265`,
   `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:400`.
5. Bind the query vector, the optional session filter, and `k` as parameters
   rather than interpolating them — `packages/atif-cli/src/atif_cli/app.py:945`.
6. Order by `array_cosine_distance` ascending: that is what triggers the cosine
   HNSW index lookup, and cosine is the only magnitude-invariant choice against
   int8-cast document vectors whose magnitudes run into the thousands — `:919`.
7. Join back to `steps` on the first entry of `source_uuids` for a
   200-character snippet, selecting cosine similarity as the reported score —
   `:933`.
8. Emit uuid, session id, similarity, and snippet; a DuckDB error classifies to
   its own exit code — `:946`,
   `packages/atif-cli/src/atif_cli/output.py:129`.

### Related

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:830`
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:46`
- `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414`
- `packages/atif-cli/src/atif_cli/errors.py:25`

## refresh lane — the scheduled driver

Entry point: `scripts/atif-sql-refresh.sh:109`

1. Pin the identity trio once from the invoking environment and unset
   `AWS_PROFILE` and `AWS_DEFAULT_PROFILE`, because an inherited profile name
   with no matching config entry makes botocore raise at credential resolution
   before Bedrock is reached — `:84`, `:96`.
2. Normalize the lane argument before it names a lock file, so two spellings of
   one lane cannot take two locks; an unknown mode exits 64 rather than
   defaulting to a plane — `:109`.
3. Read the rotated Bedrock bearer token at run time and export it only when
   non-empty, because botocore treats an empty value as a real broken
   credential and skips the default chain — `:122`.
4. Resolve the corpus list — the primary config directory first, then each
   `ATIF_SQL_EXTRA_CONFIG_DIRS` entry — and export both `CLAUDE_CONFIG_DIR` and
   `ATIF_SQL_SOURCE_ROOT` per corpus so the source root and the corpus slug
   stay coherent — `:134`, `:312`.
5. Resolve the CLI through the override, then a user install, then the
   workspace venv script, and take a nonblocking per-lane `flock`: a busy lane
   skips this tick rather than queueing behind it — `:151`, `:166`.
6. On the two analytics lanes, probe `atif-sql --help` for `analyze` and exit 0
   when it is absent, so an armed crontab line against an older CLI is a no-op
   instead of an hourly error — `:181`.
7. Run the lane per corpus: `materialize` plus a bounded `embed --limit 500`
   piggyback, `analyze --structural-only`, or
   `analyze --no-dry-run --llm-only --max-sessions 50 --max-cost-usd 25.0` —
   the only spending line in the file — `:255`, `:274`, `:286`.
8. On embed exit 78, write a terminal marker keyed on the store path and its
   mtime and suppress further embeds for that corpus until the mtime changes;
   every other nonzero exit stays transient and retries next tick — `:217`,
   `:244`.

### Related

- `scripts/atif-sql-refresh-selftest.sh:97`
- `scripts/atif-sql-refresh-selftest.sh:110`
- `packages/atif-cli/src/atif_cli/cron.py:47`
- `packages/atif-cli/src/atif_cli/app.py:66`

## Minor flows

- status — entry at `packages/atif-cli/src/atif_cli/app.py:444`. Read-only
  freshness report: scans source mtimes and replays the same pure plan
  `materialize` would build without converting anything, so its stale,
  up-to-date, and live counts are exactly what a pass would do
  (`packages/atif-corpus/src/atif_corpus/domain/sessions.py:159`).
- examples — entry at `packages/atif-cli/src/atif_cli/app.py:995`. Derives one
  runnable example per view and macro from the static catalog and filters by
  `--category` and `--requires`; an unknown value exits 64
  (`packages/atif-duck/src/atif_duck/domain/examples.py:178`).
- schema — entry at `packages/atif-cli/src/atif_cli/app.py:1087`. Dumps the
  view schemas and macro signatures from the static catalog with no DuckDB
  import and no connection (`packages/atif-duck/src/atif_duck/domain/catalog.py:51`).
- cron install — entry at `packages/atif-cli/src/atif_cli/cron.py:179`. Renders
  the three-lane crontab block for a human to paste and never writes the
  crontab itself (`:79`).
- cron status — entry at `packages/atif-cli/src/atif_cli/cron.py:198`. Probes
  each lane's flock nonblocking, so the probe perturbs nothing, and parses the
  last completion and last skip per lane out of the refresh log (`:91`, `:109`).
- main — entry at `packages/atif-cli/src/atif_cli/app.py:1125`. Replaces
  loguru's default DEBUG sink with WARNING-and-up so routine reads keep stderr
  quiet, then hands control to cyclopts.

## See also

- [module map](../architecture/module-map.md) — 42 shared source citations
- [business logic](../insights/business-logic.md) — 36 shared source citations
- [contract map](../insights/contract-map.md) — 35 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 35 shared source citations
- [debugging guide](../insights/debugging-guide.md) — 29 shared source citations
