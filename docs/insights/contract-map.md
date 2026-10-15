# atif-sql · Contract map

**What counts as a contract here.** This workspace has almost no shared-type imports to trace,
because `pyproject.toml:595-604` forbids most of them: the independent members may never import
each other, and atif-analytics may import only atif-models. What crosses a module boundary instead
is a **shape agreed by two packages that cannot reference one another's symbols**: a file layout, a
JSON key order, a column projection, a `typing.Protocol` typed to a document rather than to an
implementation. So a contract in this file is any of:

1. a `typing.Protocol` declared in one package's `domain/ports.py` and satisfied by an adapter
   elsewhere (each has a section below or a bullet under "Other contracts");
2. a shape declared **twice or more** in packages that cannot import each other, where the two
   declarations must agree or a query silently returns wrong rows;
3. a build-enforced dependency rule (import-linter), which is a stronger fact than a convention;
4. an upstream API this workspace depends on and pins;
5. a version pin between workspace members, which the release tooling rewrites and checks on
   every bump.

**Every Protocol here is satisfied STRUCTURALLY, with no import in either direction.** That is the
single most important structural fact for reading this file. `RealConverter`
(`packages/atif-cli/src/atif_cli/converter_adapter.py:80`) implements `ConverterPort`
(`packages/atif-corpus/src/atif_corpus/domain/ports.py:101`) by shape alone; it names the
Protocol only in its docstring, and the Protocol can't name it back, because atif-corpus may never
import atif-converter. The Protocol name appears at only these import sites in the whole workspace:
`packages/atif-corpus/src/atif_corpus/application/materialize.py:172` (the module that CALLS it),
`packages/atif-cli/tests/test_converter_adapter.py:17`, and
`packages/atif-cli/tests/test_codex_cli.py:26` (the tests that bind the two together in an
annotation). So each Protocol row below has a producer of the shape and an implementer that never
imports it; the type system meets the two only at a typed site (an annotated test binding, a
factory's annotated return, a call into a port-typed parameter), and the answer to "who finds out
if the shape drifts" is one of those sites or nobody. Do not read a Protocol row as a dependency edge: `pyproject.toml:595-598` forbids the edge
it would imply.

Every contract below names its producer, its consumers, the verbatim shape, the assumptions
consumers make beyond the shape, and the drift risk. **Consumer counts are grep-derived and
confirmed at each import, annotation, or call site**: this repo has no code index
(`.gitignore:25` lists `.codegraph/`, and no index exists on disk), so no count here comes from a
symbol graph. These grep hazards shape every count: names collide (`DomainError` and
`EmbeddingProviderMismatch` across packages, `cached_tokens` across coordinate spaces), so every
attribution here is by module path and never by bare name; and outside four atif-cli modules
(listed under the import-linter contracts) every cross-package import under `src/` is indented
inside a function body or a `TYPE_CHECKING` block, so a line-anchored grep of `src/` finds almost
nothing.

The sections below aren't ranked; "Other contracts" at the end collects the smaller ones.

## The materialized corpus artifact layout

**Producer:** `packages/atif-corpus/src/atif_corpus/domain/layout.py:18-24`

**Consumers:**

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:697-701`: builds the `meta.json`
  glob (and, further down, each session's stored-file list) and binds it as the one parameter of
  its `read_json` readers, spelling the names from its own constants at
  `packages/atif-duck/src/atif_duck/domain/artifacts.py:28-36`.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:155-173`: gates on
  `meta.json` and opens each stored trajectory with its own DuckDB connection.
- `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:72-74`: declares
  `TRAJECTORY_FILENAME` / `EDGES_FILENAME` / `META_FILENAME` again as its own constants.
- `packages/atif-corpus/src/atif_corpus/application/materialize.py:691-731`: the writer side, the
  only place the artifacts are produced.
- `packages/atif-cli/tests/test_artifact_names_twin_pin.py:29`: pins the writer's names against
  every reader's copy, and `:19` pins the `.zst` suffix.
- `docs/CONTRACT.md:22-60`: the hand-written specification all of them agree to.

**Shape:**

```text
<corpus_root>/                     # default: ~/.atif-sql/corpus/<corpus-slug>/
  sessions/<session_id>/
    trajectory.json.zst            # compact JSON (separators=(',',':')), ATIF-v1.7, one zstd frame
    loss_report.json               # atif_converter LossReport.to_json()
    edges.jsonl.zst                # one line per RAW record: {uuid, parent_uuid,
                                   #  message_id, type, ts, is_sidechain,
                                   #  is_compact_summary, source_file, tool_use_ids: [..]}
    session_events.jsonl.zst       # one line per KEPT non-message record; may be empty
    meta.json                      # {session_id, source_mtime_ns, source_files: [...],
                                   #  harbor_version, converter_version, converter_schema,
                                   #  materialized_at, agent, source_present,
                                   #  source_removed_at?, source_archive?,
                                   #  columnar_schema? (old layout)}
    source/<path>.zst              # raw source archive: one zstd file per source file
  blobs/sha256/<ab>/<sha256>.<ext> # inline attachments, content-addressed, written 0444
  watermark.json                   # {path: mtime_ns} across source corpus
  empty_sessions.json              # sessions whose transcript held nothing to convert
  sink_pending.json                # sessions the SessionSink hasn't taken yet
```

A corpus written by an older build keeps the plain `trajectory.json`, `edges.jsonl`, and
`session_events.jsonl`, plus five per-session parquet files, until `atif-sql corpus slim` converts
it, and every reader accepts either spelling. The parquet files are a cache of what the views
compute from the trajectory, never a source of truth, and materialize doesn't write them: the CLI
passes no artifact producer (`packages/atif-cli/src/atif_cli/app.py:975-977`), and the lake loader
runs atif-duck's `ColumnarArtifactProducer` to stage each session it loads
(`packages/atif-duck/src/atif_duck/infrastructure/lake.py:561`). atif-duck reads a session's own
parquet files only when `meta.columnar_schema` matches its version and all five are present and
non-empty (`packages/atif-duck/src/atif_duck/infrastructure/columnar.py:116`), and falls back to
the trajectory per session otherwise. The names and the schema version live in
`packages/atif-duck/src/atif_duck/domain/columnar.py`; atif-corpus never learns them, and it merges
whatever keys an `ArtifactProducer` returns into `meta.json`.

**Assumptions consumers make:**

- **`meta.json` present means the session dir is complete.** Every reader implements the same
  torn-set gate independently: `packages/atif-duck/src/atif_duck/infrastructure/registry.py:703-709` restricts the edges and loss readers to
  dirs where `v_raw_meta` has a row (the trajectory list is built from `v_raw_meta` too); `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:164-169` skips a dir with no `meta.json`;
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:178-191` does the same and logs a warning. Nothing in the file layout expresses
  this: it's a write-ORDER promise made at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:635-638`.
- **No reader ever sees a partially written directory**, because the writer stages every
  per-session artifact under `<corpus_root>/.staging/` and swaps the whole dir into place
  (`packages/atif-corpus/src/atif_corpus/application/materialize.py:656` and `:733`), and `.staging` is deliberately outside `sessions/` so a DuckDB glob
  can't reach it (`packages/atif-corpus/src/atif_corpus/domain/layout.py:89-94`). A `SIGKILL` or power loss inside the swap leaves the session missing rather than torn.
- **`trajectory.json` is ONE JSON document, `edges.jsonl` is newline-delimited.** atif-duck encodes
  that split in its reader choice at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:25-28`; atif-embed re-derives it at
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:88`.
- **A single trajectory document can be enormous.** Both DuckDB readers allow a 1 GiB
  `maximum_object_size`, and their measured ceilings disagree: atif-duck caps a bound it sizes from the largest file at 1 GiB, citing 436 MB
  observed (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:217-220`), while atif-embed sets 1 GiB flat, citing 85 MB (`packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:56-58`). Same ceiling, two independent
  justifications.
- **`source_mtime_ns` and every `watermark.json` value are epoch NANOSECONDS** from
  `os.stat().st_mtime_ns` (unit stated in the identifier and again at
  `packages/atif-corpus/src/atif_corpus/domain/watermark.py:11-12`). atif-duck types the column
  `BIGINT` at `packages/atif-duck/src/atif_duck/domain/raw_readers.py:101`, which preserves the magnitude but drops the unit from the schema.
- **`materialized_at` is an ISO-8601 UTC string, not a timestamp.** atif-duck projects it as
  `VARCHAR` (`packages/atif-duck/src/atif_duck/domain/raw_readers.py:105`); the CLI supplies it (`packages/atif-cli/src/atif_cli/app.py:963`).

**The hand-written contract and the manifests split the job.** Its CLI heading
(`docs/CONTRACT.md:300`) names atif-cli the package that composes the others. The atif-cli
manifest declares five siblings (`packages/atif-cli/pyproject.toml:32-36`) and the CLI imports
every one of them; atif-models reaches it through atif-analytics, which imports atif-models
(`packages/atif-analytics/pyproject.toml:23`) as the import-linter contracts permit.
`docs/CONTRACT.md:17-19` leaves VSS/`semantic_search` and the analytics pipelines to
`docs/CONTRACT-V2.md` rather than ruling them out; both are shipped commands (the `atif-analytics`
and `atif-embed` pins at `packages/atif-cli/pyproject.toml:32` and `:36`, plus the `analyze`,
`embed`, and `search` commands at `packages/atif-cli/src/atif_cli/app.py:1634`, `:1801`, and
`:2018`). Read `docs/CONTRACT.md:22-60` as authoritative for the layout (that's the part the
packages actually implement) and the manifests as authoritative for who imports whom.

**Drift risk:** a new artifact, a renamed file, or a new `meta.json` key must be applied in every
place above, and most of them are reader-side, so an addition silently reaches nobody. The file
names and the `.zst` suffix are pinned across all four packages by
`packages/atif-cli/tests/test_artifact_names_twin_pin.py:29` and `:19`; a new `meta.json` key has no such
pin. Mitigation: the writer-side constants at
`packages/atif-corpus/src/atif_corpus/domain/layout.py:18-24` are the single source of truth on the
write side. Any layout change starts there and then greps the reader modules named above.

## The static SQL catalog

**Producer:** `packages/atif-duck/src/atif_duck/domain/catalog.py:30` (`VIEW_NAMES`), with
`VIEW_SCHEMA:59`, `MACRO_NAMES:324`, `MACRO_SIGNATURES:343`, `ANALYTICS_VIEW_NAMES:370`,
`ANALYTICS_VIEW_SCHEMA:384`, `ANALYTICS_MACRO_SIGNATURES:453`, `TABLE_MACRO_NAMES:469`,
`DESCRIPTIONS:487`

**Consumers:**

- `packages/atif-cli/src/atif_cli/app.py:2335-2387`: the `schema` command reads `VIEW_SCHEMA`,
  `MACRO_SIGNATURES`, `ANALYTICS_VIEW_SCHEMA`, and `ANALYTICS_MACRO_SIGNATURES`, tags each object
  with its `requires` value, and answers with no DuckDB connection.
- `packages/atif-duck/src/atif_duck/domain/examples.py:35-44`: the examples generator imports every
  catalog and derives one runnable query per object.
- `packages/atif-cli/src/atif_cli/app.py:2294`: the `examples` command calls
  `build_examples()`.
- `packages/atif-duck/tests/test_duck_views.py:51` and `:67`: the `DESCRIBE`-vs-`VIEW_SCHEMA` and
  DDL-vs-`MACRO_SIGNATURES` drift tests.
- `packages/atif-duck/tests/test_examples.py:208`, `:218`, and `:227`: `DESCRIPTIONS` coverage,
  `ARG_EXEMPLARS` coverage, and the `AS TABLE` set drift test.
- `packages/atif-duck/tests/test_analytics_views.py:260` and `:242`: the analytics-side drift
  tests.
- `packages/atif-cli/tests/test_app.py:135`: asserts the CLI's JSON payload keys equal the
  catalog keys.

**Shape:**

```python
VIEW_NAMES: tuple[str, ...] = (
    "sessions",
    "steps",
    "messages",
    ...
)

VIEW_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
    "sessions": (
        ("session_id", "VARCHAR"),
        ("agent", "VARCHAR"),
        ...
    ),
    ...
}

MACRO_SIGNATURES: dict[str, tuple[str, ...]] = {
    "ago": ("interval_text",),
    "model_used": ("sid",),
    ...
}
```

**Assumptions consumers make:**

- **Column ORDER is part of the contract, not just the column set.** The drift test asserts tuple
  equality against `DESCRIBE` output, stated at `packages/atif-duck/src/atif_duck/domain/catalog.py:56-58`; reordering a view's `SELECT`
  list fails CI even though every column still exists.
- **`DESCRIPTIONS` covers every object in every catalog, exactly.** `packages/atif-duck/tests/test_examples.py:208`
  fails on a missing entry AND on a stale entry keyed to an object that no longer exists, so the
  dict is a bijection with the union of the name catalogs.
- **Every macro parameter name has an `ARG_EXEMPLARS` literal**, or example generation raises
  rather than emitting a broken query: `packages/atif-duck/src/atif_duck/domain/examples.py:165-171` fails loud, and
  `packages/atif-duck/tests/test_examples.py:218` pre-empts it.
- **`TABLE_MACRO_NAMES` decides the SQL call shape.** `packages/atif-duck/src/atif_duck/domain/examples.py:172-175` emits
  `SELECT * FROM name(args)` for a member and `SELECT name(args)` otherwise, so a macro whose DDL
  gains or loses `AS TABLE` breaks every derived example, pinned by the regex test at
  `packages/atif-duck/tests/test_examples.py:227`.
- **The catalog is answerable without a corpus.** `packages/atif-cli/src/atif_cli/app.py:2344-2346` states the sub-50 ms,
  no-DuckDB-bind guarantee the `schema` command rests on; a runtime `DESCRIBE` would violate it.
- **The raw readers are deliberately NOT in `VIEW_NAMES`** (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:141-142`), so a
  consumer enumerating `VIEW_NAMES` does not see `v_raw_trajectories` and friends.

