# atif-sql · CLI

The `atif-sql` console script (`packages/atif-cli/pyproject.toml:42`) is the repo's public contract: one cyclopts router (`packages/atif-cli/src/atif_cli/app.py:52`) dispatches the ten subcommands below, and a root-level `--version` resolves the installed `atif-sql` distribution (`:60`).

Three conventions hold across every subcommand. `--format` takes `auto`, `table`, `json`, or `csv` from the `OutputFormat` `StrEnum` at `packages/atif-cli/src/atif_cli/output.py:58`, and `auto` resolves to `table` when stdout is a TTY and `json` otherwise (`packages/atif-cli/src/atif_cli/output.py:74`). Exit codes come from one table, `EXIT_CODES` at `packages/atif-cli/src/atif_cli/errors.py:28`: `0` ok, `2` empty-session / no-embeddings, `64` invalid input or SQL parse error, `65` catalog error / validation error / embedding mismatch, `70` runtime error, `78` terminal state or suspicious scan, `127` harbor missing. A number in that table is a wire contract: keys may be added, never renumbered. And stderr carries WARNING and above only, so a piped read emits data on stdout and nothing on stderr unless something is wrong; `ATIF_SQL_LOG_LEVEL` widens it (`INFO`, `DEBUG`) and is the only way to reach the INFO surface, because the entry point replaces loguru's default handler and with it the `LOGURU_LEVEL` that would have parameterized it (`packages/atif-cli/src/atif_cli/app.py:1141-1148`).

`analyze`, `embed`, and `search` call Amazon Bedrock and spend money; the other seven are offline.

Four subcommands take `--agent`, which selects the transcript format and, with it, the default source and corpus roots: `convert`, `materialize`, `status`, and `query`. The accepted spellings are `claude-code` (the default) and `codex`, and an unknown one exits `64` naming both (`packages/atif-cli/src/atif_cli/app.py:257`). `codex` moves the source root to `$CODEX_HOME` (default `~/.codex`) `/sessions` and the corpus root to `~/.atif-sql/corpus/codex`; an explicit flag or `ATIF_SQL_*` env var still wins (`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py`).

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

A transcript whose session id fails the boundary in `packages/atif-corpus/src/atif_corpus/domain/session_id.py` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, at most 255 characters) is skipped with a logged reason and counted: the report carries `rejected` and `rejected_session_ids` beside `unreadable` and `unreadable_session_ids`, and the table form prints one `REJECTED` line per name on stderr. Nothing from such a session is written, and a corpus directory an older version wrote under that name is kept rather than removed as a ghost.

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

Exit codes: `0` ok, `64` `--workers` below `1`; `78` the corpus at this root holds the other agent's sessions, refused with nothing removed; `78` suspicious scan — the source scan found zero sessions while the corpus holds materialized ones, so ghost removal was refused and nothing was deleted. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`
- `--columnar` / `--no-columnar` — write the typed columnar artifacts (`session.parquet`, `steps.parquet`, `tool_calls.parquet`, `tool_results.parquet`) beside the four JSON artifacts, staged and swapped with them; default `True`. `--no-columnar` writes exactly the contract's JSON artifacts and `query` reads those sessions from `trajectory.json`. `packages/atif-cli/src/atif_cli/app.py:452`
- `--format` — report format. `:346`

The report carries `convert_seconds` and `artifact_seconds` (the time spent writing the columnar files; `0.0` under `--no-columnar`), printed as `columnar: N.NNs` in the table form.

What the artifacts cost, measured on a 300-session, 1.6 GB Claude Code corpus (frozen snapshot, one machine, `/usr/bin/time`): a full `--force` pass took 91 s at a 797 MB peak with them and 55 s at a 670 MB peak without, and they add 455 MB on disk. Most of the extra memory is the producer's DuckDB and pyarrow imports (about 90 MB) plus a bounded working set; most of the extra time is the typed conversion of tool results. Every `query` after that reads typed columns: the three panel statements dropped from 2.5 to 8 s and 4.5 to 8.6 GB peak to about 0.9 s and 490 MB each. `--no-columnar` is the right call for a corpus that's written far more often than it's queried.

Exit codes: `0` ok, `78` the corpus at this root holds the other agent's sessions, refused with nothing removed; `78` suspicious scan — the source scan found zero sessions while the corpus holds materialized ones, so ghost removal was refused and nothing was deleted. Check `--source-root`; a retry over the same root cannot succeed. `packages/atif-cli/src/atif_cli/app.py:429-443`

## status

```
atif-sql status [OPTIONS]
```

Report corpus freshness: watermark age, counts, bytes, staleness.
`packages/atif-cli/src/atif_cli/app.py:444`

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

The statement runs against a hardened connection: reads reach the registered views and nothing else, and the only writable path is the query engine's own spill directory `<corpus_root>/.duckdb_tmp`. `:601`

