# atif-sql · Dead code

Measured 2026-10-12.

**Seven public definitions are dead code.** Seven public top-level definitions
under `packages/*/src` are never used: no code in their own module or in any
other calls, reads, or imports them, and five of the seven appear only as a
string in their module's `__all__`. The first table below lists them. Every other public top-level
definition with no reference outside its own file resolves to a use inside its
own module (a call, a construction, an annotation, a default argument, or a
read). What the other two tables record is a narrower defect: an **export
declaration with no consumer**: a name in an `__all__` that nothing imports.

**Technique, and what it cannot see.** No code index covers this repo (there is no `.codegraph/`, no LSP index, no AST
symbol graph), and no dead-code analyzer is wired into the project: `grep -n
"vulture|dead|unused|deptry|knip"` over `pyproject.toml` and `mise.toml` returns
only unrelated hits, ty's `unused-ignore-comment` rule at `pyproject.toml:460`
and a comment about it on line 465. The
finding set is therefore derived, in these passes:

1. **AST enumeration.** `ast.parse` every file under `packages/*/src/**/*.py`,
   collecting every top-level `FunctionDef` / `AsyncFunctionDef` / `ClassDef` /
   `type` alias / module-level assignment whose name does not begin with `_`,
   together with its `__all__` membership, and whether its module declares an
   `__all__` at all.
2. **Unanchored reference index.** For each name, the whole-word pattern
   `(?<!\w)NAME(?!\w)` against every line of every git-tracked text file
   (every path `git ls-files -z` yields, all readable as text), excluding the
   defining file and the `docs/` tree, whose pages (this one included) name the
   symbols they describe. The pattern also matches after a dot, so an
   attribute read such as `module.NAME` counts. Driving the search from
   `git ls-files` is what guarantees that no citation here lands in a
   gitignored path and that `.md`, `.toml`, and `.yml` surfaces are searched,
   not only Python.
3. **Read-back at the use site.** A name with outside matches goes through an
   AST pass over every tracked Python file that resolves `from ... import`
   statements, attribute reads through an imported module, and dotted-path
   strings such as a `monkeypatch.setattr` target. A name that pass found no
   reference for was read by hand at every match. Every listed candidate was
   opened and its intra-file use read.

`vulture` 2.16 was run as a cross-check (`uvx vulture packages
--min-confidence 80`, exit 3) and contributes **no rows**: eight of its nine
findings are the `exc_type` and `tb` parameters an `__exit__` signature has to
declare, and the ninth is a boto3 keyword-only parameter on a signature-matching
test stub. Its unit of analysis is
one file, so it cannot answer the cross-module question.

The limits of this derivation, stated because no index backs it:

- **The text index isn't namespace-qualified.** A bare name match
  cross-attributes symbols that share a name across packages, and this repo has
  such collisions on purpose: `DomainError` is declared independently in
  atif-models, atif-converter, and atif-embed, and `EmbeddingProviderMismatch`
  in more than one. The direction of that error is conservative: it inflates reference
  counts, so it can hide a dead symbol but cannot invent one. The read-back in
  pass 3 is what separates them: atif-models' `DomainError` has no consumer
  outside its module and is listed below, while the other two are imported.
- **A textual match is not a semantic reference.** A match that keeps a symbol
  off these tables is a resolved import or attribute read, or was read at its
  site. Two kinds of indirect reference count: a test that reads a module as
  source text and pins its constants by name, and a re-import that hands a
  name on to a third module.
- **Import position is irrelevant to the method, deliberately.** Because
  `PLC0415` is off (`pyproject.toml:227`) to satisfy the lean-import assertion at
  `packages/atif-cli/tests/test_lean_import.py:41`, atif-cli's cross-package
  imports sit *inside* command bodies:
  `packages/atif-cli/src/atif_cli/app.py:712`,
  `packages/atif-cli/src/atif_cli/app.py:934`, and
  `packages/atif-cli/src/atif_cli/app.py:1459` are all indented. The import graph
  uses `ast.walk`, which descends into function
  bodies, and the reference regex is unanchored, so a deferred import counts as
  an inbound edge.
