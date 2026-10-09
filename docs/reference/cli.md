# atif-sql · CLI

The `atif-sql` console script (`packages/atif-cli/pyproject.toml:42`) is the repo's public contract: one cyclopts router (`packages/atif-cli/src/atif_cli/app.py:52`) dispatches the subcommands below, and a root-level `--version` resolves the installed `atif-sql` distribution (`:60`).

These conventions hold across every subcommand. `--format` takes `auto`, `table`, `json`, or `csv` from the `OutputFormat` `StrEnum` at `packages/atif-cli/src/atif_cli/output.py:58`, and `auto` resolves to `table` when stdout is a TTY and `json` otherwise (`packages/atif-cli/src/atif_cli/output.py:74`). Exit codes come from one table, `EXIT_CODES` at `packages/atif-cli/src/atif_cli/errors.py:28`: `0` ok, `2` empty-session / no-embeddings, `64` invalid input or SQL parse error, `65` catalog error / validation error / embedding mismatch, `70` runtime error, `78` terminal state or suspicious scan, `127` harbor missing. A number in that table is a wire contract: keys may be added, never renumbered. A usage error cyclopts refuses (an unknown option or command, a missing or uncoercible value) prints cyclopts' error panel and exits `64`, never cyclopts' own `2`. And stderr carries WARNING and above only, so a piped read emits data on stdout and nothing on stderr unless something is wrong; `ATIF_SQL_LOG_LEVEL` widens it (`INFO`, `DEBUG`) and is the only way to reach the INFO surface, because the entry point replaces loguru's default handler and with it the `LOGURU_LEVEL` that would have parameterized it (`packages/atif-cli/src/atif_cli/app.py:1141-1148`).

`analyze`, `embed`, and `search` call Amazon Bedrock and spend money; the others are offline.

Some subcommands take `--agent`, which selects the transcript format and, with it, the default source and corpus roots: `convert`, `materialize`, `status`, and `query`. The accepted spellings are `claude-code` (the default) and `codex`, and an unknown one exits `64` naming both (`packages/atif-cli/src/atif_cli/app.py:257`). `codex` moves the source root to `$CODEX_HOME` (default `~/.codex`) `/sessions` and the corpus root to `~/.atif-sql/corpus/codex`; an explicit flag or `ATIF_SQL_*` env var still wins (`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py`).

## convert

```
atif-sql convert [OPTIONS] SESSION-JSONL
```

Convert one Claude Code session JSONL or one Codex CLI rollout JSONL to ATIF plus a loss report and edges.
`packages/atif-cli/src/atif_cli/app.py:282`

Flags:

- `SESSION-JSONL` / `--session-jsonl`: required path to the transcript, `~/.claude/projects/<proj>/<session>.jsonl`, or `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl` under `--agent codex`. `:283`
- `--agent`: `claude-code` or `codex`; picks the converter and the fidelity policy. Defaults from `ATIF_SQL_AGENT`, then to `claude-code`. `:284`
- `--include-subagents` / `--no-subagents`: stage `<session>/subagents/**.jsonl` side-files alongside the main chain; default `True`, with the negative form named explicitly. `:228`
- `--trajectory-out`: write the trajectory JSON here and `edges.jsonl` beside it, instead of stdout. `:229`

Exit codes: `0` ok, `2` empty session, `64` invalid input, `65` validation, `70` conversion. `:250`

## materialize

```
atif-sql materialize [OPTIONS]
```

Sync the materialized corpus with the raw transcript corpus in one scan-plan-convert-write pass.
`packages/atif-cli/src/atif_cli/app.py:448`

A transcript whose session id fails the boundary in `packages/atif-corpus/src/atif_corpus/domain/session_id.py` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, at most 255 characters) is skipped with a logged reason and counted: the report carries `rejected` and `rejected_session_ids` beside `unreadable` and `unreadable_session_ids`, and the table form prints one `REJECTED` line per name on stderr. Nothing from such a session is written, and a corpus directory an older version wrote under that name is kept and never marked.