**Drift risk:** adding a view or macro without a `DESCRIPTIONS` entry, an `ARG_EXEMPLARS` entry for
each new parameter name, or `TABLE_MACRO_NAMES` membership fails CI loudly: the risk is inverted
here, and the real exposure is a *type* that only the fixture corpus produces. Mitigation:
`VIEW_SCHEMA["message_embeddings"]` at `packages/atif-duck/src/atif_duck/domain/catalog.py:281-287` hardcodes `FLOAT[1024]`, so run the
drift test against a store built at a non-default `output_dimension` before changing that setting.

## The import-linter architecture contracts

**Producer:** `pyproject.toml:543-604`

**Consumers:**

- `mise.toml:250`: `lint:imports` is one of the `mise run check` gates, so the build is
  the consumer.
- `packages/atif-corpus/src/atif_corpus/domain/ports.py:5-7`: the ports module docstring cites
  the independence contract as the reason its Protocols are typed to the contract's shapes.
- `packages/atif-embed/src/atif_embed/domain/ports.py:11-15`: cites it as the reason `TextRowsPort`
  exists.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:5-10`: cites it as the
  reason atif-embed carries its own corpus reader.
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-9`: cites it as the reason the
  guard is duplicated.
- `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:16-18`: cites it as the reason the
  analytics artifact names are pinned by hand.
