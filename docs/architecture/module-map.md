# atif-sql · Module map

Seven uv workspace members live under `packages/*`, declared at `pyproject.toml:100`; the root carries
no `[project]` table because it is a virtual workspace holding only the member list, the shared dev
dependency-group, and the shared tool config (`pyproject.toml:1`). The internal import graph is a
star: atif-cli declares five siblings as `==`-pinned dependencies
(`packages/atif-cli/pyproject.toml:32`), plus one further edge from atif-analytics to atif-models that
a `forbidden` import-linter contract leaves open (`pyproject.toml:520`), with every other pair closed
by an `independence` contract (`pyproject.toml:515`). Every cross-package import sits inside a
function body or a `TYPE_CHECKING` block so the CLI's fast path pulls in no duckdb, harbor, lancedb,
boto3, or polars, which is why `PLC0415` is ignored workspace-wide (`pyproject.toml:164`). Modules
below are ordered by total source LOC, descending; LOC figures are `wc -l` over the file.

## atif-analytics

`run_analyze` composes eight pipelines in a fixed stage order — cluster, terms, community, classify,
trajectory, conflicts, friction, perceived
(`packages/atif-analytics/src/atif_analytics/application/analyze.py:36`). The first three are
structural math at zero LLM cost (`:8`); the remaining five call a model and honor `dry_run`, which
defaults to True as a cost guard so those stages return plan dicts instead of spending (`:19`). Every
classifier system prompt lives in one module, four of them assembled by concatenating a shared
appendix (`packages/atif-analytics/src/atif_analytics/application/prompts.py:1006`), and the pydantic
v2 response schemas they bind against are pure domain models whose field descriptions are themselves
part of the prompt surface (`packages/atif-analytics/src/atif_analytics/domain/models.py:3`). This is
the one member permitted to import a sibling — atif-models and nothing else
(`pyproject.toml:520`).

- `packages/atif-analytics/src/atif_analytics/application/prompts.py` (1021 LOC) — the task-framing
  system prompts, public constants assembled at
  `packages/atif-analytics/src/atif_analytics/application/prompts.py:1006`.
- `packages/atif-analytics/src/atif_analytics/domain/structure/community.py` (643 LOC) — pure
  Leiden+CPM and mutual-kNN graph math, no I/O, with `graspologic-native` behind one solver seam
  (`packages/atif-analytics/src/atif_analytics/domain/structure/community.py:261`).
- `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py` (638 LOC) — three
  friction tiers behind a message-length pre-filter: regex fast path, deterministic stamp rules, then
  the LLM (`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:5`).
- `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py` (523 LOC) — one
  window per text step, sent in chunks of at most 16 windows with a shared anchor turn
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:13`).
- `packages/atif-analytics/src/atif_analytics/domain/models.py` (516 LOC) — the response schemas, from
  `SessionClassification` (`packages/atif-analytics/src/atif_analytics/domain/models.py:22`) to
  `PerceivedErrorsResult` (`:484`).
- `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py` (451 LOC) —
  LangSmith's Perceived Error definition on the conflicts chassis; clean sessions produce zero rows
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:16`).
- `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py` (426 LOC) — one row
  per detected stance-conflict pair, keyed on two turn uuids, with refusals routed to a sidecar
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:11`).
- `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py` (404 LOC) — one row
  per session, anti-joined against the parquet cache and written in crash-resilient chunks
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:8`).

## atif-duck

