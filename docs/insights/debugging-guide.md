# atif-sql · Debugging guide

Something is broken. Where do you look first?

atif-sql is a CLI plus six libraries with no server, no job queue, and no
observability platform. That narrows the search surface to four places, and
this guide is organized around them:

1. **The process exit code.** `EXIT_CODES` is one dict of 11 keys
   (`packages/atif-cli/src/atif_cli/errors.py:25-39`) and it is the primary
   diagnostic. Nine of the eleven are reachable.
2. **The classified error envelope on stderr** — one readable line on a TTY,
   one JSON object on a pipe (`packages/atif-cli/src/atif_cli/output.py:274-281`).
3. **The loguru sink.** Exactly one, added in `main()`, stderr, WARNING and up
   (`packages/atif-cli/src/atif_cli/app.py:1133-1134`).
4. **The cron refresh log and its marker files**, for anything that failed
   unattended (`scripts/atif-sql-refresh.sh:102-103`).

The exit-code contract, read from source and confirmed by running the CLI:

| code | keys | meaning |
| --- | --- | --- |
| 0 | `ok` | success |
| 2 | `empty_session`, `no_embeddings` | nothing to work on; not a fault |
| 64 | `invalid_input`, `parse_error` | the caller's input is malformed |
| 65 | `catalog_error`, `validation_error`, `embedding_mismatch` | the named object cannot be bound |
| 70 | `runtime_error` | everything else the adapter raises |
| 78 | `terminal_state`, `suspicious_scan` | an operator has to act; retrying cannot succeed |
| 127 | `harbor_missing` | RETIRED: kept so the table never renumbers; no code path raises it since the conversion became ours |
| 1 | *(none)* | an unhandled exception — outside the taxonomy |

One of these codes exists to be told apart from a crash rather than from another code.
**`suspicious_scan: 78`** is raised only by
`materialize` (`packages/atif-cli/src/atif_cli/app.py:429-443`), when the source scan finds
zero sessions over a non-empty corpus and ghost removal is refused; it shares 78 with
`terminal_state` because both mean an operator has to act, and carries its own `kind` string
because the remedy is a path rather than a store.

**Exit 1 is still a reachable outcome**, and it means an unhandled exception: `analyze` maps
nothing into the taxonomy (`packages/atif-cli/src/atif_cli/app.py:755-773` has no `except`),
and neither does a registration error outside `REGISTRATION_ERRORS`
(`packages/atif-cli/src/atif_cli/duck_errors.py:14-18`). Read a 1 as a bug report, never as a
diagnosis.

On a pipe the envelope is the last line on stderr, in this shape:

```json
{"error": {"kind": "invalid_input", "message": "a real embed run needs an explicit scope", "hint": "pass --limit N (bounded) or --all (full backfill), or preview with --dry-run"}}
```

## Failure-mode index