- `packages/atif-analytics/src/atif_analytics/domain/layout.py:11-13`: cites it as the reason its
  layout is computed in one place.

**Shape:**

```toml
[tool.importlinter]
root_packages = ["atif_converter", "atif_corpus", "atif_duck", "atif_models", "atif_analytics", "atif_embed", "atif_cli"]

[[tool.importlinter.contracts]]
name = "converter / corpus / duck / models / embed are mutually independent (only atif-cli composes; atif-analytics may import atif-models only)"
type = "independence"
modules = ["atif_converter", "atif_corpus", "atif_duck", "atif_models", "atif_embed"]

[[tool.importlinter.contracts]]
name = "atif-analytics imports only atif-models among workspace packages"
type = "forbidden"
source_modules = ["atif_analytics"]
forbidden_modules = ["atif_converter", "atif_corpus", "atif_duck", "atif_embed", "atif_cli"]
```

**Assumptions consumers make:**

- **atif-cli is the only composition root**, and it is absent from both the `independence` module
  list (`pyproject.toml:598`) and the `forbidden` source list (`pyproject.toml:603`). The one module that imports
  two independent packages at module scope is `packages/atif-cli/src/atif_cli/converter_adapter.py:64-74`
  (`app.py` imports them inside command bodies), and its
  docstring names that privilege explicitly at `packages/atif-cli/src/atif_cli/converter_adapter.py:5-9`.
- **atif-analytics is absent from the independence list on purpose** so it can compose atif-models,
  with the `forbidden` contract pinning its other edges shut; the reasoning is inline at
  `pyproject.toml:590-594`. Verified: `grep -rn 'atif_duck' packages/atif-analytics/` finds two
  prose mentions of the authorship twin and no import, so the CONTRACT-V2-era design of reading atif-duck's views is not what the code does.
- **atif-duck declares no `layers` contract.** Five members do (`pyproject.toml:546-588`), atif-models
  among them with just `infrastructure > domain`; atif-duck has the same two layers, `domain/` and
  `infrastructure/`, and no contract orders them, so its `domain/` staying free of infrastructure
  imports is a convention.
- **Every layered member's `domain/` is the innermost layer**, which is why every port Protocol lives
  in `domain/ports.py` and none in an `application/ports.py`: no such file exists in this
  workspace.