- **Dynamic dispatch was swept by hand.** No module under `packages/*/src` is
  imported by string: the `importlib` uses in `src` are
  `packages/atif-cli/src/atif_cli/app.py:129`, which reads an installed
  distribution's version, and
  `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:115`, which
  imports `torch` and `sentence_transformers` by name. The `importlib` uses
  outside `src` load `scripts/` files by path, import upstream harbor modules,
  read installed versions, or probe for third-party packages.
  No `getattr` targets a first-party module object; every site takes an
  instance, an upstream class, or a standard-library or third-party module,
  including
  `packages/atif-models/src/atif_models/infrastructure/settings.py:74`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:58`,
  and `packages/atif-cli/src/atif_cli/app.py:306` (`os`).

Enforced gates are why the list is short rather than thin. ruff runs
`select = ["ALL"]` (`pyproject.toml:209`) at zero, so `F401`, `F811`, `F841` and
`ERA001` leave no unused import, no redefinition, no unused local, and no
commented-out code, with two carve-outs: `pyproject.toml:287`
(`"**/__init__.py" = ["F401", "E402"]`), and the vendored harbor tree, which
ruff doesn't lint at all (`pyproject.toml:191`). pyright runs
`typeCheckingMode = "strict"` (`pyproject.toml:684`) over every `src/` and
`tests/` tree plus `scripts` and `fuzz` (`pyproject.toml:685-702`) at zero errors, which enforces
`reportUnusedFunction` / `reportUnusedClass` / `reportUnusedVariable` for every
`_`-prefixed definition unused inside its own file. Neither gate can see a
*public* symbol that no other module imports, and that gap is exactly what the
tables below fill.

## Unreferenced definitions

The names below have no reference in any tracked file outside `docs/`, their own
module included: the definition and, for five of them, a string in their
module's `__all__` are the only occurrences. Nothing in the repo calls, reads,
or re-exports them, so each one can be deleted together with its `__all__`
entry.

| Symbol | Path | Last modified |
| --- | --- | --- |
| `is_interrupt` | `packages/atif-analytics/src/atif_analytics/domain/authorship.py:125` | 2026-09-27 |
| `codex_edge_ids` | `packages/atif-converter/src/atif_converter/domain/codex_edges.py:175` | 2026-09-11 |
| `CODEX_CONVERTIBLE_RECORD_TYPES` | `packages/atif-converter/src/atif_converter/domain/codex_fidelity.py:47` | 2026-09-11 |
| `is_claude_session_event` | `packages/atif-converter/src/atif_converter/domain/session_events.py:206` | 2026-09-27 |
| `is_codex_session_event` | `packages/atif-converter/src/atif_converter/domain/session_events.py:295` | 2026-09-27 |
| `convert_codex_session` | `packages/atif-converter/src/atif_converter/infrastructure/codex_adapter.py:67` | 2026-09-11 |
| `LAKE_TABLE_NAMES` | `packages/atif-duck/src/atif_duck/domain/lake.py:254` | 2026-09-28 |

## Unreferenced exports

The names below appear in their own module's `__all__` and have no reference in
any other tracked file outside `docs/`.
The symbol is live inside its module; the unreferenced thing is the export
declaration, so the fix is to narrow `__all__`, not to delete code. Each row's
intra-module use site is named after the table.

| Symbol | Path | Last modified |
| --- | --- | --- |
| `session_kind` | `packages/atif-analytics/src/atif_analytics/domain/authorship.py:130` | 2026-09-27 |
| `CHARS_PER_TOKEN` | `packages/atif-analytics/src/atif_analytics/domain/costs.py:25` | 2026-10-12 |
| `CLASSIFICATIONS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:25` | 2026-08-28 |
| `CONFLICTS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:26` | 2026-08-28 |
| `USER_FRICTION_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:27` | 2026-08-28 |
| `PERCEIVED_ERRORS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:28` | 2026-08-28 |
| `REFUSALS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:39` | 2026-08-28 |
| `STATE_DB_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:41` | 2026-08-28 |
| `TOOL_INPUT_PREVIEW_CHARS` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:68` | 2026-08-28 |
| `UUID_HEADER_ESCAPE` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:79` | 2026-08-28 |
| `main_chain` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:242` | 2026-08-28 |
| `parse_trajectory_step` | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:115` | 2026-09-28 |
| `read_trajectory_steps` | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:238` | 2026-09-28 |
| `read_edges_uuids` | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:249` | 2026-09-28 |
| `is_sharded_dir` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:35` | 2026-08-28 |
| `write_part` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:90` | 2026-08-28 |
| `read_all` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:114` | 2026-08-28 |
| `MAX_ATTEMPTS_DEFAULT` | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42` | 2026-08-28 |
| `StorageLayout` | `packages/atif-cli/src/atif_cli/corpus.py:83` | 2026-09-28 |
| `LaneStatus` | `packages/atif-cli/src/atif_cli/cron.py:74` | 2026-08-28 |
| `DuckLakeSteps` | `packages/atif-cli/src/atif_cli/embed_lake.py:70` | 2026-09-28 |
| `TURNS_BATCH_SIZE` | `packages/atif-cli/src/atif_cli/lake_sessions.py:65` | 2026-09-28 |
| `STEPS_BATCH_SIZE` | `packages/atif-cli/src/atif_cli/lake_sessions.py:69` | 2026-09-28 |
| `emit_row_batches` | `packages/atif-cli/src/atif_cli/output.py:181` | 2026-08-28 |
| `CODEX_TOOL_IMAGE_MEDIA_TYPES` | `packages/atif-converter/src/atif_converter/domain/blobs.py:96` | 2026-10-08 |
| `codex_tool_image_media_type` | `packages/atif-converter/src/atif_converter/domain/blobs.py:339` | 2026-10-08 |
| `codex_edge_from_record` | `packages/atif-converter/src/atif_converter/domain/codex_edges.py:116` | 2026-09-11 |
| `COMMAND_TOOL_NAME` | `packages/atif-converter/src/atif_converter/domain/codex_nested_calls.py:48` | 2026-10-09 |
| `VIA_EXEC` | `packages/atif-converter/src/atif_converter/domain/codex_nested_calls.py:51` | 2026-10-09 |
| `PAYLOAD_LIST_MAX_ITEMS` | `packages/atif-converter/src/atif_converter/domain/session_events.py:81` | 2026-09-27 |
| `CLAUDE_SYSTEM_SUBTYPES` | `packages/atif-converter/src/atif_converter/domain/session_events.py:89` | 2026-09-27 |
| `CLAUDE_ATTACHMENT_TYPES` | `packages/atif-converter/src/atif_converter/domain/session_events.py:93` | 2026-09-27 |
| `CLAUDE_WHOLE_RECORD_TYPES` | `packages/atif-converter/src/atif_converter/domain/session_events.py:96` | 2026-09-27 |
| `CODEX_WHOLE_RECORD_TYPES` | `packages/atif-converter/src/atif_converter/domain/session_events.py:124` | 2026-09-27 |
| `CODEX_EVENT_MSG_TYPES` | `packages/atif-converter/src/atif_converter/domain/session_events.py:126` | 2026-09-27 |
| `REPORTED_COST_SOURCE_KEY` | `packages/atif-converter/src/atif_converter/domain/session_events.py:339` | 2026-09-27 |
| `REPORTED_UNKNOWN_MODEL_COST_KEY` | `packages/atif-converter/src/atif_converter/domain/session_events.py:340` | 2026-09-27 |
| `REPORTED_COST_SOURCE_COST_STATE` | `packages/atif-converter/src/atif_converter/domain/session_events.py:342` | 2026-09-27 |
| `read_codex_rollout` | `packages/atif-converter/src/atif_converter/infrastructure/codex_converter.py:39` | 2026-09-11 |
| `ARCHIVE_ZSTD_LEVEL` | `packages/atif-converter/src/atif_converter/infrastructure/source_archive.py:49` | 2026-09-27 |
| `ArchivedSourceFile` | `packages/atif-converter/src/atif_converter/infrastructure/source_archive.py:63` | 2026-09-27 |
| `Compressed` | `packages/atif-corpus/src/atif_corpus/infrastructure/compress_artifacts.py:57` | 2026-09-28 |
| `Requires` | `packages/atif-duck/src/atif_duck/domain/examples.py:49` | 2026-08-28 |
| `Category` | `packages/atif-duck/src/atif_duck/domain/examples.py:54` | 2026-08-28 |
| `IDENTITY_COLUMNS` | `packages/atif-duck/src/atif_duck/domain/lake.py:102` | 2026-09-28 |
| `COLLISION_PREFIX` | `packages/atif-duck/src/atif_duck/domain/lake.py:110` | 2026-09-28 |
| `INFO_SCHEMA_VERSION` | `packages/atif-duck/src/atif_duck/domain/lake.py:113` | 2026-09-28 |
| `INFO_SCHEMA_DIGEST` | `packages/atif-duck/src/atif_duck/domain/lake.py:114` | 2026-09-28 |
| `INFO_COLUMNAR_SCHEMA` | `packages/atif-duck/src/atif_duck/domain/lake.py:115` | 2026-09-28 |
| `LAKE_INFO_COLUMNS` | `packages/atif-duck/src/atif_duck/domain/lake.py:121` | 2026-09-28 |
| `CORPORA_COLUMNS` | `packages/atif-duck/src/atif_duck/domain/lake.py:122` | 2026-09-28 |
| `DUCKLAKE_EXTENSION` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:113` | 2026-09-28 |
| `CLEANUP_GRACE_HOURS` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:142` | 2026-09-28 |
| `LakeExtensionMissingError` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:169` | 2026-09-28 |
| `ducklake_extension_installed` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:291` | 2026-09-28 |
| `RebuildReport` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:710` | 2026-09-28 |
| `LakeMismatch` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:1071` | 2026-09-28 |
| `VerifyReport` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:1082` | 2026-09-28 |
| `LakeStatus` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:1184` | 2026-09-28 |
| `CompactReport` | `packages/atif-duck/src/atif_duck/infrastructure/lake.py:1282` | 2026-09-28 |
| `DEFAULT_READER_MEMORY_BYTES` | `packages/atif-duck/src/atif_duck/infrastructure/lake_sessions.py:57` | 2026-09-28 |
| `STEPS_TABLE` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:52` | 2026-09-28 |
| `STEP_TEXTS_SQL` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:67` | 2026-09-28 |
| `CHANGED_STEP_TEXTS_SQL` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:75` | 2026-09-28 |
| `STEP_KEYS_SQL` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:84` | 2026-09-28 |
| `REGISTERED_AT_SQL` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:90` | 2026-09-28 |
| `SNAPSHOT_RANGE_SQL` | `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py:95` | 2026-09-28 |
| `StepMemberAccess` | `packages/atif-duck/src/atif_duck/infrastructure/projections.py:35` | 2026-09-12 |
| `is_compressed` | `packages/atif-duck/src/atif_duck/infrastructure/stored_artifacts.py:38` | 2026-09-28 |
| `SqlFragment` | `packages/atif-embed/src/atif_embed/domain/sql_literal.py:27` | 2026-09-12 |
| `TEXT_ONLY_CONFIG` | `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:60` | 2026-10-09 |
| `INSTALL_HINT` | `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:68` | 2026-10-09 |
| `pick_device` | `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:121` | 2026-10-09 |
| `pick_dtype` | `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:133` | 2026-10-09 |
| `watermark_position` | `packages/atif-embed/src/atif_embed/infrastructure/lake_text_rows.py:79` | 2026-09-28 |
| `write_schema_version` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:159` | 2026-08-28 |
| `migrate_pre_stamp_table` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166` | 2026-08-28 |
| `add_chunk` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:242` | 2026-08-28 |
| `ensure_index` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:254` | 2026-08-28 |
| `optimize_if_needed` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:285` | 2026-08-28 |
| `table_identity` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:309` | 2026-08-28 |
| `PROVIDER_DIMENSIONS` | `packages/atif-embed/src/atif_embed/infrastructure/settings.py:30` | 2026-10-09 |
| `DEFAULT_DIMENSION` | `packages/atif-embed/src/atif_embed/infrastructure/settings.py:36` | 2026-10-09 |
| `STORE_DIRNAME` | `packages/atif-embed/src/atif_embed/infrastructure/settings.py:40` | 2026-10-09 |
| `DomainError` | `packages/atif-models/src/atif_models/domain/ports.py:39` | 2026-08-28 |
| `ReasoningEffort` | `packages/atif-models/src/atif_models/domain/registry.py:42` | 2026-08-28 |