| Symptom | Likely surface | First check | Citation |
| --- | --- | --- | --- |
| `query` exits 64, kind `parse_error` | `duckdb.ParserException`, or no SQL argument at all | Run `atif-sql schema --format json` for the real view and column names; a bare `atif-sql query` also exits 64 | `packages/atif-cli/src/atif_cli/duck_errors.py:37-43`, `packages/atif-cli/src/atif_cli/app.py:599-610` |
| `query` exits 65, kind `catalog_error` | `duckdb.CatalogException` — the view, macro, or column does not exist | `atif-sql schema` answers from the static catalog with no DuckDB bind, so it works even on an unmaterialized corpus | `packages/atif-cli/src/atif_cli/duck_errors.py:44-50`, `packages/atif-cli/src/atif_cli/app.py:1086-1122` |
| `query` exits 70 with `IO Error: No files found that match the pattern .../sessions/*/meta.json` | The corpus is not materialized; `register_raw`'s glob matches nothing | `atif-sql status`; a `watermark: never materialized` line means run `atif-sql materialize` | `packages/atif-cli/src/atif_cli/duck_errors.py:51-57`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:306-309` |
| `query` or `search` exits 65, kind `embedding_mismatch` | The Lance store's stamped `(model, dim)` differs from the active embedder | The message names both sides; either point `ATIF_SQL_EMBED_MODEL_ID` at the model that wrote the store, or delete the store | `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:61-80`, `packages/atif-cli/src/atif_cli/duck_errors.py:74-81` |
| `search` exits 2, kind `no_embeddings` | The store is empty or absent, so `register_vss` bound an empty fallback table | `atif-sql query 'SELECT count(*) FROM message_embeddings'`; zero rows confirms it | `packages/atif-cli/src/atif_cli/app.py:930-941`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:896-914` |
| A bare `atif-sql embed` exits 64 | The scope guard, which runs before any Bedrock client is built | Pass `--limit N`, `--all`, or `--dry-run`; this is the accidental-full-backfill guard, not a fault | `packages/atif-cli/src/atif_cli/app.py:810-820` |
| `embed` exits 78 and the cron lane stops trying | A `DomainError` with `terminal = True` — the store or its config needs an operator | Read the terminal marker file the refresh lane drops: three lines, store path, mtime, reason | `packages/atif-embed/src/atif_embed/domain/errors.py:14-26`, `scripts/atif-sql-refresh.sh:244-249` |
| A session directory exists under `sessions/` but no view returns its rows | Torn artifact set: the dir has no `meta.json`, so the meta gate excludes it from every reader | Grep the WARNING; the complete set is four files, and `meta.json` is written last | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:173-177`, `docs/CONTRACT.md:22-31` |
| `atif-sql status` and `SELECT count(*) FROM sessions` disagree | `status` counts directories with no meta check; the view applies the meta gate | The difference is exactly the number of incomplete dirs — use it as the torn-set count | `packages/atif-cli/src/atif_cli/app.py:482-486`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:221` |
| `materialize` prints all zeroes and reads as an idle, complete corpus | Every session was unreadable; an unreadable session lands in no other counter | Read the `unreadable` field, not just `materialized` | `packages/atif-cli/src/atif_cli/app.py:312-346`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:136-142` |
| `materialize` aborts with a traceback naming `SuspiciousEmptyScanError`, exit 1 | The scan found zero sessions over a non-empty corpus — almost always a wrong `source_root` | Compare the `source_root` line in `atif-sql status` against where the transcripts actually are | `packages/atif-corpus/src/atif_corpus/application/materialize.py:97-104`, raise at `packages/atif-corpus/src/atif_corpus/application/materialize.py:553-559` |
| A changed session never materializes | Quiescence: the newest source mtime is younger than `quiesce_seconds` (default 300) | `atif-sql status` reports it under `live`; `--force` overrides staleness but never quiescence | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:81-104`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:172-173`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:201-203` |
| A session stays `live` forever and warns about a FUTURE mtime | The writing host's clock is ahead, so the age is negative and never meets the threshold | Fix the clock on the host writing the transcript | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:96-103` |
| `materialize` or `status` aborts inside `read_watermark` with `ValueError` or `TypeError` | A watermark whose values are not numbers; the `int(value)` coercion sits outside the try | Delete `watermark.json`; a missing watermark costs one full re-materialization pass and is always safe | `packages/atif-corpus/src/atif_corpus/application/materialize.py:160-176` |
| Every session fails conversion at once, on the same `ConversionError` | The converter is a port of harbor 0.22.0's, so a systematic failure is a bug in the port or a transcript shape it never saw | Run the parity oracle: `ATIF_PARITY_LIMIT=0 uv run pytest packages/atif-converter/tests/test_parity_live.py`, and read the diff paths | `packages/atif-converter/tests/harbor_oracle.py:142` |
| One session fails with `N source file(s) changed while converting …` | The session is still being written; the snapshot re-check refused to publish inconsistent artifacts | Retry once the session goes quiet — this is self-clearing | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:93-102`, `packages/atif-converter/src/atif_converter/domain/errors.py:57-70` |
| `convert` prints a trajectory and then exits 65 | The enriched trajectory failed harbor's `TrajectoryValidator` | Read `validation_errors` in the JSON it already printed | `packages/atif-cli/src/atif_cli/app.py:283-304`, `packages/atif-cli/src/atif_cli/converter_adapter.py:62-64` |
| `loss_report.json` shows `records_dropped > 0` | Fidelity gap 2: harbor 0.22.0 converts only user and assistant records | Check `gaps_observed` against the seven named gaps — these are documented losses, not defects | `packages/atif-converter/src/atif_converter/domain/fidelity.py:44-79` |
| `analyze` summary carries `budget_exhausted: true` and a stage `{"skipped": "budget_exhausted"}` | The dollar ceiling was crossed against running actual usage | Read `llm_spent_usd` in the same summary; raise `--max-cost-usd` or wait for the next run | `packages/atif-analytics/src/atif_analytics/application/analyze.py:164-179`, `packages/atif-analytics/src/atif_analytics/application/analyze.py:202-204` |
| An LLM stage skips the same sessions every run, forever | The retry queue is exhausted at 5 attempts; `attempts >= 5` with `completed_at IS NULL` is permanent and nothing resets it | `sqlite3 <corpus_root>/analytics/state.db 'SELECT * FROM retry_queue'` — note the pipeline is `user_friction`, never `friction` | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:176-193`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:35-41` |
| An LLM run stalls for minutes with no output | Bedrock throttling under tenacity: 10 attempts, exponential 2 s to 60 s | The per-backoff WARNING names the attempt number and the sleep | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:251-257`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:112-121` |
| A classify row reads `[refused]`, or a refusals sidecar row appears | `RefusalError` — a content filter or a refusal `finish_reason` | Nothing to fix here: a refusal is terminal, checkpointed, and never retried | `packages/atif-models/src/atif_models/domain/ports.py:43-48`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:284-290` |
| An LLM unit lands in the retry queue with a truncation error | `finish_reason=length` twice, or once already at the effort floor | Raise `max_completion_tokens` or shrink the prompt; the automatic ladder has only one reachable rung | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:291-297`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:229-249` |
| `analyze` crashes with a raw traceback and exit 1 | The `analyze` command body has no `except`, so nothing maps analytics errors into `EXIT_CODES` | Read the traceback's innermost frame; the exit code carries no information here | `packages/atif-cli/src/atif_cli/app.py:739-757` |
| `atif-sql examples` raises a bare `KeyError` naming `ARG_EXEMPLARS` or `DESCRIPTIONS` | A catalog drift tripwire firing at example-build time, not query time | Add the missing exemplar or description entry named in the message | `packages/atif-duck/src/atif_duck/domain/examples.py:150-158`, `packages/atif-duck/src/atif_duck/domain/examples.py:165-175` |
| A cron lane logs `analytics not yet installed, skipping` and exits 0 | The resolved CLI predates the `analyze` subcommand; `~/.local/bin/atif-sql` wins over the workspace venv | Check which binary the lane resolved, in the CLI-resolution order the script documents | `scripts/atif-sql-refresh.sh:180-185`, `scripts/atif-sql-refresh.sh:150-158` |