- **The comment at `pyproject.toml:590-594` grants a permission that atif-cli doesn't exercise.**
  It leaves atif-cli and atif-analytics free to import atif-models, and atif-cli neither declares
  atif-models in `packages/atif-cli/pyproject.toml:32-36` nor imports it from `src/`:
  `grep -rn 'atif_models' packages/atif-cli/` finds one test import
  (`packages/atif-cli/tests/test_analyze_lake.py:44`). The live atif-models edge is
  atif-analytics' alone, its import sites led by
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:32-33`.
- **Every cross-package import under `src/` is INDENTED** (inside a function body or a
  `TYPE_CHECKING` block) because `PLC0415` is ignored workspace-wide to satisfy the lean-import
  test (`pyproject.toml:220`). The exceptions are module-scope imports in atif-cli
  modules that are themselves only ever imported inside a command body:
  `packages/atif-cli/src/atif_cli/converter_adapter.py:64-74`,
  `packages/atif-cli/src/atif_cli/duck_errors.py:27`,
  `packages/atif-cli/src/atif_cli/lake_sessions.py:43-51`, and
  `packages/atif-cli/src/atif_cli/embed_lake.py:19`. A line-anchored grep of `src/` for another
  package's `^from atif_` finds only those four; the atif-cli and atif-analytics test suites do
  import siblings at module scope. That's why every count in this file comes from an unanchored
  grep confirmed at the site.

**Drift risk:** a new workspace member that is not added to `root_packages` (`pyproject.toml:544`) is silently
unchecked: import-linter reports every contract kept while the new package imports whatever it
likes. Mitigation: adding a `packages/*` member means adding it to `root_packages` and giving it
either a `layers` contract or a place in the `independence` list in the same commit.

## `LlmStructuredProvider`: the structured-output port

**Producer:** `packages/atif-models/src/atif_models/domain/ports.py:131`

**Consumers:**

- `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:69` (annotated at
  `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:102`, `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:381`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:83` (`packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:120`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:361`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:81` (`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:214`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:508`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:94` (`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:145`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:368`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:32`: the provider
  factory and the usage/budget plumbing.
- `packages/atif-models/src/atif_models/infrastructure/providers.py:20`: `build_structured_provider`,
  which picks the adapter by `spec.family` and returns it typed as the port (`:20-34`).
- `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:81`: `OpenAiBedrockProvider`,
  the openai-family adapter (the default family).
- `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:130`:
  `AnthropicBedrockProvider`, the anthropic-family adapter.
- `packages/atif-models/tests/test_models_ports.py:29-38`: binds `OpenAiBedrockProvider` to the
  port by annotation, `isinstance`, and a member sweep.
- `packages/atif-analytics/tests/analytics_fixtures.py:300`: the deterministic double.

**Shape:**

```python
@runtime_checkable
class LlmStructuredProvider(Protocol):
    """Port: one structured-output call. One adapter per backend.

    The seam is deliberately narrow — everything provider-specific is
    fixed at adapter construction.
    """

    async def classify_structured(
        self, *, system: str, prompt: str, schema: type[SchemaT]
    ) -> SchemaT: ...
```

**Assumptions consumers make:**

- **The error taxonomy is binary and load-bearing.** `RefusalError` is terminal and
  `ProviderUnavailable` is retryable (`packages/atif-models/src/atif_models/domain/ports.py:43-58`),
  and the pipelines route on exactly that split: a third error type would fall through to neither
  the refusal sidecar nor the retry queue. Each adapter maps its own stop signal onto it: a
  `content_filter` or `refusal` finish reason and Claude's `stop_reason: refusal` raise
  `RefusalError` (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:219-225`,
  `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:253-257`), and a
  truncated response is retried once at one `EFFORT_DEGRADE` step lower before it raises
  `ProviderUnavailable`
  (`packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:44-63`).
- **The Protocol says nothing about token accounting, yet every consumer reads it off the concrete
  provider.** `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:75` and `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:132` both reach for `getattr(provider, "usage", None)` because
  `usage` is not on the port; a conforming adapter without that attribute silently reports zero
  spend and the `RunBudget` ceiling never trips.
- **`CallUsage` counts are PER CALL, and some of them are subsets of others.**
  `reasoning_tokens` is a subset of `output_tokens` and `cached_tokens` a subset of `input_tokens`
  (`packages/atif-models/src/atif_models/domain/ports.py:65-71`), so `estimate_cost` at `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:78-84` prices `input_tokens` and
  `output_tokens` and never adds `reasoning_tokens` or `cached_tokens`, which would double-charge.
  `long_input_tokens` / `long_output_tokens` are subsets too: the accumulator adds a long-context call's tokens to both
  the totals and the long split (`packages/atif-models/src/atif_models/domain/ports.py:107-109`),
  and `estimate_cost` bills the split at the long rates in place of the standard ones
  (`packages/atif-models/src/atif_models/domain/registry.py:202-210`).
- **`input_tokens` is the TOTAL billed input for both families.** The OpenAI adapter maps
  `prompt_tokens` straight across
  (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:74`), while the Anthropic
  `input_tokens` field leaves out cache reads and writes, so the Anthropic adapter adds the three
  together (`packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:84-99`). An adapter
  that copied the provider's field as-is would under-report Claude spend.
- **`UsageAccumulator` must be thread-safe, not task-safe.** The lock is a `threading.Lock`
  (`packages/atif-models/src/atif_models/domain/ports.py:90`) because adapters dispatch blocking `invoke_model` through `anyio.to_thread`,
  reasoning stated at `packages/atif-models/src/atif_models/domain/ports.py:16-22`.
- **The budget is a stop-dispatch trigger, not a cap.** `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:108-115` documents the
  overshoot bound as `max_cost_usd` plus at most `BUDGET_CHECK_BATCH` units in flight; a consumer
  treating `max_cost_usd` as a hard ceiling is wrong.
- **`pricing_in` / `pricing_out` on `ModelSpec` are USD per 1,000,000 tokens**, stated at
  `packages/atif-models/src/atif_models/domain/registry.py:53-54`, and `None` when unknown, which
  `estimate_cost` (`packages/atif-models/src/atif_models/domain/registry.py:180`) turns into a `None` cost rather than a zero.
- **A call over `long_context_threshold` bills WHOLE at the long rates**
  (`packages/atif-models/src/atif_models/domain/registry.py:60-64`). Each adapter marks such a
  call through `is_long_context` before it adds the usage
  (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:209-212`,
  `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:248-251`), so an
  adapter that skipped the mark would price a long call at the standard rate.

- **Neither adapter names the port outside a docstring.** Both import `CallUsage`,
  `ProviderUnavailable`, `RefusalError`, `SchemaT`, and `UsageAccumulator` from the port module and
  not the Protocol (`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:41-47`,
  `packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:61-67`). The static
  link is the factory's annotated return: `packages/atif-models/src/atif_models/infrastructure/providers.py:26`
  declares `LlmStructuredProvider` and `:31` and `:34` return the two concrete adapters, so ty
  and pyright check both at those return statements;
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:36-38` passes the
  port type on as `tuple[LlmStructuredProvider, ModelSpec]`. Only `OpenAiBedrockProvider` has a
  conformance test (`packages/atif-models/tests/test_models_ports.py:29-38`);
  `packages/atif-models/tests/test_anthropic_provider.py:298-302` checks just that the factory
  returns each concrete class.
- **`build_structured_provider` sends every family that isn't `anthropic` to the OpenAI adapter**
  (`packages/atif-models/src/atif_models/infrastructure/providers.py:28-34`). What keeps a
  registry family with no adapter from reaching it is `RUNNABLE_FAMILIES`, which settings load
  checks before any call
  (`packages/atif-models/src/atif_models/infrastructure/settings.py:25` and `:41-54`).

**Drift risk:** the port is `@runtime_checkable`, so an `isinstance` check passes on method names
alone and would accept an adapter with the wrong parameter kinds (positional instead of
keyword-only), and the only `isinstance` against it is the conformance test's
(`packages/atif-models/tests/test_models_ports.py:33`); no production code guards on it.
Mitigation: the keyword-only signature at
`packages/atif-models/src/atif_models/domain/ports.py:138-140` is the contract, and the factory's
annotated return at `packages/atif-models/src/atif_models/infrastructure/providers.py:26` is where
a mismatched adapter fails the typecheck gate.

## The `edges.jsonl` line shape

**Producer:** `packages/atif-converter/src/atif_converter/domain/edges.py:22-32`

**Consumers:**

- `packages/atif-duck/src/atif_duck/domain/raw_readers.py:62-72`: `EDGE_COLUMNS` (the registry's
  `_EDGE_COLUMNS`), the same keys in the same order with DuckDB types.
- `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:249-268`: reads the
  `uuid` field for the conflicts pipeline's returned-uuid validity guard.
- `packages/atif-corpus/src/atif_corpus/application/materialize.py:695-698`: writes the lines and
  owns line termination.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:94`: the `messages` view publishes this
  surface, every key but `tool_use_ids`, plus `session_id`.

**Shape:**

```python
#: Stable key order for one edges.jsonl line (contract-fixed shape).
EDGE_FIELDS: tuple[str, ...] = (
    "uuid",
    "parent_uuid",
    "message_id",
    "type",
    "ts",
    "is_sidechain",
    "is_compact_summary",
    "source_file",
    "tool_use_ids",
)
```

**Assumptions consumers make:**

- **The producer emits lines WITHOUT trailing newlines and the writer adds them.** Stated on the
  port's `edges_lines` field at
  `packages/atif-corpus/src/atif_corpus/domain/ports.py:74-77` and honoured at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:695-698`; a producer that terminated its own lines would double them.
- **`parent_uuid` is a nullable string, and atif-duck declares it `VARCHAR` outright** rather than
  letting JSON union inference decide; the reason is recorded at `packages/atif-duck/src/atif_duck/domain/raw_readers.py:57-61`.
- **`tool_use_ids` means two different things by record type**: tool_use block ids for assistant
  records, the `tool_use_id` of tool_result blocks for user records (`packages/atif-converter/src/atif_converter/domain/edges.py:11-13`). One field,
  two semantics, discriminated by the sibling `type` column.
- **`edges.jsonl` is the only source of raw-record identity.** The trajectory cannot supply uuids
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:70-72`, fidelity gap 7 `UUID_NOT_PRESERVED`), which is why the `messages` view is
  reconstructed from edges rather than from steps.
- **`records_total` in the loss report equals this file's line count.** Stated at
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:123-127` and asserted by
  `packages/atif-converter/tests/test_snapshot_and_drift.py:133`. Both derive from the same
  raw census, so a consumer may cross-check one against the other.

**Drift risk:** the keys are declared twice in packages that cannot import each other, and a
new key added on the producer side is simply invisible to `EDGE_COLUMNS`: a projection reader
drops unknown keys without erroring. Mitigation: `EDGE_FIELDS` is a single tuple; changing it means
editing `packages/atif-duck/src/atif_duck/domain/raw_readers.py:62-72` in the same commit.

## `EmbeddingProvider`, `VectorStorePort`, `TextRowsPort`

**Producer:** `packages/atif-embed/src/atif_embed/domain/ports.py:37`, `packages/atif-embed/src/atif_embed/domain/ports.py:65`, `packages/atif-embed/src/atif_embed/domain/ports.py:93`

**Consumers:**

- `packages/atif-embed/src/atif_embed/application/embed.py:35`: imports every one of them; annotated at
  `packages/atif-embed/src/atif_embed/application/embed.py:46-47` and `packages/atif-embed/src/atif_embed/application/embed.py:65-67`.
- `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:285`: the Cohere
  `EmbeddingProvider` adapter (the default); `packages/atif-embed/src/atif_embed/infrastructure/gemma_local.py:193`
  is the local EmbeddingGemma one, and `packages/atif-embed/src/atif_embed/infrastructure/providers.py:19`
  picks between them.
- `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:376`: the `VectorStorePort`
  adapter.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:199`: the `TextRowsPort`
  adapter over the corpus files; `packages/atif-embed/src/atif_embed/infrastructure/lake_text_rows.py:89`
  wraps it with a lake-backed reader and a watermark.
- `packages/atif-embed/tests/embed_fixtures.py:170`: the deterministic `EmbeddingProvider` double.
- `packages/atif-embed/tests/test_embed_use_case.py:457`: a recording `TextRowsPort` double.

**Shape:**

```python
class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float] | None]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VectorStorePort(Protocol):
    def table_identity(self) -> tuple[str, int] | None: ...
    def get_embedded_hashes(self) -> dict[str, str]: ...
    def delete_uuids(self, uuids: Iterable[str]) -> int: ...
    def add_chunk(self, df: pl.DataFrame) -> None: ...
    def optimize(self) -> None: ...
    def ensure_index(self, *, metric: str = "cosine") -> None: ...


class TextRowsPort(Protocol):
    def iter_unembedded(
        self,
        corpus_root: Path,
        *,
        embedded: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[PendingText]: ...
    @property
    def discovery(self) -> str: ...
    def commit(self, *, stored_rows: int) -> None: ...
```

**Assumptions consumers make:**

- **`embed_documents` returns one slot per input text in input order, and the caller enforces it.**
  `packages/atif-embed/src/atif_embed/application/embed.py:200` zips with `strict=True`, so a provider returning a different length raises
  `ValueError` instead of misattributing vectors to texts.
- **A `None` slot means "not embedded this run", not "failed the run".** The port documents bounded
  loss at `packages/atif-embed/src/atif_embed/domain/ports.py:53-56`; `packages/atif-embed/src/atif_embed/application/embed.py:198-204` filters the `None`s, counts them as skipped, and
  leaves those uuids for the next pass.
- **The store's `(model, dim)` stamp is checked BEFORE any append**, `packages/atif-embed/src/atif_embed/application/embed.py:175-183`, using the
  identity `table_identity()` returns; `None` there means empty and any provider may claim it.
- **`add_chunk` accepts a polars frame with a fixed-size `pl.Array`, not a `pl.List`.**
  The schema is built at `packages/atif-embed/src/atif_embed/application/embed.py:225-244` and the reason is stated at `packages/atif-embed/src/atif_embed/application/embed.py:221-224`: a variable-size
  list is rejected by Lance for indexing.
- **`iter_unembedded`'s laziness bounds only the CALLER's residency.** The port says so explicitly
  at `packages/atif-embed/src/atif_embed/domain/ports.py:117-120`: peak resident text inside an implementation is each adapter's own
  problem, and `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:71-77` establishes it by batching on BYTES (4 MiB) rather than
  on file count.
- **Staleness is decided by hash, not by uuid presence.** A uuid under a different `text_hash` is
  yielded with `replaces_existing=True` (`packages/atif-embed/src/atif_embed/domain/ports.py:112-115`), and the caller must delete before
  appending or the uuid fans out to two vectors
  (`packages/atif-embed/src/atif_embed/application/embed.py:212-219`).
- **The CLI injects only the text-rows port.** `packages/atif-cli/src/atif_cli/app.py:1937-1943` calls
  `run_backfill` with `corpus_root`, `settings`, `text_rows` (`DuckDbTextRows`, wrapped in
  `LakeTextRows` unless `--no-lake` is passed), `limit`, and `dry_run`; the use case constructs the
  default store and embedder itself under deferred imports (`packages/atif-embed/src/atif_embed/application/embed.py:114-117`, `packages/atif-embed/src/atif_embed/application/embed.py:166-169`),
  and the embedder only after the dry-run return, so a dry run never loads boto3 or torch.

- **Each file-path adapter is bound to its Protocol by a test**, as `RealConverter` is to
  `ConverterPort`: `packages/atif-embed/tests/test_embed_ports.py:49`, `:55`, and `:61` assign
  `CohereBedrockEmbedder`, `LanceVectorStore`, and `DuckDbTextRows` to port-annotated names and
  sweep the Protocol's members, and `packages/atif-embed/tests/test_gemma_local.py:146` does the
  same for the Gemma embedder. Inside `run_backfill` each default is assigned to a parameter
  already annotated with the port
  (`packages/atif-embed/src/atif_embed/application/embed.py:114-117` against the
  annotations at `packages/atif-embed/src/atif_embed/application/embed.py:65-67`), and
  `build_embedder` returns both adapters under an `EmbeddingProvider` annotation; ty and pyright
  check those too. `LakeTextRows` has no member sweep: its one static link is a test passing it as
  `run_backfill`'s `text_rows` argument
  (`packages/atif-embed/tests/test_lake_text_rows.py:265`), which pyright checks and ty doesn't,
  because ty ignores argument types under `packages/*/tests/` (`pyproject.toml:443-450`). The
  CLI types its own `text_rows` as `Any`.

**Drift risk:** `dimension` is read once per run and stamped on every row (`packages/atif-embed/src/atif_embed/application/embed.py:171`), so
a provider whose width depends on the input rather than on configuration would write a store whose
rows disagree with their own `dim` column. Mitigation: `dimension` is a property fixed at adapter
construction from settings: `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:305-308` reads it from
`packages/atif-embed/src/atif_embed/infrastructure/settings.py:110-114`, never from a response.

## `ConverterPort` and `ConversionOutput`

**Producer:** `packages/atif-corpus/src/atif_corpus/domain/ports.py:101` (`ConverterPort`) and `packages/atif-corpus/src/atif_corpus/domain/ports.py:66`
(`ConversionOutput`)

**Consumers:**

- `packages/atif-corpus/src/atif_corpus/application/materialize.py:172`: imports the port;
  annotated at `packages/atif-corpus/src/atif_corpus/application/materialize.py:615` and `packages/atif-corpus/src/atif_corpus/application/materialize.py:1501`.
- `packages/atif-cli/src/atif_cli/converter_adapter.py:80-148`: `RealConverter`, the production
  adapter and the only module importing both atif-converter and atif-corpus at module scope.
- `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:72`: the test adapter
  that ships in `src/`, not in `tests/`.
- `packages/atif-cli/tests/test_converter_adapter.py:17`: asserts the mapping and binds
  `RealConverter` to the port annotation at `packages/atif-cli/tests/test_converter_adapter.py:95`;
  `packages/atif-cli/tests/test_codex_cli.py:156` binds the Codex-configured adapter the same way.
- `packages/atif-cli/src/atif_cli/app.py:961`: wires `RealConverter(agent=settings.agent)` into the
  use case.

**Shape:**

```python
@dataclass(frozen=True, slots=True)
class ConversionOutput:
    trajectory_dict: dict[str, Any]
    loss_report_dict: dict[str, Any]
    edges_lines: list[str]
    events_lines: list[str] = field(default_factory=list)
    source_archive: tuple[ArchivedSource, ...] = ()
    blobs: tuple[BlobOutput, ...] = ()


