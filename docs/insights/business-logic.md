# atif-sql · Business logic

This file indexes the domain rules `atif-sql` enforces: input validations, invariants the
code holds across a boundary, derived-value calculations, and the policy gates that decide
whether work runs at all.

**Scope.** Application-layer and domain-layer rules across the seven workspace members,
plus the SQL surface `atif-duck` registers into DuckDB. There is no database server, no
migration directory, and no HTTP surface in this repo, so there are no DDL constraints or
request-validation middlewares to survey — the DuckDB views and macros are the closest
thing to a "schema", and the rules encoded in their DDL are in scope and captured under
Calculations and Invariants. LLM-output schemas count as validations here, because the
provider adapter re-validates every response with pydantic before it reaches a parquet
row. Ruff/pyright/import-linter rules are toolchain policy, not domain logic, and are out
of scope.

**Test provenance.** Where a rule is pinned by a test, the test is cited beside the
implementation. A row with no test citation is a rule read out of the implementation and
not covered by a named test — that difference is stated, never smoothed over.

**Units.** Every `*_ns` value is epoch nanoseconds from `os.stat().st_mtime_ns`; every
`*_chars` value counts Python string characters, not bytes or tokens; pricing is USD per
1,000,000 tokens; backoff is minutes and tenacity waits are seconds. Scope (per session,
per run, per pipeline) is stated per row.