## Log and error surfaces

There is no structured-logging platform, no log file the application writes,
and no observability bootstrap. The whole surface is loguru on stderr plus the
shell-side refresh log.

| Surface | Where it emits | What to grep for | Citation |
| --- | --- | --- | --- |
| The one loguru sink | stderr, WARNING and above; `logger.remove()` deletes loguru's default handler first | `WARNING` / `ERROR`; nothing at INFO or DEBUG is emitted at all | `packages/atif-cli/src/atif_cli/app.py:1125-1135` |
| Classified error envelope, pipe form | stderr, one JSON object | `"error"`, `"kind"`, `"hint"` | `packages/atif-cli/src/atif_cli/errors.py:60-68` |
| Classified error, TTY form | stderr, one line plus an optional hint line | `[<kind>]` at line start, then `hint:` | `packages/atif-cli/src/atif_cli/output.py:274-281` |
| Process exit code | the shell | the nine reachable `EXIT_CODES` values | `packages/atif-cli/src/atif_cli/errors.py:25-39` |
| Per-session materialize failures | stderr, table format only | `FAILED <session_id>:` and `UNREADABLE <session_id>` | `packages/atif-cli/src/atif_cli/app.py:343-346` |
| Materialize report | stdout, JSON on a pipe | `unreadable`, `failures`, `removed_session_ids` | `packages/atif-cli/src/atif_cli/app.py:320-332` |
| Corpus freshness | `atif-sql status` stdout | `watermark_age_seconds`, `staleness`, `source_root` | `packages/atif-cli/src/atif_cli/app.py:508-520` |
| DDL failure narration | stderr via `logger.exception`, with loguru's decorated traceback, then re-raised | `Failed to register raw readers over`, `Failed to register derived views`, `Failed to register macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:308`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:802`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1203` |
| Torn-session exclusion | stderr WARNING, one line per excluded dir | `Skipping incomplete session dir` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:173-177` |
| Empty-store fallback | stderr WARNING at VSS bind | `No Lance embeddings table at` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:897-902` |
| Unreadable-source warnings | stderr WARNING during the scan | `NOT treated as deleted`, `NOT treating it as deleted` | `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:133-138`, `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:179-184` |
| Ghost-removal suppression | stderr WARNING | `skipping ghost removal`, `keeping session` | `packages/atif-corpus/src/atif_corpus/application/materialize.py:566-570`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:416-420` |
| Watermark degradation | stderr WARNING | `treating corpus as unmaterialized` | `packages/atif-corpus/src/atif_corpus/application/materialize.py:171`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:174` |
| Bedrock retry narration | stderr WARNING, one per backoff | `bedrock invoke retry` (LLM), `Retrying` … `seconds as it raised` (embeddings) | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:112-121`, `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:94-101` |
| Degraded-effort retry | stderr WARNING | `finish_reason=length at effort=` | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:237-243` |
| Cost-ceiling events | stderr, WARNING or ERROR at 3 consecutive skips | `cost ceiling hit` | `packages/atif-analytics/src/atif_analytics/application/analyze.py:169-178`, `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:205` |
| Terminally failed embed batch | stderr ERROR | `failed terminally`, `the next run re-picks these rows` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:377-384` |
| Clipped embedding input | stderr WARNING | `Clipping text at position`, `content past the cap will not match a search` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:332-338` |
| Refresh-script log | a file appended under the script's `.run/` sibling, one `date -Is`-stamped line per event; the directory is gitignored, so the path is named in prose only | `refresh complete (mode=`, `skip[`, `TERMINAL:`, `FATAL:` | `scripts/atif-sql-refresh.sh:100-103`, `scripts/atif-sql-refresh.sh:337` |
| Refresh log, machine-read subset | `atif-sql cron status` stdout | only two line shapes are parsed: completion and skip | `packages/atif-cli/src/atif_cli/cron.py:55-56` |
| Lane lock state | `atif-sql cron status`, from a nonblocking flock probe plus the pidfile | `RUNNING (pid …)` versus `idle` | `packages/atif-cli/src/atif_cli/cron.py:109-124`, `packages/atif-cli/src/atif_cli/cron.py:127-144` |
| Embed terminal marker | one file per corpus beside the refresh log; three lines — store path, store mtime, reason | the reason string, parsed out of the exit-78 envelope | `scripts/atif-sql-refresh.sh:238-249` |
| Analytics durable state | `<corpus_root>/analytics/state.db`, sqlite in WAL mode; three tables, no read command | `retry_queue`, `budget_skips`, `session_checkpoint` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:106-108`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:43-62` |
| Analyze summary | stdout JSON | `budget_exhausted`, `llm_spent_usd`, `consecutive_skips` | `packages/atif-analytics/src/atif_analytics/application/analyze.py:179`, `packages/atif-analytics/src/atif_analytics/application/analyze.py:202-204` |
| Per-session loss accounting | `loss_report.json` inside each corpus session dir | `gaps_observed`, `records_dropped`, `records_total` | `packages/atif-converter/src/atif_converter/domain/fidelity.py:114-137` |

