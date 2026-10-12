# atif-sql · Business logic

This file indexes the domain rules `atif-sql` enforces: input validations, invariants the
code holds across a boundary, derived-value calculations, and the policy gates that decide
whether work runs at all.

**Scope.** Application-layer and domain-layer rules across the workspace members,
plus the SQL surface `atif-duck` registers into DuckDB. There is no database server, no
migration directory, and no HTTP surface in this repo, so there are no DDL constraints or
request-validation middlewares to survey; the DuckDB views and macros are the closest
thing to a "schema", and the rules encoded in their DDL are in scope and captured under
Calculations and Invariants. LLM-output schemas count as validations here, because each
provider adapter re-validates every response with pydantic before it reaches a parquet
row. Ruff/pyright/import-linter rules are toolchain policy, not domain logic, and are out
of scope.

**Test provenance.** Where a rule is pinned by a test, the test is cited beside the
implementation. A row with no test citation is a rule read out of the implementation and
not covered by a named test, and that difference is stated, never smoothed over.

**Units.** Every `*_ns` value is epoch nanoseconds from `os.stat().st_mtime_ns`; every
`*_chars` value counts Python string characters, not bytes or tokens; pricing is USD per
1,000,000 tokens; backoff is minutes and tenacity waits are seconds. Scope (per session,
per run, per pipeline) is stated per row.