Intra-module use sites, one per file, all opened and read. Where a symbol is used
more than once, the citation carries the first site and the remaining line
numbers follow it as plain numbers.

- `session_kind` is called at
  `packages/atif-analytics/src/atif_analytics/domain/authorship.py:153`. The
  other `session_kind` matches are a function of the same name in
  `transcript.py` and a reader method.
- `CHARS_PER_TOKEN` is the default `chars_per_token` argument of `tokens_for_chars` at
  `packages/atif-analytics/src/atif_analytics/domain/costs.py:28`; the dry-run plans pass the resolved
  `ModelSpec.chars_per_token` instead, so the fallback applies only to a caller that omits it.
- The `*_DIRNAME` constants and `STATE_DB_FILENAME` are each joined onto
  `analytics_dir` by one property in
  `packages/atif-analytics/src/atif_analytics/domain/layout.py:55-83`, in
  declaration order.
- `TOOL_INPUT_PREVIEW_CHARS` is a default argument at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:101`;
  `UUID_HEADER_ESCAPE` is the substitution at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:91`;
  `main_chain` is called at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:282`. The
  `main_chain` in
  `packages/atif-duck/tests/test_result_views.py:86` is a local variable.
- `parse_trajectory_step` is called at
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:246`,
  inside `read_trajectory_steps`, which a reader method calls at `:303`;
  `read_edges_uuids` is called at `:311`.