Two properties of this surface change how you read it.

**Everything below WARNING is unreachable through the CLI.** `main()` hardcodes
the level and there is no `--verbose`, no `--log-level`, and no `LOGURU_LEVEL`
support — `logger.remove()` deletes the default handler that would honor the
env var (`packages/atif-cli/src/atif_cli/app.py:1133-1134`). The INFO lines
that would narrate a materialize pass
(`packages/atif-corpus/src/atif_corpus/application/materialize.py:634-644`) or
an embed backfill
(`packages/atif-embed/src/atif_embed/application/embed.py:255-261`) never
appear. Reaching them means importing `atif_cli.app` and calling `app()`
directly after adding your own sink, instead of going through `main()`.

**A `logger.exception` traceback on stderr is not a crash.** The registration
path logs and re-raises, so the decorated traceback is followed by the JSON
envelope and a classified exit code
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:306-309`). Read
the last stderr line, not the first.

## First-checks ladder

Cheapest first. Steps 1 through 6 are free and read-only; step 10 spends money.

1. **Read the exit code.** It partitions the problem before you read any text:
   64 means your input, 65 means the object cannot be bound, 70 means the
   adapter, 78 means an operator is required, and 1 means the taxonomy did not
   cover it. `packages/atif-cli/src/atif_cli/errors.py:25-39`
2. **Read the last line on stderr.** On a pipe it is the JSON envelope with
   `kind`, `message`, and `hint`; on a TTY it is `[kind] message` plus a
   `hint:` line. The hint names the recovery command in most cases.
   `packages/atif-cli/src/atif_cli/output.py:267-281`
3. **Run `atif-sql status`.** It replays the exact planning decision
   `materialize` would make, without converting anything, so its
   `stale / up-to-date / live` split answers "would a pass do anything right
   now?" — and it prints the resolved `source_root` and `corpus_root`, which is
   how a wrong-root problem becomes visible.
   `packages/atif-cli/src/atif_cli/app.py:475-481`,
   `packages/atif-cli/src/atif_cli/app.py:495-496`
4. **Run `atif-sql schema` or `atif-sql examples`.** Both answer from the
   static catalog with no DuckDB connection, so they work on an unmaterialized
   corpus and separate "my SQL is wrong" from "the corpus is not there". Every
   example is executed by the test suite against a fixture corpus, so a
   verbatim copy is known to parse.
   `packages/atif-cli/src/atif_cli/app.py:1022-1026`,
   `packages/atif-cli/src/atif_cli/app.py:1099`,
   `packages/atif-cli/src/atif_cli/app.py:987-991`
5. **Cross-check `status`'s session count against the view.** `atif-sql query
   'SELECT count(*) FROM sessions'` applies the meta gate; `status` counts
   directories. A gap is the torn-dir count, and each torn dir also emits a
   WARNING naming itself.
   `packages/atif-duck/src/atif_duck/infrastructure/registry.py:173-177`
6. **Run `atif-sql cron status`.** For anything that failed unattended: per
   lane it reports whether the flock is held and by which pid, the last
   completion with its exit code, the last skip, and a log tail. The lock probe
   acquires and releases nonblocking, so it perturbs no running lane.
   `packages/atif-cli/src/atif_cli/cron.py:205-208`,
   `packages/atif-cli/src/atif_cli/cron.py:226-245`
7. **Look for a terminal marker file** beside the refresh log. Its presence is
   why an embed stopped being attempted; it holds the store path, the mtime it
   was keyed on, and the reason. Any operator touch that changes the store
   directory's mtime clears it on the next tick.
   `scripts/atif-sql-refresh.sh:217-236`
8. **Query `state.db` directly** at `<corpus_root>/analytics/state.db`. There
   is no CLI or SQL surface over it. `retry_queue` rows with `attempts >= 5`
   and `completed_at IS NULL` are permanently blocked and need manual
   clearing; `budget_skips` rows are the consecutive-skip streak. The pipeline
   name is `user_friction`.
   `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42-56`
9. **Re-run the failing library call in-process with your own loguru sink** if
   the WARNING lines were not enough. This is the only way to see the DEBUG
   narration of the staging sweep, the mid-scan races, the atomic-write tmp
   cleanups, and every view and macro registration.
   `packages/atif-cli/src/atif_cli/app.py:1133-1134`
10. **Only now re-run a billable command**, and bound it: `analyze` defaults to
    a dry run and needs `--no-dry-run` to spend, and `embed` refuses to run
    without `--limit N` or `--all`. `atif-sql search` always calls Bedrock,
    once, to embed the query.
    `packages/atif-cli/src/atif_cli/app.py:743`,
    `packages/atif-cli/src/atif_cli/app.py:810`,
    `packages/atif-cli/src/atif_cli/app.py:943`

## Known incident patterns

- **`EmbeddingStoreSchemaStale` and the silent retry storm:** a store state no
  retry can clear once retried 400+ times across 3 days of 10-minute ticks with
  zero escalation, because every failure exited alike. Signal: an embed failing
  identically every tick with no operator ever paged. Mitigation: the terminal
  flag on the error, exit 78 as its own code, and a marker keyed on the store
  path plus its mtime that suppresses retries until an operator touch changes
  it. `packages/atif-embed/src/atif_embed/domain/errors.py:14-26`,
  `scripts/atif-sql-refresh.sh:187-194`
- **Destroy-and-rebuild on a missing column:** raising a rebuild-demanding
  error for a store that merely predates the `text_hash` stamp rebuilt both
  fleet corpora, roughly 3.4M vectors of Cohere spend. Signal: a full
  re-embed triggered by a schema read rather than by a provider change.
  Mitigation: additive drift now migrates online via `Table.add_columns` and a
  sentinel that re-embeds incrementally through the ordinary staleness path,
  so search stays online throughout.
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:344-351`,
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166-192`
- **Corpus-slug collapse:** setting only `ATIF_SQL_SOURCE_ROOT` points one
  corpus's source at another root's transcripts while the corpus root still
  slugs from `CLAUDE_CONFIG_DIR`, so two corpora collapse onto one slug
  (`projects-73b00a87`, verified live 2026-08-23). Signal: two config roots,
  one corpus directory, and sessions from both interleaved. Mitigation: every
  tick exports both `CLAUDE_CONFIG_DIR` and `ATIF_SQL_SOURCE_ROOT` per corpus.
  `scripts/atif-sql-refresh.sh:52-59`, `scripts/atif-sql-refresh.sh:312-313`
- **Divergence from harbor's conversion:** the converters are parity ports of harbor
  0.22.0's private ones, and harbor keeps changing those files upstream. Signal:
  `test_harbor_oracle.py` reports a golden diff after a harbor bump, or the live
  parity test names diverging JSON paths. Mitigation: each diff is a decision to
  follow upstream or not, recorded in the fidelity policy; re-freeze the goldens
  only after it (`packages/atif-converter/tests/harbor_oracle.py:135`). `packages/atif-converter/tests/harbor_oracle.py:59`
- **`SourceMutatedDuringConversion`:** a session that resumes writing
  mid-conversion would yield a census, a trajectory, and an `edges.jsonl` each
  describing different bytes. Signal: one session failing with a
  `N source file(s) changed` message while its siblings succeed. Mitigation: the
  files are read once, fingerprinted in the same pass that parses them, and the
  fingerprints are re-checked (stat and digest) once the artifacts are built,
  refusing rather than publishing; retrying once the session goes quiet succeeds.
  `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:11-22`,
  `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:93-102`
- **`SuspiciousEmptyScanError`:** a wrong `source_root` — a typo, an unmounted
  disk, a stale env var — makes the scan empty, and ghost collection would then
  delete the entire corpus. Signal: a traceback naming the error, with the
  scanned root and the materialized session count in the message, and nothing
  removed. Mitigation: the guard runs before ghost removal and refuses the
  pass; an operator who genuinely emptied the source tree deletes the corpus
  directory explicitly.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:97-104`,
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:553-559`
- **Absence mistaken for deletion:** `Path.glob` swallows `PermissionError` and
  yields nothing, which would make every session under an unlistable project
  directory a ghost. Signal: a WARNING naming the directory count, and a pass
  that removed nothing. Mitigation: only `FileNotFoundError` on `stat` counts
  as gone; an unlistable directory disables ghost collection for the whole
  pass, and the affected session ids are resolved from the watermark so their
  entries are retained.
  `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:83-95`,
  `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:119-140`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:560-571`
