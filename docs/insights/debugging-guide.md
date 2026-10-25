# atif-sql · Debugging guide

Something is broken. Where do you look first?

atif-sql is a CLI plus its libraries with no server, no job queue, and no
observability platform. That narrows the search surface to a few places, and
this guide is organized around them:

1. **The process exit code.** `EXIT_CODES` is one dict
   (`packages/atif-cli/src/atif_cli/errors.py:30-66`) and it is the primary
   diagnostic. Not every key is reachable.
2. **The classified error envelope on stderr**: one readable line on a TTY,
   one JSON object on a pipe (`packages/atif-cli/src/atif_cli/output.py:278-285`).
3. **The loguru sink.** Exactly one, added in `main()`, stderr, WARNING and up unless
   `ATIF_SQL_LOG_LEVEL` sets another level (`packages/atif-cli/src/atif_cli/app.py:2449-2452`).
4. **The cron refresh log and its marker files**, for anything that failed
   unattended (`scripts/atif-sql-refresh.sh:151-153`).

The exit-code contract, read from source and confirmed by running the CLI:

| code | keys | meaning |
| --- | --- | --- |
| 0 | `ok` | success |
| 2 | `empty_session`, `no_embeddings` | nothing to work on; not a fault |
| 64 | `invalid_input`, `parse_error` | the caller's input is malformed; a flag cyclopts refuses exits 64 too |
| 65 | `catalog_error`, `validation_error`, `embedding_mismatch`, `lake_mismatch` | the named object cannot be bound, or the lake disagrees with the artifacts |
| 70 | `runtime_error`, `sandbox_refused` | everything else the adapter raises, or a statement kind the query sandbox never runs |
| 77 | `root_refused` | `query`, `search`, or `analyze` running as uid 0 without `ATIF_SQL_ALLOW_ROOT=1` |
| 78 | `terminal_state`, `suspicious_scan`, `extension_missing`, `lake_unavailable` | an operator has to act; retrying cannot succeed |
| 127 | `harbor_missing` | RETIRED: kept so the table never renumbers; no code path raises it |
| 1 | *(none)* | an unhandled exception, outside the taxonomy |

One of these codes exists to be told apart from a crash rather than from another code.
**`suspicious_scan: 78`** is raised only by
`materialize` (`packages/atif-cli/src/atif_cli/app.py:1027-1041`), when the source scan finds
zero sessions over a non-empty corpus and marking sessions source-removed is refused; it shares 78 with
`terminal_state` because both mean an operator has to act, and carries its own `kind` string
because the remedy is a path rather than a store.

**Exit 1 is still a reachable outcome**, and it means an unhandled exception: `analyze` maps
no analytics error into the taxonomy (`packages/atif-cli/src/atif_cli/app.py:1739-1753` has a
`finally` and no `except`), and neither does a registration error outside `REGISTRATION_ERRORS`
(`packages/atif-cli/src/atif_cli/duck_errors.py:32`). Read a 1 as a bug report, never as a
diagnosis.

On a pipe the envelope is the last line on stderr, in this shape:

```json
{"error": {"kind": "invalid_input", "message": "a real embed run needs an explicit scope", "hint": "pass --limit N (bounded) or --all (full backfill), or preview with --dry-run"}}
```

## Failure-mode index