- `is_sharded_dir` is called at
  `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:97`;
  the module functions `write_part` and `read_all` are what the `ParquetCache`
  methods of the same names call, at `:162` and `:166`.
- `MAX_ATTEMPTS_DEFAULT` is a default argument at several sites, first at
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:150`
  and again on lines 181, 269, and 275 of that file.
- `StorageLayout` is the return type of `storage_layout` at
  `packages/atif-cli/src/atif_cli/corpus.py:125` and is constructed at `:143`.
- `LaneStatus` annotates `packages/atif-cli/src/atif_cli/cron.py:234` and is
  constructed at `packages/atif-cli/src/atif_cli/cron.py:245`.
- `DuckLakeSteps` is the return type of `lake_steps_port` at
  `packages/atif-cli/src/atif_cli/embed_lake.py:89` and is constructed at `:95`.
- `TURNS_BATCH_SIZE` and `STEPS_BATCH_SIZE` are default arguments at
  `packages/atif-cli/src/atif_cli/lake_sessions.py:96` and `:97`.
- `emit_row_batches` is the shared writer both public emitters call, at
  `packages/atif-cli/src/atif_cli/output.py:155` and again on line 178 of that
  file.
- `CODEX_TOOL_IMAGE_MEDIA_TYPES` is the membership test at
  `packages/atif-converter/src/atif_converter/domain/blobs.py:344`, inside
  `codex_tool_image_media_type`, which is called at `:363`.
- `codex_edge_from_record` is called at
  `packages/atif-converter/src/atif_converter/domain/codex_edges.py:155`.
- `COMMAND_TOOL_NAME` names a nested command call at
  `packages/atif-converter/src/atif_converter/domain/codex_nested_calls.py:105`,
  and `VIA_EXEC` is written into the call's `extra` at `:110`.
- In `packages/atif-converter/src/atif_converter/domain/session_events.py`,
  `PAYLOAD_LIST_MAX_ITEMS` caps a payload list at `:142` and again on line 144;
  `CLAUDE_SYSTEM_SUBTYPES`, `CLAUDE_ATTACHMENT_TYPES`, and
  `CLAUDE_WHOLE_RECORD_TYPES` are membership tests at `:184`, `:189`, and
  `:200`; `CODEX_WHOLE_RECORD_TYPES` and `CODEX_EVENT_MSG_TYPES` are membership
  tests at `:285` and `:290`; `REPORTED_COST_SOURCE_KEY` and
  `REPORTED_COST_SOURCE_COST_STATE` make up the dict entry at `:367`, and
  `REPORTED_UNKNOWN_MODEL_COST_KEY` is the key written at `:371`.
- `read_codex_rollout` is called at
  `packages/atif-converter/src/atif_converter/infrastructure/codex_converter.py:66`.
- `ARCHIVE_ZSTD_LEVEL` sets the compressor level at
  `packages/atif-converter/src/atif_converter/infrastructure/source_archive.py:112`;
  `ArchivedSourceFile` annotates `:86` and is constructed at `:121`.
- `Compressed` is the return type of `measure_compressed` at
  `packages/atif-corpus/src/atif_corpus/infrastructure/compress_artifacts.py:114`
  and of `compress_in_place` on line 121, and is constructed at `:118` and
  again on line 152.
- `Requires` feeds `get_args` at
  `packages/atif-duck/src/atif_duck/domain/examples.py:56` and annotates `:124`;
  `Category` feeds `get_args` at `:57` and annotates `:125`.
- In `packages/atif-duck/src/atif_duck/domain/lake.py`, `IDENTITY_COLUMNS`
  leads a table's column list at `:175` and `COLLISION_PREFIX` renames a
  colliding column at `:187`; `LAKE_INFO_COLUMNS` and `CORPORA_COLUMNS` render
  the bootstrap DDL at `:268` and `:272`; `INFO_SCHEMA_VERSION`,
  `INFO_SCHEMA_DIGEST`, and `INFO_COLUMNAR_SCHEMA` are the keys of
  `expected_lake_info` at `:382-384`.
- In `packages/atif-duck/src/atif_duck/infrastructure/lake.py`,
  `DUCKLAKE_EXTENSION` is bound into the install probe at `:294`;
  `ducklake_extension_installed` is called at `:305`;
  `LakeExtensionMissingError` is raised at `:319`; `CLEANUP_GRACE_HOURS` is
  interpolated into the cleanup call at `:1330`; `RebuildReport`,
  `VerifyReport`, `LakeStatus`, and `CompactReport` are return types at `:766`,
  `:1109`, `:1222`, and `:1305`; `LakeMismatch` annotates `:1087`.
- `DEFAULT_READER_MEMORY_BYTES` is the reader's fallback memory limit at
  `packages/atif-duck/src/atif_duck/infrastructure/lake_sessions.py:103`.
- In `packages/atif-duck/src/atif_duck/infrastructure/lake_steps.py`,
  `STEPS_TABLE` is interpolated at `:60`; `STEP_TEXTS_SQL`,
  `CHANGED_STEP_TEXTS_SQL`, `STEP_KEYS_SQL`, `REGISTERED_AT_SQL`, and
  `SNAPSHOT_RANGE_SQL` are executed at `:128`, `:133`, `:149`, `:200`, and
  `:201`.
- `StepMemberAccess` is constructed at
  `packages/atif-duck/src/atif_duck/infrastructure/projections.py:69` and
  annotates `:88`.
- `is_compressed` is called at
  `packages/atif-duck/src/atif_duck/infrastructure/stored_artifacts.py:55`.
- `SqlFragment` is the return type of `sql_literal` at
  `packages/atif-embed/src/atif_embed/domain/sql_literal.py:30` and is
  constructed at `:46`. The `SqlFragment` imported across atif-duck is
  atif-duck's own twin.
- In `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py`,
  `TEXT_ONLY_CONFIG` is copied into the model's config arguments at `:167`;
  `INSTALL_HINT` ends the missing-dependency message at `:117`; `pick_device`
  and `pick_dtype` are called at `:153` and `:154`.
- `watermark_position` is called at
  `packages/atif-embed/src/atif_embed/infrastructure/lake_text_rows.py:149`.
- `write_schema_version` is called at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:185` and
  again on lines 209 and 212; `migrate_pre_stamp_table` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:205`; the module
  functions `table_identity`, `add_chunk`, `optimize_if_needed`, and
  `ensure_index` are what the `LanceVectorStore` methods call, at `:396`,
  `:408`, `:412`, and `:419`.
- `PROVIDER_DIMENSIONS` is read at
  `packages/atif-embed/src/atif_embed/infrastructure/settings.py:99`,
  `DEFAULT_DIMENSION` at `:104` and again on line 113, and `STORE_DIRNAME` at
  `:137`.
- `DomainError` is the base class of `RefusalError` at
  `packages/atif-models/src/atif_models/domain/ports.py:43` and of
  `ProviderUnavailable` at `:51`.
- `ReasoningEffort` types `DEFAULT_REASONING_EFFORT` at
  `packages/atif-models/src/atif_models/domain/registry.py:45` and a model field
  at `packages/atif-models/src/atif_models/domain/registry.py:80`.

Further public-by-naming symbols have no outside reference and are **not**
listed, because their modules make no export claim about them. Most live in
modules that declare no `__all__` at all, so they're module-internal helpers:

- `packages/atif-converter/src/atif_converter/domain/atif/content.py:21`
  (`AudioMediaType`) and
  `packages/atif-converter/src/atif_converter/domain/atif/trajectory_validator.py:236`
  (`main`), both in the vendored harbor tree.
- `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:78`
  (`SCHEMA_VERSION`),
  `packages/atif-converter/src/atif_converter/domain/edges.py:60`
  (`edge_from_record`),
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:110`
  (`AnyFidelityGap`), and
  `packages/atif-converter/src/atif_converter/domain/pricing.py:88`
  (`TABLE_FILENAME`).