The views themselves carry no corpus path as statement text. The registry hands its globs and file lists to `read_json(?)` as bound parameters and builds the parquet readers through DuckDB's relation API, so a corpus root such as `o'brien ?; --$1` and transcript content carrying SQL text both register as data (`packages/atif-duck/src/atif_duck/infrastructure/registry.py`). A session directory whose name fails the session id boundary (`packages/atif-duck/src/atif_duck/domain/session_id.py`) registers nothing and is logged once.

Sessions that carry current columnar artifacts are served from their parquet files, so no JSON is parsed for them at query time; the rest are read from `trajectory.json`, and the views union the two. The per-session parquet files the registry bound are granted to the sandbox the same way the analytics parquets are (as individual `allowed_paths` entries, `packages/atif-cli/src/atif_cli/app.py:221`), and they're written read-only (`0444`), so a `COPY ... TO` at one of them fails at the filesystem even though DuckDB's grant is read-write. `atif-sql status` says which path a corpus takes.

Exit codes: `64` parse error, `65` catalog error, `65` embedding mismatch, `70` runtime error. `:550`

## analyze

```
atif-sql analyze [OPTIONS]
```

Run the analytics pipelines — cluster, terms, community, plus the LLM classify, trajectory, conflicts, friction, and perceived stages.
`packages/atif-cli/src/atif_cli/app.py:659`

Flags:

- `--since-days` — restrict LLM stages to sessions whose last step is within N days; default `30`, and structural stages always run over the full store. `:629`
- `--limit` — cap the number of sessions, newest-first, per LLM stage. `:630`
- `--max-sessions` — hard per-run session ceiling per LLM pipeline; overrides `ATIF_SQL_LLM_MAX_SESSIONS_PER_RUN`, default 50. `:631`
- `--max-cost-usd` — hard per-run dollar ceiling across all LLM pipelines, checked against running actual usage; overrides `ATIF_SQL_LLM_MAX_COST_USD_PER_RUN`, default 25.0. `:632`
- `--no-dry-run` — execute the LLM stages for real, which costs money; the default is a dry run emitting plan dicts and cost estimates. `:633`
- `--structural-only` — run only cluster, terms, and community, the hourly cron lane that fires at minute 17. `:634`
- `--llm-only` — run only classify, trajectory, conflicts, friction, and perceived, the nightly lane. `:635`
- `--skip-cluster` — opt out of the cluster stage. `:636`
- `--skip-terms` — opt out of the terms stage. `:637`
- `--skip-community` — opt out of the community stage. `:638`
- `--skip-classify` — opt out of the classify stage. `:639`
- `--skip-trajectory` — opt out of the trajectory stage. `:640`
- `--skip-conflicts` — opt out of the conflicts stage. `:641`
- `--skip-friction` — opt out of the friction stage. `:642`
- `--skip-perceived` — opt out of the perceived stage. `:643`
- `--force-cluster` — recompute clustering even when the mtime sidecar says the input is unchanged. `:644`
- `--force-community` — recompute community detection even when the mtime sidecar says the input is unchanged. `:645`
- `--corpus-root` — override the materialized corpus root. `:646`
- `--format` — summary format. `:647`

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

List every registered view with its columns and every macro signature.
`packages/atif-cli/src/atif_cli/app.py:1087`

Flags:

- `--format` — a TTY listing, or a JSON object carrying `views`, `macros`, and `examples_hint`. `:1057`

The answer comes from the static `VIEW_SCHEMA` and `MACRO_SIGNATURES` dicts with no DuckDB import and no view registration. `:1067`

## cron

```
atif-sql cron COMMAND
```

Inspect and manually install the `atif-sql` refresh cron lanes.
`packages/atif-cli/src/atif_cli/cron.py:38`

The group is attached to the root router by `app.command(cron_app)` at `packages/atif-cli/src/atif_cli/app.py:66`, and its three lanes — `materialize` on `*/10 * * * *`, `structural` on `17 * * * *`, `llm` on `20 10 * * *` — are declared once in `LANES` at `packages/atif-cli/src/atif_cli/cron.py:47`.

### cron install

```
atif-sql cron install [OPTIONS]
```

Print the crontab block for the three refresh lanes and never write it.
`packages/atif-cli/src/atif_cli/cron.py:180`

Flags:

- `--script` — path to `atif-sql-refresh.sh`. `packages/atif-cli/src/atif_cli/cron.py:180`

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

- [processes](../behavior/processes.md) — 7 shared source citations
- [module map](../architecture/module-map.md) — 6 shared source citations
- [debugging guide](../insights/debugging-guide.md) — 6 shared source citations
- [dead code](../analysis/dead-code.md) — 5 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 5 shared source citations