`register(con, corpus_root)` binds a DuckDB connection to a contract-shaped corpus tree and exposes 16
views plus 9 macros (`packages/atif-duck/src/atif_duck/__init__.py:9`,
`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1212`). Those names are not introspected
at runtime: a static catalog answers `atif-sql schema` in under 50 ms with no DuckDB bind, and two
drift tests assert it column-for-column against `DESCRIBE` and signature-for-signature against the
DDL (`packages/atif-duck/src/atif_duck/domain/catalog.py:3`). The 12 analytics views and 13 analytics
macros register separately, each only when its backing parquet is populated, because a corpus with no
`atif-sql analyze` run is the default state
(`packages/atif-duck/src/atif_duck/infrastructure/analytics.py:124`). It is the one member with
`domain/` and `infrastructure/` but no `application/`, a deliberate shape that is why it carries no
layers contract among the seven (`pyproject.toml:462`).

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py` (1294 LOC) — the raw readers, the 16
  core views, the 9 macros, and the Lance attach path
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:830`).
- `packages/atif-duck/src/atif_duck/infrastructure/analytics.py` (581 LOC) — the v2 views and macros
  over the analytics parquet outputs, gated on file presence
  (`packages/atif-duck/src/atif_duck/infrastructure/analytics.py:124`).
- `packages/atif-duck/src/atif_duck/domain/catalog.py` (417 LOC) — `VIEW_NAMES`
  (`packages/atif-duck/src/atif_duck/domain/catalog.py:28`), `MACRO_SIGNATURES` (`:247`), the
  analytics catalogs (`:269`), and one `DESCRIPTIONS` entry per object (`:326`).
- `packages/atif-duck/src/atif_duck/domain/examples.py` (251 LOC) — `build_examples` derives one
  runnable query per catalog object from `ARG_EXEMPLARS` rather than hardcoding strings
  (`packages/atif-duck/src/atif_duck/domain/examples.py:178`).
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py` (83 LOC) — the read-side provider and
  dimension guard, a deliberate twin of atif-embed's copy because the independence contract forbids
  sharing it (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5`).
- `packages/atif-duck/src/atif_duck/__init__.py` (40 LOC) — re-exports the catalog constants and the
  four `register*` entry points (`packages/atif-duck/src/atif_duck/__init__.py:30`).
- `packages/atif-duck/src/atif_duck/domain/__init__.py` (32 LOC) — the pure catalog layer, no duckdb
  import (`packages/atif-duck/src/atif_duck/domain/__init__.py:5`).
- `packages/atif-duck/src/atif_duck/infrastructure/__init__.py` (17 LOC) — the registry layer's
  re-export surface (`packages/atif-duck/src/atif_duck/infrastructure/__init__.py:5`).

## atif-cli

The composition root: it declares five siblings as `==`-pinned dependencies
(`packages/atif-cli/pyproject.toml:32`) and wires every cross-package seam — the `ConverterPort`
adapter, the clock, version pins, the DuckDB connection (`packages/atif-cli/src/atif_cli/app.py:5`).
Ten commands hang off one cyclopts `App`: nine `@app.command` functions from `convert`
(`packages/atif-cli/src/atif_cli/app.py:226`) to `schema` (`:1055`), plus the `cron` sub-app
registered at `:65`, with `main` (`:1093`) exposed as the single console script named `atif-sql`
(`packages/atif-cli/pyproject.toml:42`). Heavy imports — duckdb, harbor through atif-converter,
pydantic through atif-corpus — are deferred into the command bodies that use them so `schema`,
`--help`, and `--version` stay on a lean import graph that a fresh-interpreter test pins
(`packages/atif-cli/src/atif_cli/app.py:22`). Failures resolve to stable exit codes, 64 for parse, 65
for catalog, 70 for runtime, split between a pure taxonomy module
(`packages/atif-cli/src/atif_cli/errors.py:25`) and a driver-dependent classifier
(`packages/atif-cli/src/atif_cli/duck_errors.py:34`) so the lean path never imports duckdb.

- `packages/atif-cli/src/atif_cli/app.py` (1120 LOC) — the ten commands and every wiring decision
  between them (`packages/atif-cli/src/atif_cli/app.py:52`).
- `packages/atif-cli/src/atif_cli/cron.py` (302 LOC) — `cron install` prints a crontab block and never
  writes one; `cron status` reports per-lane lock and last-run state from injected probes
  (`packages/atif-cli/src/atif_cli/cron.py:7`).