- `packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:46`
  (`staged_side_name`) and
  `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:58`
  (`require_transcript_file`).
- `packages/atif-corpus/src/atif_corpus/application/materialize.py:210`
  (`MaterializationFailure`), `:321` (`DEFAULT_SINK_BATCH_SIZE`), and `:1414`
  (`PassPreview`).
- `packages/atif-corpus/src/atif_corpus/domain/layout.py:25`
  (`WATERMARK_FILENAME`), `:27` (`EMPTY_SESSIONS_FILENAME`), and `:30`
  (`SINK_PENDING_FILENAME`), and
  `packages/atif-corpus/src/atif_corpus/domain/watermark.py:26`
  (`SourceDelta`).
- `packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:36`
  (`COMPACT_SEPARATORS`),
  `packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:60`
  (`WATCHED_SIDE_FILE_GLOBS`), and
  `packages/atif-corpus/src/atif_corpus/infrastructure/source_archive.py:51`
  (`ARCHIVE_CODEC`) with `:122` (`read_manifest`).

The rest sit in modules that do declare an `__all__` and are deliberately
absent from it, which is the module saying they aren't exported:
`packages/atif-cli/src/atif_cli/app.py:2201` (`search_rows`),
`packages/atif-cli/src/atif_cli/corpus.py:71` (`SessionLayout`) and `:158`
(`CorpusSlimReport`),
`packages/atif-duck/src/atif_duck/infrastructure/lake.py:117`
(`DEFAULT_AGENT`), and
`packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:77`
(`DistanceMetric`).