class ConverterPort(Protocol):
    """Anything that can turn one session JSONL into corpus artifacts.

    Implementations may raise any exception: the materialize use case
    records the failure against the session and continues — one broken
    transcript must never abort a corpus sync. :class:`EmptySourceError` is
    the one exception with its own meaning: the session is empty, not broken.
    """

    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        """Convert one session (main JSONL + its side-files) to artifacts."""
        ...
```

**Assumptions consumers make:**

- **`RealConverter` never imports the Protocol it implements.** It names it only in its docstring
  and imports the return-value data classes and the empty-source error
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:69-74`), so nothing in either package
  links the class to the port. The static links are the annotated assignments
  `converter: ConverterPort = RealConverter()` at
  `packages/atif-cli/tests/test_converter_adapter.py:95`, whose own comment
  (`packages/atif-cli/tests/test_converter_adapter.py:93-94`) states that the assignment is what ty
  verifies, and its Codex twin at `packages/atif-cli/tests/test_codex_cli.py:156`, plus the CLI's
  call at `packages/atif-cli/src/atif_cli/app.py:961`, which hands the adapter to materialize's
  `converter: ConverterPort` parameter. A signature change on either side fails the typecheck gate
  at those sites, not at materialize time.
- **The port is typed to `docs/CONTRACT.md`'s artifact shapes, not to converter internals**:
  loosely typed `dict[str, Any]` / `list[str]` fields instead of the converter's own
  `ConversionResult`. The docstring at `packages/atif-corpus/src/atif_corpus/domain/ports.py:3-10` names the independence contract as the
  reason, so the weak typing is the contract, not an omission.