| Symptom | Likely surface | First check | Citation |
| --- | --- | --- | --- |
| `query` exits 64, kind `parse_error` | `duckdb.ParserException`, or no SQL argument at all | Run `atif-sql schema --format json` for the real view and column names; a bare `atif-sql query` also exits 64 | `packages/atif-cli/src/atif_cli/duck_errors.py:38-44`, `packages/atif-cli/src/atif_cli/app.py:1448-1459` |
| `query` exits 65, kind `catalog_error` | `duckdb.CatalogException`: the view, macro, or column does not exist | `atif-sql schema` answers from the static catalog with no DuckDB bind, so it works even on an unmaterialized corpus | `packages/atif-cli/src/atif_cli/duck_errors.py:45-51`, `packages/atif-cli/src/atif_cli/app.py:2357` |
| `query` exits 70 with `IO Error: No files found that match the pattern .../sessions/*/meta.json` | The corpus is not materialized; `register_raw`'s glob matches nothing | `atif-sql status`; a `watermark: never materialized` line means run `atif-sql materialize` | `packages/atif-cli/src/atif_cli/duck_errors.py:52-58`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:732` |
| `query` or `search` exits 65, kind `embedding_mismatch` | The Lance store's stamped `(model, dim)` differs from the active embedder | The message names both sides; either point `ATIF_SQL_EMBED_MODEL_ID` at the model that wrote the store, or delete the store | `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:78-85`, `packages/atif-cli/src/atif_cli/duck_errors.py:75-82` |
| `search` exits 2, kind `no_embeddings` | The store is empty or absent, so `register_vss` bound an empty fallback table | `atif-sql query 'SELECT count(*) FROM message_embeddings'`; a count of 0 confirms it | `packages/atif-cli/src/atif_cli/app.py:2167-2178`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1721-1739` |
| A bare `atif-sql embed` exits 64 | The scope guard, which runs before any Bedrock client is built | Pass `--limit N`, `--all`, or `--dry-run`; this is the accidental-full-backfill guard, not a fault | `packages/atif-cli/src/atif_cli/app.py:1930-1940` |
| `embed` exits 78 and the cron lane stops trying | A `DomainError` with `terminal = True`; the store or its config needs an operator | Read the terminal marker file the refresh lane drops: three lines, store path, mtime, reason | `packages/atif-embed/src/atif_embed/domain/errors.py:14-26`, `scripts/atif-sql-refresh.sh:398-403` |
| A session directory exists under `sessions/` but no view returns its rows | Torn artifact set: the dir has no `meta.json`, so the meta gate excludes it from every reader | Grep the WARNING; `meta.json` is written last and completes the set | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:562-566`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:706-707` |
| `atif-sql status` and `SELECT count(*) FROM sessions` disagree | `status` counts directories with no meta check; on the per-session path the view applies the meta gate and the session id boundary, and skips a dir with no readable trajectory | Run the count with `--no-lake`: the difference is the number of excluded dirs, and each one logs a WARNING | `packages/atif-cli/src/atif_cli/app.py:1239-1243`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:743` |
| `materialize` prints all zeroes and reads as an idle, complete corpus | Every session was unreadable; an unreadable session lands in no other counter | Read the `unreadable` field, not just `materialized` | `packages/atif-cli/src/atif_cli/app.py:780`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:253-261` |
| `materialize` exits 78, kind `suspicious_scan` | The scan found zero sessions over a non-empty corpus while every source directory listed, almost always a wrong `source_root` | Compare the `source_root` line in `atif-sql status` against where the transcripts actually are; nothing was marked | `packages/atif-cli/src/atif_cli/app.py:1027-1041`, raise at `packages/atif-corpus/src/atif_corpus/application/materialize.py:1661-1667` |
| A changed session never materializes | Quiescence: the newest source mtime is younger than `quiesce_seconds` (default 300) | `atif-sql status` reports it under `live`; `--force` overrides staleness but never quiescence | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:84`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:218-219`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:220-221` |
| A session stays `live` forever and warns about a FUTURE mtime | The writing host's clock is ahead, so the age is negative and never meets the threshold | Fix the clock on the host writing the transcript | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:107-113` |
| `materialize` re-converts every quiescent session after `watermark.json` is damaged | An unreadable, wrong-shaped, or non-numeric watermark degrades to empty with a WARNING, so every session reads as stale | Grep `treating corpus as unmaterialized`; the pass writes a fresh watermark, and the cost is one full re-materialization | `packages/atif-corpus/src/atif_corpus/application/materialize.py:424-451` |
| Every session fails conversion at once, on the same `ConversionError` | The converter is a port of harbor 0.24.0's, so a systematic failure is a bug in the port or a transcript shape it never saw | Run the parity oracle: `ATIF_PARITY_LIMIT=0 uv run pytest packages/atif-converter/tests/test_parity_live.py`, and read the diff paths | `packages/atif-converter/tests/harbor_oracle.py:358` |
| One session fails with `N source file(s) changed while converting …` | The session is still being written; the snapshot re-check refused to publish inconsistent artifacts | Retry once the session goes quiet; this is self-clearing | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:199-218`, `packages/atif-converter/src/atif_converter/domain/errors.py:57` |
| `convert` prints a trajectory and then exits 65 | The enriched trajectory failed the `TrajectoryValidator` vendored from harbor | Read `validation_errors` in the JSON it already printed | `packages/atif-cli/src/atif_cli/app.py:760-772`, `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:76` |
| `loss_report.json` shows `records_dropped > 0` | Fidelity gap 2: only user and assistant records become steps, and a record `session_events.jsonl` doesn't keep is dropped | Check `gaps_observed` against the named gaps; these are documented losses, not defects | `packages/atif-converter/src/atif_converter/domain/fidelity.py:71-77` |
| `analyze` summary carries `budget_exhausted: true` and a stage `{"skipped": "budget_exhausted", "consecutive_skips": N}` | The dollar ceiling was crossed against running actual usage | Read `llm_spent_usd` in the same summary; raise `--max-cost-usd` or wait for the next run | `packages/atif-analytics/src/atif_analytics/application/analyze.py:112-128`, `packages/atif-analytics/src/atif_analytics/application/analyze.py:150-152` |
| An LLM stage skips the same sessions every run, forever | The retry queue is exhausted at 5 attempts; `attempts >= 5` with `completed_at IS NULL` is permanent and nothing resets it | `sqlite3 <corpus_root>/analytics/state.db 'SELECT * FROM retry_queue'` (note the pipeline is `user_friction`, never `friction`) | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:199-204`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:37-42` |
| An LLM run stalls for minutes with no output | Bedrock throttling under tenacity, in either adapter: 10 attempts, exponential 2 s to 60 s | The per-backoff WARNING names the attempt number and the sleep | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:183-189`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:221-227`, `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:76-85` |
| A classify row reads `[refused]`, or a refusals sidecar row appears | `RefusalError`: a content filter or a refusal `finish_reason` (openai), or `stop_reason: refusal` (anthropic, with its `stop_details.category` in the reason) | Nothing to fix here: a refusal is terminal, checkpointed, and never retried | `packages/atif-models/src/atif_models/domain/ports.py:43-48`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:219-225`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:252-257` |
| An LLM unit lands in the retry queue with a truncation error | `finish_reason=length` (openai) or `stop_reason=max_tokens` (anthropic) twice, or once already at the effort floor | Raise `max_completion_tokens` or shrink the prompt; the automatic ladder has only one reachable rung | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:226-232`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:160-181`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:258-264`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:195-219` |
| An anthropic unit lands in the retry queue naming `unexpected stop_reason` or `no text content` | The response stopped somewhere other than `end_turn`, or carried only a `thinking` block | Read the reason string; it names the stop reason or the block types the response held | `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:265-276` |
| `llm_spent_usd` runs well past the dry-run plan's estimate | The plan prices at the standard rates only; a call over the spec's long-context threshold (272,000 input tokens on GPT-6, 100,000 on Haiku 5.5) bills the whole request at the long rates, and running actuals price that | Divide the plan's `estimated_input_tokens` by its `llm_calls`: an average near the threshold means some calls bill at the long rates. The long-context split isn't in the `analyze` summary | `packages/atif-analytics/src/atif_analytics/domain/costs.py:35-49`, `packages/atif-models/src/atif_models/domain/registry.py:175-210` |
| A classify, conflicts, or perceived verdict ignores what a tool returned | The transcript renders each tool result as a size stub, `(N chars omitted)`, so the model never saw the content | Set `ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES=true` to send bodies clipped at 50,000 characters, and expect the input tokens to grow with them | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:306-312`, `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:90-94` |
| `analyze` crashes with a raw traceback and exit 1 | The `analyze` command body has no `except`, so nothing maps analytics errors into `EXIT_CODES` | Read the traceback's innermost frame; the exit code carries no information here | `packages/atif-cli/src/atif_cli/app.py:1739-1753` |
| `atif-sql examples` raises a bare `KeyError` naming `ARG_EXEMPLARS` or `DESCRIPTIONS` | A catalog drift tripwire firing at example-build time, not query time | Add the missing exemplar or description entry named in the message | `packages/atif-duck/src/atif_duck/domain/examples.py:164-171`, `packages/atif-duck/src/atif_duck/domain/examples.py:180-188` |
| A cron lane logs `analytics not yet installed, skipping` and exits 0 | The resolved CLI predates the `analyze` subcommand; `~/.local/bin/atif-sql` wins over the workspace venv | Check which binary the lane resolved, in the CLI-resolution order the script documents | `scripts/atif-sql-refresh.sh:324-328`, `scripts/atif-sql-refresh.sh:266-273` |
| The refresh log shows `[structural] lane removed 2026-09-27 ...` once an hour | A crontab line still calls the `structural` lane, which the script accepts and exits 0 on | Delete the hourly `structural` crontab line; `atif-sql cron install` prints a reminder comment for it | `scripts/atif-sql-refresh.sh:164-167`, `packages/atif-cli/src/atif_cli/cron.py:201-202` |