## Unreferenced files

`_none_`

The import graph reports some source modules with no inbound first-party
import. All of them are framework-dispatched and none is removable, so the bucket is
empty:

- **Package `__init__.py` files.** The interpreter executes a package's
  `__init__.py` on *any* submodule import, so
  `import atif_analytics.domain.costs` runs
  `packages/atif-analytics/src/atif_analytics/__init__.py`. That inbound edge is
  structural and invisible to an import-statement graph; deleting the file breaks
  every import of the package.
- **The module entry point**, dispatched by the interpreter's `-m` switch.
  `packages/atif-cli/src/atif_cli/__main__.py:3` names itself the
  `python -m atif_cli` entry and
  `packages/atif-cli/src/atif_cli/__main__.py:5-8` calls `atif_cli.app.main`. It
  is exercised in-repo by a subprocess test at
  `packages/atif-cli/tests/test_integration.py:166`.

## Dead imports

`__init__.py` files are the only linted files where `F401` is off
(`pyproject.toml:287`), so outside the vendored harbor tree, which ruff skips,
they're the only place an import with no consumer survives lint (an isolated ruff run of
`F401`, `F811`, and `F841` over the vendored tree finds none). Two members'
`__init__.py` files re-export names. atif-converter's vendored
`domain/atif/__init__.py` re-exports harbor's model classes; the converter
imports them through it
(`packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:66`),
and `packages/atif-converter/tests/test_vendored_atif.py:112` pins every name
against upstream. atif-duck's three `__init__.py` files hold the rest, and
**every one of those re-exports is consumed by nothing** in the repo. Each name
is listed in its file's `__all__`, which is what keeps the statement alive to
the linter; no tracked file imports any of them through a package `__init__`.
The members aren't published on their own, but the one atif-sql wheel ships
`atif_duck` as an importable package (`pyproject.toml:99`), so an installed copy
can reach the facade although nothing in the repo does.