- **Exceptions cross this port by design.** `packages/atif-corpus/src/atif_corpus/domain/ports.py:104-107` says an implementation may raise
  anything and the use case records the failure and continues; `EmptySourceError` alone means
  the session is empty, not broken. `RealConverter` relies on that: it
  raises `TrajectoryValidationError` for an invalid trajectory rather than materializing it
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:129-130`, reasoning at `packages/atif-cli/src/atif_cli/converter_adapter.py:49-53`).
- **`trajectory_dict` is the ENRICHED trajectory and is NOT yet compact-serialized.** The writer
  owns `separators=(",", ":")`; the mapping decision is stated at `packages/atif-cli/src/atif_cli/converter_adapter.py:30-31` and
  the writer applies it at `packages/atif-corpus/src/atif_corpus/application/materialize.py:691-693`.
- **`loss_report_dict` is `LossReport.to_json()`-shaped with enum members flattened to strings**
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:32-33`), which is what makes it readable by atif-duck's
  `LOSS_REPORT_COLUMNS` projection at `packages/atif-duck/src/atif_duck/domain/raw_readers.py:77-90`.
- **Importing the adapter loads atif-converter and its ATIF pydantic models.** `packages/atif-cli/src/atif_cli/converter_adapter.py:55-57` forbids importing it at
  `atif_cli.app` module scope, and `packages/atif-cli/src/atif_cli/app.py:925` obeys by importing inside the command body,
  enforced by the fresh-interpreter lean-import test named at `pyproject.toml:220`.

**Drift risk:** the `dict[str, Any]` fields mean a converter that renames a trajectory key
type-checks perfectly and produces artifacts atif-duck's explicit projections silently null out;
and because the implementer never names the port, a `convert` signature change on either side is
caught only where a typed site meets the adapter. Mitigation:
`packages/atif-cli/tests/test_converter_adapter.py:92-96` is the dedicated assertion (treat it as part of
the contract, not as a redundant smoke test), and
`packages/atif-cli/tests/test_converter_adapter.py:44` pins the field mapping while atif-duck's
`DESCRIBE` drift test catches a projection that stops matching.

## Token semantics and the pricing table

**Producer:** `packages/atif-duck/src/atif_duck/infrastructure/registry.py:73-76` (the semantics)
and `packages/atif-duck/src/atif_duck/domain/catalog.py:612` (`DEFAULT_PRICING`)

**Consumers:**

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1936-1956`: the `cost_estimate`
  macro, which consumes both.
- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1892`: `register_macros` resolves
  the pricing override against `DEFAULT_PRICING`.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:85-88`: the `steps` view schema publishes
  `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_creation` to every SQL consumer.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:557-563`: the agent-facing `cost_estimate`
  description carries the trust condition.

**Shape:**

```python
# Model pricing per 1M tokens (in_rate, out_rate) at public list rates from
# Anthropic's published pricing page.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    ...
    "claude-opus-5": (5.0, 25.0),
    ...
    "claude-sonnet-5": (2.0, 10.0),
    ...
    "claude-haiku-4-5": (1.0, 5.0),
}
```

**Assumptions consumers make:**

- **`prompt_tokens` is a CUMULATIVE TOTAL, per step: non-cached + cache_read + cache_creation.**
  Stated at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:73-76` and read from `metrics.prompt_tokens` at `packages/atif-duck/src/atif_duck/infrastructure/projections.py:130`. The
  identifier does not say so, so a consumer summing `prompt_tokens + cached_tokens` double-counts
  every cache read.
- **`cached_tokens` is the cache-READ subset of `prompt_tokens`**, not an additional quantity
  (`packages/atif-duck/src/atif_duck/infrastructure/projections.py:96-97`). `cost_estimate` therefore prices `prompt_tokens - cached_tokens` and
  documents that identity at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1915-1918`; cache reads are charged nothing.
- **`in_rate` / `out_rate` are USD per 1,000,000 tokens.** The unit appears in neither name: only
  the comment at `packages/atif-duck/src/atif_duck/domain/catalog.py:602` and the `/ 1e6` at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1943` carry it.
- **`est_cost_usd` is USD and covers the PRICED steps only.** It is meaningful only when
  `unpriced_steps = 0`, stated in each of these places: `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1925-1930`, `packages/atif-duck/src/atif_duck/domain/catalog.py:557-563`, and the
  `LEFT JOIN` shape itself at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1951`.
- **`unpriced_steps` counts only steps with a non-NULL `model_name`**, because user steps carry no
  model and cost nothing
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1932-1935`). Counting them would put every conversation
  above zero and mask real pricing gaps.
- **Cache write and read multipliers are NOT modelled.** `packages/atif-duck/src/atif_duck/domain/catalog.py:609-611` states that the 1.25x
  / 2x / 0.1x tiers are out of scope, so `est_cost_usd` under-reports a cache-heavy session.
- **A step's `model_name` matches a pricing row by exact name once a trailing `-YYYYMMDD` is stripped**
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1952`), so `claude-haiku-4-5-20251001` prices as `claude-haiku-4-5`.

**Drift risk:** `cached_tokens` exists in two coordinate spaces under one name: atif-duck's
per-step cache-read count (`packages/atif-duck/src/atif_duck/infrastructure/projections.py:132`) and atif-models' per-call
`CallUsage.cached_tokens` (`packages/atif-models/src/atif_models/domain/ports.py:77`). Neither
name states its scope, so a cross-plane join or a copied formula silently mixes them. Mitigation:
attribute the field by module path, and read the producer docstring
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:73-76` or
`packages/atif-models/src/atif_models/domain/ports.py:65-71`) before using either in arithmetic.

## The `EmbeddingProviderMismatch` guard, declared twice

**Producer:** two coordinate declarations, coupled by contract rather than by import:
`packages/atif-embed/src/atif_embed/domain/errors.py:29` with the rule at
`packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38`, and
`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33` with the rule at `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:51`

**Consumers:**

- `packages/atif-embed/src/atif_embed/application/embed.py:175-183`: the WRITE path calls
  `ensure_store_matches` before appending.
- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1707-1722`: the READ/bind path calls
  it before binding `message_embeddings` (guard-before-bind).
- `packages/atif-cli/src/atif_cli/duck_errors.py:27-32`: puts atif-duck's copy in
  `REGISTRATION_ERRORS` and maps it to exit 65 at `packages/atif-cli/src/atif_cli/duck_errors.py:75-82`.
- `packages/atif-embed/tests/test_guard_twin_pin.py:85`: reads both twin modules as source text
  and requires the recovery hint to appear after the `raise` keyword.

**Shape:**