## Validations

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| A session path must end in `.jsonl` AND be an existing file | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:50` | raise `InvalidSessionInput`; CLI exits 64 |
| Production code imports harbor's PUBLIC surface only (the ATIF data classes and the validator) | Conversion | `packages/atif-converter/tests/test_harbor_public_surface_guard.py:29`; the conversion itself is ours at `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:75` and `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:781` | the `ast` guard fails the suite on any other `harbor.*` import |
| The converter must return a trajectory for the session | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:83` | raise `EmptySessionError`; CLI exits 2 |
| The trajectory must pass harbor's `TrajectoryValidator` AFTER enrichment mutates `extra` | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:68`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:107` | `validation_errors` is non-empty; the materialize adapter raises `TrajectoryValidationError` |
| No source file may change, vanish, or appear between the pre-read snapshot and the end of the raw parse | Conversion | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:93-102`, checked twice at `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:135` and `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:148`; tests `packages/atif-converter/tests/test_snapshot_and_drift.py:193`, `packages/atif-converter/tests/test_snapshot_and_drift.py:217`, `packages/atif-converter/tests/test_snapshot_and_drift.py:274` | raise `SourceMutatedDuringConversion`; the materialize pass records the session as failed and continues |
| A JSONL line that is not valid JSON, or parses to a non-dict, is not a record | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:136-142` | silent drop, DEBUG log (same policy as harbor) |
| An agent step and an assistant-record group pair up only when their tool ids intersect, or when BOTH sides carry no tool ids | Conversion | `packages/atif-converter/src/atif_converter/domain/enrichment.py:304-318`; test `packages/atif-converter/tests/test_enrichment.py:331` | refuse: attribution stops at that step, `enrichment_truncated_at_step` recorded, WARNING logged |
| A user step and a user text record pair up only while the record's harbor-derived text equals the step's message | Conversion | `packages/atif-converter/src/atif_converter/domain/enrichment.py:347-360`; test `packages/atif-converter/tests/test_enrichment.py:372` | refuse: same truncation marker, WARNING logged |
| A session materializes only once its newest source mtime is at least `quiesce_seconds` old (default 300 seconds of source silence, per session) | Corpus | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:83-104` and `packages/atif-corpus/src/atif_corpus/domain/sessions.py:201-202`; tests `packages/atif-corpus/tests/test_domain.py:40` (inclusive boundary) and `packages/atif-corpus/tests/test_domain.py:98` | deferred into `skipped_live`; revisited next pass |
| A source mtime in the FUTURE never satisfies the quiescence threshold | Corpus | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:96-104`; test `packages/atif-corpus/tests/test_domain.py:45` | stays skipped, plus a WARNING every pass so the starvation is visible |
| `watermark.json` must parse as a mapping of path to int | Corpus | `packages/atif-corpus/src/atif_corpus/application/materialize.py:160-176` | degrade to empty (one full re-materialization pass), WARNING logged — never a refusal to sync |
| A scan finding zero sessions while the corpus holds materialized ones is not a deletion | Corpus | `packages/atif-corpus/src/atif_corpus/application/materialize.py:97-104` and `packages/atif-corpus/src/atif_corpus/application/materialize.py:553-559`; test `packages/atif-corpus/tests/test_materialize.py:292` | raise `SuspiciousEmptyScanError`; nothing is removed |
| Only a genuine `FileNotFoundError` proves a source vanished; any other `OSError` means unreadable | Corpus | `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:83-96` and `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:176-186`; test `packages/atif-corpus/tests/test_materialize.py:819` | session lands in `SourceScan.unreadable`, is never ghosted, and its watermark entries are retained |
| `structural_only` and `llm_only` are mutually exclusive | Analytics | `packages/atif-analytics/src/atif_analytics/application/analyze.py:61-63` | raise `ValueError` |
| A retry-queue or checkpoint `pipeline` must be one of the five contract names | Analytics | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:96-98`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:35-41`; tests `packages/atif-analytics/tests/test_state.py:145` and `packages/atif-analytics/tests/test_state.py:150` | raise `ValueError` |
| Every LLM-output `confidence` is bounded to the closed interval 0.0 to 1.0 (dimensionless, per emitted row) | Analytics | `packages/atif-analytics/src/atif_analytics/domain/models.py:82-85`, `packages/atif-analytics/src/atif_analytics/domain/models.py:171-174`, `packages/atif-analytics/src/atif_analytics/domain/models.py:275-278`, `packages/atif-analytics/src/atif_analytics/domain/models.py:367-370`, `packages/atif-analytics/src/atif_analytics/domain/models.py:471-474` | pydantic `ValidationError` at `model_validate`, translated to `ProviderUnavailable` (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:307-311`); the unit goes to the retry queue |
| Every categorical LLM-output field is a closed `Literal` union, and free text is length-capped (goal 280 chars, rationale 200 chars, evidence 280 chars, summary 280 chars, a turn uuid 64 chars) | Analytics | `packages/atif-analytics/src/atif_analytics/domain/models.py:31`, `packages/atif-analytics/src/atif_analytics/domain/models.py:72-75`, `packages/atif-analytics/src/atif_analytics/domain/models.py:357-360`, `packages/atif-analytics/src/atif_analytics/domain/models.py:393-396`, `packages/atif-analytics/src/atif_analytics/domain/models.py:449-452`, `packages/atif-analytics/src/atif_analytics/domain/models.py:460-463` | same pydantic rejection path |
| A returned `turn_uuid` must be in the session's USER-role main-chain text-step header uuid set | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:123-134` and `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:303-312`; tests `packages/atif-analytics/tests/test_perceived.py:161`, `packages/atif-analytics/tests/test_resource_guards.py:617` | row dropped with a WARNING; an EMPTY universe drops every row (fails closed) |
| A returned conflict pair's two uuids must BOTH be real `edges.jsonl` uuids for that session | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:266-296`; test `packages/atif-analytics/tests/test_resource_guards.py:590` | pair dropped; an unreadable universe drops every pair (fails closed) |
| An untrusted step body may not present itself as a turn header | Analytics | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:64-73`; tests `packages/atif-analytics/tests/test_resource_guards.py:561` and `packages/atif-analytics/tests/test_resource_guards.py:576` | coerce: `[uuid=` is rewritten to `(uuid=` case-insensitively, length preserved, before any header is built |
| A friction candidate is a user-role main-chain text step with a uuid, 1 to `friction_max_chars` characters (default 300, per message), CLI bookkeeping text excluded | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:154-175`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:81-86`; test `packages/atif-analytics/tests/test_friction_tiers.py:98` | filtered out at the candidate boundary; no LLM call is ever made for it |
| `ATIF_SQL_LLM_FAMILY` must name a family with a wired provider adapter | Models | `packages/atif-models/src/atif_models/infrastructure/settings.py:25` and `packages/atif-models/src/atif_models/infrastructure/settings.py:41-54`; tests `packages/atif-models/tests/test_settings.py:59` and `packages/atif-models/tests/test_settings.py:64` | raise `ValueError` at settings load — a startup refusal instead of a Bedrock 400 on every call |
| `size_for` accepts only a pipeline with an `llm_size_<pipeline>` field | Models | `packages/atif-models/src/atif_models/infrastructure/settings.py:67-77`; test `packages/atif-models/tests/test_settings.py:88` | raise `KeyError` |
| A schema with a schema-valued `additionalProperties` (a `dict[str, X]` field) cannot be expressed in OpenAI strict mode | Models | `packages/atif-models/src/atif_models/domain/schema.py:63-69`; test `packages/atif-models/tests/test_schema.py:71` | raise `ValueError` at body-build time |
| `finish_reason` of `content_filter` or `refusal`, or a non-empty `message.refusal`, is a decline | Models | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:77`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:284-290`; tests `packages/atif-models/tests/test_openai_provider.py:182` and `packages/atif-models/tests/test_openai_provider.py:190` | raise `RefusalError` — terminal, never retried |
| `finish_reason` of `length` gets exactly ONE retry, at `reasoning_effort` degraded one step | Models | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:85-90`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:217-249`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:291-297`; tests `packages/atif-models/tests/test_openai_provider.py:129`, `packages/atif-models/tests/test_openai_provider.py:144`, `packages/atif-models/tests/test_openai_provider.py:163` | raise `ProviderUnavailable` when still truncated or already at the floor; the unit goes to the retry queue |
| Response content must be a non-empty string that parses as JSON and validates against the schema | Models | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:298-311`; tests `packages/atif-models/tests/test_openai_provider.py:198`, `packages/atif-models/tests/test_openai_provider.py:206`, `packages/atif-models/tests/test_openai_provider.py:212` | raise `ProviderUnavailable` |
| A store's stamped `(model, dim)` must match the active embedder before any vector is read or appended | Embeddings | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38-80`, `packages/atif-embed/src/atif_embed/application/embed.py:166-174`; tests `packages/atif-embed/tests/test_embed_use_case.py:153`, `packages/atif-embed/tests/test_lance_store.py:321` and `packages/atif-embed/tests/test_lance_store.py:330` | raise `EmbeddingProviderMismatch` (`terminal = True`); `query`/`search` exit 65, `embed` exits 78 |
| `ensure_index` accepts only `cosine`, `l2`, or `dot` | Embeddings | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414-419`; test `packages/atif-embed/tests/test_lance_store.py:110` | raise `ValueError` |
| A text over 50,000 characters (per embeddable text) cannot be embedded whole | Embeddings | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:35`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:43-47`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:65-68`; tests `packages/atif-embed/tests/test_embed_use_case.py:369` and `packages/atif-embed/tests/test_embed_use_case.py:386` | coerce: sent head-only, and the row stamps `truncated = true` so the search miss is attributable |
| Every macro parameter name must have an `ARG_EXEMPLARS` entry | SQL surface | `packages/atif-duck/src/atif_duck/domain/examples.py:58-75`, `packages/atif-duck/src/atif_duck/domain/examples.py:148-158`; test `packages/atif-duck/tests/test_examples.py:188` | `build_examples()` raises; the drift test fails with it |
| Every catalog object must yield a runnable example or carry a documented `EXCLUSIONS` entry | SQL surface | `packages/atif-duck/src/atif_duck/domain/examples.py:94-99`; test `packages/atif-duck/tests/test_examples.py:160` | drift test fails |
| A materialized session dir with no `meta.json` is incomplete | SQL surface | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:160-177`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:188-192`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:213-221`; test `packages/atif-duck/tests/test_duck_views.py:630` | excluded from every view, plus one WARNING per skipped dir so the exclusion is observable |
| A real `embed` run needs an explicit scope: `--limit N` or `--all` | CLI | `packages/atif-cli/src/atif_cli/app.py:810-820` | exit 64 with the hint naming both flags and `--dry-run` |
| `search` against an empty embeddings store is not a zero-result query | CLI | `packages/atif-cli/src/atif_cli/app.py:930-941` | exit 2 with the hint `atif-sql embed --all --no-dry-run` |

Notes on two rows above. The strict-JSON transform in
`packages/atif-models/src/atif_models/domain/schema.py:6-11` is a *rewriting* validation
rather than a rejecting one: it forces `additionalProperties: false` on every object level
and moves every property into `required`, expressing optionality by making the field's
type nullable. Only the open-mapping shape has no representation and raises. Second: the
provider's structured-output contract and pydantic are two gates in series — the wire
schema constrains shape, and `model_validate` re-applies the `ge`/`le` bounds the model
may have ignored (`packages/atif-models/src/atif_models/domain/schema.py:18-21`).

## Invariants

### Conversion

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The seven `FidelityGap` members are the complete, typed statement of what harbor 0.22.0's conversion loses; four are structural and observed on every session, three are session-conditional | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:44-79`; `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:52-59` (structural set) and `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:70-76` (conditional set); test `packages/atif-converter/tests/test_convert_and_audit.py:163` |
| Only `RecordType.USER` and `RecordType.ASSISTANT` are convertible; every other record type is dropped upstream | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:41`; test `packages/atif-converter/tests/test_convert_and_audit.py:121` |
| `LossReport.records_total` equals the `edges.jsonl` line count for any session, because the census and the edges emitter read one snapshot | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:91-98`, `packages/atif-converter/src/atif_converter/infrastructure/census.py:57-67`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:137-139`; test `packages/atif-converter/tests/test_snapshot_and_drift.py:122` |
| `records_converted` counts raw user/assistant RECORDS, not ATIF steps — harbor bundles several assistant events sharing one `message.id` into one agent step, so the two numbers differ by design | Application code | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:122-126`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:62-68` |
| `subagent_files_convertible` equals `subagent_files_found`: the staging layer flattens workflow-nested side-files into harbor's visible directory, so `WORKFLOW_SUBAGENTS_MISSED` names the UPSTREAM gap rather than a local loss | Application code | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:83-88`, `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:71-92`; tests `packages/atif-converter/tests/test_convert_and_audit.py:127` and `packages/atif-converter/tests/test_convert_and_audit.py:136` |
| A truncation marker or a non-zero leftover count means enrichment attribution was REFUSED, never guessed | Application code | `packages/atif-converter/src/atif_converter/domain/enrichment.py:45-62`, `packages/atif-converter/src/atif_converter/domain/enrichment.py:379-399`; tests `packages/atif-converter/tests/test_enrichment.py:272`, `packages/atif-converter/tests/test_enrichment.py:287`, `packages/atif-converter/tests/test_enrichment.py:302` |
| Staged side-files are per-FILE symlinks under real directories, flattened with `__` joins, because Python 3.13's `rglob` does not descend a symlinked directory and a flat namespace can collide | Application code | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:71-92` |
| `edges.jsonl` line order is `(ts, uuid)` with missing values sorting first as empty strings, and the key order is contract-fixed | Application code | `packages/atif-converter/src/atif_converter/domain/edges.py:22-32`, `packages/atif-converter/src/atif_converter/domain/edges.py:82-90`; tests `packages/atif-converter/tests/test_edges.py:43` and `packages/atif-converter/tests/test_edges.py:47` |
| A renamed harbor private method and a bad transcript must not look alike in a materialization log | Application code | `packages/atif-converter/src/atif_converter/domain/errors.py:47-54`; test `packages/atif-converter/tests/test_snapshot_and_drift.py:331` |

### Corpus

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| A reader never observes a torn artifact SET: all four artifacts are written into `<corpus_root>/.staging/` and the whole directory is swapped in by rename | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:98-137`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:190-244`; tests `packages/atif-corpus/tests/test_atomic.py:85`, `packages/atif-corpus/tests/test_materialize.py:346` |
| `meta.json` is written LAST inside the staging directory, and it is the marker `atif-duck` gates every reader on | Application code, both sides | `packages/atif-corpus/src/atif_corpus/application/materialize.py:207-212`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:188-192`; test `packages/atif-duck/tests/test_duck_views.py:630` |
| Persistence order matches write order: each artifact is fsynced before the rename that publishes it, and the parent directory after | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:64-95`, `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:140-158`; tests `packages/atif-corpus/tests/test_atomic.py:131`, `packages/atif-corpus/tests/test_atomic.py:149`, `packages/atif-corpus/tests/test_atomic.py:194` |
| A kill inside the swap window leaves the session dir MISSING rather than torn, and the next pass force-replans it | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:113-123`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:313-339`; tests `packages/atif-corpus/tests/test_materialize.py:791`, `packages/atif-corpus/tests/test_domain.py:216` |
| Only sessions that materialized successfully advance their watermark entries, so a failed session stays stale and is retried | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:428-473`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:615-622`; test `packages/atif-corpus/tests/test_materialize.py:333` |
| A VANISHED source path keeps its watermark entry unless the owning session succeeded this pass or left the scan entirely — the retained entry IS the staleness signal | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:455-473`; tests `packages/atif-corpus/tests/test_materialize.py:191` and `packages/atif-corpus/tests/test_materialize.py:227` |
| `owns_path` is the single definition of a session's watermark scope, so staleness and retention can never disagree about which entries belong to a session | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:129-140`; tests `packages/atif-corpus/tests/test_domain.py:191`, `packages/atif-corpus/tests/test_domain.py:202`, `packages/atif-corpus/tests/test_domain.py:207` |
| Only an exactly-equal `mtime_ns` counts as unchanged; an mtime that moved BACKWARDS is `modified` | Application code | `packages/atif-corpus/src/atif_corpus/domain/watermark.py:61-76`; test `packages/atif-corpus/tests/test_domain.py:86` |
| A staging entry is removed only when EVERY pid its name carries is proven gone; only `ProcessLookupError` proves that | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:256-310`; tests `packages/atif-corpus/tests/test_materialize.py:429`, `packages/atif-corpus/tests/test_materialize.py:457`, `packages/atif-corpus/tests/test_materialize.py:481` |
| One broken transcript never aborts a corpus sync — the port may raise anything, and the pass records the failure and continues | Application code | `packages/atif-corpus/src/atif_corpus/domain/ports.py:41-47`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:597-616`; test `packages/atif-corpus/tests/test_materialize.py:316` |
| The domain reads no clock: `now_ns` is always passed in, so identical inputs always yield an identical plan | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:18-21`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:48-52`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:528`; test `packages/atif-corpus/tests/test_domain.py:145` |
| All three plan partitions are sorted by session id, so the same scan yields the same work order, log output, and failure ordering | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:110-121`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:195`; test `packages/atif-corpus/tests/test_domain.py:145` |
| The corpus root `~/.claude` maps to the reserved slug `default`; every other root is `<sanitized-dirname>-<8 hex of sha256 of the resolved path>` | Application code | `packages/atif-corpus/src/atif_corpus/domain/slug.py:23`, `packages/atif-corpus/src/atif_corpus/domain/slug.py:45-50`; tests `packages/atif-corpus/tests/test_slug.py:19`, `packages/atif-corpus/tests/test_slug.py:24`, `packages/atif-corpus/tests/test_slug.py:32` |
| The per-session artifact filenames and the watermark filename are fixed by the contract | Application code | `packages/atif-corpus/src/atif_corpus/domain/layout.py:17-22`; test `packages/atif-corpus/tests/test_domain.py:245` |

### Analytics

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The retry queue is the SINGLE re-admission gate for a failed unit: a live entry blocks dispatch until `drain` says it is due, because a failed unit is never checkpointed and would otherwise be re-admitted fresh every run forever | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:176-193`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:167-173`; tests `packages/atif-analytics/tests/test_state.py:124`, `packages/atif-analytics/tests/test_perceived.py:231` |
| A FAILED unit is not checkpointed; a REFUSED unit is (terminal), and its refusal is durable — an in-band sentinel row for session-keyed `classify`, a `analytics/refusals` sidecar row for the uuid-keyed pipelines | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:222-246`, `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:191-229`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:277-289`; test `packages/atif-analytics/tests/test_perceived.py:252` |
| Nothing is stamped — no checkpoint, no cache row, no retry entry — for a unit that was never dispatched | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:155-183`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:248-266`; test `packages/atif-analytics/tests/test_perceived.py:280` |
| Budget overshoot is bounded by `BUDGET_CHECK_BATCH` units (8, per dispatch batch) on the priciest watched model, and does not scale with the session ceiling or the write chunk size | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:100-112`, `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:146-152`; test `packages/atif-analytics/tests/test_resource_guards.py:362` (over both dispatch shapes) |
| A checkpoint skip requires BOTH `last_ts` and `last_mtime` to be non-advancing; either bound moving forward re-admits the session | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:136-176`; tests `packages/atif-analytics/tests/test_state.py:48` and `packages/atif-analytics/tests/test_state.py:57` |
| `state.db` is corpus-scoped at `<corpus_root>/analytics/state.db`, so one corpus's completions can never skip another's re-scoring | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:22-24` |
| Sidechain steps are excluded from every rendered transcript, because the prompts are calibrated on transcripts with no subagent content and harbor inlines sidechains | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:24-27`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:132-134`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:162`; test `packages/atif-analytics/tests/test_transcript.py:50` |
| A compact-summary step is synthetic text: it never participates in a trajectory window and never counts toward a human-AI pair | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:199-217`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:253` |
| Only `render_session_text` writes a real `[uuid=` header; every body is escaped first | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:33-38`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:160-161`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:173-183`; test `packages/atif-analytics/tests/test_resource_guards.py:561` |
| `CLI_BOOKKEEPING_TEXTS` is defined once in the domain and shared by the friction candidate filter and the perceived-eligibility pair counter | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:78-86`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:94-100`; test `packages/atif-analytics/tests/test_perceived.py:80` |
| The `source` column vocabulary `regex` / `sql` / `llm` / `refused` names the row's PROVENANCE TIER, not the engine that computed it, and downstream views bind to those literals | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:29-33`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:86-89` |
| Two friction stamp rules matching one uuid resolve by higher confidence, ties keeping the first stamp, with rules applied in the fixed order 1 to 3 | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:183-195` |
| Structural hyperparameter defaults are pinned by contract and `seed = 42` threads into every UMAP and Leiden call, so a same-seed rerun reproduces the clustering | Application code | `packages/atif-analytics/src/atif_analytics/domain/config.py:10-12`, `packages/atif-analytics/src/atif_analytics/domain/config.py:20-40`, `packages/atif-analytics/src/atif_analytics/domain/config.py:43-60` |
| A session's community id is `-1` when it is a singleton or unclusterable — an out-of-band sentinel that survives `Int32` serialization | Application code | `packages/atif-analytics/src/atif_analytics/domain/structure/community.py:84-86` |
| A community-detection resolution `γ` must be strictly positive: CPM at `γ = 0` has no null term and collapses every graph to one community | Application code | `packages/atif-analytics/src/atif_analytics/domain/structure/community.py:100-102` |

### Models and embeddings

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The registry is the ONLY place in the workspace where a model id is written down, and it is total over family times size (6 entries) | Application code | `packages/atif-models/src/atif_models/domain/registry.py:5-8`, `packages/atif-models/src/atif_models/domain/registry.py:60-106`; test `packages/atif-models/tests/test_registry.py:61` |
| `estimate_cost` returns `None`, never `0.0`, when either rate is unknown — callers must render "pricing unavailable" | Application code | `packages/atif-models/src/atif_models/domain/registry.py:125-137`; tests `packages/atif-models/tests/test_registry.py:85` and `packages/atif-models/tests/test_registry.py:95` |
| Usage is accumulated BEFORE any `finish_reason` gate, because a length-truncated call still billed tokens | Application code | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:269-277`; test `packages/atif-models/tests/test_openai_provider.py:249` |
| `RETRY_CODES` (8 Bedrock codes) is a deliberate TWIN of atif-embed's set, pinned against the same literal in each package's own suite because the two may not import each other | Application code | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:59-74`; tests `packages/atif-models/tests/test_openai_provider.py:318` and `packages/atif-models/tests/test_openai_provider.py:322` |
| Every embeddings row stamps `model`, `dim`, `text_hash`, and `truncated`, so a re-conversion that changes a step's text is detectable under the same uuid | Application code | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:6-18`, `packages/atif-embed/src/atif_embed/application/embed.py:216-235`; test `packages/atif-embed/tests/test_embed_use_case.py:89` |
| A uuid never fans out to two vectors: a stale row under the same uuid is DELETED before the new vector is appended | Application code | `packages/atif-embed/src/atif_embed/application/embed.py:203-210`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:50-63`; tests `packages/atif-embed/tests/test_embed_use_case.py:211`, `packages/atif-embed/tests/test_lance_store.py:129` |
| `_PRE_STAMP_SENTINEL` (`<pre-stamp>`) can never equal a real blake2b hex digest, so every pre-stamp row mismatches its corpus hash and re-embeds through the ordinary staleness path | Application code | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:69-73`; test `packages/atif-embed/tests/test_lance_store.py:192` |
| ADDITIVE schema drift migrates ONLINE (metadata-only `add_columns`, no re-embedding, search stays up); a BREAKING change — provider or dimension switch — stays fail-loud | Application code | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:16-32`, `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166-192`; tests `packages/atif-embed/tests/test_embed_use_case.py:293` and `packages/atif-embed/tests/test_embed_use_case.py:314` |
| The write chunk is a multiple of `batch_size`, so a checkpoint boundary never splits a Bedrock batch | Application code | `packages/atif-embed/src/atif_embed/application/embed.py:145-147` |
| The provider/dimension guard exists as two copies (atif-embed and atif-duck) because the independence contract forbids the import; a behavioral change in either must be ported | Application code | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:15-18`, `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:46`; tests `packages/atif-embed/tests/test_guard_twin_pin.py:76`, `packages/atif-embed/tests/test_guard_twin_pin.py:79`, `packages/atif-embed/tests/test_guard_twin_pin.py:85` |
| The recovery hint names the real store locations (`ATIF_SQL_LANCE_URI` or `<corpus-root>/embeddings_lance`), never a fixed home-directory path | Application code | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:25-35`; tests `packages/atif-embed/tests/test_guard_twin_pin.py:97` and `packages/atif-embed/tests/test_guard_twin_pin.py:103` |

### SQL surface and CLI

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| Registration order is raw TEMP tables, then views, then VSS, then macros, then the v2 analytics views and macros — each layer binds against the previous at CREATE time | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1228-1234`; test `packages/atif-duck/tests/test_duck_views.py:615` |
| `DESCRIPTIONS` covers the catalog exactly (16 views plus 9 macros plus 12 analytics views plus 13 analytics macros equals 50) | Application code | `packages/atif-duck/src/atif_duck/domain/catalog.py:326`; test `packages/atif-duck/tests/test_examples.py:178` |
| `cost_estimate`'s `est_cost_usd` covers PRICED steps only and is meaningful only when `unpriced_steps = 0`; an inner join would return a partial number indistinguishable from a complete one | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1053-1058`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1066-1077`; tests `packages/atif-duck/tests/test_duck_views.py:407` and `packages/atif-duck/tests/test_duck_views.py:482` |
| Both `cost_estimate` counters filter on `model_name IS NOT NULL`, because user steps carry no model and would otherwise make every session look like a pricing gap | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1060-1063`; test `packages/atif-duck/tests/test_duck_views.py:533` |
| An absent or unattachable Lance store degrades to an empty `message_embeddings` TABLE with the right schema, so `semantic_search` always binds | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:884-905`; tests `packages/atif-duck/tests/test_vss.py:112` and `packages/atif-duck/tests/test_vss.py:248` |
| Exit codes are a stable wire contract shared with `atif-converter`'s taxonomy on every common number (64, 65, 70, 127) | Application code | `packages/atif-cli/src/atif_cli/errors.py:5-11`, `packages/atif-cli/src/atif_cli/errors.py:23-39` |
| `terminal` versus transient decides the `embed` exit code, so an unattended lane can stop retrying instead of burning identical ticks | Application code | `packages/atif-embed/src/atif_embed/domain/errors.py:17-26`, `packages/atif-cli/src/atif_cli/app.py:833-847` |

## Calculations

| Calculation | Inputs | Output | Citation |
| --- | --- | --- | --- |
| Dry-run dollar projection for a planned batch | measured `input_tokens`, `output_tokens`, and a `(in_rate, out_rate)` pair in USD per 1M tokens | USD for the batch, flat linear, no minimums or tiers | `packages/atif-analytics/src/atif_analytics/domain/costs.py:33-47` |
| Characters to tokens | character count | token estimate `max(1, chars // 4)`, floor 1 when non-empty | `packages/atif-analytics/src/atif_analytics/domain/costs.py:23-30` |
| Accumulated-usage dollar estimate for one `ModelSpec` | `ModelSpec.pricing_in`, `pricing_out` (USD per 1M tokens), accumulated `input_tokens` and `output_tokens` | USD, or `None` when a rate is unknown | `packages/atif-models/src/atif_models/domain/registry.py:125-137`; test `packages/atif-models/tests/test_registry.py:76` |
| Running actual spend for a run | every watched provider's `UsageAccumulator` summary plus its `ModelSpec` | total USD spent this run, across all LLM pipelines | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:123-139` |
| `cost_estimate(sid)` SQL macro | a session's `steps` rows (`prompt_tokens`, `cached_tokens`, `completion_tokens`, `model_name`) LEFT JOINed against `DEFAULT_PRICING` | `(est_cost_usd, priced_steps, unpriced_steps)` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1066-1083`; test `packages/atif-duck/tests/test_duck_views.py:368` |
| Retry backoff delay | attempt counter | `min(2 ** attempts, 60)` MINUTES, per `(pipeline, unit_id)`: 2, 4, 8, 16, 32, capped at 60 | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:78-81`; test `packages/atif-analytics/tests/test_state.py:102` |
| Bedrock call retry schedule | the raised exception | up to 10 attempts, exponential wait with multiplier 2, minimum 2 SECONDS and maximum 60 SECONDS, only for `RETRY_CODES` and network errors | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:251-257`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:102-109`; test `packages/atif-models/tests/test_openai_provider.py:284` |
| Parquet write chunk size | `batch_size` (default 96 units) | `max(batch_size * 4, 256)` ROWS per part — bounds crash loss, not spend | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:242-250` |
| Quiescence age test | newest source `mtime_ns`, `now_ns`, `quiesce_seconds` | boolean: `now_ns - newest_mtime_ns >= quiesce_seconds * 1_000_000_000` | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:96-104`; test `packages/atif-corpus/tests/test_domain.py:40` |
| Source delta between two scans | two `path` to `mtime_ns` maps | added / modified / removed sorted tuples; `touched` is added then modified | `packages/atif-corpus/src/atif_corpus/domain/watermark.py:57-76`; test `packages/atif-corpus/tests/test_domain.py:76` |
| Corpus slug | a corpus root path | `default` for `~/.claude`, else `<sanitized-dirname (<= 32 chars)>-<8 hex of sha256>` | `packages/atif-corpus/src/atif_corpus/domain/slug.py:45-50`; test `packages/atif-corpus/tests/test_slug.py:24` |
| Loss accounting for one session | raw record counts by `RecordType`, side-file classification | `records_converted` (user plus assistant records), `records_dropped` (total minus converted), `gaps_observed` | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:62-90`, `packages/atif-converter/src/atif_converter/domain/fidelity.py:109-112` |
| Completed human-to-AI exchange count | a session's `StepEvent` list | integer pair count, the perceived-error eligibility input | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:236-262`; test `packages/atif-analytics/tests/test_perceived.py:63` |
| Sentiment delta for a trajectory window | `prev_sentiment`, `curr_sentiment` over the encoding negative equals -1, neutral equals 0, positive equals 1 | `curr - prev` as a float in the closed interval -2.0 to 2.0, or `None` when there is no previous turn | `packages/atif-analytics/src/atif_analytics/domain/trajectory.py:31-32`, `packages/atif-analytics/src/atif_analytics/domain/trajectory.py:130-134`, `packages/atif-analytics/src/atif_analytics/domain/trajectory.py:171-183` |
| Content stamp for an embeddable text | the exact text sent to the embedder | blake2b digest, 16 bytes hex-encoded (128 bits) | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:26-40` |
| c-TF-IDF term weights | one pseudo-document per cluster plus a frozen `TermsConfig` | `(cluster_id, term, weight, rank)` rows, ranks 1-based, non-positive weights dropped, top 10 per cluster | `packages/atif-analytics/src/atif_analytics/domain/structure/terms.py:44-74` |
| CPM partition quality | a weighted graph, a label vector, and `γ` | scalar objective in the same units as the stored `quality` column | `packages/atif-analytics/src/atif_analytics/domain/structure/community.py:288-308` |
| Resolution-profile call budget | the configured `γ` range | maximum distinct `γ` the bisection can evaluate, clamped by `_PROFILE_MAX_CALLS` (512 Leiden calls) | `packages/atif-analytics/src/atif_analytics/domain/structure/community.py:134-170` |
| `friction_rate(since_days)` | `user_friction` label counts per session; user-role main-chain non-empty `steps` as denominator | per-session `rate` plus seven label counters | `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:344-384` |
| `success_rate_by_work(since_days)` | `session_classifications` rows | `unknown_fraction` over ALL sessions; success, failure and partial rates over KNOWN outcomes only | `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:271-293` |
| `semantic_search(query_vec, k)` | a unit-norm query vector and `k` | top-`k` `(uuid, sim, distance)` ordered by cosine distance | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1145-1152`; test `packages/atif-duck/tests/test_vss.py:195` |
| `todo_velocity(sid)` | `todo_state_current` rows for one session | completed count divided by distinct subject count, `NULL` when there are no subjects | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1101-1107`; test `packages/atif-duck/tests/test_duck_views.py:566` |
| `subagent_fanout(sid)` | `subagent_spawns` rows for one session | count of Task/Agent launch INTENTS, not side-transcript files | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1116-1124`; test `packages/atif-duck/tests/test_duck_views.py:572` |

Three of these need the formula spelled out.

**`cost_estimate(sid)`.** Per step, uncharged-cache base input is
`prompt_tokens - cached_tokens`, because ATIF's `prompt_tokens` is the TOTAL (input plus
cache-read plus cache-creation); the charge is
`(prompt_tokens - cached_tokens) * in_rate + completion_tokens * out_rate`, summed over
the session's steps and divided by `1e6`. Cache reads are uncharged, and the pricing join
strips a dated model suffix (`claude-haiku-4-5-20251001` matches `claude-haiku-4-5`) via
`regexp_replace(model_name, '-\d{8}$', '')`. The pricing table itself is
`DEFAULT_PRICING`, 11 entries of `(in_rate, out_rate)` in **USD per 1,000,000 tokens**,
base rates only — the prompt-cache write and read multipliers (1.25x, 2x, 0.1x of base
input) are deliberately not modeled, matching the macro
(`packages/atif-duck/src/atif_duck/domain/catalog.py:395-417`, pinned by
`packages/atif-duck/tests/test_duck_views.py:459` against published list rates and by
`packages/atif-duck/tests/test_duck_views.py:470` for sanity bounds).

**`human_ai_pair_count`.** Walk the step list in materialized order. Skip any step that
is sidechain, compact-summary, or has empty text. A user-role step arms a pending pair —
unless its stripped text is one of the two `CLI_BOOKKEEPING_TEXTS` strings, which are
Claude Code's own user-role injections and are skipped outright. An assistant-role step
completes the pending pair and disarms it. Consecutive user turns therefore collapse into
one pending pair, because judging a perceived error requires the human RESPONDING to AI
output (`packages/atif-analytics/src/atif_analytics/domain/transcript.py:236-262`). The
perceived pipeline admits a session only at 2 or more pairs
(`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:101-103`,
`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:118-120`).

**c-TF-IDF.** `CountVectorizer` (lowercased, unicode-stripped accents, `min_df = 2`,
`max_df = 0.95`, ngram range 1 to 2) produces a clusters-by-vocabulary count matrix. Term
frequency is L1-normalized per cluster row. The IDF factor is
`log(1 + sum(avg) / max(col_sum, 1e-9))` where `avg = col_sum / total`, and the weight is
the row-normalized TF times that IDF. Terms are ranked descending per cluster and the top
10 kept, with non-positive weights dropped
(`packages/atif-analytics/src/atif_analytics/domain/structure/terms.py:44-74`).

## Policy and gates

### Cost and spend

- **Dry-run by default:** every LLM analytics stage and the embed backfill treat
  `dry_run=True` as the default, returning a plan dict instead of spending; a real run is
  an explicit opt-out. `packages/atif-analytics/src/atif_analytics/application/analyze.py:41`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:362`.
- **Session ceiling:** each LLM pipeline admits at most `llm_max_sessions_per_run`
  sessions per run (default 50, newest-first so fresh sessions win), enforced DURING the
  admission walk so a deferred session is never rendered or eligibility-probed.
  `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:88-92`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:176-193`;
  tests `packages/atif-analytics/tests/test_resource_guards.py:84` and `packages/atif-analytics/tests/test_resource_guards.py:113`.
- **Cost ceiling:** one `RunBudget` of `llm_max_cost_usd_per_run` (default 25.0 USD per
  `analyze` run, shared across all five LLM pipelines) is priced from running actuals and
  checked at every dispatch batch boundary; when crossed, remaining LLM work stops and
  nothing is stamped for unstarted units.
  `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:93-97`,
  `packages/atif-analytics/src/atif_analytics/application/analyze.py:122-144`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:90-143`;
  test `packages/atif-analytics/tests/test_analyze.py:75`.
- **Budget-skip starvation escalation:** a stage skipped for budget records a durable
  `budget_skips` row, and the log escalates from WARNING to ERROR once the SAME stage has
  been starved 3 consecutive runs; a stage that actually runs clears its rows, so the row
  count IS the streak. `packages/atif-analytics/src/atif_analytics/application/analyze.py:148-180`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:54-62`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:217-246`; test `packages/atif-analytics/tests/test_analyze.py:99`.
- **Retry attempt cap:** a `(pipeline, unit_id)` stops being drained at 5 attempts, which
  is what closes the uncapped-retry money leak.
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:176-193`; test `packages/atif-analytics/tests/test_state.py:118`.
- **LLM-free tiers run first:** the friction pipeline pays no LLM call for a regex
  fast-path hit (flat 0.9 confidence over the first 512 characters of a message) or for a
  deterministic stamp-rule hit, and ambiguous phrasings deliberately fall through so one
  mis-tuned pattern cannot poison the corpus.
  `packages/atif-analytics/src/atif_analytics/domain/friction.py:100-114`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:178-232`;
  tests `packages/atif-analytics/tests/test_friction_tiers.py:47`, `packages/atif-analytics/tests/test_friction_tiers.py:79`, `packages/atif-analytics/tests/test_friction_tiers.py:87`, `packages/atif-analytics/tests/test_friction_tiers.py:92`.
- **Zero-cost visualization is off:** `compute_viz_coords` defaults to False because the
  2-d UMAP projection measured 66% of the cluster stage's wall clock and nothing consumes
  the coordinates. `packages/atif-analytics/src/atif_analytics/domain/config.py:37-40`.

### Model selection

- **No package hardcodes a model id:** every pipeline names a `(family, size)` pair and
  the registry resolves it; that is itself the policy.
  `packages/atif-models/src/atif_models/domain/registry.py:5-8`.
- **Runnable-family gate:** only families with a wired provider adapter may be selected
  (`RUNNABLE_FAMILIES` is `openai` alone); the anthropic column records ids and pricing
  for a future adapter and is rejected at settings load until one exists.
  `packages/atif-models/src/atif_models/infrastructure/settings.py:20-25`,
  `packages/atif-models/src/atif_models/infrastructure/settings.py:41-54`; tests `packages/atif-models/tests/test_settings.py:69` and `packages/atif-models/tests/test_settings.py:72`.
- **Per-pipeline size assignment:** classify, trajectory and perceived take `medium`,
  conflicts takes `large` (the hardest judgment task), friction takes `small` (a
  per-message enum) — each overridable via `ATIF_SQL_LLM_SIZE_<PIPELINE>`.
  `packages/atif-models/src/atif_models/infrastructure/settings.py:56-65`; test `packages/atif-models/tests/test_settings.py:26`.

### Freshness and data safety

- **Force overrides staleness only, never quiescence:** a live session is not converted
  even under `--force`, because converting a half-written transcript produces a WRONG
  artifact rather than a stale one.
  `packages/atif-corpus/src/atif_corpus/domain/sessions.py:170-174`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:200-206`; tests
  `packages/atif-corpus/tests/test_domain.py:117`,
  `packages/atif-corpus/tests/test_materialize.py:148`.
- **Ghost removal is all-or-nothing per pass:** when ANY source directory could not be
  listed, ghost removal is skipped entirely for the pass, because absence is not evidence
  of deletion without a complete picture of what exists.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:560-577`; tests
  `packages/atif-corpus/tests/test_materialize.py:609` and `packages/atif-corpus/tests/test_materialize.py:647`.
- **A `--sessions` filter never widens deletion:** ghost removal keys off the FULL scan,
  so an unplanned session is never removed just because it was not planned.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:511-519`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:573-577`; test `packages/atif-corpus/tests/test_materialize.py:280`.
- **Staging sweep errs toward keeping:** a signal delivered, a `PermissionError`, or any
  unexpected `OSError` all answer "do not delete", and a recycled pid only means the sweep
  skips debris a later pass collects. `packages/atif-corpus/src/atif_corpus/application/materialize.py:256-271`; test
  `packages/atif-corpus/tests/test_materialize.py:481`.
- **Corrupt state degrades, never refuses:** an unreadable or wrong-shaped
  `watermark.json` is treated as unmaterialized, costing one full pass, which is always
  safe. `packages/atif-corpus/src/atif_corpus/application/materialize.py:160-176`.

### Embeddings and the SQL surface

- **A provider switch is fail-loud, not silent:** the guard runs before any read or
  append and raises rather than mixing incompatible vector spaces; the error is `terminal`
  so an unattended lane exits 78 and suppresses retries instead of burning identical
  ticks. `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:3-18`,
  `packages/atif-embed/src/atif_embed/domain/errors.py:29-41`,
  `packages/atif-cli/src/atif_cli/app.py:833-847`.
- **Embed requires an explicit scope:** a bare `atif-sql embed --no-dry-run` exits 64 so a
  full backfill cannot happen from a mistyped command; `--dry-run` needs no scope because
  it spends nothing. `packages/atif-cli/src/atif_cli/app.py:778-783`, `packages/atif-cli/src/atif_cli/app.py:810-820`.
- **`skip_vss` escape hatch:** registration can skip both the `message_embeddings` view
  and the `semantic_search` macro, because the backfill writes the store the view reads
  and binding it first would be circular. `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1244-1250`;
  test `packages/atif-duck/tests/test_vss.py:258`.
- **Register-or-fail-loud:** any DuckDB error during view or macro registration is logged
  with `logger.exception` and re-raised — an empty or absent corpus fails registration
  rather than yielding empty views. `packages/atif-duck/src/atif_duck/infrastructure/registry.py:201-207`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1201-1204`; test
  `packages/atif-duck/tests/test_duck_views.py:623`.
- **A new view or macro cannot land silently:** the drift tests require a `DESCRIPTIONS`
  entry, an `ARG_EXEMPLARS` entry for any new parameter name, `TABLE_MACRO_NAMES`
  membership when the DDL is `AS TABLE`, and an example that EXECUTES — or a documented
  `EXCLUSIONS` entry. `packages/atif-duck/src/atif_duck/domain/examples.py:94-99`; tests
  `packages/atif-duck/tests/test_examples.py:160`, `packages/atif-duck/tests/test_examples.py:178`, `packages/atif-duck/tests/test_examples.py:188`, `packages/atif-duck/tests/test_examples.py:197`.

### Scheduling

- **Three cron lanes, split by cost:** `materialize` every 10 minutes (cheap incremental),
  `structural` hourly at minute 17 (zero LLM cost), `llm` once daily at 10:20 (the lane
  that spends). `packages/atif-cli/src/atif_cli/cron.py:43-51`.
- **`cron install` never writes a crontab:** it prints the block for a human to paste,
  after checking `crontab -l`. `packages/atif-cli/src/atif_cli/cron.py:180-197`.

## See also

- [module map](../architecture/module-map.md) — 39 shared source citations
- [processes](../behavior/processes.md) — 36 shared source citations
- [impact analysis](impact-analysis.md) — 34 shared source citations
- [contract map](contract-map.md) — 33 shared source citations
- [debugging guide](debugging-guide.md) — 22 shared source citations