| Path | Symbol | Imported from |
| --- | --- | --- |
| `packages/atif-duck/src/atif_duck/__init__.py:18` | `DEFAULT_PRICING` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:19` | `MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:20` | `MACRO_SIGNATURES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:21` | `VIEW_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:22` | `VIEW_SCHEMA` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:25` | `RawSources` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:26` | `register` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:27` | `register_macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:28` | `register_raw` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:29` | `register_views` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:12` | `DEFAULT_PRICING` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:13` | `DESCRIPTIONS` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:14` | `MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:15` | `MACRO_SIGNATURES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:16` | `TABLE_MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:17` | `VIEW_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:18` | `VIEW_SCHEMA` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:20` | `ExampleQuery` | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:20` | `build_examples` | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:11` | `ColumnarArtifactProducer` | `packages/atif-duck/src/atif_duck/infrastructure/columnar.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:12` | `ColumnarCoverage` | `packages/atif-duck/src/atif_duck/infrastructure/columnar.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:13` | `columnar_coverage` | `packages/atif-duck/src/atif_duck/infrastructure/columnar.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:14` | `session_has_columnar` | `packages/atif-duck/src/atif_duck/infrastructure/columnar.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:17` | `RawSources` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:18` | `register` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:19` | `register_macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:20` | `register_raw` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:21` | `register_views` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |

The absence is checked against the exact import form a consumer would have to
write, over every tracked file:

- `from atif_duck import ...`: no site.
- `from atif_duck.domain import ...`: only sites that import a **submodule**
  (`artifacts`, `authorship`, `lake`), never one of the re-exported names:
  `packages/atif-duck/src/atif_duck/domain/columnar.py:38` and
  `packages/atif-cli/tests/test_artifact_names_twin_pin.py:15`, for example.
- `from atif_duck.infrastructure import ...`: only sites that import a
  **submodule** (`analytics`, `lake`, `registry`), never one of the re-exported
  names: `packages/atif-duck/tests/test_analytics_views.py:24` and
  `packages/atif-duck/tests/test_examples.py:42`, for example.
- No `import atif_duck` followed by attribute access. The one test that reads
  an `__all__` (`grep -rn '__all__' packages/*/tests scripts`) is
  `packages/atif-converter/tests/test_vendored_atif.py:113`, and it reads the
  converter's vendored package, so atif-duck's `__all__` membership is not
  itself pinned.

Every real consumer bypasses the facade for the deep module:

- `packages/atif-cli/src/atif_cli/app.py:1459` and
  `packages/atif-cli/src/atif_cli/app.py:2086` import `register` from
  `atif_duck.infrastructure.registry`.
- `packages/atif-cli/src/atif_cli/app.py:2270`:
  `from atif_duck.domain.examples import (...)`.
- `packages/atif-cli/src/atif_cli/app.py:2352`:
  `from atif_duck.domain.catalog import (...)`, which takes `MACRO_SIGNATURES`
  and `VIEW_SCHEMA` with their analytics counterparts.
- `packages/atif-cli/src/atif_cli/duck_errors.py:27`,
  `packages/atif-cli/tests/test_app.py:136`, and
  `packages/atif-cli/tests/test_app.py:931`.

The one dynamic reference to the package,
`packages/atif-cli/tests/test_lean_import.py:21`, names
`"atif_duck.infrastructure"` inside the `_FORBIDDEN_EAGER_IMPORTS` tuple at
`packages/atif-cli/tests/test_lean_import.py:17-38`, an assertion that the
module must **not** load on `import atif_cli.app`, which is the opposite of a
consumer.

Each `__init__.py` presents the facade as intentional:
`packages/atif-duck/src/atif_duck/__init__.py:6` documents
`register(con, corpus_root)` as the package entry point. Read against
`pyproject.toml:17` (one distribution named `atif-sql`, bundling every module
tree in a single wheel rather than shipping any of them as an install target),
the facade's only possible consumer is code outside the repo that imports the
installed wheel.

## See also

- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [impact analysis](../insights/impact-analysis.md)
- [business logic](../insights/business-logic.md)
- [contract map](../insights/contract-map.md)