```python
def ensure_store_matches(
    *,
    stored_model: str | None,
    stored_dim: int | None,
    expected_model: str,
    expected_dim: int | None,
) -> None:
    if stored_model is None or stored_dim is None:
        return
    model_ok = stored_model == expected_model
    dim_ok = expected_dim is None or stored_dim == expected_dim
    if model_ok and dim_ok:
        return
    raise EmbeddingProviderMismatch(...)
```

**Assumptions consumers make:**

- **The two copies must stay behaviourally identical, including the message.** Both docstrings say
  so (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-11` and
  `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:15-18`), and the pin test is what
  makes it more than a comment.
- **The message text must be constructed INSIDE the `raise`.** Hoisting it to a local satisfies
  ruff's `EM102` / `TRY003` and defeats the twin pin, so both rules are suppressed on purpose at
  `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:72-79`.
- **A `None` on either stored value means "empty store, any provider may claim it"**, not
  "unknown, be careful" (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:60-61`).
- **`expected_dim=None` trusts `model_id` alone**, and dim is checked only because Cohere's single
  model id can emit different Matryoshka widths
  (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:62-64`).
- **The two classes have different base classes and that matters at the CLI.** atif-duck's derives
  from `Exception` (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33`) while atif-embed's derives from that package's
  `DomainError` (`packages/atif-embed/src/atif_embed/domain/errors.py:29`), so a bare
  `except duckdb.Error` on the registration path would let atif-duck's escape as exit 1 (the
  reason `REGISTRATION_ERRORS` widens the caught set, stated at
  `packages/atif-cli/src/atif_cli/duck_errors.py:15-19`).