Nothing is ever deleted. A session whose source transcript vanished keeps its artifacts, and its `meta.json` gets `source_present: false` and `source_removed_at` once; the report's `removed` / `removed_session_ids` are the sessions newly marked this pass and `retained` counts every session kept without a source. Every live conversion also writes a zstd archive of the session's raw source files to `sessions/<id>/source/` (listed in `meta.source_archive`), from the bytes the converter parsed. A session is stale when its sources moved OR when its recorded `converter_schema` (the converter's `CONVERTER_SCHEMA_VERSION`; and, on a columnar pass, `columnar_schema`) differs from the running one, and `meta.converter_version` is now the real `atif-sql` release instead of `"unknown"`; a stale source-removed session with an archive is re-converted from it and counted in `from_archive` / `from_archive_session_ids`. A transcript with nothing to convert is counted in `empty` / `empty_session_ids`, recorded in `empty_sessions.json`, and not retried until it's written to or the converter schema changes, so `failed` only counts real failures. See `docs/CONTRACT.md`.

The convert-write stage runs across a process pool by default. Each worker builds its own converter once and writes through the same per-session staging directory and atomic swap the single-process path uses, so the artifacts are byte-identical either way and a crash still costs at most the session in flight. `--workers 1` is the single-process reference path. The report's `convert_seconds` is the per-session sum, so with several workers it can exceed `total_seconds`, which stays the wall clock; `workers` in the report is the pool size the pass actually used, which is never more than the number of sessions planned (`packages/atif-corpus/src/atif_corpus/application/materialize.py:385`).

Flags:

- `--agent`: `claude-code` (default) or `codex`; picks the discovery layout and the default roots. `:426`
- `--force` / `--no-force`: re-materialize every quiescent session regardless of the watermark; default `False`. `:341`
- `--quiesce-seconds`: source-silence threshold; defaults from settings (contract: 300). `:342`
- `--source-root`: override the raw transcript root, otherwise `ATIF_SQL_SOURCE_ROOT` or `<CLAUDE_CONFIG_DIR>/projects`. `:343`
- `--corpus-root`: override the materialized corpus root, otherwise env or `~/.atif-sql/corpus/<slug>`. `:344`
- `--sessions`: comma-separated session-id filter; only these sessions are planned this pass. `:345`
- `--workers`: processes for the convert-write stage; default `ATIF_SQL_MATERIALIZE_WORKERS`, else `min(8, cpu_count)`. `1` is the single-process path. `:455`
- `--format`: report format. `:346`

Exit codes: `0` ok, `64` `--workers` below `1`; `78` the corpus at this root holds the other agent's sessions, refused with nothing touched; `78` suspicious scan: the source scan found zero sessions while the corpus holds materialized ones, so the pass refused to mark them source-removed and touched nothing. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`
- `--format`: report format. `:346`

Each session is written as `trajectory.json.zst`, `edges.jsonl.zst` and `session_events.jsonl.zst` (zstd; decompressed, each is the plain file an earlier version wrote), a plain `loss_report.json` and `meta.json`, and the raw source archive. No per-session parquet is written, so the report's `artifact_seconds` is `0.0`. A corpus written by an earlier version keeps its plain files and parquet until `atif-sql corpus slim` converts it (see below).

Exit codes: `0` ok, `78` the corpus at this root holds the other agent's sessions, refused with nothing touched; `78` suspicious scan: the source scan found zero sessions while the corpus holds materialized ones, so the pass refused to mark them source-removed and touched nothing. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`

## status

```
atif-sql status [OPTIONS]
```

Report corpus freshness: watermark age, counts, bytes, staleness.
`packages/atif-cli/src/atif_cli/app.py:444`

It replays the planning a default `materialize` would do, so `staleness.stale` includes sessions a converter change made stale (`staleness.generation_stale` counts those alone) and `staleness.from_archive` counts source-removed sessions the next pass would re-convert from their archive. `retained_sessions` counts sessions kept without a source, `empty_sessions` the transcripts recorded as empty, and `converter_schema` / `columnar_schema` are the running versions a current session must carry.

Flags:

- `--agent`: `claude-code` (default) or `codex`; the scan and the reported `agent` field follow it. `:529`
- `--source-root`: override the raw transcript root. `:414`
- `--corpus-root`: override the materialized corpus root. `:415`
- `--quiesce-seconds`: source-silence threshold used to replay `materialize`'s planning decision. `:416`
- `--format`: report format. `:417`

Both roots resolve through `_corpus_settings` (`:433`), so a `--source-root` given without `--corpus-root` re-derives the corpus root from the overridden source's slug unless `ATIF_SQL_CORPUS_ROOT` is set (`:206`).

The report also says how the corpus is stored and how `query` reads it without the lake. `layout` counts the complete sessions still in the old layout (plain JSON files or per-session parquet) and their bytes; the table form prints one line, and `atif-sql corpus slim` converts them. `query path` is `columnar` when every complete session still carries current parquet, `json` when none does (every session written or slimmed since materialize stopped writing parquet), and `mixed` in between. `status` applies the same per-session predicate the registry does (`meta.columnar_schema` current and all five files present and non-empty). `packages/atif-cli/src/atif_cli/app.py:639`

## query

```
atif-sql query [OPTIONS] [ARGS]
```

Run one SQL statement against the atif-duck catalog and emit results.
`packages/atif-cli/src/atif_cli/app.py:529`

Flags:

- `SQL`: positional-only statement; omitting it without `--examples` is a parse error. `:498`
- `--examples`: short-circuit to the `examples` listing, honoring `--category` and `--requires`, without opening DuckDB. `:501`
- `--category`: forwarded to the `examples` listing. `:502`
- `--requires`: forwarded to the `examples` listing. `:503`
- `--agent`: `claude-code` (default) or `codex`; selects which corpus the statement reads. `:628`
- `--corpus-root`: override the materialized corpus root. `:504`
- `--all-corpora`: scope every view to every corpus the lake holds; `sessions.corpus` says which one a row came from. It has no per-session fallback, so it exits `78` without a usable lake and `64` together with `--no-lake`.
- `--lake` / `--no-lake`: read the lake when it holds the corpus (default), or read the per-session files.
- `--format`: `table` on a TTY, a JSON array of row objects on a pipe. `:505`

The statement runs against a hardened connection: reads reach the registered views and nothing else, and nothing under the corpus root is writable. Before registration the connection is sized to the host: a memory cap derived from available RAM (half of physical RAM or 8 GiB, whichever is larger, never above 80% of what's available) and a thread count of one per 2 GiB of that cap, capped at the CPUs the process may use. `ATIF_SQL_QUERY_MEMORY_LIMIT` (a DuckDB size such as `6GB`) and `ATIF_SQL_QUERY_THREADS` override both; a malformed value exits 64. The spill directory is a private `mkdtemp` (mode 0700) under the system temp dir, the only directory the sandbox grants, and it's removed when the process exits. Extension auto-install and auto-load are off, and the lance extension is loaded only when it's already installed, so registration never reaches the network (`packages/atif-cli/src/atif_cli/app.py`, `_configure_query_resources`).

Layered guards keep caller SQL from writing the corpus. DuckDB's file grants are read-write and it has no read-only grant, so `COPY ... TO <granted parquet> (USE_TMP_FILE false)` would overwrite one; the CLI therefore refuses every statement kind that names a file before executing anything, using DuckDB's own parser: `COPY`, `EXPORT`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `PREPARE` and `EXECUTE` exit 70 with kind `sandbox_refused`, for any uid, and a batch containing one of them runs nothing. And because a `0444` file mode doesn't bind root, `query` refuses to run as uid 0 (exit 77, kind `root_refused`) unless `ATIF_SQL_ALLOW_ROOT=1` is set, which logs a warning. `search` and `analyze` refuse root the same way.

What caller SQL can still see: `duckdb_settings()` and `current_setting(...)` return the sandbox's own configuration, including the corpus root, the spill directory, the memory cap and every granted parquet path, which names every session id. DuckDB can't hide a setting from SQL and the grants have to be per file, so this is accepted: the caller is the local user, who can list the corpus and `SELECT session_id FROM sessions` anyway, and `query` isn't a privilege boundary (see SECURITY.md).

The views themselves carry no corpus path as statement text. The registry hands its globs and file lists to `read_json(?)` as bound parameters and builds the parquet readers through DuckDB's relation API, so a corpus root such as `o'brien ?; --$1` and transcript content carrying SQL text both register as data (`packages/atif-duck/src/atif_duck/infrastructure/registry.py`). A session directory whose name fails the session id boundary (`packages/atif-duck/src/atif_duck/domain/session_id.py`) registers nothing and is logged once.

Without the lake, an old-layout session that still carries current parquet is served from it, and every other session is parsed from its stored trajectory (`trajectory.json.zst`, decompressed by DuckDB's JSON reader) on each run. That makes the fallback slow on a large corpus, and it holds each document it reads in memory, so a full scan of `tool_calls` or `tool_results` over a large corpus can run out of query memory. That error's hint says to run `atif-sql lake rebuild` (or raise `ATIF_SQL_QUERY_MEMORY_LIMIT`), because the lake answers the same query in bounded memory. The fallback stays for a corpus the lake doesn't hold yet, a lake whose schema is stale, and small corpora. The per-session parquet files the registry bound are granted to the sandbox the same way the analytics parquets are (as individual `allowed_paths` entries, `packages/atif-cli/src/atif_cli/app.py:221`), and they're written read-only (`0444`), so a `COPY ... TO` at one of them fails at the filesystem even though DuckDB's grant is read-write. `atif-sql status` says which path a corpus takes.

Exit codes: `64` parse error or a malformed `ATIF_SQL_QUERY_*` override, `65` catalog error, `65` embedding mismatch, `70` runtime error, `70` `sandbox_refused` (a statement kind the sandbox never runs), `77` `root_refused`. `:550`

## analyze

```
atif-sql analyze [OPTIONS]
```

Run the LLM analytics pipelines: classify, conflicts, friction, and perceived.
`packages/atif-cli/src/atif_cli/app.py:1240`

Flags:

- `--since-days`: restrict the stages to sessions whose last step is within N days; default `30`. `:1242`
- `--limit`: cap the number of sessions, newest-first, per stage. `:1243`
- `--max-sessions`: hard per-run session ceiling per LLM pipeline; overrides `ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN`, default 50. `:1244`
- `--max-cost-usd`: hard per-run dollar ceiling across all LLM pipelines, checked against running actual usage; overrides `ATIF_SQL_LLM_MAX_COST_USD_PER_RUN`, default 25.0. `:1245`
- `--no-dry-run`: execute the LLM stages for real, which costs money; the default is a dry run emitting plan dicts and cost estimates. `:1246`
- `--llm-only`: accepted and ignored. Every stage is an LLM stage now, and the flag stays so old crontab lines keep parsing. `:1247`
- `--skip-classify`: opt out of the classify stage. `:1248`
- `--skip-conflicts`: opt out of the conflicts stage. `:1249`
- `--skip-friction`: opt out of the friction stage. `:1250`
- `--skip-perceived`: opt out of the perceived stage. `:1251`
- `--corpus-root`: override the materialized corpus root. `:1252`
- `--no-lake`: read session data from the per-session files rather than the lake. `:1534`
- `--format`: summary format. `:1253`

Session data comes from the lake when it holds the corpus; a session whose lake rows aren't its current artifacts is read from its files, and with no usable lake the command warns once and reads the files. The summary's `session_source` says which source ran (`lake` or `files`), and on the lake `sessions_read_from_files` counts the sessions the last stage read from files. If a lake read fails partway (a `lake rebuild` swapped the lake out from under the run), the command warns once, reads the files for the rest of the run, and reports `lake_read_failed: true`. The plans, the rendered transcripts and the checkpoint bounds are the same whichever source runs. Outputs land under the corpus's `analytics/` directory either way. `:1624`

classify and conflicts skip non-interactive sessions (`session_outcomes.kind` of `turn_audit` or `one_shot_job`), and friction and perceived read human turns only. The deterministic authorship views (`user_steps`, `human_turns`, `session_outcomes`) are plain views, so they need no `analyze` run.

## embed

```
atif-sql embed [OPTIONS]
```

Embed unembedded corpus steps and append them to LanceDB, with Cohere Embed v4 on Bedrock by default or with EmbeddingGemma 2 on this machine.
`packages/atif-cli/src/atif_cli/app.py:766`

`ATIF_SQL_EMBED_PROVIDER` picks the embedder:

- `cohere` (the default) sends step text to Cohere Embed v4 on Bedrock, which bills the AWS account, and writes `<corpus_root>/embeddings_lance` at 1024 dimensions (256, 512, or 1536 with `ATIF_SQL_OUTPUT_DIMENSION`).
- `gemma` runs `google/embeddinggemma-2` text-only on this machine at a pinned revision, free, and writes `<corpus_root>/embeddings_lance_gemma` at 768 dimensions (512, 256, or 128 with `ATIF_SQL_OUTPUT_DIMENSION`). It needs the `local` extra and downloads about 1.5 GB into the Hugging Face cache on first use; `HF_HUB_OFFLINE=1` keeps later runs off the network. `ATIF_SQL_GEMMA_DEVICE` (`auto`, `cpu`, `cuda`, `mps`) and `ATIF_SQL_GEMMA_BATCH_SIZE` (default 32) tune it. `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:193`

Each store is stamped with the model and width that wrote it, so a store written by one provider refuses the other's vectors rather than mixing two vector spaces. `ATIF_SQL_LANCE_URI` names one store for either provider.

Flags:

- `--limit`: cap the number of steps embedded this run. `:736`
- `--all`: explicitly embed every unembedded step, a full backfill. `:737`
- `--dry-run`: preview only; emit the plan JSON with keys `pipeline, discovery, candidates, batches, batch_size, concurrency, provider, model, dim, store, limit, dry_run` and make no embedding calls. `:738`
- `--corpus-root`: override the materialized corpus root. `:739`
- `--format`: output format. `:740`

A real run requires an explicit scope: a bare `atif-sql embed` exits `64` with a hint rather than starting an unbounded backfill. `:778`

Exit codes: `0` success, `64` missing `--limit` or `--all`, or an `ATIF_SQL_OUTPUT_DIMENSION` the provider can't emit, `70` runtime (Bedrock, local model, DuckDB, or Lance failure: transient, safe to retry), `78` terminal state, where the store or its config needs operator action and unattended lanes suppress retries. The `gemma` provider without the `local` extra exits `78` with the install command in the message. `:767`

## search

```
atif-sql search [OPTIONS] QUERY_TEXT
```

Semantic top-k nearest-neighbor search over step embeddings.
`packages/atif-cli/src/atif_cli/app.py:860`

Flags:

- `QUERY_TEXT`: required positional-only text, embedded by the provider `ATIF_SQL_EMBED_PROVIDER` selects: Cohere Embed v4 in `search_query` mode, or EmbeddingGemma 2 on this machine under its `SearchQuery` prompt. The search reads that provider's store. `:829`
- `-k` / `--k`: top-k; default `10`. This is the CLI's only short flag. `:832`
- `--session-id`: confine the kNN to one session. `:833`
- `--corpus-root`: override the materialized corpus root. `:834`
- `--all-corpora`: search every corpus's embedding store and add a `corpus` column; exits `78` without a usable lake and `64` together with `--no-lake`.
- `--lake` / `--no-lake`: read the lake when it holds the corpus (default), or read the per-session files.
- `--format`: output format. `:835`

Output columns are `uuid`, `session_id`, `snippet`, and `sim` (`:927`), ranked by cosine distance ascending so the highest similarity comes first (`:935`).

Exit codes: `0` success, `2` no embeddings yet, `65` embedding mismatch when the store was written by another provider, `70` runtime, including a failed query embedding, `78` the `gemma` provider without the `local` extra. `:864`

## examples

```
atif-sql examples [OPTIONS]
```

List tested example queries for every view and macro, derived from the static atif-duck catalog.
`packages/atif-cli/src/atif_cli/app.py:995`

Flags:

- `--category`: filter to one of `view`, `table-macro`, `scalar-macro`, validated against `CATEGORY_VALUES`. `:965`
- `--requires`: filter to one of `core`, `analytics`, `vss`, validated against `REQUIRES_VALUES`. `:966`
- `--format`: a TTY table grouped by `requires`, or a JSON object carrying `note` and `examples`. `:967`

Both value sets are `Literal` aliases in the producer package: `Requires` at `packages/atif-duck/src/atif_duck/domain/examples.py:48` and `Category` at `:53`, exported as tuples at `:55` and `:56`.

Exit codes: `0` ok, `64` unknown `--category` or `--requires` value. `packages/atif-cli/src/atif_cli/app.py:1020`

## schema

```
atif-sql schema [OPTIONS]
```

List every core and analytics view with its columns and every macro signature, each with its `requires` value.
`packages/atif-cli/src/atif_cli/app.py:1727`

Flags:

- `--format`: a TTY listing, or a JSON object carrying `views`, `view_requires` (view name to `requires`), `macros` (each with `name`, `params`, and `requires`), and `examples_hint`. `:1729`

The answer comes from the static `VIEW_SCHEMA`, `MACRO_SIGNATURES`, `ANALYTICS_VIEW_SCHEMA`, and `ANALYTICS_MACRO_SIGNATURES` dicts with no DuckDB import and no view registration. `requires` is `core` for an object that binds on any corpus, `analytics` for one that binds once `analyze` has written its parquet, and `vss` for one that needs the embedding store. `:1744`

## `lake`

```
atif-sql lake COMMAND
```

Build, check and maintain the DuckLake every corpus is queried through.
`packages/atif-cli/src/atif_cli/lake.py:30`

The group is attached to the root router by `app.command(lake_app)` in `packages/atif-cli/src/atif_cli/app.py`, and the lake itself lives in `atif_duck.infrastructure.lake`. The lake root defaults to `ATIF_SQL_LAKE_ROOT`, else `~/.atif-sql/lake`, and holds every corpus. All four commands are offline except the one-time `ducklake` extension install that `rebuild` makes. After a `rebuild`, every `materialize` keeps the lake current, and `query`, `analyze`, `embed` and `search` read it instead of opening each session's files. `--format` takes the same values as everywhere else: a table (or human lines) on a TTY, JSON on a pipe.

### `lake rebuild`

```
atif-sql lake rebuild [OPTIONS]
```

Load every corpus's per-session artifacts into a fresh lake, then swap it into place.
`packages/atif-cli/src/atif_cli/lake.py:79`

Flags:

- `--corpus-root`: a corpus to load; repeat for more. Default: every corpus the current lake holds, plus every directory under `ATIF_SQL_CORPUS_BASE` (default `~/.atif-sql/corpus`) that holds `sessions/`. `:82`
- `--lake-root`: the lake to rebuild. `:83`
- `--format`: report format. `:84`

The new lake is built beside the old one under the writer lock and renamed into place when complete, so a reader sees one lake or the other. It installs the `ducklake` DuckDB extension first when it is missing, which is the one network fetch the lake needs; `query` never installs it.

Exit codes: `0` done, `64` no corpus to load, or two corpora with the same directory name.

### `lake verify`

```
atif-sql lake verify [OPTIONS]
```

Compare every session's lake rows with its per-session artifacts.
`packages/atif-cli/src/atif_cli/lake.py:150`

Flags:

- `--corpus-root`: verify only this corpus; repeat for more. Default: every corpus the lake holds. `:153`
- `--lake-root`: the lake to verify. `:154`
- `--limit`: how many differences to list; all are counted. Default `20`. `:155`
- `--format`: report format. `:156`

Per table and session it compares the row count and an order-free content hash of the lake's rows against the same read from the per-session path. The report carries `clean`, `mismatched_sessions` and the first `--limit` mismatches.

Exit codes: `0` clean, `65` `lake_mismatch` (some session differs), `78` `lake_unavailable` (no lake, or its schema is stale).

### `lake status`

```
atif-sql lake status [OPTIONS]
```

Report the lake: present, schema current, corpora, snapshots, files, last write.
`packages/atif-cli/src/atif_cli/lake.py:240`

Flags:

- `--lake-root`: the lake to report on. `:243`
- `--format`: report format. `:244`

It reads the published reader catalog, so it never waits on a writer. A missing lake is a normal answer (`present: false`), not an error.

### `lake compact`

```
atif-sql lake compact [OPTIONS]
```

Merge small files, expire old snapshots, and remove the files nothing references.
`packages/atif-cli/src/atif_cli/lake.py:283`

Flags:

- `--expire-older-than-days`: expire snapshots older than this; default `ATIF_SQL_LAKE_EXPIRE_DAYS`, else `30`. Files only expired snapshots referenced are removed once they are an hour old, so a reader in flight keeps every file it names. `:286`
- `--memory-limit`: DuckDB's memory budget for the run, as a size such as `2GiB` or `1500MB`, instead of the one derived from the host and cgroup. The writer's own 2 GiB ceiling still applies. The nightly refresh lane passes one that fits its memory scope. `:287`
- `--lake-root`: the lake to compact. `:288`
- `--format`: report format. `:289`

The report carries the data file and snapshot counts before and after. If a merge runs out of memory nothing was published, so readers keep the previous catalog and the next run starts over.

Exit codes: `0` done, `64` a negative `--expire-older-than-days` or a malformed `--memory-limit`, `70` runtime error (an out-of-memory merge), `78` `lake_unavailable`.

## cron

```
atif-sql cron COMMAND
```

Inspect and manually install the `atif-sql` refresh cron lanes.
`packages/atif-cli/src/atif_cli/cron.py:38`

The group is attached to the root router by `app.command(cron_app)` at `packages/atif-cli/src/atif_cli/app.py:98`, and its lanes (`materialize` on `*/10 * * * *` and `llm` on `20 10 * * *`) are declared once in `LANES` at `packages/atif-cli/src/atif_cli/cron.py:47`. The `structural` lane was removed on 2026-09-27 and is listed in `REMOVED_LANES` (`:56`). `scripts/atif-sql-refresh.sh` still accepts `structural` (and `struct`) and exits 0 after logging `[structural] lane removed 2026-09-27 ...`, so a crontab line nobody has deleted yet stays quiet.

### cron install

```
atif-sql cron install [OPTIONS]
```

Print the crontab block for the refresh lanes and never write it. The block includes one comment per removed lane asking you to delete its line.
`packages/atif-cli/src/atif_cli/cron.py:185`

Flags:

- `--script`: path to `atif-sql-refresh.sh`. `packages/atif-cli/src/atif_cli/cron.py:185`

Without the flag the script is located by walking up from this module (`:147`); a tree where `scripts/atif-sql-refresh.sh` is unreachable exits `64` demanding `--script` (`:175`).

### cron status

```
atif-sql cron status [OPTIONS]
```

Report each lane's lock holder plus the last run and skip parsed from the refresh log.
`packages/atif-cli/src/atif_cli/cron.py:199`

Flags:

- `--script`: path to `atif-sql-refresh.sh`, whose `.run/` sibling holds the locks and the log. `packages/atif-cli/src/atif_cli/cron.py:201`
- `--tail`: how many trailing log lines to include; `0` disables, default `10`. `:202`
- `--format`: human lines on a TTY, JSON on a pipe. `:203`

The lock probe acquires and releases nonblocking, so the command perturbs no running lane. `:127`

## corpus slim

```
atif-sql corpus slim [--corpus-root PATH ...] [--lake-root PATH] [--no-dry-run] [--format auto|table|json|csv]
```

Converts corpora written by an earlier version to the compressed layout. Nothing else converts a corpus. A new version reads the old layout as it is. `packages/atif-cli/src/atif_cli/corpus.py`

It's a dry run by default. The dry run compresses each plain file into a byte counter, so the bytes it reports are the bytes a real run frees, and it changes nothing. With `--no-dry-run`, for each corpus it does three things in order:

1. It compresses every plain `trajectory.json`, `edges.jsonl` and `session_events.jsonl` into `<name>.zst`. Each copy is read back and compared with the plain file before the plain file is removed, and it keeps the plain file's mtime, because `analyze` bounds each session by its trajectory's mtime.
2. If any session still has per-session parquet, it runs `lake verify` for that corpus, reading every session from its trajectory and ignoring the parquet.
3. When that verify is clean, it deletes the parquet files.

It never rewrites `meta.json`. A corpus the lake doesn't hold, or one whose sessions differ from their lake rows (a lake write still pending, for example), keeps its parquet, and the report says why. Run `atif-sql materialize` to retry pending lake writes, or `atif-sql lake rebuild`, then run slim again. Run it while no materialize pass is running.

Flags:

- `--corpus-root`: a corpus to slim; repeat for more. Default: every corpus the lake holds, plus every directory under `ATIF_SQL_CORPUS_BASE` that holds `sessions/`.
- `--lake-root`: the lake that verifies the corpora (default `ATIF_SQL_LAKE_ROOT`).
- `--no-dry-run`: act.

The report lists, per corpus, the sessions in the old layout, the plain and compressed bytes, the parquet files and what happened to them (`deleted`, `would_delete`, `kept`, or `none`), and `bytes_before`, `bytes_after` and `bytes_freed`.

Exit codes: `0` done, `64` no corpus found, `65` `lake_mismatch` (a corpus kept its parquet because its sessions differ from the lake), `70` a file couldn't be compressed, `78` `lake_unavailable` (no usable lake for a corpus). A dry run exits `0`.

## See also

- [processes](../behavior/processes.md)
- [module map](../architecture/module-map.md)
- [debugging guide](../insights/debugging-guide.md)
- [dead code](../analysis/dead-code.md)
- [impact analysis](../insights/impact-analysis.md)