## Validations

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| A session path must end in `.jsonl` AND be an existing file | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:71-73` | raise `InvalidSessionInput`; CLI exits 64 |
| Production code imports nothing from harbor or litellm (the ATIF data classes and the validator are vendored in `packages/atif-converter/src/atif_converter/domain/atif/`) | Conversion | `packages/atif-converter/tests/test_dev_only_imports_guard.py`; the conversion itself is ours at `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:981` and `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:924` | the `ast` guard fails the suite on any `harbor` or `litellm` import in any member's `src/` |
| The converter must return a trajectory for the session | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:115-117` | raise `EmptySessionError`; CLI exits 2 |
| The trajectory must pass the vendored ATIF `TrajectoryValidator` AFTER enrichment mutates `extra` | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:76`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:289` | `validation_errors` is non-empty; the materialize adapter raises `TrajectoryValidationError` |
| No source file may change, vanish, or appear between the one read that parses and fingerprints it and the end of the audit; the converter and the audit consume that one read's records, so each file is opened twice (read, then re-hash), parsed once, hashed twice | Conversion | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:11-24`, checked once after enrichment at `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:285`; the read is `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py` (`load_session`); tests `packages/atif-converter/tests/test_snapshot_and_drift.py` (`TestConvertRefusesOnMidConversionWrite`, `TestSinglePass`) | raise `SourceMutatedDuringConversion`; the materialize pass records the session as failed and continues |
| A JSONL line that is not valid JSON is not a record; a line that parses to a non-object is kept for the converter, as harbor keeps it, and only the audit's record pairs drop it | Conversion | `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:199-202`, `packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:316` | invalid JSON: silent drop, DEBUG log (same policy as harbor) |
| An agent step and an assistant-record group pair up only when their tool ids intersect, or when BOTH sides carry no tool ids | Conversion | `packages/atif-converter/src/atif_converter/domain/enrichment.py:304-318`; test `packages/atif-converter/tests/test_enrichment.py:331` | refuse: attribution stops at that step, `enrichment_truncated_at_step` recorded, WARNING logged |
| A user step and a user text record pair up only while the record's harbor-derived text equals the step's message | Conversion | `packages/atif-converter/src/atif_converter/domain/enrichment.py:347-360`; test `packages/atif-converter/tests/test_enrichment.py:372` | refuse: same truncation marker, WARNING logged |
| A session materializes only once its newest source mtime is at least `quiesce_seconds` old (default 300 seconds of source silence, per session) | Corpus | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:84` and `packages/atif-corpus/src/atif_corpus/domain/sessions.py:218-219`; tests `packages/atif-corpus/tests/test_domain.py:40` (inclusive boundary) and `packages/atif-corpus/tests/test_domain.py:98` | deferred into `skipped_live`; revisited next pass |
| A source mtime in the FUTURE never satisfies the quiescence threshold | Corpus | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:106-114`; test `packages/atif-corpus/tests/test_domain.py:45` | stays skipped, plus a WARNING every pass once the mtime is more than `future_warn_seconds` (1 second) ahead, so the starvation is visible |
| `watermark.json` must parse as a mapping of path to int | Corpus | `packages/atif-corpus/src/atif_corpus/application/materialize.py:424-452` | degrade to empty (one full re-materialization pass), WARNING logged, never a refusal to sync |
| A scan finding zero sessions while the corpus holds materialized ones (with every source directory listed) is not a deletion | Corpus | `packages/atif-corpus/src/atif_corpus/application/materialize.py:179` and `packages/atif-corpus/src/atif_corpus/application/materialize.py:1661-1667`; test `packages/atif-corpus/tests/test_materialize.py:380` | raise `SuspiciousEmptyScanError`; nothing is marked source-removed |
| Only a genuine `FileNotFoundError` proves a source vanished; any other `OSError` means unreadable | Corpus | `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:113-125` and `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:264-273`; test `packages/atif-corpus/tests/test_materialize.py:909` | session lands in `SourceScan.unreadable`, is never ghosted, and its watermark entries are retained |
| A `pipeline` added to the retry queue must be one of the contract names in `PIPELINE_NAMES` (the tuple the checkpoint module defines) | Analytics | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:96-98`, `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:37-42`; tests `packages/atif-analytics/tests/test_state.py:145` and `packages/atif-analytics/tests/test_state.py:150` | raise `ValueError` |
| Every LLM-output `confidence` is bounded to the closed interval 0.0 to 1.0 (dimensionless, per emitted row) | Analytics | `packages/atif-analytics/src/atif_analytics/domain/models.py:66-69`, `packages/atif-analytics/src/atif_analytics/domain/models.py:146-149`, `packages/atif-analytics/src/atif_analytics/domain/models.py:238-241`, `packages/atif-analytics/src/atif_analytics/domain/models.py:342-345` | pydantic `ValidationError` at `model_validate`, translated to `ProviderUnavailable` by either adapter (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:242-246`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:284-288`); the unit goes to the retry queue |
| Every categorical LLM-output field is a closed `Literal` union, and free text is length-capped (goal 280 chars, rationale 200 chars, evidence 280 chars, summary 280 chars, a turn uuid 64 chars) | Analytics | `packages/atif-analytics/src/atif_analytics/domain/models.py:35`, `packages/atif-analytics/src/atif_analytics/domain/models.py:56-59`, `packages/atif-analytics/src/atif_analytics/domain/models.py:82-85`, `packages/atif-analytics/src/atif_analytics/domain/models.py:228-231`, `packages/atif-analytics/src/atif_analytics/domain/models.py:320-323`, `packages/atif-analytics/src/atif_analytics/domain/models.py:331-334` | same pydantic rejection path |
| A returned `turn_uuid` must be in the session's human-turn header uuid set (main-chain user steps a human wrote) | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:130-138` and `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:299-316`; tests `packages/atif-analytics/tests/test_perceived.py:167`, `packages/atif-analytics/tests/test_resource_guards.py:498` | row dropped with a WARNING; an EMPTY universe drops every row (fails closed) |
| A returned conflict pair's two uuids must BOTH be real `edges.jsonl` uuids for that session | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:283-310`; test `packages/atif-analytics/tests/test_resource_guards.py:471` | pair dropped; an unreadable universe drops every pair (fails closed) |
| An untrusted step body may not present itself as a turn header | Analytics | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:70-91`; tests `packages/atif-analytics/tests/test_resource_guards.py:442` and `packages/atif-analytics/tests/test_resource_guards.py:457` | coerce: `[uuid=` is rewritten to `(uuid=` case-insensitively, length preserved, before any header is built |
| A friction candidate is a human turn (a main-chain user step whose author is `human`) with a uuid, 1 to `friction_max_chars` characters (default 300, per message); hook feedback, task notifications, retry nudges and image metadata never qualify | Analytics | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:125-143`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:228-230`; test `packages/atif-analytics/tests/test_friction_tiers.py:98` | filtered out at the candidate boundary; no LLM call is ever made for it |
| `ATIF_SQL_LLM_FAMILY` must name a family with a wired provider adapter; both registry families (`openai`, `anthropic`) have one, so the validator guards a family that reaches the registry before its adapter does | Models | `packages/atif-models/src/atif_models/infrastructure/settings.py:25` and `packages/atif-models/src/atif_models/infrastructure/settings.py:41-54`; test `packages/atif-models/tests/test_settings.py:68` (narrows `RUNNABLE_FAMILIES` to prove the refusal) | raise `ValueError` at settings load: a startup refusal instead of a Bedrock 400 on every call |
| `size_for` accepts only a pipeline with an `llm_size_<pipeline>` field | Models | `packages/atif-models/src/atif_models/infrastructure/settings.py:68-78`; test `packages/atif-models/tests/test_settings.py:95` | raise `KeyError` |
| A schema with a schema-valued `additionalProperties` (a `dict[str, X]` field) cannot be expressed in OpenAI strict mode | Models | `packages/atif-models/src/atif_models/domain/schema.py:63-69`; test `packages/atif-models/tests/test_schema.py:71` | raise `ValueError` at body-build time |
| `finish_reason` of `content_filter` or `refusal`, or a non-empty `message.refusal`, is a decline (openai adapter) | Models | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:60`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:219-225`; tests `packages/atif-models/tests/test_openai_provider.py:182` and `packages/atif-models/tests/test_openai_provider.py:190` | raise `RefusalError` (terminal, never retried) |
| `stop_reason` of `refusal` is a decline (anthropic adapter) | Models | `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:252-257`; test `packages/atif-models/tests/test_anthropic_provider.py:150` | raise `RefusalError` naming the `stop_details.category` (terminal, never retried) |
| `finish_reason` of `length` gets exactly ONE retry, at `reasoning_effort` degraded one step (openai adapter) | Models | `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:50-55`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:149-181`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:226-232`; tests `packages/atif-models/tests/test_openai_provider.py:129`, `packages/atif-models/tests/test_openai_provider.py:144`, `packages/atif-models/tests/test_openai_provider.py:163` | raise `ProviderUnavailable` when still truncated or already at the floor; the unit goes to the retry queue |
| `stop_reason` of `max_tokens` gets exactly ONE retry, at `output_config.effort` degraded one step on the same ladder (anthropic adapter) | Models | `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:185-219`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:258-264`; tests `packages/atif-models/tests/test_anthropic_provider.py:158` and `packages/atif-models/tests/test_anthropic_provider.py:171` | raise `ProviderUnavailable` when still truncated or already at the floor; the unit goes to the retry queue |
| A response must carry a choice, and its content must be a non-empty string that parses as JSON and validates against the schema (openai adapter) | Models | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:213-216`, `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:233-246`; tests `packages/atif-models/tests/test_openai_provider.py:198`, `packages/atif-models/tests/test_openai_provider.py:206`, `packages/atif-models/tests/test_openai_provider.py:212` | raise `ProviderUnavailable` |
| An anthropic response must stop at `end_turn`, and its `text` blocks (any `thinking` block is skipped) must join into JSON that validates against the schema | Models | `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:265-288`; tests `packages/atif-models/tests/test_anthropic_provider.py:139`, `packages/atif-models/tests/test_anthropic_provider.py:185`, `packages/atif-models/tests/test_anthropic_provider.py:191`, `packages/atif-models/tests/test_anthropic_provider.py:199` | raise `ProviderUnavailable` |
| An anthropic string enum value that differs from one declared member only in letter case is that member | Models | `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:102-127`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:282-283`; tests `packages/atif-models/tests/test_anthropic_provider.py:218`, `packages/atif-models/tests/test_anthropic_provider.py:225`, `packages/atif-models/tests/test_anthropic_provider.py:232`, `packages/atif-models/tests/test_anthropic_provider.py:237` | coerce: folded onto the declared spelling before `model_validate`; a value matching no member, or more than one, passes through for pydantic to reject |
| A store's stamped `(model, dim)` must match the active embedder before any vector is read or appended | Embeddings | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38-80`, `packages/atif-embed/src/atif_embed/application/embed.py:173-183`; tests `packages/atif-embed/tests/test_embed_use_case.py:153`, `packages/atif-embed/tests/test_lance_store.py:321` and `packages/atif-embed/tests/test_lance_store.py:330` | raise `EmbeddingProviderMismatch` (`terminal = True`); `query`/`search` exit 65, `embed` exits 78 |
| `ensure_index` accepts only `cosine`, `l2`, or `dot` | Embeddings | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:414-419`; test `packages/atif-embed/tests/test_lance_store.py:110` | raise `ValueError` |
| A text over 50,000 characters (per embeddable text) cannot be embedded whole | Embeddings | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:35`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:43-47`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:65-68`; tests `packages/atif-embed/tests/test_embed_use_case.py:369` and `packages/atif-embed/tests/test_embed_use_case.py:386` | coerce: sent head-only, and the row stamps `truncated = true` so the search miss is attributable |
| Every macro parameter name must have an `ARG_EXEMPLARS` entry | SQL surface | `packages/atif-duck/src/atif_duck/domain/examples.py:59-76`, `packages/atif-duck/src/atif_duck/domain/examples.py:161-171`; test `packages/atif-duck/tests/test_examples.py:218` | `build_examples()` raises; the drift test fails with it |
| Every catalog object must yield a runnable example or carry a documented `EXCLUSIONS` entry | SQL surface | `packages/atif-duck/src/atif_duck/domain/examples.py:95-100`; test `packages/atif-duck/tests/test_examples.py:190` | drift test fails |
| A materialized session dir with no `meta.json` is incomplete | SQL surface | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:639-644`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:703-709`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:527-532`; test `packages/atif-duck/tests/test_duck_views.py:690` | excluded from every view, plus one WARNING per skipped dir so the exclusion is observable |
| A real `embed` run needs an explicit scope: `--limit N` or `--all` | CLI | `packages/atif-cli/src/atif_cli/app.py:1908-1918` | exit 64 with the hint naming both flags and `--dry-run` |
| `search` against an empty embeddings store is not a zero-result query | CLI | `packages/atif-cli/src/atif_cli/app.py:2145-2156` | exit 2 with the hint `atif-sql embed --all --no-dry-run` |

Notes on two rows above. The strict-JSON transform in
`packages/atif-models/src/atif_models/domain/schema.py:6-11` is a *rewriting* validation
rather than a rejecting one: it forces `additionalProperties: false` on every object level
and moves every property into `required`, expressing optionality by making the field's
type nullable. Only the open-mapping shape has no representation and raises. Second: the
provider's structured-output contract and pydantic are two gates in series: the wire
schema constrains shape, and `model_validate` re-applies the `ge`/`le` bounds the model
may have ignored (`packages/atif-models/src/atif_models/domain/schema.py:18-21`). The
anthropic adapter sends the schema through `transform_schema` from the Anthropic SDK
instead, which moves `minimum`, `maximum`, `minLength`, and `maxLength` into the field
descriptions because the Claude 5.5 models reject them, so for that family pydantic is the
only gate that enforces those bounds (`packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:17-22`,
pinned by `packages/atif-models/tests/test_anthropic_provider.py:112` and
`packages/atif-models/tests/test_anthropic_provider.py:207`).

## Invariants

### Conversion

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The `FidelityGap` members are the typed list of what the conversion loses, inherited from harbor 0.22.0's converter that this one ports for parity; `parent_chain_flattened` is structural and observed on every session, the rest are session-conditional | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:55-99`; `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:79` (structural set) and `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:151-155` (conditional set); test `packages/atif-converter/tests/test_convert_and_audit.py:157` |
| Only `RecordType.USER` and `RecordType.ASSISTANT` are convertible; of the other record types, the ones `session_events` keeps are captured and the rest are dropped | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:52`; test `packages/atif-converter/tests/test_convert_and_audit.py:123` |
| `LossReport.records_total` equals the `edges.jsonl` line count for any session, because the census and the edges emitter read one snapshot | Application code | `packages/atif-converter/src/atif_converter/domain/fidelity.py:123-127`, `packages/atif-converter/src/atif_converter/infrastructure/census.py:57-67`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:266-268`; test `packages/atif-converter/tests/test_snapshot_and_drift.py:133` |
| `records_converted` counts raw user/assistant RECORDS, not ATIF steps; harbor bundles several assistant events sharing one `message.id` into one agent step, so the two numbers differ by design | Application code | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:238-241`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:143-147` |
| `subagent_files_convertible` equals `subagent_files_found`: our converter discovers every side file itself, workflow-nested ones included, so nothing found is unconvertible; the retired `workflow_subagents_missed` value is never emitted | Application code | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:167-170`; test `packages/atif-converter/tests/test_convert_and_audit.py:129` |
| A truncation marker or a non-zero leftover count means enrichment attribution was REFUSED, never guessed | Application code | `packages/atif-converter/src/atif_converter/domain/enrichment.py:46-62`, `packages/atif-converter/src/atif_converter/domain/enrichment.py:379-399`; tests `packages/atif-converter/tests/test_enrichment.py:272`, `packages/atif-converter/tests/test_enrichment.py:287`, `packages/atif-converter/tests/test_enrichment.py:302` |
| Side-files are read in place, each keyed by its path under the side directory joined with `__` (a leading `subagents` part dropped), the flat name harbor's staging gave it; when two flatten to one name, the first in sorted `rglob` order wins | Application code | `packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:46-69` |
| `edges.jsonl` line order is `(ts, uuid)` with missing values sorting first as empty strings, and the key order is contract-fixed | Application code | `packages/atif-converter/src/atif_converter/domain/edges.py:22-32`, `packages/atif-converter/src/atif_converter/domain/edges.py:82-97`; tests `packages/atif-converter/tests/test_edges.py:47` and `packages/atif-converter/tests/test_edges.py:81` |

### Corpus

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| A reader never observes a torn artifact SET: all the artifacts are written into `<corpus_root>/.staging/` and the whole directory is swapped in by rename | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:165-204`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:627-632`; tests `packages/atif-corpus/tests/test_atomic.py:85`, `packages/atif-corpus/tests/test_materialize.py:435` |
| `meta.json` is written LAST inside the staging directory, and it is the marker `atif-duck` gates every reader on | Application code, both sides | `packages/atif-corpus/src/atif_corpus/application/materialize.py:706-731`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:703-709`; test `packages/atif-duck/tests/test_duck_views.py:690` |
| Persistence order matches write order: each artifact is fsynced before the rename that publishes it, and the parent directory after | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:72-103`, `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:51-69`; tests `packages/atif-corpus/tests/test_atomic.py:131`, `packages/atif-corpus/tests/test_atomic.py:149`, `packages/atif-corpus/tests/test_atomic.py:194` |
| A kill inside the swap window leaves the session dir MISSING rather than torn, and the next pass force-replans it | Application code | `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:180-189`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1017-1052`; tests `packages/atif-corpus/tests/test_materialize.py:881`, `packages/atif-corpus/tests/test_domain.py:216` |
| Only sessions that materialized successfully (or converted to nothing) advance their watermark entries, so a failed session stays stale and is retried | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:1310-1357`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1776-1779`; test `packages/atif-corpus/tests/test_materialize.py:422` |
| A VANISHED source path keeps its watermark entry unless the owning session succeeded this pass or left the scan entirely; the retained entry IS the staleness signal | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:1341-1352`; tests `packages/atif-corpus/tests/test_materialize.py:218` and `packages/atif-corpus/tests/test_materialize.py:254` |
| `owns_path` is the single definition of a session's watermark scope, so staleness and retention can never disagree about which entries belong to a session | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:139`; tests `packages/atif-corpus/tests/test_domain.py:191`, `packages/atif-corpus/tests/test_domain.py:202`, `packages/atif-corpus/tests/test_domain.py:207` |
| Only an exactly equal `mtime_ns` counts as unchanged; an mtime that moved BACKWARDS is `modified` | Application code | `packages/atif-corpus/src/atif_corpus/domain/watermark.py:61-76`; test `packages/atif-corpus/tests/test_domain.py:86` |
| A staging entry is removed only when EVERY pid its name carries is proven gone; only `ProcessLookupError` proves that | Application code | `packages/atif-corpus/src/atif_corpus/application/materialize.py:944-1014`; tests `packages/atif-corpus/tests/test_materialize.py:518`, `packages/atif-corpus/tests/test_materialize.py:546`, `packages/atif-corpus/tests/test_materialize.py:570` |
| One broken transcript never aborts a corpus sync: the port may raise anything, and the pass records the failure and continues | Application code | `packages/atif-corpus/src/atif_corpus/domain/ports.py:101-107`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:793-798`; test `packages/atif-corpus/tests/test_materialize.py:405` |
| The domain reads no clock: `now_ns` is always passed in, so identical inputs always yield an identical plan | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:18-21`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:104-110`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1625`; test `packages/atif-corpus/tests/test_domain.py:145` |
| Every plan partition is sorted by session id, so the same scan yields the same work order, log output, and failure ordering | Application code | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:121-123`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:212`; test `packages/atif-corpus/tests/test_domain.py:145` |
| The corpus root `~/.claude` maps to the reserved slug `default` and `~/.codex/sessions` to `codex`; every other root is `<sanitized-dirname>-<8 hex of sha256 of the resolved path>` | Application code | `packages/atif-corpus/src/atif_corpus/domain/slug.py:32`, `packages/atif-corpus/src/atif_corpus/domain/slug.py:40`, `packages/atif-corpus/src/atif_corpus/domain/slug.py:65-72`; tests `packages/atif-corpus/tests/test_slug.py:25`, `packages/atif-corpus/tests/test_slug.py:30`, `packages/atif-corpus/tests/test_slug.py:44`, `packages/atif-corpus/tests/test_slug.py:52` |
| The per-session artifact filenames and the watermark filename are fixed by the contract | Application code | `packages/atif-corpus/src/atif_corpus/domain/layout.py:18-25`; test `packages/atif-corpus/tests/test_domain.py:245` |

### Analytics

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The retry queue is the SINGLE re-admission gate for a failed unit: a live entry blocks dispatch until `drain` says it is due, because a failed unit is never checkpointed and would otherwise be re-admitted fresh every run forever | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:176-193`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:169-177`; tests `packages/atif-analytics/tests/test_state.py:124`, `packages/atif-analytics/tests/test_perceived.py:237` |
| A FAILED unit is not checkpointed; a REFUSED unit is (terminal), and its refusal is durable: an in-band sentinel row for session-keyed `classify`, a `analytics/refusals` sidecar row for the uuid-keyed pipelines | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:247-270`, `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:196-234`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:281-292`; test `packages/atif-analytics/tests/test_perceived.py:258` |
| Nothing is stamped (no checkpoint, no cache row, no retry entry) for a unit that was never dispatched | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:160-188`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:250-270`; test `packages/atif-analytics/tests/test_perceived.py:286` |
| Budget overshoot is bounded by `BUDGET_CHECK_BATCH` units (8, per dispatch batch) on the priciest watched model, and does not scale with the session ceiling or the write chunk size | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:108-115`, `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:151-157`; test `packages/atif-analytics/tests/test_resource_guards.py:265` (parametrized over classify and conflicts) |
| A checkpoint skip requires BOTH `last_ts` and `last_mtime` to be non-advancing; either bound moving forward re-admits the session | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:137-177`; tests `packages/atif-analytics/tests/test_state.py:48` and `packages/atif-analytics/tests/test_state.py:57` |
| `state.db` is corpus-scoped at `<corpus_root>/analytics/state.db`, so one corpus's completions can never skip another's re-scoring | Application code | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:23-24` |
| Sidechain steps are excluded from every rendered transcript, because the prompts are calibrated on transcripts with no subagent content and harbor inlines sidechains | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:31-33`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:242-244`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:282`; test `packages/atif-analytics/tests/test_transcript.py:91` |
| A compact-summary step is synthetic text: its author is `harness`, so it never counts toward a human-AI pair and renders under the `harness` label | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:146-149`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:295-297`; test `packages/atif-analytics/tests/test_transcript.py:137` |
| Only `render_session_text` writes a real `[uuid=` header; every body is escaped first | Application code | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:43-48`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:275-277`, `packages/atif-analytics/src/atif_analytics/domain/transcript.py:298-302`; test `packages/atif-analytics/tests/test_resource_guards.py:442` |
| Every LLM prompt fences its transcript or message in a `<transcript id="...">` or `<message id="...">` tag pair whose id is the first 8 hex digits of the content's sha256, with the one-line request after the fence, so a body cannot close its own fence and a rerun sends the same bytes | Application code | `packages/atif-analytics/src/atif_analytics/application/prompts.py:732-756`; tests `packages/atif-analytics/tests/test_prompts.py:96`, `packages/atif-analytics/tests/test_prompts.py:103`, `packages/atif-analytics/tests/test_prompts.py:108`, `packages/atif-analytics/tests/test_prompts.py:117` |
| Who wrote a user step is a prefix match over `AUTHOR_PREFIX_RULES` after stripping leading space, tab, CR and LF, first rule wins, with a compaction summary always `harness`; the SQL `step_author` macro and the Python twin in atif-analytics must agree | Application code | `packages/atif-duck/src/atif_duck/domain/authorship.py:51`, `packages/atif-analytics/src/atif_analytics/domain/authorship.py:108-122`; tests `packages/atif-duck/tests/test_authorship_twin_pin.py:64`, `packages/atif-cli/tests/test_authorship_parity.py:53` |
| classify and conflicts admit only `interactive` sessions: a `turn_audit` (opens on an audit prompt) or `one_shot_job` (at most one human turn) is checkpointed as skipped without a call | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:160-184`, `packages/atif-analytics/src/atif_analytics/domain/authorship.py:130` |
| The `source` column vocabulary `regex` / `sql` / `llm` / `refused` names the row's PROVENANCE TIER, not the engine that computed it, and downstream views bind to those literals | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:34-38`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:91-94` |
| Two friction stamp rules matching one uuid resolve by higher confidence, ties keeping the first stamp, with rules applied in the fixed order 1 to 3 | Application code | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:150-163` |

### Models and embeddings

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| The registry is the ONLY place in the workspace where a model id is written down, and it is total over family times size | Application code | `packages/atif-models/src/atif_models/domain/registry.py:5-8`, `packages/atif-models/src/atif_models/domain/registry.py:95-159`; test `packages/atif-models/tests/test_registry.py:63` |
| `estimate_cost` returns `None`, never `0.0`, when either rate is unknown; callers must render "pricing unavailable" | Application code | `packages/atif-models/src/atif_models/domain/registry.py:196-203`; tests `packages/atif-models/tests/test_registry.py:135` and `packages/atif-models/tests/test_registry.py:145` |
| Usage is accumulated BEFORE any `finish_reason` or `stop_reason` gate, because a truncated or refused call still billed tokens | Application code | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:209-212`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:248-251`; tests `packages/atif-models/tests/test_openai_provider.py:251` and `packages/atif-models/tests/test_anthropic_provider.py:171` |
| `RETRY_CODES` (the retryable Bedrock codes) is a deliberate TWIN of atif-embed's set, pinned against the same literal in each package's own suite because the two may not import each other | Application code | `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:27-42` (shared by both adapters); tests `packages/atif-models/tests/test_openai_provider.py:344` and `packages/atif-models/tests/test_openai_provider.py:348` |
| Every embeddings row stamps `model`, `dim`, `text_hash`, and `truncated`, so a re-conversion that changes a step's text is detectable under the same uuid | Application code | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:7-18`, `packages/atif-embed/src/atif_embed/application/embed.py:225-234`; test `packages/atif-embed/tests/test_embed_use_case.py:89` |
| A uuid never fans out to two vectors: a stale row under the same uuid is DELETED before the new vector is appended | Application code | `packages/atif-embed/src/atif_embed/application/embed.py:213-219`, `packages/atif-embed/src/atif_embed/domain/text_stamp.py:50-63`; tests `packages/atif-embed/tests/test_embed_use_case.py:211`, `packages/atif-embed/tests/test_lance_store.py:129` |
| `_PRE_STAMP_SENTINEL` (`<pre-stamp>`) can never equal a real blake2b hex digest, so every pre-stamp row mismatches its corpus hash and re-embeds through the ordinary staleness path | Application code | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:69-73`; test `packages/atif-embed/tests/test_lance_store.py:192` |
| ADDITIVE schema drift migrates ONLINE (metadata-only `add_columns`, no re-embedding, search stays up); a BREAKING change (provider or dimension switch) stays fail-loud | Application code | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:18-27`, `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166-192`; tests `packages/atif-embed/tests/test_embed_use_case.py:293` and `packages/atif-embed/tests/test_embed_use_case.py:314` |
| The write chunk, `max(batch_size * 4, 256)`, is a multiple of `batch_size` at both default batch sizes (96 for Cohere, 32 for Gemma) and at any batch size of 64 or more, so at those sizes a checkpoint boundary never splits a provider batch | Application code | `packages/atif-embed/src/atif_embed/application/embed.py:153-155` |
| The provider/dimension guard exists as two copies (atif-embed and atif-duck) because the independence contract forbids the import; a behavioral change in either must be ported | Application code | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:15-18`, `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-11`; tests `packages/atif-embed/tests/test_guard_twin_pin.py:76`, `packages/atif-embed/tests/test_guard_twin_pin.py:79`, `packages/atif-embed/tests/test_guard_twin_pin.py:85` |
| The recovery hint names the real store locations (`ATIF_SQL_LANCE_URI` or `<corpus-root>/embeddings_lance`), never a fixed home-directory path | Application code | `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:25-35`; tests `packages/atif-embed/tests/test_guard_twin_pin.py:97` and `packages/atif-embed/tests/test_guard_twin_pin.py:103` |

### SQL surface and CLI

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| Registration order is raw TEMP tables, then views, then VSS, then macros, then the authorship macro and views, then the v2 analytics views and macros; each layer binds against the previous at CREATE time | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2172-2193`; test `packages/atif-duck/tests/test_duck_views.py:675` |
| `DESCRIPTIONS` covers the catalog exactly | Application code | `packages/atif-duck/src/atif_duck/domain/catalog.py:487`; test `packages/atif-duck/tests/test_examples.py:208` |
| `cost_estimate`'s `est_cost_usd` covers PRICED steps only and is meaningful only when `unpriced_steps = 0`; an inner join would return a partial number indistinguishable from a complete one | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1925-1930`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1938-1954`; tests `packages/atif-duck/tests/test_duck_views.py:464` and `packages/atif-duck/tests/test_duck_views.py:542` |
| Both `cost_estimate` counters filter on `model_name IS NOT NULL`, because user steps carry no model and would otherwise make every session look like a pricing gap | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1944-1949`; test `packages/atif-duck/tests/test_duck_views.py:425` |
| An absent or unattachable Lance store degrades to an empty `message_embeddings` TABLE with the right schema, so `semantic_search` always binds | Application code | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1687-1705`; tests `packages/atif-duck/tests/test_vss.py:112` and `packages/atif-duck/tests/test_vss.py:248` |
| Exit codes are a stable wire contract: `EXIT_CODES` is the workspace's one table, atif-converter carries no codes of its own (`convert` translates its typed errors), and no number is ever reassigned, so the retired `harbor_missing` keeps 127 | Application code | `packages/atif-cli/src/atif_cli/errors.py:5-17`, `packages/atif-cli/src/atif_cli/errors.py:29-66` |
| `terminal` versus transient decides the `embed` exit code, so an unattended lane can stop retrying instead of burning identical ticks | Application code | `packages/atif-embed/src/atif_embed/domain/errors.py:17-26`, `packages/atif-cli/src/atif_cli/app.py:1945-1959` |

## Calculations

| Calculation | Inputs | Output | Citation |
| --- | --- | --- | --- |
| Dry-run dollar projection for a planned batch | measured `input_tokens`, `output_tokens`, and a `(in_rate, out_rate)` pair in USD per 1M tokens | USD for the batch, flat linear at the standard rates, with no minimums, tiers, or long-context rates | `packages/atif-analytics/src/atif_analytics/domain/costs.py:35-49` |
| Characters to tokens | character count and the resolved spec's `chars_per_token` (3.3 for GPT-6, 2.2 for Claude 5.5, a 4.0 fallback) | token estimate `max(1, int(chars / chars_per_token))`, floor 1 when non-empty | `packages/atif-analytics/src/atif_analytics/domain/costs.py:19-32`, `packages/atif-models/src/atif_models/domain/registry.py:91-93`; test `packages/atif-models/tests/test_registry.py:126` |
| Input tokens for one call | the provider's usage block | openai: `prompt_tokens`; anthropic: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`, because Anthropic reports `input_tokens` without the cache; either way `cached_tokens` is the cache-read subset | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:63-78`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:81-99`; test `packages/atif-models/tests/test_anthropic_provider.py:243` |
| Long-context marking for one call | the call's input tokens and the spec's `long_context_threshold` (272,000 for every GPT-6 size, 100,000 for Haiku 5.5, none for Sonnet and Opus 5.5) | `long_context = True` when input tokens exceed the threshold; the accumulator then adds the call's input and output to `long_input_tokens` and `long_output_tokens`, per pipeline | `packages/atif-models/src/atif_models/domain/registry.py:175-177`, `packages/atif-models/src/atif_models/domain/ports.py:99-109`; tests `packages/atif-models/tests/test_registry.py:118`, `packages/atif-models/tests/test_openai_provider.py:282`, `packages/atif-models/tests/test_openai_provider.py:293`, `packages/atif-models/tests/test_anthropic_provider.py:251`, `packages/atif-models/tests/test_anthropic_provider.py:264` |
| Accumulated-usage dollar estimate for one `ModelSpec` | `pricing_in`, `pricing_out`, `long_pricing_in`, `long_pricing_out` (USD per 1M tokens), accumulated `input_tokens` and `output_tokens`, and their `long_input_tokens` and `long_output_tokens` shares | USD: the long share at the long rates (the standard rates when the spec has none) plus the rest at the standard rates, or `None` when a standard rate is unknown | `packages/atif-models/src/atif_models/domain/registry.py:180-210`; tests `packages/atif-models/tests/test_registry.py:78`, `packages/atif-models/tests/test_registry.py:84`, `packages/atif-models/tests/test_registry.py:98` |
| Running actual spend for a run | every watched provider's `UsageAccumulator` summary, long-context shares included, plus its `ModelSpec` | total USD spent this run, across all LLM pipelines | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:126-144` |
| `cost_estimate(sid)` SQL macro | a session's `steps` rows (`prompt_tokens`, `cached_tokens`, `completion_tokens`, `model_name`) LEFT JOINed against `DEFAULT_PRICING` | `(est_cost_usd, priced_steps, unpriced_steps)` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1938-1954`; test `packages/atif-duck/tests/test_duck_views.py:425` |
| Retry backoff delay | attempt counter | `min(2 ** attempts, 60)` MINUTES, per `(pipeline, unit_id)`: 2, 4, 8, 16, 32, capped at 60 | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:78-81`; test `packages/atif-analytics/tests/test_state.py:102` |
| Bedrock call retry schedule | the raised exception | up to 10 attempts, exponential wait with multiplier 2, minimum 2 SECONDS and maximum 60 SECONDS, only for `RETRY_CODES` and network errors | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:183-189`, `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:221-227`, `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:66-73`; tests `packages/atif-models/tests/test_openai_provider.py:310` and `packages/atif-models/tests/test_anthropic_provider.py:275` |
| Parquet write chunk size | `batch_size` (default 96 units) | `max(batch_size * 4, 256)` ROWS per part (bounds crash loss, not spend) | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:247-255` |
| Quiescence age test | newest source `mtime_ns`, `now_ns`, `quiesce_seconds` | boolean: `now_ns - newest_mtime_ns >= quiesce_seconds * 1_000_000_000` | `packages/atif-corpus/src/atif_corpus/domain/sessions.py:106-114`; test `packages/atif-corpus/tests/test_domain.py:40` |
| Source delta between two scans | two `path` to `mtime_ns` maps | added / modified / removed sorted tuples; `touched` is added then modified | `packages/atif-corpus/src/atif_corpus/domain/watermark.py:57-76`; test `packages/atif-corpus/tests/test_domain.py:76` |
| Corpus slug | a corpus root path | `default` for `~/.claude`, `codex` for `~/.codex/sessions`, else `<sanitized-dirname (<= 32 chars)>-<8 hex of sha256>` | `packages/atif-corpus/src/atif_corpus/domain/slug.py:65-72`; test `packages/atif-corpus/tests/test_slug.py:44` |
| Loss accounting for one session | raw record counts by `RecordType`, side-file classification, and the count of records kept as `session_events` rows | `records_converted` (user plus assistant records), `records_captured`, `records_dropped` (total minus converted minus captured), `gaps_observed` | `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:137-172`, `packages/atif-converter/src/atif_converter/domain/fidelity.py:142-145` |
| Completed human-to-AI exchange count | a session's `StepEvent` list | integer pair count, the perceived-error eligibility input | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:328-352`; test `packages/atif-analytics/tests/test_perceived.py:63` |
| Content stamp for an embeddable text | the exact text sent to the embedder | blake2b digest, 16 bytes hex-encoded (128 bits) | `packages/atif-embed/src/atif_embed/domain/text_stamp.py:26-40` |
| `friction_rate(since_days)` | `user_friction` label counts per session; `human_turns` rows as denominator | per-session `rate` plus one counter per friction label | `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:292-325` |
| `session_outcomes` | a session's main-chain `steps`, each labeled by `step_author` | `kind` from the opening user step's author and the human-turn count (`turn_audit`, `one_shot_job` at one human turn or fewer, else `interactive`); `outcome` is the first that holds of an audit verdict `pass` or `block` as the last agent message, any Stop hook block (`reviewer_blocked`), any interrupt (`interrupted`), else `clean_end` | `packages/atif-duck/src/atif_duck/infrastructure/authorship.py:91-131` |
| `semantic_search(query_vec, k)` | a unit-norm query vector and `k` | top-`k` `(uuid, sim, distance)` ordered by cosine distance | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2016-2023`; test `packages/atif-duck/tests/test_vss.py:195` |
| `todo_velocity(sid)` | `todo_state_current` rows for one session | completed count divided by distinct subject count, `NULL` when there are no subjects | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1973-1978`; test `packages/atif-duck/tests/test_duck_views.py:626` |
| `subagent_fanout(sid)` | `subagent_spawns` rows for one session | count of Task/Agent launch INTENTS, not side-transcript files | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1982-1994`; test `packages/atif-duck/tests/test_duck_views.py:632` |

Some of these need the formula spelled out.

**`cost_estimate(sid)`.** Per step, uncharged-cache base input is
`prompt_tokens - cached_tokens`, because ATIF's `prompt_tokens` is the TOTAL (input plus
cache-read plus cache-creation); the charge is
`(prompt_tokens - cached_tokens) * in_rate + completion_tokens * out_rate`, summed over
the session's steps and divided by `1e6`. Cache reads are uncharged, and the pricing join
strips a dated model suffix (`claude-haiku-4-5-20251001` matches `claude-haiku-4-5`) via
`regexp_replace(model_name, '-\d{8}$', '')`. The pricing table itself is
`DEFAULT_PRICING`, entries of `(in_rate, out_rate)` in **USD per 1,000,000 tokens**,
base rates only: the prompt-cache write and read multipliers (1.25x, 2x, 0.1x of base
input) are deliberately not modeled, matching the macro
(`packages/atif-duck/src/atif_duck/domain/catalog.py:602-626`, pinned by
`packages/atif-duck/tests/test_duck_views.py:519` against published list rates and by
`packages/atif-duck/tests/test_duck_views.py:530` for sanity bounds).

**`human_ai_pair_count`.** Walk the step list in materialized order. Skip any step that
is sidechain, compact-summary, or has empty text. A human turn arms a pending pair; a
user-role step a human didn't write (hook feedback, notifications, harness text) is
skipped outright. An assistant-role step completes the pending pair and disarms it.
Consecutive human turns therefore collapse into one pending pair, because judging a
perceived error requires the human RESPONDING to AI output
(`packages/atif-analytics/src/atif_analytics/domain/transcript.py:340-352`). The
perceived pipeline admits a session only at 2 or more pairs
(`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:110`,
`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:125-127`).

## Policy and gates

### Cost and spend

- **Dry-run by default:** every LLM analytics stage treats `dry_run=True` as the default,
  returning a plan dict instead of spending, and `analyze` spends only under `--no-dry-run`.
  The embed backfill is the exception: `run_backfill` defaults to `dry_run=False`, so the
  explicit-scope gate under Embeddings and the SQL surface is what keeps a mistyped `embed`
  from spending. `packages/atif-analytics/src/atif_analytics/application/analyze.py:42`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:366`,
  `packages/atif-cli/src/atif_cli/app.py:1640`,
  `packages/atif-embed/src/atif_embed/application/embed.py:69`.
- **Session ceiling:** each LLM pipeline admits at most `llm_max_sessions_per_run`
  sessions per run (default 50, newest-first so fresh sessions win), enforced DURING the
  admission walk so a deferred session is never rendered or eligibility-probed.
  `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:76-80`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:179-199`;
  tests `packages/atif-analytics/tests/test_resource_guards.py:79` and `packages/atif-analytics/tests/test_resource_guards.py:108`.
- **Cost ceiling:** one `RunBudget` of `llm_max_cost_usd_per_run` (default 25.0 USD per
  `analyze` run, shared across every LLM pipeline) is priced from running actuals and
  checked at every dispatch batch boundary; when crossed, remaining LLM work stops and
  nothing is stamped for unstarted units.
  `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:81-85`,
  `packages/atif-analytics/src/atif_analytics/application/analyze.py:71-89`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:97-148`;
  test `packages/atif-analytics/tests/test_analyze.py:78`.
- **Budget-skip starvation escalation:** a stage skipped for budget records a durable
  `budget_skips` row, and the log escalates from WARNING to ERROR once the SAME stage has
  been starved 3 consecutive runs; only a stage that runs and finishes inside the budget
  clears its rows, so the row count IS the streak. `packages/atif-analytics/src/atif_analytics/application/analyze.py:109-146`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:55-63`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/checkpointer.py:218-247`; tests `packages/atif-analytics/tests/test_analyze.py:100` and `packages/atif-analytics/tests/test_analyze.py:142`.
- **Retry attempt cap:** a `(pipeline, unit_id)` stops being drained at 5 attempts, which
  is what closes the uncapped-retry money leak.
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:176-193`; test `packages/atif-analytics/tests/test_state.py:118`.
- **LLM-free tiers run first:** the friction pipeline pays no LLM call for a regex
  fast-path hit (flat 0.9 confidence over the first 512 characters of a message) or for a
  deterministic stamp-rule hit, and ambiguous phrasings deliberately fall through so one
  mis-tuned pattern cannot poison the corpus.
  `packages/atif-analytics/src/atif_analytics/domain/friction.py:100-114`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:146-202`;
  tests `packages/atif-analytics/tests/test_friction_tiers.py:47`, `packages/atif-analytics/tests/test_friction_tiers.py:79`, `packages/atif-analytics/tests/test_friction_tiers.py:87`, `packages/atif-analytics/tests/test_friction_tiers.py:92`.
- **Tool-result bodies stay out of LLM prompts by default:** the transcript renders each
  tool result as a size stub, `[tool_result <id> <ts>] (N chars omitted)`, with
  `, the step has a failed result` added when the step carries an error, so file contents
  and command output never reach the model or its bill. Setting
  `ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES=true` restores the bodies, each clipped at
  50,000 characters. Tool calls keep their argument previews either way.
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:306-312`,
  `packages/atif-analytics/src/atif_analytics/domain/config.py:21-34`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:91-94`;
  tests `packages/atif-analytics/tests/test_transcript.py:57`, `packages/atif-analytics/tests/test_transcript.py:68`, `packages/atif-analytics/tests/test_transcript.py:190`.

### Model selection

- **No package hardcodes a model id:** every pipeline names a `(family, size)` pair and
  the registry resolves it; that is itself the policy.
  `packages/atif-models/src/atif_models/domain/registry.py:5-8`.
- **Runnable-family gate:** only families with a wired provider adapter may be selected.
  `RUNNABLE_FAMILIES` holds `openai` and `anthropic`, and
  `build_structured_provider` picks the adapter for the resolved spec's family, importing
  only that adapter's module.
  `packages/atif-models/src/atif_models/infrastructure/settings.py:20-25`,
  `packages/atif-models/src/atif_models/infrastructure/settings.py:41-54`,
  `packages/atif-models/src/atif_models/infrastructure/providers.py:20-34`; tests `packages/atif-models/tests/test_settings.py:59`, `packages/atif-models/tests/test_settings.py:62`, and `packages/atif-models/tests/test_anthropic_provider.py:300`.
- **Per-pipeline size assignment:** classify and friction take `small`, perceived takes
  `medium`, conflicts takes `large` (the hardest judgment task), each overridable via
  `ATIF_SQL_LLM_SIZE_<PIPELINE>`. On the default `openai` family the three sizes are
  `gpt-6-luna`, `gpt-6.1-sol`, and `gpt-6-astra`; on `anthropic` they're
  `claude-haiku-5-5`, `claude-sonnet-5-5`, and `claude-opus-5-5`.
  `packages/atif-models/src/atif_models/infrastructure/settings.py:56-66`,
  `packages/atif-models/src/atif_models/domain/registry.py:95-159`; test `packages/atif-models/tests/test_settings.py:26`.

### Freshness and data safety

- **Force overrides staleness only, never quiescence:** a live session is not converted
  even under `--force`, because converting a half-written transcript produces a WRONG
  artifact rather than a stale one.
  `packages/atif-corpus/src/atif_corpus/domain/sessions.py:183-185`, `packages/atif-corpus/src/atif_corpus/domain/sessions.py:218-220`; tests
  `packages/atif-corpus/tests/test_domain.py:117`,
  `packages/atif-corpus/tests/test_materialize.py:160`.
- **Nothing is deleted, and marking is all-or-nothing per pass:** a session whose source
  vanished keeps its artifacts and is marked `source_present: false`; when ANY source
  directory could not be listed, marking is skipped entirely for the pass, because absence
  is not evidence of deletion without a complete picture of what exists.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:1668-1690`; tests
  `packages/atif-corpus/tests/test_materialize.py:698` and `packages/atif-corpus/tests/test_materialize.py:736`.
- **A `--sessions` filter never widens marking:** source-removal marking keys off the FULL
  scan, so an unplanned session is never marked just because it was not planned.
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:1563-1571`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1679-1690`; test `packages/atif-corpus/tests/test_materialize.py:368`.
- **Staging sweep errs toward keeping:** a signal delivered, a `PermissionError`, or any
  unexpected `OSError` all answer "do not delete", and a recycled pid only means the sweep
  skips debris a later pass collects. `packages/atif-corpus/src/atif_corpus/application/materialize.py:953-968`; test
  `packages/atif-corpus/tests/test_materialize.py:570`.
- **Corrupt state degrades, never refuses:** an unreadable or wrong-shaped
  `watermark.json` is treated as unmaterialized, costing one full pass, which is always
  safe. `packages/atif-corpus/src/atif_corpus/application/materialize.py:424-452`.

### Embeddings and the SQL surface

- **A provider switch is fail-loud, not silent:** the guard runs before any read or
  append and raises rather than mixing incompatible vector spaces; the error is `terminal`
  so an unattended lane exits 78 and suppresses retries instead of burning identical
  ticks. `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:3-18`,
  `packages/atif-embed/src/atif_embed/domain/errors.py:29-41`,
  `packages/atif-cli/src/atif_cli/app.py:1945-1959`.
- **Embed requires an explicit scope:** a bare `atif-sql embed` is a real run and exits 64,
  so a full backfill cannot happen from a mistyped command; `--dry-run` needs no scope
  because it spends nothing. `packages/atif-cli/src/atif_cli/app.py:1864-1868`, `packages/atif-cli/src/atif_cli/app.py:1908-1918`.
- **`skip_vss` escape hatch:** registration can skip both the `message_embeddings` view
  and the `semantic_search` macro, because the backfill writes the store the view reads
  and binding it first would be circular. `packages/atif-duck/src/atif_duck/infrastructure/registry.py:2134-2140`;
  test `packages/atif-duck/tests/test_vss.py:258`.
- **Register-or-fail-loud:** any DuckDB error during view or macro registration is logged
  with `logger.exception` and re-raised: an empty or absent corpus fails registration
  rather than yielding empty views. `packages/atif-duck/src/atif_duck/infrastructure/registry.py:955-958`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1527-1530`; test
  `packages/atif-duck/tests/test_duck_views.py:683`.
- **A new view or macro cannot land silently:** the drift tests require a `DESCRIPTIONS`
  entry, an `ARG_EXEMPLARS` entry for any new parameter name, `TABLE_MACRO_NAMES`
  membership when the DDL is `AS TABLE`, and an example that EXECUTES, or a documented
  `EXCLUSIONS` entry. `packages/atif-duck/src/atif_duck/domain/examples.py:95-100`; tests
  `packages/atif-duck/tests/test_examples.py:190`, `packages/atif-duck/tests/test_examples.py:208`, `packages/atif-duck/tests/test_examples.py:218`, `packages/atif-duck/tests/test_examples.py:227`.

### Scheduling

- **Cron lanes, split by cost:** `materialize` every 10 minutes (cheap incremental),
  `llm` once daily at 10:20 (the lane that spends), and `compact` nightly at 03:40 (lake
  maintenance).
  `packages/atif-cli/src/atif_cli/cron.py:43-51`.
- **`cron install` never writes a crontab:** it prints the block for a human to paste,
  after checking `crontab -l`. `packages/atif-cli/src/atif_cli/cron.py:186-203`.

## See also

- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [impact analysis](impact-analysis.md)
- [contract map](contract-map.md)
- [debugging guide](debugging-guide.md)