- **Lane-lock inheritance through fd 9:** the refresh lane's flock is held by
  an open descriptor, not by a pid, so a descendant that inherits it keeps the
  lane locked for as long as it lives — a lane that stops doing work while
  every log line still reads like a clean single-flight. Signal: `cron status`
  reporting a held lock with no corresponding work in the log. Mitigation:
  every `atif-sql` invocation in the script closes fd 9 with `9>&-`, while the
  parent keeps its own.
  `scripts/atif-sql-refresh.sh:160-176`
- **`AWS_PROFILE` reaching a Bedrock call:** an inherited profile name with no
  matching `~/.aws/config` entry makes botocore raise `FileNotFoundError` at
  credential resolution, killing the lane before it reaches Bedrock at all.
  Signal: an llm lane failing instantly with a credential-resolution error
  rather than a Bedrock one. Mitigation: the script unsets `AWS_PROFILE` and
  `AWS_DEFAULT_PROFILE`, and authenticates from a run-time-read bearer token or
  the default credential chain.
  `scripts/atif-sql-refresh.sh:91-96`, `scripts/atif-sql-refresh.sh:116-128`
- **The seven `FidelityGap` members:** these are documented, verified upstream
  losses, not defects — workflow-nested subagents missed by harbor's own
  discovery, non-message records dropped, the parent chain flattened by
  timestamp sort, subagents inlined rather than embedded, compaction summaries
  unhandled, the cache split only partially preserved, and the event uuid not
  preserved. Signal: `records_dropped > 0` or a non-empty `gaps_observed` in a
  `loss_report.json`. Mitigation: none is intended — the converter's job is
  honest loss accounting, and unit tests pin each gap so a harbor bump that
  changes behavior trips the suite.
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:44-79`
- **Budget overshoot and the streak that never escalates:** the cost ceiling
  stops dispatch at an 8-unit batch boundary rather than capping spend exactly,
  and the consecutive-skip streak is cleared whenever a stage ran at all —
  including a run that aborted mid-chunk on the ceiling. Signal: repeated
  `cost ceiling hit` WARNINGs that never become the ERROR line. Mitigation:
  read `llm_spent_usd` in the summary rather than trusting the streak, and
  treat a mid-stage abort as leaving nothing durable behind: no checkpoint, no
  cache row, no retry entry for the unstarted units.
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:141-152`,
  `packages/atif-analytics/src/atif_analytics/application/analyze.py:164-198`