- `packages/atif-cli/src/atif_cli/output.py` (293 LOC) — `--format auto` resolves to a table on a TTY
  and JSON on a pipe (`packages/atif-cli/src/atif_cli/output.py:74`); `emit_cursor` streams
  `fetchmany` batches so the client holds one batch (`:154`).
- `packages/atif-cli/src/atif_cli/duck_errors.py` (96 LOC) — the duckdb exception classifier, plus a
  widened caught set for the registration path since `EmbeddingProviderMismatch` is not a
  `duckdb.Error` (`packages/atif-cli/src/atif_cli/duck_errors.py:31`).
- `packages/atif-cli/src/atif_cli/converter_adapter.py` (72 LOC) — `RealConverter` adapts
  atif-converter's use case to atif-corpus's port, the one place allowed to import both
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:44`).
- `packages/atif-cli/src/atif_cli/errors.py` (62 LOC) — `EXIT_CODES` and the `ClassifiedError` shape,
  pure and free of any `atif_*` import (`packages/atif-cli/src/atif_cli/errors.py:52`).
- `packages/atif-cli/src/atif_cli/__main__.py` (8 LOC) — `python -m atif_cli` reaching the same `main`
  (`packages/atif-cli/src/atif_cli/__main__.py:5`).
- `packages/atif-cli/src/atif_cli/__init__.py` (3 LOC) — package docstring only
  (`packages/atif-cli/src/atif_cli/__init__.py:3`).

## atif-embed

The vector-search write path: Cohere Embed v4 on Bedrock behind `EmbeddingProvider`, a local LanceDB
store behind `VectorStorePort`, and a corpus reader behind `TextRowsPort`
(`packages/atif-embed/src/atif_embed/domain/ports.py:31`). `run_backfill` anti-joins step text against
the store's uuid-to-text-hash map, embeds the misses, and bounds loss three ways — chunked discovery,
mid-run checkpoints, and per-batch isolation inside a chunk
(`packages/atif-embed/src/atif_embed/application/embed.py:61`). Two stamps keep the vector space
honest: `model_id` and `dimension` on every row, checked on both the write and the read path so a
provider switch fails loud instead of corrupting kNN
(`packages/atif-embed/src/atif_embed/domain/embedding_guard.py:3`), and `text_hash` of the exact text
a row was built from, so a re-conversion under a stable uuid reads as stale
(`packages/atif-embed/src/atif_embed/domain/text_stamp.py:3`). Additive schema columns migrate online
through a metadata-only `add_columns`, keyed on a `SCHEMA_VERSION` sidecar file
(`packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:61`).

- `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py` (444 LOC) — connect, open or
  create, online-migrate, delete by predicate, append, index, and compact
  (`packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:195`).
- `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py` (432 LOC) — the `invoke_model`
  adapter under a tenacity retry with botocore retries off, and the document-`int8` / query-`float`
  asymmetry (`packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:284`).
- `packages/atif-embed/src/atif_embed/application/embed.py` (277 LOC) — `discover_unembedded`
  (`packages/atif-embed/src/atif_embed/application/embed.py:43`), `run_backfill` (`:61`), and the sync
  `embed_query` the search command calls (`:265`).
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py` (250 LOC) — reads the
  contract corpus layout with its own DuckDB connection, since importing atif-duck is forbidden
  (`packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:155`).
- `packages/atif-embed/src/atif_embed/domain/ports.py` (119 LOC) — the three Protocols:
  `EmbeddingProvider` (`packages/atif-embed/src/atif_embed/domain/ports.py:31`), `VectorStorePort`
  (`:59`), `TextRowsPort` (`:87`).
- `packages/atif-embed/src/atif_embed/domain/embedding_guard.py` (83 LOC) — `ensure_store_matches`,
  the fail-loud provider and dimension rule
  (`packages/atif-embed/src/atif_embed/domain/embedding_guard.py:46`).
- `packages/atif-embed/src/atif_embed/domain/errors.py` (81 LOC) — the `DomainError` taxonomy the CLI
  catches, with `terminal` separating operator-needed states from retryable ones
  (`packages/atif-embed/src/atif_embed/domain/errors.py:14`).
