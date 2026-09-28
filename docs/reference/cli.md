# atif-sql · CLI

The `atif-sql` console script (`packages/atif-cli/pyproject.toml:42`) is the repo's public contract: one cyclopts router (`packages/atif-cli/src/atif_cli/app.py:52`) dispatches the subcommands below, and a root-level `--version` resolves the installed `atif-sql` distribution (`:60`).

These conventions hold across every subcommand. `--format` takes `auto`, `table`, `json`, or `csv` from the `OutputFormat` `StrEnum` at `packages/atif-cli/src/atif_cli/output.py:58`, and `auto` resolves to `table` when stdout is a TTY and `json` otherwise (`packages/atif-cli/src/atif_cli/output.py:74`). Exit codes come from one table, `EXIT_CODES` at `packages/atif-cli/src/atif_cli/errors.py:28`: `0` ok, `2` empty-session / no-embeddings, `64` invalid input or SQL parse error, `65` catalog error / validation error / embedding mismatch, `70` runtime error, `78` terminal state or suspicious scan, `127` harbor missing. A number in that table is a wire contract: keys may be added, never renumbered. And stderr carries WARNING and above only, so a piped read emits data on stdout and nothing on stderr unless something is wrong; `ATIF_SQL_LOG_LEVEL` widens it (`INFO`, `DEBUG`) and is the only way to reach the INFO surface, because the entry point replaces loguru's default handler and with it the `LOGURU_LEVEL` that would have parameterized it (`packages/atif-cli/src/atif_cli/app.py:1141-1148`).

`analyze`, `embed`, and `search` call Amazon Bedrock and spend money; the others are offline.

Some subcommands take `--agent`, which selects the transcript format and, with it, the default source and corpus roots: `convert`, `materialize`, `status`, and `query`. The accepted spellings are `claude-code` (the default) and `codex`, and an unknown one exits `64` naming both (`packages/atif-cli/src/atif_cli/app.py:257`). `codex` moves the source root to `$CODEX_HOME` (default `~/.codex`) `/sessions` and the corpus root to `~/.atif-sql/corpus/codex`; an explicit flag or `ATIF_SQL_*` env var still wins (`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py`).

## convert

```
atif-sql convert [OPTIONS] SESSION-JSONL
```

Convert one Claude Code session JSONL or one Codex CLI rollout JSONL to ATIF plus a loss report and edges.
`packages/atif-cli/src/atif_cli/app.py:282`

Flags:

- `SESSION-JSONL` / `--session-jsonl` — required path to the transcript: `~/.claude/projects/<proj>/<session>.jsonl`, or `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl` under `--agent codex`. `:283`
- `--agent` — `claude-code` or `codex`; picks the converter and the fidelity policy. Defaults from `ATIF_SQL_AGENT`, then to `claude-code`. `:284`
- `--include-subagents` / `--no-subagents` — stage `<session>/subagents/**.jsonl` side-files alongside the main chain; default `True`, with the negative form named explicitly. `:228`
- `--trajectory-out` — write the trajectory JSON here and `edges.jsonl` beside it, instead of stdout. `:229`

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

- `--agent` — `claude-code` (default) or `codex`; picks the discovery layout and the default roots. `:426`
- `--force` / `--no-force` — re-materialize every quiescent session regardless of the watermark; default `False`. `:341`
- `--quiesce-seconds` — source-silence threshold; defaults from settings (contract: 300). `:342`
- `--source-root` — override the raw transcript root, otherwise `ATIF_SQL_SOURCE_ROOT` or `<CLAUDE_CONFIG_DIR>/projects`. `:343`
- `--corpus-root` — override the materialized corpus root, otherwise env or `~/.atif-sql/corpus/<slug>`. `:344`
- `--sessions` — comma-separated session-id filter; only these sessions are planned this pass. `:345`
- `--workers` — processes for the convert-write stage; default `ATIF_SQL_MATERIALIZE_WORKERS`, else `min(8, cpu_count)`. `1` is the single-process path. `:455`
- `--format` — report format. `:346`