- **The recovery hint must not name a fixed home directory.** The store lives at
  `<corpus_root>/embeddings_lance` by default, and both copies record that naming the wrong path
  makes the operator delete nothing
  (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:21-24` and
  `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:25-29`).

**Drift risk:** the twin pin holds both `RECOVERY_HINT` literals to one expected string
(`packages/atif-embed/tests/test_guard_twin_pin.py:76` and `:79`) and checks that each reaches its
`raise`; nothing compares the two comparison rules or the rest of the message, so the logic could
diverge while every test passes. Mitigation: treat the two functions as one. Change both in
the same commit, which is what both docstrings instruct.

## harbor's ATIF models, vendored and consumed by our ported converters

**Producer:** upstream `harbor.models.trajectories` (RFC 0001 data classes) and
`harbor.utils.trajectory_validator` at harbor 0.24.0, vendored as `packages/atif-converter/src/atif_converter/domain/atif/`
(`UPSTREAM_VERSION` in its `__init__`); harbor itself is a dev dependency

**Consumers:**

- `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:981`: `convert_claude_code_records`, our Claude Code converter, a parity port of harbor
  0.24.0's, building `Trajectory` / `Step` / `ToolCall` / `Metrics` from the public models.
- `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:924`: `convert_codex_records`, the Codex counterpart.
- `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py`: `validate_trajectory`, the only call into the vendored validator.
- `packages/atif-converter/tests/test_dev_only_imports_guard.py`: the `ast` guard that keeps `harbor` and `litellm` out of every `src/` tree.
- `packages/atif-converter/tests/test_vendored_atif.py`: the conformance test (the vendored files equal the installed harbor's, and both agree on the goldens).
- `packages/atif-converter/tests/harbor_oracle.py:118` and `:135`: the parity ORACLE (harbor's private converters, reached from the tests
  only), with frozen goldens and a live-corpus diff.

**Shape:**

```python
#: Distributions that are dev-only, so no production module may import them.
DEV_ONLY: frozenset[str] = frozenset({"harbor", "litellm"})
```

**Assumptions consumers make:**

- **The data classes are the contract, and they are versioned by ATIF.** Our converters write
  `schema_version` explicitly (`packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:1067`, `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:1023`), so a harbor release that adds a newer
  default still validates what we emit; a release that changes a field fails the drift tests.
- **Parity is measured, not assumed.** `test_harbor_oracle.py` asserts the live oracle equals the
  frozen goldens, and `test_parity_*` asserts our output equals both. A harbor bump that changes
  conversion behavior surfaces as a named JSON-path diff (`packages/atif-converter/tests/harbor_oracle.py:166`), which is a decision to
  record in the fidelity policy before any re-freeze.
- **Side-file discovery is ours.** Every `*.jsonl` under a session's side directory is read,
  workflow-nested ones included, with nested path parts joined by `__`
  (`packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:56`); harbor's
  own discovery cannot see those files, which is why the oracle stages them flat the same way.
- **harbor ships no `py.typed`**, so every import from it carries an `import-untyped` ignore.

## The workspace members' `==0.3.0` sibling pins

**Producer:** `packages/atif-cli/pyproject.toml:32-36` and
`packages/atif-analytics/pyproject.toml:23`

**Consumers:**

- `pyproject.toml:527-532`: commitizen's `version_files`, which rewrites every pin on each bump.
- `packages/atif-cli/pyproject.toml:60-65`: `[tool.uv.sources]`, the local-workspace resolution
  that these pins deliberately do NOT express.
- `packages/atif-analytics/pyproject.toml:47-48`: the same pairing for its single sibling dependency.
- `pyproject.toml:484-487`: `[tool.commitizen] version = "0.3.0"`, the one version this repo
  publishes.
- `pyproject.toml:16-17`: the distribution is named `atif-sql` at the workspace root, while the
  console script's module stays `atif_cli` in a member.

**Shape:**

```toml
dependencies = [
    "atif-analytics==0.3.0",
    "atif-converter==0.3.0",
    "atif-corpus==0.3.0",
    "atif-duck==0.3.0",
    "atif-embed==0.3.0",
    "cyclopts>=5.1.1",
    "duckdb>=1.5.2,<2",
    "loguru>=0.7.3",
]
```

```toml
version_files = [
    'pyproject.toml:^version',
    'packages/*/pyproject.toml:^version',
    'packages/atif-cli/pyproject.toml:^\s*"atif-',
    'packages/atif-analytics/pyproject.toml:^\s*"atif-models==',
]
```

The other five members (atif-converter, atif-corpus, atif-duck, atif-embed, atif-models) declare
no sibling, so these six pins are every edge between members.

**Assumptions consumers make:**

- **No external installer sees these pins.** The member manifests are dev manifests that the
  release never builds (`pyproject.toml:10-14`); the one published wheel is the root's, its
  `dependencies` hold no `atif-*` entry (`pyproject.toml:34-40`), and
  `packages/atif-cli/tests/test_distribution.py:143` fails if one reaches it. The comment at
  `packages/atif-cli/pyproject.toml:24-31` reasons as if the manifest shipped: the pins keep
  the workspace coherent, and `[tool.uv.sources]` is what resolves them.
- **One version covers the whole repository.** commitizen reads its own `version` key rather than
  the PEP 621 field (`pyproject.toml:484-487`), and `version_files` propagates it to the published
  `[project] version` and to every member manifest; a separate number per member for one artifact is the
  shape being refused.
- **The pin entries in `version_files` are per-file rather than globbed on purpose**, because
  `--check-consistency` requires a hit in every matched file and five of the seven members
  pin no sibling (`pyproject.toml:518-520`).
- **The lockfile must land in the same commit as the versions it resolves.** `pre_bump_hooks` run
  `uv lock` and `git add uv.lock` after the rewrite and before the commit
  (`pyproject.toml:502-512`), because `uv.lock` records every member's version and
  `mise run lock:check` would otherwise fail on the release commit itself.
- **A breaking change moves 0.3.0 to 0.4.0, not 1.0.0**, because `major_version_zero = true`
  (`pyproject.toml:493-495`); reaching 1.0.0 is a decision, not a side effect of a `!` in a subject
  line.

**Drift risk:** a hand-edited pin, or a renamed member whose pin line stops matching its
`version_files` regex, goes stale silently until a release. Mitigation: `--check-consistency` fails
rather than skipping a file that no longer contains the current version; the guarantee is stated at
`pyproject.toml:513-516`.

## Other contracts

- **`LossReport.to_json()`**: producer
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:147`, whose docstring calls the
  keys wire contract; consumed by `packages/atif-duck/src/atif_duck/domain/raw_readers.py:77-90`
  as `LOSS_REPORT_COLUMNS` and by `packages/atif-cli/src/atif_cli/converter_adapter.py:133`.
  `record_counts` and `gaps_observed` stay `JSON` because they are an enum-keyed dict and a sorted
  list of enum values.
- **The `meta.json` provenance record**: written at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:706-731`, projected at
  `packages/atif-duck/src/atif_duck/domain/raw_readers.py:99-121`. `harbor_version` (the vendored
  models' upstream release) and `converter_version` (the `atif-sql` release) are supplied by the
  CLI (`packages/atif-cli/src/atif_cli/app.py:966` and `:971`), so the corpus records which
  converter built it without atif-corpus importing either.
- **The embeddings-store row shape**: the Arrow fields written at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:115-121`, of which the
  `message_embeddings` view exposes only some
  (`packages/atif-duck/src/atif_duck/domain/catalog.py:281-287`). `text_hash` and `truncated` are
  unreachable from SQL, so a query cannot distinguish a row embedded from its head alone from a complete one.
- **The Lance schema version sidecar**: `SCHEMA_VERSION = 2` and `schema_version.json` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:61-67`, kept as a sidecar rather
  than a column because reading it must not require the table.
- **The user-step authorship rule table**: `AUTHOR_VALUES`, `AUTHOR_STRIP_CHARS`,
  `AUTHOR_PREFIX_RULES`, and `INTERRUPT_PREFIXES` at
  `packages/atif-duck/src/atif_duck/domain/authorship.py:37-102`, which the `step_author` macro and
  the `user_steps`, `human_turns`, and `session_outcomes` views compile from. atif-analytics can't
  import atif-duck, so `packages/atif-analytics/src/atif_analytics/domain/authorship.py:37-102`
  carries a twin that its pipelines and transcript renderer read. The twin is pinned as source
  text by `packages/atif-duck/tests/test_authorship_twin_pin.py:64`, and
  `packages/atif-cli/tests/test_authorship_parity.py:53` runs the SQL and the Python over the same
  steps.
- **The analytics parquet layout**: the artifact names at
  `packages/atif-analytics/src/atif_analytics/domain/layout.py:25-41`, of which atif-duck pins the
  pipeline directories as `_ANALYTICS_SOURCES` (`packages/atif-duck/src/atif_duck/infrastructure/analytics.py:61-66`).
  `REFUSALS_DIRNAME` (`packages/atif-analytics/src/atif_analytics/domain/layout.py:39`) has no view, as its own comment says
  (`packages/atif-analytics/src/atif_analytics/domain/layout.py:35-38`), so the refusal audit has no name in `atif-sql schema` and a
  reader has to `read_parquet` its shard files directly.
- **The CLI exit-code taxonomy**: `EXIT_CODES` at
  `packages/atif-cli/src/atif_cli/errors.py:30-66`, consumed at many sites in
  `packages/atif-cli/src/atif_cli/app.py` and mapped from DuckDB exceptions at
  `packages/atif-cli/src/atif_cli/duck_errors.py:35-64`. Code 78 matters most: it means
  an operator must act, and unattended lanes suppress retries on it
  (`packages/atif-cli/src/atif_cli/errors.py:49-51`).
- **`PendingText` and the text stamp**: `packages/atif-embed/src/atif_embed/domain/text_stamp.py:51`,
  with `MAX_EMBEDDABLE_CHARS = 50_000` at `packages/atif-embed/src/atif_embed/domain/text_stamp.py:35` in CHARACTERS per text; the same constant governs
  both the row's `truncated` flag and the adapter's wire-level clip so the two can never disagree
  (`packages/atif-embed/src/atif_embed/domain/text_stamp.py:31-34`).
- **The `steps`-view rendering semantics, mirrored without a shared symbol**:
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:30-44` reproduces
  atif-duck's `steps` rules by hand (message-union flattening, `extra.source_uuids[0]` as the
  step key, `agent` → `assistant`, error recovery from the typed `extra.is_error` or from
  `extra.tool_result_metadata.is_error`), and
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:13-26` reproduces some of
  them again.
- **The retryable Bedrock error codes, declared twice**: `RETRY_CODES` at
  `packages/atif-models/src/atif_models/infrastructure/bedrock_common.py:27-42`, which both LLM
  adapters retry on through `is_retryable` (`:66-73`), and its twin at
  `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:56-72`. The two packages
  can't import each other, so each suite pins its copy against the same spelled-out set
  (`packages/atif-models/tests/test_openai_provider.py:342` and
  `packages/atif-embed/tests/test_cohere_bedrock.py:442`), and each copy's comment names the other
  (`packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:56-60`).
- **The `ATIF_SQL_` settings prefix**: the one env namespace across every member;
  `packages/atif-embed/src/atif_embed/infrastructure/settings.py:64` declares
  `output_dimension: int | None = None`, and an unset width takes the provider's default from
  `packages/atif-embed/src/atif_embed/infrastructure/settings.py:36` (1024 for Cohere, 768 for
  Gemma). Cohere's 1024 is the value the static catalog hardcodes as `FLOAT[1024]`.
- **The lean-import contract**: `pyproject.toml:220` and `pyproject.toml:235-238` record that `atif_cli.app` must not
  import duckdb, harbor, lancedb, boto3, or polars at module scope, asserted in a fresh interpreter
  by `packages/atif-cli/tests/test_lean_import.py`; the deferred-import sites exist because of it.
- **The other ports, satisfied the same way**: atif-corpus's `ArtifactProducer`
  (`packages/atif-corpus/src/atif_corpus/domain/ports.py:123`), which atif-duck's
  `ColumnarArtifactProducer` fits (`packages/atif-duck/src/atif_duck/infrastructure/columnar.py:450`)
  and which the CLI's `materialize` doesn't inject, and `SessionSink`
  (`packages/atif-corpus/src/atif_corpus/domain/ports.py:155`), which atif-duck's
  `DuckLakeSessionSink` fits (`packages/atif-duck/src/atif_duck/infrastructure/lake.py:868`) and
  the CLI wires in at `packages/atif-cli/src/atif_cli/app.py:986`; atif-embed's `LakeStepsPort` and
  `LakeStepSnapshot` (`packages/atif-embed/src/atif_embed/domain/ports.py:202` and `:154`), which
  atif-cli's `DuckLakeSteps` fits (`packages/atif-cli/src/atif_cli/embed_lake.py:70`); and
  atif-analytics' `SessionSource` (`packages/atif-analytics/src/atif_analytics/domain/ports.py:40`),
  which `TrajectoryFileSource`
  (`packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:271`) and atif-cli's
  `LakeSessionSource` (`packages/atif-cli/src/atif_cli/lake_sessions.py:92`) fit.

## See also

- [impact analysis](impact-analysis.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [business logic](business-logic.md)
- [components](../diagrams/architecture/components.md)