- **Deterministic truncation and the one-rung ladder:** resending a
  `finish_reason=length` request with identical parameters truncates identically
  and doubles the bill, so the single retry degrades `reasoning_effort` instead.
  Signal: a `finish_reason=length at effort=high` WARNING followed by a retry
  queue entry. Mitigation: the degrade ladder — but note every spec resolves at
  `high`, so only `high` to `medium` is reachable in practice, and a second
  truncation raises `ProviderUnavailable` for the retry queue. Usage is
  accumulated before every gate, so a truncated call still counts against the
  budget.
  `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:79-99`,
  `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:229-249`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:277`
- **The `EmbeddingProviderMismatch` twin:** the class exists independently in
  two packages with different bases — a `DomainError` in atif-embed, a bare
  `Exception` in atif-duck — because the independence contract forbids the
  import. Signal: an unhandled traceback with exit 1 instead of the classified
  exit 65. Mitigation: `REGISTRATION_ERRORS` widens the caught tuple to cover
  both, and a test reads both twin modules as source text to require the shared
  recovery hint appear after the `raise` keyword in each.
  `packages/atif-cli/src/atif_cli/duck_errors.py:14-31`,
  `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16`

## See also

- [processes](../behavior/processes.md) — 29 shared source citations
- [module map](../architecture/module-map.md) — 24 shared source citations
- [business logic](business-logic.md) — 22 shared source citations
- [impact analysis](impact-analysis.md) — 21 shared source citations
- [contract map](contract-map.md) — 20 shared source citations