- `packages/atif-embed/src/atif_embed/domain/text_stamp.py` (71 LOC) — `text_hash`
  (`packages/atif-embed/src/atif_embed/domain/text_stamp.py:38`) and the `MAX_EMBEDDABLE_CHARS`
  head-only cap (`:35`).

## atif-corpus

One materialization pass is sweep, scan, plan, convert, write, advance watermark, and `materialize` is
that pass (`packages/atif-corpus/src/atif_corpus/application/materialize.py:476`). The decision half
is pure — the domain never stats a file and never reads a clock, so `build_plan` partitions scanned
sessions into to-materialize, up-to-date, and skipped-live deterministically from mtimes in epoch
nanoseconds plus an injected now (`packages/atif-corpus/src/atif_corpus/domain/sessions.py:159`). The
write half is atomic by construction: artifacts land in a temp session directory that is renamed into
place, and every writer fsyncs the tmp file before the rename and the directory after, because
atif-duck reads the corpus with no locks and no journal
(`packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:5`). Conversion arrives through the
`ConverterPort` Protocol, typed to the contract's artifact shapes rather than converter internals
since this member may never import atif-converter
(`packages/atif-corpus/src/atif_corpus/domain/ports.py:41`). What counts as a transcript, and how deep
under the source root it sits, is one value object per agent — depth 1 for Claude Code's
`<project>/<session>.jsonl`, depth 3 for Codex's `<YYYY>/<MM>/<DD>` nesting
(`packages/atif-corpus/src/atif_corpus/domain/source_layout.py:119`) — so the scanner walks either
layout without branching on the agent, and `layout_for` refuses an agent that has none (`:131`).
The agent enum itself is an AST-pinned twin of the converter's, since the independence contract
forbids the import (`packages/atif-corpus/src/atif_corpus/domain/agents.py:25`).

- `packages/atif-corpus/src/atif_corpus/application/materialize.py` (634 LOC) — the pass itself
  (`packages/atif-corpus/src/atif_corpus/application/materialize.py:476`), the report value object
  (`:118`), and the public `read_watermark` its consumers call (`:160`).
- `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py` (222 LOC) — the only place this
  member stats the source corpus, keeping a genuinely absent transcript separate from a failed `stat`
  so a transient error never deletes live artifacts
  (`packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:15`).
- `packages/atif-corpus/src/atif_corpus/domain/sessions.py` (211 LOC) — `SessionSource`
  (`packages/atif-corpus/src/atif_corpus/domain/sessions.py:42`), `QuiescencePolicy` (`:71`),
  `MaterializationPlan` (`:108`), and `build_plan` (`:159`).
- `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py` (158 LOC) — tmp-sibling write,
  fsync, rename, for files (`packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:64`) and
  whole directories (`:98`).
- `packages/atif-corpus/src/atif_corpus/domain/watermark.py` (78 LOC) — `diff_source_mtimes` partitions
  two mtime maps into added, modified, and removed
  (`packages/atif-corpus/src/atif_corpus/domain/watermark.py:58`).
- `packages/atif-corpus/src/atif_corpus/domain/layout.py` (75 LOC) — `CorpusLayout` is the single
  writer-side computation of every contract path
  (`packages/atif-corpus/src/atif_corpus/domain/layout.py:26`).
- `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py` (69 LOC) — a scriptable
  `ConverterPort` fake shipped in `infrastructure` so the type checker holds it to the port on every
  run (`packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:3`).
- `packages/atif-corpus/src/atif_corpus/infrastructure/settings.py` (58 LOC) — `ATIF_SQL_`-prefixed
  settings whose default factories read env at call time, not import time
  (`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:44`).

## atif-converter