Exit codes: `0` ok, `64` `--workers` below `1`; `78` the corpus at this root holds the other agent's sessions, refused with nothing touched; `78` suspicious scan — the source scan found zero sessions while the corpus holds materialized ones, so the pass refused to mark them source-removed and touched nothing. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`
- `--columnar` / `--no-columnar` — write the typed columnar artifacts (`session.parquet`, `steps.parquet`, `tool_calls.parquet`, `tool_results.parquet`) beside the JSON artifacts, staged and swapped with them; default `True`. `--no-columnar` writes only the contract's JSON artifacts and the source archive, and `query` reads those sessions from `trajectory.json`. `packages/atif-cli/src/atif_cli/app.py:452`
- `--format` — report format. `:346`

The report carries `convert_seconds` and `artifact_seconds` (the time spent writing the columnar files; `0.0` under `--no-columnar`), printed as `columnar: N.NNs` in the table form.

What the artifacts cost, measured on a 300-session, 1.6 GB Claude Code corpus (frozen snapshot, one machine, `/usr/bin/time`): a full `--force` pass took 91 s at a 797 MB peak with them and 55 s at a 670 MB peak without, and they add 455 MB on disk. Most of the extra memory is the producer's DuckDB and pyarrow imports (about 90 MB) plus a bounded working set; most of the extra time is the typed conversion of tool results. Every `query` after that reads typed columns: the panel statements dropped from 2.5 to 8 s and 4.5 to 8.6 GB peak to about 0.9 s and 490 MB each. `--no-columnar` is the right call for a corpus that's written far more often than it's queried.

Exit codes: `0` ok, `78` the corpus at this root holds the other agent's sessions, refused with nothing touched; `78` suspicious scan — the source scan found zero sessions while the corpus holds materialized ones, so the pass refused to mark them source-removed and touched nothing. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`

## status

```
atif-sql status [OPTIONS]
```

Report corpus freshness: watermark age, counts, bytes, staleness.
`packages/atif-cli/src/atif_cli/app.py:444`

It replays the planning a default `materialize` would do, so `staleness.stale` includes sessions a converter or columnar-schema change made stale (`staleness.generation_stale` counts those alone) and `staleness.from_archive` counts source-removed sessions the next pass would re-convert from their archive. `retained_sessions` counts sessions kept without a source, `empty_sessions` the transcripts recorded as empty, and `converter_schema` / `columnar_schema` are the running versions a current session must carry.

Flags:

- `--agent` — `claude-code` (default) or `codex`; the scan and the reported `agent` field follow it. `:529`
- `--source-root` — override the raw transcript root. `:414`
- `--corpus-root` — override the materialized corpus root. `:415`
- `--quiesce-seconds` — source-silence threshold used to replay `materialize`'s planning decision. `:416`
- `--format` — report format. `:417`

Both roots resolve through `_corpus_settings` (`:433`), so a `--source-root` given without `--corpus-root` re-derives the corpus root from the overridden source's slug unless `ATIF_SQL_CORPUS_ROOT` is set (`:206`).

The report also says how `query` will read this corpus. The table form prints `query path: columnar|json|mixed|empty (N of M complete sessions carry typed columnar artifacts)`; the JSON form carries `query_path`, `columnar_sessions`, and `json_sessions`. `columnar` means every complete session has current parquet artifacts, `json` means none does (a corpus materialized before the artifacts existed, or with `--no-columnar`), and `mixed` means the registry will union the two. `status` applies the same per-session predicate the registry does (`meta.columnar_schema` current and all four files present and non-empty), so it can't report `columnar` for a session `query` would read from JSON. `packages/atif-cli/src/atif_cli/app.py:639`

## query

```
atif-sql query [OPTIONS] [ARGS]
```

Run one SQL statement against the atif-duck catalog and emit results.
`packages/atif-cli/src/atif_cli/app.py:529`

Flags:

- `SQL` — positional-only statement; omitting it without `--examples` is a parse error. `:498`
- `--examples` — short-circuit to the `examples` listing, honoring `--category` and `--requires`, without opening DuckDB. `:501`
- `--category` — forwarded to the `examples` listing. `:502`
- `--requires` — forwarded to the `examples` listing. `:503`
- `--agent` — `claude-code` (default) or `codex`; selects which corpus the statement reads. `:628`
- `--corpus-root` — override the materialized corpus root. `:504`
- `--format` — `table` on a TTY, a JSON array of row objects on a pipe. `:505`

The statement runs against a hardened connection: reads reach the registered views and nothing else, and nothing under the corpus root is writable. Before registration the connection is sized to the host: a memory cap derived from available RAM (half of physical RAM or 8 GiB, whichever is larger, never above 80% of what's available) and a thread count of one per 2 GiB of that cap, capped at the CPUs the process may use. `ATIF_SQL_QUERY_MEMORY_LIMIT` (a DuckDB size such as `6GB`) and `ATIF_SQL_QUERY_THREADS` override both; a malformed value exits 64. The spill directory is a private `mkdtemp` (mode 0700) under the system temp dir, the only directory the sandbox grants, and it's removed when the process exits. Extension auto-install and auto-load are off, and the lance extension is loaded only when it's already installed, so registration never reaches the network (`packages/atif-cli/src/atif_cli/app.py`, `_configure_query_resources`).

Layered guards keep caller SQL from writing the corpus. DuckDB's file grants are read-write and it has no read-only grant, so `COPY ... TO <granted parquet> (USE_TMP_FILE false)` would overwrite one; the CLI therefore refuses every statement kind that names a file before executing anything, using DuckDB's own parser: `COPY`, `EXPORT`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `PREPARE` and `EXECUTE` exit 70 with kind `sandbox_refused`, for any uid, and a batch containing one of them runs nothing. And because a `0444` file mode doesn't bind root, `query` refuses to run as uid 0 (exit 77, kind `root_refused`) unless `ATIF_SQL_ALLOW_ROOT=1` is set, which logs a warning. `search` and `analyze` refuse root the same way.

What caller SQL can still see: `duckdb_settings()` and `current_setting(...)` return the sandbox's own configuration, including the corpus root, the spill directory, the memory cap and every granted parquet path, which names every session id. DuckDB can't hide a setting from SQL and the grants have to be per file, so this is accepted: the caller is the local user, who can list the corpus and `SELECT session_id FROM sessions` anyway, and `query` isn't a privilege boundary (see SECURITY.md).

The views themselves carry no corpus path as statement text. The registry hands its globs and file lists to `read_json(?)` as bound parameters and builds the parquet readers through DuckDB's relation API, so a corpus root such as `o'brien ?; --$1` and transcript content carrying SQL text both register as data (`packages/atif-duck/src/atif_duck/infrastructure/registry.py`). A session directory whose name fails the session id boundary (`packages/atif-duck/src/atif_duck/domain/session_id.py`) registers nothing and is logged once.

Sessions that carry current columnar artifacts are served from their parquet files, so no JSON is parsed for them at query time; the rest are read from `trajectory.json`, and the views union the two. The per-session parquet files the registry bound are granted to the sandbox the same way the analytics parquets are (as individual `allowed_paths` entries, `packages/atif-cli/src/atif_cli/app.py:221`), and they're written read-only (`0444`), so a `COPY ... TO` at one of them fails at the filesystem even though DuckDB's grant is read-write. `atif-sql status` says which path a corpus takes.

Exit codes: `64` parse error or a malformed `ATIF_SQL_QUERY_*` override, `65` catalog error, `65` embedding mismatch, `70` runtime error, `70` `sandbox_refused` (a statement kind the sandbox never runs), `77` `root_refused`. `:550`

## analyze

```
atif-sql analyze [OPTIONS]
```

Run the LLM analytics pipelines: classify, conflicts, friction, and perceived.
`packages/atif-cli/src/atif_cli/app.py:1240`

Flags:

- `--since-days` — restrict the stages to sessions whose last step is within N days; default `30`. `:1242`
- `--limit` — cap the number of sessions, newest-first, per stage. `:1243`
- `--max-sessions` — hard per-run session ceiling per LLM pipeline; overrides `ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN`, default 50. `:1244`
- `--max-cost-usd` — hard per-run dollar ceiling across all LLM pipelines, checked against running actual usage; overrides `ATIF_SQL_LLM_MAX_COST_USD_PER_RUN`, default 25.0. `:1245`
- `--no-dry-run` — execute the LLM stages for real, which costs money; the default is a dry run emitting plan dicts and cost estimates. `:1246`
- `--llm-only` — accepted and ignored. Every stage is an LLM stage now, and the flag stays so old crontab lines keep parsing. `:1247`
- `--skip-classify` — opt out of the classify stage. `:1248`
- `--skip-conflicts` — opt out of the conflicts stage. `:1249`
- `--skip-friction` — opt out of the friction stage. `:1250`
- `--skip-perceived` — opt out of the perceived stage. `:1251`
- `--corpus-root` — override the materialized corpus root. `:1252`
- `--no-lake` — read session data from the per-session files rather than the lake. `:1534`
- `--format` — summary format. `:1253`

Session data comes from the lake when it holds the corpus; a session whose lake rows aren't its current artifacts is read from its files, and with no usable lake the command warns once and reads the files. The summary's `session_source` says which source ran (`lake` or `files`), and on the lake `sessions_read_from_files` counts the sessions the last stage read from files. The plans, the rendered transcripts and the checkpoint bounds are the same whichever source runs. Outputs land under the corpus's `analytics/` directory either way. `:1624`

classify and conflicts skip non-interactive sessions (`session_outcomes.kind` of `turn_audit` or `one_shot_job`), and friction and perceived read human turns only. The deterministic authorship views (`user_steps`, `human_turns`, `session_outcomes`) are plain views, so they need no `analyze` run.

## embed

```
atif-sql embed [OPTIONS]
```

Embed unembedded corpus steps with Cohere Embed v4 and append them to LanceDB.
`packages/atif-cli/src/atif_cli/app.py:766`

Flags:

- `--limit` — cap the number of steps embedded this run. `:736`
- `--all` — explicitly embed every unembedded step, a full backfill. `:737`
- `--dry-run` — preview only; emit the plan JSON with keys `pipeline, candidates, batches, batch_size, concurrency, model, limit, dry_run` and make no embedding calls. `:738`
- `--corpus-root` — override the materialized corpus root. `:739`
- `--format` — output format. `:740`

A real run requires an explicit scope: a bare `atif-sql embed` exits `64` with a hint rather than starting an unbounded backfill. `:778`

Exit codes: `0` success, `64` missing `--limit` or `--all`, `70` runtime (Bedrock, DuckDB, or Lance failure — transient, safe to retry), `78` terminal state, where the store or its config needs operator action and unattended lanes suppress retries. `:767`

## search

```
atif-sql search [OPTIONS] QUERY_TEXT
```

Semantic top-k nearest-neighbor search over step embeddings.
`packages/atif-cli/src/atif_cli/app.py:860`

Flags:

- `QUERY_TEXT` — required positional-only text, embedded with Cohere Embed v4 in `search_query` mode. `:829`
- `-k` / `--k` — top-k; default `10`. This is the CLI's only short flag. `:832`
- `--session-id` — confine the kNN to one session. `:833`
- `--corpus-root` — override the materialized corpus root. `:834`
- `--format` — output format. `:835`

Output columns are `uuid`, `session_id`, `snippet`, and `sim` (`:927`), ranked by cosine distance ascending so the highest similarity comes first (`:935`).

Exit codes: `0` success, `2` no embeddings yet, `65` embedding mismatch when the store was written by another provider, `70` runtime. `:864`

## examples

```
atif-sql examples [OPTIONS]
```

List tested example queries for every view and macro, derived from the static atif-duck catalog.
`packages/atif-cli/src/atif_cli/app.py:995`

Flags:

- `--category` — filter to one of `view`, `table-macro`, `scalar-macro`, validated against `CATEGORY_VALUES`. `:965`
- `--requires` — filter to one of `core`, `analytics`, `vss`, validated against `REQUIRES_VALUES`. `:966`
- `--format` — a TTY table grouped by `requires`, or a JSON object carrying `note` and `examples`. `:967`

Both value sets are `Literal` aliases in the producer package: `Requires` at `packages/atif-duck/src/atif_duck/domain/examples.py:48` and `Category` at `:53`, exported as tuples at `:55` and `:56`.

Exit codes: `0` ok, `64` unknown `--category` or `--requires` value. `packages/atif-cli/src/atif_cli/app.py:1020`

## schema

```
atif-sql schema [OPTIONS]
```

List every core and analytics view with its columns and every macro signature, each with its `requires` value.
`packages/atif-cli/src/atif_cli/app.py:1727`

Flags:

- `--format` — a TTY listing, or a JSON object carrying `views`, `view_requires` (view name to `requires`), `macros` (each with `name`, `params`, and `requires`), and `examples_hint`. `:1729`

The answer comes from the static `VIEW_SCHEMA`, `MACRO_SIGNATURES`, `ANALYTICS_VIEW_SCHEMA`, and `ANALYTICS_MACRO_SIGNATURES` dicts with no DuckDB import and no view registration. `requires` is `core` for an object that binds on any corpus, `analytics` for one that binds once `analyze` has written its parquet, and `vss` for one that needs the embedding store. `:1744`

## cron

```
atif-sql cron COMMAND
```

Inspect and manually install the `atif-sql` refresh cron lanes.
`packages/atif-cli/src/atif_cli/cron.py:38`

The group is attached to the root router by `app.command(cron_app)` at `packages/atif-cli/src/atif_cli/app.py:98`, and its lanes — `materialize` on `*/10 * * * *` and `llm` on `20 10 * * *` — are declared once in `LANES` at `packages/atif-cli/src/atif_cli/cron.py:47`. The `structural` lane was removed on 2026-09-27 and is listed in `REMOVED_LANES` (`:56`). `scripts/atif-sql-refresh.sh` still accepts `structural` (and `struct`) and exits 0 after logging `[structural] lane removed 2026-09-27 ...`, so a crontab line nobody has deleted yet stays quiet.

### cron install

```
atif-sql cron install [OPTIONS]
```

Print the crontab block for the refresh lanes and never write it. The block includes one comment per removed lane asking you to delete its line.
`packages/atif-cli/src/atif_cli/cron.py:185`

Flags:

- `--script` — path to `atif-sql-refresh.sh`. `packages/atif-cli/src/atif_cli/cron.py:185`

Without the flag the script is located by walking up from this module (`:147`); a tree where `scripts/atif-sql-refresh.sh` is unreachable exits `64` demanding `--script` (`:175`).

### cron status

```
atif-sql cron status [OPTIONS]
```

Report each lane's lock holder plus the last run and skip parsed from the refresh log.
`packages/atif-cli/src/atif_cli/cron.py:199`

Flags:

- `--script` — path to `atif-sql-refresh.sh`, whose `.run/` sibling holds the locks and the log. `packages/atif-cli/src/atif_cli/cron.py:201`
- `--tail` — how many trailing log lines to include; `0` disables, default `10`. `:202`
- `--format` — human lines on a TTY, JSON on a pipe. `:203`

The lock probe acquires and releases nonblocking, so the command perturbs no running lane. `:127`

## See also

- [processes](../behavior/processes.md)
- [module map](../architecture/module-map.md)
- [debugging guide](../insights/debugging-guide.md)
- [dead code](../analysis/dead-code.md)
- [impact analysis](../insights/impact-analysis.md)