## Log and error surfaces

There is no structured-logging platform, no log file the application writes,
and no observability bootstrap. The whole surface is loguru on stderr plus the
shell-side refresh log.

| Surface | Where it emits | What to grep for | Citation |
| --- | --- | --- | --- |
| The one loguru sink | stderr, WARNING and above unless `ATIF_SQL_LOG_LEVEL` sets another level; `logger.remove()` deletes loguru's default handler first | `WARNING` / `ERROR`; INFO and DEBUG lines appear only under `ATIF_SQL_LOG_LEVEL=INFO` or `DEBUG` | `packages/atif-cli/src/atif_cli/app.py:2449-2457` |
| Classified error envelope, pipe form | stderr, one JSON object | `"error"`, `"kind"`, `"hint"` | `packages/atif-cli/src/atif_cli/errors.py:78-86` |
| Classified error, TTY form | stderr, one line plus an optional hint line | `[<kind>]` at line start, then `hint:` | `packages/atif-cli/src/atif_cli/output.py:279-282` |
| Process exit code | the shell | the reachable `EXIT_CODES` values | `packages/atif-cli/src/atif_cli/errors.py:30-66` |
| Per-session materialize failures | stderr, table format only | `FAILED <session_id>:`, `UNREADABLE <session_id>`, `REJECTED '<name>'`, and `LAKE PENDING` | `packages/atif-cli/src/atif_cli/app.py:843-854` |
| Materialize report | stdout, JSON on a pipe | `unreadable`, `failures`, `removed_session_ids` | `packages/atif-cli/src/atif_cli/app.py:798-824` |
| Corpus freshness | `atif-sql status` stdout | `watermark_age_seconds`, `staleness`, `source_root` | `packages/atif-cli/src/atif_cli/app.py:1286-1311` |
| DDL failure narration | stderr via `logger.exception`, with loguru's decorated traceback, then re-raised | `Failed to register raw readers over`, `Failed to register derived views`, `Failed to register macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:991`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1563`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2108` |
| Torn-session exclusion | stderr WARNING, one line per excluded dir | `Skipping incomplete session dir` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:562-566` |
| Empty-store fallback | stderr WARNING at VSS bind | `No Lance embeddings table at` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1722-1727` |
| Unreadable-source warnings | stderr WARNING during the scan | `NOT treated as deleted`, `NOT treating it as deleted` | `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:169`, `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:269` |
| Source-removal marking suppression | stderr WARNING | `not marking any session source-removed`, `keeping session` | `packages/atif-corpus/src/atif_corpus/application/materialize.py:1675`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1204` |
| Watermark degradation | stderr WARNING | `treating corpus as unmaterialized` | `packages/atif-corpus/src/atif_corpus/application/materialize.py:435`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:438`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:449` |
| Bedrock retry narration | stderr WARNING, one per backoff | `bedrock invoke retry` (LLM, both adapters), `Retrying` … `seconds as it raised` (embeddings) | `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:76-85`, `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:94-101` |
| Degraded-effort retry | stderr WARNING | `finish_reason=length at effort=` (openai), `stop_reason=max_tokens at effort=` (anthropic) | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:169-175`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:205-210` |
| Cost-ceiling events | stderr, WARNING or ERROR at 3 consecutive skips | `cost ceiling hit` | `packages/atif-analytics/src/atif_analytics/application/analyze.py:116-126`, `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:230` |
| Terminally failed embed batch | stderr ERROR | `failed terminally`, `the next run re-picks these rows` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:378-385` |
| Clipped embedding input | stderr WARNING | `Clipping text at position`, `content past the cap will not match a search` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:333-339` |
| Refresh-script log | a file appended under the script's `.run/` sibling (or `ATIF_SQL_REFRESH_RUN_DIR`), one `date -Is`-stamped line per event; the directory is gitignored, so the path is named in prose only | `refresh complete (mode=`, `skip[`, `TERMINAL:`, `FATAL:` | `scripts/atif-sql-refresh.sh:151-154`, `scripts/atif-sql-refresh.sh:540` |
| Refresh log, machine-read subset | `atif-sql cron status` stdout | only the completion and skip line shapes are parsed | `packages/atif-cli/src/atif_cli/cron.py:61-62` |
| Lane lock state | `atif-sql cron status`, from a nonblocking flock probe plus the pidfile | `RUNNING (pid …)` versus `idle` | `packages/atif-cli/src/atif_cli/cron.py:115-130`, `packages/atif-cli/src/atif_cli/cron.py:133-150` |
| Embed terminal marker | one file per corpus beside the refresh log; three lines: store path, store mtime, reason | the reason string, parsed out of the exit-78 envelope | `scripts/atif-sql-refresh.sh:398-403` |
| Analytics durable state | `<corpus_root>/analytics/state.db`, sqlite in WAL mode, no read command | `retry_queue`, `budget_skips`, `session_checkpoint` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:81-83`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:44-62` |
| Analyze summary | stdout JSON | `budget_exhausted`, `llm_spent_usd`, `consecutive_skips` | `packages/atif-analytics/src/atif_analytics/application/analyze.py:127`, `packages/atif-analytics/src/atif_analytics/application/analyze.py:150-152` |
| Per-session loss accounting | `loss_report.json` inside each corpus session dir | `gaps_observed`, `records_dropped`, `records_total` | `packages/atif-converter/src/atif_converter/domain/fidelity.py:114-137` |

These properties of this surface change how you read it.

**Everything below WARNING is hidden by default.** `main()` takes the level
from `ATIF_SQL_LOG_LEVEL` (default `WARNING`; an unknown level falls back to it
with a warning). There is no `--verbose` and no `--log-level`, and `LOGURU_LEVEL`
has no effect: `logger.remove()` deletes the default handler that would honor
it (`packages/atif-cli/src/atif_cli/app.py:2449-2457`). The INFO lines that
narrate a materialize pass
(`packages/atif-corpus/src/atif_corpus/application/materialize.py:1811-1825`) or
an embed backfill
(`packages/atif-embed/src/atif_embed/application/embed.py:270-276`) appear only
under `ATIF_SQL_LOG_LEVEL=INFO`, and each materialize pool worker reads the same
variable (`packages/atif-cli/src/atif_cli/app.py:859`).

**A `logger.exception` traceback on stderr is not a crash.** The registration
path logs and re-raises, so the decorated traceback is followed by the JSON
envelope and a classified exit code
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:989-992`). Read
the last stderr line, not the first.

## First-checks ladder

Cheapest first. Steps 1 through 6 are free and read-only; step 10 spends money.

1. **Read the exit code.** It partitions the problem before you read any text:
   64 means your input, 65 means the object cannot be bound, 70 means the
   adapter or the query sandbox, 77 means the process runs as root, 78 means
   an operator is required, and 1 means the taxonomy did not cover it.
   `packages/atif-cli/src/atif_cli/errors.py:30-66`
2. **Read the last line on stderr.** On a pipe it is the JSON envelope with
   `kind`, `message`, and `hint`; on a TTY it is `[kind] message` plus a
   `hint:` line. The hint names the recovery command in most cases.
   `packages/atif-cli/src/atif_cli/output.py:271-285`
3. **Run `atif-sql status`.** It replays the exact planning decision
   `materialize` would make, without converting anything, so its
   `stale / up-to-date / live` split answers "would a pass do anything right
   now?", and it prints the resolved `source_root` and `corpus_root`, which is
   how a wrong-root problem becomes visible.
   `packages/atif-cli/src/atif_cli/app.py:1224-1231`,
   `packages/atif-cli/src/atif_cli/app.py:1259-1260`
4. **Run `atif-sql schema` or `atif-sql examples`.** Both answer from the
   static catalog with no DuckDB connection, so they work on an unmaterialized
   corpus and separate "my SQL is wrong" from "the corpus is not there". Every
   example is executed by the test suite against a fixture corpus, so a
   verbatim copy is known to parse.
   `packages/atif-cli/src/atif_cli/app.py:2363-2367`,
   `packages/atif-cli/src/atif_cli/app.py:2276-2277`,
   `packages/atif-cli/src/atif_cli/app.py:2273-2275`
5. **Cross-check `status`'s session count against the view.** `atif-sql query
   --no-lake 'SELECT count(*) FROM sessions'` applies the meta gate; `status`
   counts directories. A gap is the number of dirs the gates excluded (a torn
   dir, a rejected name, no readable trajectory), and each one also emits a
   WARNING naming itself.
   `packages/atif-duck/src/atif_duck/infrastructure/registry.py:562-566`
6. **Run `atif-sql cron status`.** For anything that failed unattended: per
   lane it reports whether the flock is held and by which pid, the last
   completion with its exit code, the last skip, and a log tail. The lock probe
   acquires and releases nonblocking, so it perturbs no running lane.
   `packages/atif-cli/src/atif_cli/cron.py:207`,
   `packages/atif-cli/src/atif_cli/cron.py:228-253`
7. **Look for a terminal marker file** beside the refresh log. Its presence is
   why an embed stopped being attempted; it holds the store path, the mtime it
   was keyed on, and the reason. Any operator touch that changes the store
   directory's mtime clears it on the next tick.
   `scripts/atif-sql-refresh.sh:380-390`
8. **Query `state.db` directly** at `<corpus_root>/analytics/state.db`. There
   is no CLI or SQL surface over it. `retry_queue` rows with `attempts >= 5`
   and `completed_at IS NULL` are permanently blocked and need manual
   clearing; `budget_skips` rows are the consecutive-skip streak. The pipeline
   name is `user_friction`.
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42-56`
9. **Raise the log level** if the WARNING lines were not enough.
   `ATIF_SQL_LOG_LEVEL=DEBUG` shows the DEBUG narration of the staging sweep,
   the mid-scan races, the atomic-write tmp cleanups, and every view and macro
   registration.
   `packages/atif-cli/src/atif_cli/app.py:2418`
10. **Only now re-run a billable command**, and bound it: `analyze` defaults to
    a dry run and needs `--no-dry-run` to spend, and `embed` refuses to run
    without `--limit N` or `--all`. `atif-sql search` embeds the query once per
    run, on Bedrock under the default Cohere provider.
    `packages/atif-cli/src/atif_cli/app.py:1662`,
    `packages/atif-cli/src/atif_cli/app.py:1930`,
    `packages/atif-cli/src/atif_cli/app.py:2181`

## Known incident patterns

- **`EmbeddingStoreSchemaStale` and the silent retry storm:** a store state no
  retry can clear once retried on every 10-minute tick for days with
  no escalation, because every failure exited alike. Signal: an embed failing
  identically every tick with no operator ever paged. Mitigation: the terminal
  flag on the error, exit 78 as its own code, and a marker keyed on the store
  path plus its mtime that suppresses retries until an operator touch changes
  it. `packages/atif-embed/src/atif_embed/domain/errors.py:14-26`,
  `scripts/atif-sql-refresh.sh:331-338`
- **Destroy-and-rebuild on a missing column:** raising a rebuild-demanding
  error for a store that merely predates the `text_hash` stamp rebuilt both
  production corpora at the cost of a full Cohere re-embed. Signal: a full
  re-embed triggered by a schema read rather than by a provider change.
  Mitigation: additive drift now migrates online via `Table.add_columns` and a
  sentinel that re-embeds incrementally through the ordinary staleness path,
  so search stays online throughout.
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:344-351`,
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166-192`
- **Corpus-slug collapse:** setting only `ATIF_SQL_SOURCE_ROOT` points one
  corpus's source at another root's transcripts while the corpus root still
  slugs from `CLAUDE_CONFIG_DIR`, so two corpora collapse onto one slug
  (verified live 2026-08-23). Signal: two config roots,
  one corpus directory, and sessions from both interleaved. Mitigation: every
  tick exports both `CLAUDE_CONFIG_DIR` and `ATIF_SQL_SOURCE_ROOT` per corpus.
  `scripts/atif-sql-refresh.sh:91-98`, `scripts/atif-sql-refresh.sh:497-498`
- **Divergence from harbor's conversion:** the converters are parity ports of harbor
  0.24.0's private ones, and harbor keeps changing those files upstream. Signal:
  `test_harbor_oracle.py` reports a golden diff after a harbor bump, or the live
  parity test names diverging JSON paths. Mitigation: each diff is a decision to
  follow upstream or not, and a decision not to follow is named in the oracle's
  `DELIBERATE_DIVERGENCES`; re-freeze the goldens with `ATIF_FREEZE_GOLDENS=1`
  only after it (`packages/atif-converter/tests/test_harbor_oracle.py:31`). `packages/atif-converter/tests/harbor_oracle.py:53`
- **`SourceMutatedDuringConversion`:** a session that resumes writing
  mid-conversion would yield a census, a trajectory, and an `edges.jsonl` each
  describing different bytes. Signal: one session failing with a
  `N source file(s) changed` message while its siblings succeed. Mitigation: the
  files are read once, fingerprinted in the same pass that parses them, and the
  fingerprints are re-checked (stat and digest) once the artifacts are built,
  refusing rather than publishing; retrying once the session goes quiet succeeds.
  `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:11-24`,
  `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:199-218`
- **`SuspiciousEmptyScanError`:** a wrong `source_root` (a typo, an unmounted
  disk, a stale env var) makes the scan empty, and the pass would then
  mark the entire corpus source-removed. Signal: exit 78 with kind `suspicious_scan`,
  the scanned root and the materialized session count in the message, and nothing
  marked. Mitigation: the guard runs before any session is marked and refuses the
  pass; an operator who genuinely emptied the source tree deletes the corpus
  directory explicitly.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:179-189`,
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:1661-1667`
- **Absence mistaken for deletion:** `Path.glob` swallows `PermissionError` and
  yields nothing, which would make every session under an unlistable project
  directory look deleted. Signal: a WARNING naming the directory count, and a pass
  that marked nothing. Mitigation: only `FileNotFoundError` on `stat` counts
  as gone; an unlistable directory disables source-removal marking for the whole
  pass, and the affected session ids are resolved from the watermark so their
  entries are retained.
  `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:113-125`,
  `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:153-173`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1668-1677`
- **Lane-lock inheritance through fd 9:** the refresh lane's flock is held by
  an open descriptor, not by a pid, so a descendant that inherits it keeps the
  lane locked for as long as it lives: a lane that stops doing work while
  every log line still reads like a clean single-flight. Signal: `cron status`
  reporting a held lock with no corresponding work in the log. Mitigation:
  every `atif-sql` invocation in the script closes fd 9 with `9>&-`, while the
  parent keeps its own.
  `scripts/atif-sql-refresh.sh:288-292`
- **`AWS_PROFILE` reaching a Bedrock call:** an inherited profile name with no
  matching `~/.aws/config` entry makes botocore raise `FileNotFoundError` at
  credential resolution, killing the lane before it reaches Bedrock at all.
  Signal: an llm lane failing instantly with a credential-resolution error
  rather than a Bedrock one. Mitigation: the script unsets `AWS_PROFILE` and
  `AWS_DEFAULT_PROFILE`, and authenticates from a run-time-read bearer token or
  the default credential chain.
  `scripts/atif-sql-refresh.sh:142-147`, `scripts/atif-sql-refresh.sh:232-244`
- **The `FidelityGap` members:** these are documented, verified upstream
  losses, not defects: non-message records dropped, the parent chain flattened
  by timestamp sort, subagents inlined rather than embedded, compaction
  summaries unhandled, the cache split only partially preserved, and the event
  uuid not preserved. Gap 1 (workflow-nested subagents missed by harbor's own
  discovery) is retired, because the converter discovers every side file itself. Signal: `records_dropped > 0` or a non-empty `gaps_observed` in a
  `loss_report.json`. Mitigation: none is intended; the converter's job is
  honest loss accounting, and unit tests pin each gap so a harbor bump that
  changes behavior trips the suite.
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:55-99`
- **Budget overshoot and the starved stage:** the cost ceiling stops dispatch
  at an 8-unit batch boundary rather than capping spend exactly, and a stage
  the ceiling keeps cutting off starves run after run. Signal:
  `cost ceiling hit` WARNINGs naming the same stage on consecutive runs, which
  become the ERROR line at 3 consecutive budget skips. Mitigation: read `llm_spent_usd` in
  the summary rather than treating the ceiling as a hard cap. The streak is
  cleared only when a stage finishes inside its budget, so a stage that aborts
  mid-chunk on the ceiling holds its streak, and a mid-stage abort leaves
  nothing durable behind: no checkpoint, no cache row, no retry entry for the
  unstarted units.
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:146-157`,
  `packages/atif-analytics/src/atif_analytics/application/analyze.py:112-146`
- **Deterministic truncation and the one-rung ladder:** resending a truncated
  request (`finish_reason=length` on openai, `stop_reason=max_tokens` on anthropic)
  with identical parameters truncates identically and doubles the bill, so the
  single retry degrades the effort instead (`reasoning_effort`, or
  `output_config.effort`). Signal: a `finish_reason=length at effort=high` or
  `stop_reason=max_tokens at effort=high` WARNING followed by a retry queue entry.
  Mitigation: the degrade ladder both adapters share, but note every spec resolves
  at `high`, so only `high` to `medium` is reachable in practice, and a second
  truncation raises `ProviderUnavailable` for the retry queue. Usage is
  accumulated before every gate, so a truncated call still counts against the
  budget.
  `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:44-63`,
  `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:160-181`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:209-212`,
  `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:195-219`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:248-251`
- **The `EmbeddingProviderMismatch` twin:** the class exists independently in
  two packages with different bases (a `DomainError` in atif-embed, a bare
  `Exception` in atif-duck) because the independence contract forbids the
  import. Signal: an unhandled traceback with exit 1 instead of the classified
  exit 65. Mitigation: `REGISTRATION_ERRORS` widens the caught tuple to cover
  both, and a test reads both twin modules as source text to require the shared
  recovery hint appear after the `raise` keyword in each.
  `packages/atif-cli/src/atif_cli/duck_errors.py:15-32`,
  `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16`

## See also

- [processes](../behavior/processes.md)
- [module map](../architecture/module-map.md)
- [business logic](business-logic.md)
- [impact analysis](impact-analysis.md)
- [contract map](contract-map.md)