`convert_and_audit` returns a conversion result paired with a loss report — the trajectory plus an
accounting of what upstream dropped
(`packages/atif-converter/src/atif_converter/application/convert_and_audit.py:105`). The conversion is
ours: `convert_claude_code_records`, a port of harbor 0.22.0's Claude Code converter built on the
public ATIF data classes (`packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:75`),
reached through the file-reading seam at
`packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:73` and validated
with harbor's public `TrajectoryValidator`
(`packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:68`). The Codex path is
the same shape one module over: `convert_codex_and_audit`
(`packages/atif-converter/src/atif_converter/application/convert_codex.py:128`) over
`convert_codex_records` (`packages/atif-converter/src/atif_converter/domain/codex_conversion.py:781`),
one rollout in and one trajectory out by construction. harbor's own private converters are the
parity ORACLE, reached from the tests only (`packages/atif-converter/tests/harbor_oracle.py:94`, `:111`),
frozen to goldens and diffed against the live corpus; that is what lets the dependency widen to
`harbor>=0.22.0,<1` (`packages/atif-converter/pyproject.toml:26`). The
known conversion gaps are types rather than prose — `FidelityGap` enumerates them
(`packages/atif-converter/src/atif_converter/domain/fidelity.py:44`) and a pure enrichment pass repairs
three by re-running harbor's deterministic normalization order over the raw records
(`packages/atif-converter/src/atif_converter/domain/enrichment.py:198`). Codex has its own seven
(`packages/atif-converter/src/atif_converter/domain/codex_fidelity.py:64`), all values namespaced
`codex_*` so one `gaps_observed` array can carry either agent's, and its own enrichment pass, which
attributes agent steps by a re-derived `api_call_id` rather than by message text because harbor drops
empty text parts and an empty assistant message can never be placed by matching
(`packages/atif-converter/src/atif_converter/domain/codex_enrichment.py`). Each source file is read
once, fingerprinted and parsed in the same pass, and the converter and the audit consume that one
list of records; the fingerprints are re-checked once the artifacts are built, so a session that
resumes writing mid-conversion fails instead of publishing artifacts for bytes it no longer holds
(`packages/atif-converter/src/atif_converter/infrastructure/raw_records.py`, `load_session` and
`mutated_files`). Per-step cost
estimates come from `pricing.cost_per_token`
(`packages/atif-converter/src/atif_converter/domain/pricing.py:628`), which reads litellm's bundled
price table without importing litellm and reproduces `litellm.cost_per_token`'s floats exactly,
falling back to litellm for any shape it does not cover; `import litellm` used to be four of the
five seconds a large session took.

- `packages/atif-converter/src/atif_converter/domain/pricing.py` (675 LOC): `fast_cost_per_token`
  (`packages/atif-converter/src/atif_converter/domain/pricing.py:584`) prices the covered model shapes
  from litellm's own table located via `importlib.util.find_spec` (`:318`); `cost_per_token` (`:628`)
  wraps it with the litellm fallback and `has_pricing_entry` (`:666`) is Codex's table lookup.
- `packages/atif-converter/src/atif_converter/domain/enrichment.py` (412 LOC) — `enrich_trajectory`
  restores source uuids, the compact-summary flag, and the cache-creation total
  (`packages/atif-converter/src/atif_converter/domain/enrichment.py:198`).
- `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py` (195 LOC) — the only
  module importing harbor, which ships no `py.typed`, so the untyped surface stays contained
  (`packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:11`).
- `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py` (164 LOC) — the one raw
  reader the census, edges emitter, and enrichment share, with `mutated_files` as the race check
  (`packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:146`).
- `packages/atif-converter/src/atif_converter/application/convert_and_audit.py` (155 LOC) — the use
  case (`packages/atif-converter/src/atif_converter/application/convert_and_audit.py:105`) and its
  refusal on any mid-window mutation (`:93`).
- `packages/atif-converter/src/atif_converter/domain/fidelity.py` (137 LOC) — `RecordType` with 12
  members (`packages/atif-converter/src/atif_converter/domain/fidelity.py:18`), `FidelityGap` with 7
  (`:44`), and the `LossReport` value object (`:83`).
- `packages/atif-converter/src/atif_converter/domain/edges.py` (101 LOC) — one `edges.jsonl` line per
  raw record, key order fixed by the contract
  (`packages/atif-converter/src/atif_converter/domain/edges.py:22`).
- `packages/atif-converter/src/atif_converter/infrastructure/census.py` (89 LOC) — the raw-side counts
  that form the input half of a loss report, derived from an already-parsed snapshot
  (`packages/atif-converter/src/atif_converter/infrastructure/census.py:57`).
- `packages/atif-converter/src/atif_converter/domain/errors.py` (70 LOC) — six typed exceptions under
  one `DomainError` base, with exit-code mapping left to the CLI
  (`packages/atif-converter/src/atif_converter/domain/errors.py:14`).

## atif-models

A deliberately narrow seam: a system prompt, a user prompt, and a pydantic schema in; a validated
instance of that schema out (`packages/atif-models/src/atif_models/domain/ports.py:116`). Its only
in-repo consumer is atif-analytics — the single edge the `forbidden` contract leaves open
(`pyproject.toml:520`) — and the registry is the only place in the workspace where a Bedrock model id
is written down, so a pipeline names a family and a size alias and lets `resolve` pick the id
(`packages/atif-models/src/atif_models/domain/registry.py:6`, `:109`). The default adapter posts an
OpenAI chat-completions body to `invoke_model` in strict `json_schema` mode, dispatching the blocking
call through `anyio.to_thread` under a capacity limiter with tenacity owning the retry loop and
botocore's own retries disabled
(`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:142`). Strict mode demands
`additionalProperties: false` at every object level and every property listed in `required`, which one
pure transform enforces before the schema reaches the wire
(`packages/atif-models/src/atif_models/domain/schema.py:40`).

- `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py` (314 LOC) —
  `OpenAiBedrockProvider`
  (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:142`), the retryable-code
  set (`:63`), and usage extraction (`:124`).
- `packages/atif-models/src/atif_models/domain/registry.py` (151 LOC) — `ModelSpec`
  (`packages/atif-models/src/atif_models/domain/registry.py:39`), `resolve` (`:109`), and
  `estimate_cost` over per-1M-token rates (`:125`).
- `packages/atif-models/src/atif_models/domain/ports.py` (143 LOC) — the `LlmStructuredProvider`
  Protocol (`packages/atif-models/src/atif_models/domain/ports.py:116`), the
  terminal-versus-retryable error split (`:43`), and lock-guarded usage accumulation (`:78`).
- `packages/atif-models/src/atif_models/domain/schema.py` (109 LOC) — `to_openai_strict` keeps `$defs`
  and `$ref`, drops `default` and `title`
  (`packages/atif-models/src/atif_models/domain/schema.py:40`).
- `packages/atif-models/src/atif_models/infrastructure/settings.py` (84 LOC) — `LlmSettings` carries
  the family, region, concurrency, and per-pipeline size overrides
  (`packages/atif-models/src/atif_models/infrastructure/settings.py:28`).
- `packages/atif-models/src/atif_models/__init__.py` (14 LOC) — states the ownership rule: no other
  member hardcodes a model id (`packages/atif-models/src/atif_models/__init__.py:9`).
- `packages/atif-models/src/atif_models/domain/__init__.py` (9 LOC) — the pure layer: no boto3, no
  env, no clock (`packages/atif-models/src/atif_models/domain/__init__.py:7`).
- `packages/atif-models/src/atif_models/infrastructure/__init__.py` (8 LOC) — adapters and env
  settings, importing downward into `domain` only
  (`packages/atif-models/src/atif_models/infrastructure/__init__.py:7`).

## See also

- [processes](../behavior/processes.md) — 42 shared source citations
- [business logic](../insights/business-logic.md) — 39 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 38 shared source citations
- [contract map](../insights/contract-map.md) — 37 shared source citations
- [debugging guide](../insights/debugging-guide.md) — 24 shared source citations
