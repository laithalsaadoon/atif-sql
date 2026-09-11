# atif-sql · Contract map

**What counts as a contract here.** This workspace has almost no shared-type imports to trace,
because `pyproject.toml:514-523` forbids most of them: five of the seven members may never import
each other, and atif-analytics may import only atif-models. What crosses a module boundary instead
is a **shape agreed by two packages that cannot reference one another's symbols** — a file layout, a
JSON key order, a column projection, a `typing.Protocol` typed to a document rather than to an
implementation. So a contract in this file is any of:

1. a `typing.Protocol` declared in one package's `domain/ports.py` and satisfied by an adapter
   elsewhere (there are exactly five, listed below);
2. a shape declared **twice or more** in packages that cannot import each other, where the two
   declarations must agree or a query silently returns wrong rows;
3. a build-enforced dependency rule (import-linter), which is a stronger fact than a convention;
4. an upstream API this workspace depends on and pins;
5. a version constraint that ships in a wheel and binds an external installer.

**Every Protocol here is satisfied STRUCTURALLY, with no import in either direction.** That is the
single most important structural fact for reading this file. `RealConverter`
(`packages/atif-cli/src/atif_cli/converter_adapter.py:44`) implements `ConverterPort`
(`packages/atif-corpus/src/atif_corpus/domain/ports.py:41`) by shape alone; it never names the
Protocol, and it cannot, because atif-corpus may never import atif-converter. The Protocol name
appears at exactly two import sites in the whole workspace —
`packages/atif-corpus/src/atif_corpus/application/materialize.py:92` (the module that CALLS it) and
`packages/atif-cli/tests/test_converter_adapter.py:17` (the test that binds the two together in an
annotation). So each Protocol row below has a producer of the shape and an implementer the type
system never links to it, and the answer to "who finds out if the shape drifts" is a named test or
nobody. Do not read a Protocol row as a dependency edge — `pyproject.toml:514-517` forbids the edge
it would imply.

Every contract below names its producer, its consumers, the verbatim shape, the assumptions
consumers make beyond the shape, and the drift risk. **Consumer counts are grep-derived and
confirmed at each import, annotation, or call site** — this repo has no code index
(`.gitignore:24` lists `.codegraph/`, and no index exists on disk), so no count here comes from a
symbol graph. Two grep hazards shape every count: names collide across packages (`DomainError` in
three, `EmbeddingProviderMismatch` in two, `cached_tokens` in two coordinate spaces), so every
attribution here is by module path and never by bare name; and every cross-package import is
indented inside a function body or a `TYPE_CHECKING` block, so a line-anchored grep finds nothing.

Contracts are ordered by confirmed consumer count, descending.

## The materialized corpus artifact layout

**Producer:** `packages/atif-corpus/src/atif_corpus/domain/layout.py:18-22`

**Consumer(s):**

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:210-213` — inlines the four
  filenames as SQL glob literals for its `read_json` readers.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:120-121` — opens
  `trajectory.json` and gates on `meta.json` with its own DuckDB connection.
- `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:53-55` — declares
  `TRAJECTORY_FILENAME` / `EDGES_FILENAME` / `META_FILENAME` a third time as its own constants.
- `packages/atif-corpus/src/atif_corpus/application/materialize.py:222-238` — the writer side, the
  only place the artifacts are produced.
- `docs/CONTRACT.md:21-39` — the hand-written specification all four agree to.

**Shape:**

```text
<corpus_root>/                     # default: ~/.atif-sql/corpus/<corpus-slug>/
  sessions/<session_id>/
    trajectory.json                # compact JSON (separators=(',',':')), ATIF-v1.7
    loss_report.json               # atif_converter LossReport.to_json()
    edges.jsonl                    # one line per RAW record: {uuid, parent_uuid,
                                   #  message_id, type, ts, is_sidechain,
                                   #  is_compact_summary, source_file, tool_use_ids: [..]}
    meta.json                      # {session_id, source_mtime_ns, source_files: [...],
                                   #  harbor_version, converter_version, materialized_at}
  watermark.json                   # {path: mtime_ns} across source corpus
```

**Assumptions consumers make:**

- **`meta.json` present means the session dir is complete.** All three readers implement the same
  torn-set gate independently: `packages/atif-duck/src/atif_duck/infrastructure/registry.py:188-192` restricts the trajectory/edges/loss readers to
  dirs where `v_raw_meta` has a row; `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:121-124` skips a dir with no `meta.json`;
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:171-180` does the same and logs a warning. Nothing in the file layout expresses
  this — it is a write-ORDER promise made at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:209-212`.
- **No reader ever sees a partially-written directory**, because the writer stages all four
  artifacts under `<corpus_root>/.staging/` and swaps the whole dir with `os.replace`
  (`packages/atif-corpus/src/atif_corpus/application/materialize.py:201-212`), and `.staging` is deliberately outside `sessions/` so a DuckDB glob
  cannot reach it (`packages/atif-corpus/src/atif_corpus/domain/layout.py:50-54`).
- **`trajectory.json` is ONE JSON document, `edges.jsonl` is newline-delimited.** atif-duck encodes
  that split in its reader choice at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:23-25`; atif-embed re-derives it at
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:7-8`.
- **A single trajectory document can be enormous.** Both DuckDB readers set a 1 GiB
  `maximum_object_size`, and their measured ceilings disagree: `packages/atif-duck/src/atif_duck/infrastructure/registry.py:74-80` cites 436 MB
  observed, `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:47-49` cites 85 MB. Same constant, two independent
  justifications.
- **`source_mtime_ns` and every `watermark.json` value are epoch NANOSECONDS** from
  `os.stat().st_mtime_ns` — unit stated in the identifier and again at
  `packages/atif-corpus/src/atif_corpus/domain/watermark.py:12-13`. atif-duck types the column
  `BIGINT` at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:136`, which preserves the magnitude but drops the unit from the schema.
- **`materialized_at` is an ISO-8601 UTC string, not a timestamp.** atif-duck projects it as
  `VARCHAR` (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:140`); the CLI supplies it (`packages/atif-cli/src/atif_cli/app.py:400`).

**Where the hand-written contract disagrees with the code, the code wins — and it does disagree in
two places.** `docs/CONTRACT.md:61` heads its CLI section "atif-cli composes; only package importing
the other three", while the manifest declares five sibling dependencies
(`packages/atif-cli/pyproject.toml:32-36`) and the CLI imports all five. `docs/CONTRACT.md:16-17`
lists VSS/`semantic_search` and the v2 LLM-analytics pipelines as out of scope and then reverses
itself at `docs/CONTRACT.md:17-19`; both are shipped commands
(`packages/atif-cli/pyproject.toml:42` plus the `embed` / `search` / `analyze` commands). Read
`docs/CONTRACT.md:21-39` as authoritative for the layout — that is the part four packages actually
implement — and the manifest as authoritative for who imports whom.

**Drift risk:** a fifth artifact, a renamed file, or a new `meta.json` key must be applied in four
places that no test links, and three of the four are reader-side, so an addition silently reaches
nobody. Mitigation: the writer-side constants at
`packages/atif-corpus/src/atif_corpus/domain/layout.py:18-22` are the single source of truth on the
write side — any layout change starts there and then greps the three reader modules named above.

## The static SQL catalog

**Producer:** `packages/atif-duck/src/atif_duck/domain/catalog.py:28` (`VIEW_NAMES`), with
`VIEW_SCHEMA:51`, `MACRO_NAMES:230`, `MACRO_SIGNATURES:247`, `ANALYTICS_VIEW_NAMES:269`,
`ANALYTICS_MACRO_SIGNATURES:287`, `TABLE_MACRO_NAMES:308`, `DESCRIPTIONS:326`

**Consumer(s):**

- `packages/atif-cli/src/atif_cli/app.py:1099-1118` — the `schema` command reads `VIEW_SCHEMA` and
  `MACRO_SIGNATURES` and answers with no DuckDB connection.
- `packages/atif-duck/src/atif_duck/domain/examples.py:36-42` — the examples generator imports all
  seven catalogs and derives one runnable query per object.
- `packages/atif-cli/src/atif_cli/app.py:1025-1046` — the `examples` command calls
  `build_examples()`.
- `packages/atif-duck/tests/test_duck_views.py:31-33` — the `DESCRIBE`-vs-`VIEW_SCHEMA` and
  DDL-vs-`MACRO_SIGNATURES` drift tests.
- `packages/atif-duck/tests/test_examples.py:27-35` — `DESCRIPTIONS` coverage, `ARG_EXEMPLARS`
  coverage, and the `AS TABLE` set drift test.
- `packages/atif-duck/tests/test_analytics_views.py:20-21` — the analytics-side drift test.
- `packages/atif-cli/tests/test_app.py:78-84` — asserts the CLI's JSON payload keys equal the
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
        ("cwd", "VARCHAR"),
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
  equality against `DESCRIBE` output, stated at `packages/atif-duck/src/atif_duck/domain/catalog.py:47-50`; reordering a view's `SELECT`
  list fails CI even though every column still exists.
- **`DESCRIPTIONS` covers every object in every catalog, exactly.** `packages/atif-duck/tests/test_examples.py:181-185`
  fails on a missing entry AND on a stale entry keyed to an object that no longer exists, so the
  dict is a bijection with the union of the four name catalogs.
- **Every macro parameter name has an `ARG_EXEMPLARS` literal**, or example generation raises
  rather than emitting a broken query — `packages/atif-duck/src/atif_duck/domain/examples.py:151-157` fails loud, and
  `packages/atif-duck/tests/test_examples.py:189-194` pre-empts it.
- **`TABLE_MACRO_NAMES` decides the SQL call shape.** `packages/atif-duck/src/atif_duck/domain/examples.py:159-162` emits
  `SELECT * FROM name(args)` for a member and `SELECT name(args)` otherwise, so a macro whose DDL
  gains or loses `AS TABLE` breaks every derived example — pinned by the regex test at
  `packages/atif-duck/tests/test_examples.py:198-210`.
- **The catalog is answerable without a corpus.** `packages/atif-cli/src/atif_cli/app.py:1094-1098` states the sub-50 ms,
  no-DuckDB-bind guarantee the `schema` command rests on; a runtime `DESCRIBE` would violate it.
- **The four raw readers are deliberately NOT in `VIEW_NAMES`** (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:65-68`), so a
  consumer enumerating `VIEW_NAMES` does not see `v_raw_trajectories` and friends.

**Drift risk:** adding a view or macro without a `DESCRIPTIONS` entry, an `ARG_EXEMPLARS` entry for
each new parameter name, or `TABLE_MACRO_NAMES` membership fails CI loudly — the risk is inverted
here, and the real exposure is a *type* that only the fixture corpus produces. Mitigation:
`VIEW_SCHEMA["message_embeddings"]` at `packages/atif-duck/src/atif_duck/domain/catalog.py:218-224` hardcodes `FLOAT[1024]`, so run the
drift test against a store built at a non-default `output_dimension` before changing that setting.

## The seven import-linter architecture contracts

**Producer:** `pyproject.toml:462-523`

**Consumer(s):**

- `mise.toml:197` — `lint:imports` is gate 5 of the nine `mise run check` gates, so the build is
  the consumer.
- `packages/atif-corpus/src/atif_corpus/domain/ports.py:5-8` — the `ConverterPort` docstring cites
  the independence contract as the reason the Protocol exists at all.
- `packages/atif-embed/src/atif_embed/domain/ports.py:11-15` — cites it as the reason `TextRowsPort`
  exists.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:5-10` — cites it as the
  reason atif-embed carries its own corpus reader.
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-9` — cites it as the reason the
  guard is duplicated.
- `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:13-15` — cites it as the reason the
  analytics artifact names are pinned by hand.
- `packages/atif-analytics/src/atif_analytics/domain/layout.py:8-10` — cites it as the reason its
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
  list (`pyproject.toml:517`) and the `forbidden` source list (`pyproject.toml:522`). The one module that imports
  two independent packages is `packages/atif-cli/src/atif_cli/converter_adapter.py:36-38`, and its
  docstring names that privilege explicitly at `packages/atif-cli/src/atif_cli/converter_adapter.py:5-9`.
- **atif-analytics is absent from the independence list on purpose** so it can compose atif-models,
  with the `forbidden` contract pinning its other six edges shut — the reasoning is inline at
  `pyproject.toml:509-513`. Verified: `grep -rn 'atif_duck' packages/atif-analytics/` returns
  nothing, so the CONTRACT-V2-era design of reading atif-duck's views is not what the code does.
- **atif-duck declares no `layers` contract.** Five members do (`pyproject.toml:465-508`); atif-duck
  has `domain/` and `infrastructure/` but no `application/`, so there is no third layer to order.
- **Every layered member's `domain/` is the innermost layer**, which is why all five Protocols live
  in `domain/ports.py` and none in an `application/ports.py` — no such file exists in this
  workspace.
- **The comment at `pyproject.toml:509-510` grants a permission that is not exercised.** It reads
  "ONLY atif-cli and atif-analytics may import atif-models", but atif-cli neither declares
  atif-models in `packages/atif-cli/pyproject.toml:32-36` nor imports it anywhere:
  `grep -rn 'atif_models' packages/atif-cli/` returns nothing. The live atif-models edge is
  atif-analytics' alone, 22 import sites led by
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:28-29`.
- **Every cross-package import in this workspace is INDENTED** — inside a function body or a
  `TYPE_CHECKING` block — because `PLC0415` is ignored workspace-wide to satisfy the lean-import
  test (`pyproject.toml:164`). The four exceptions are module-scope imports in the two atif-cli
  modules that are themselves only ever imported inside a command body:
  `packages/atif-cli/src/atif_cli/converter_adapter.py:36-38` and
  `packages/atif-cli/src/atif_cli/duck_errors.py:26`. A line-anchored grep for `^from atif_` finds
  zero cross-package consumers, which is why every count in this file comes from an unanchored grep
  confirmed at the site.

**Drift risk:** a new workspace member that is not added to `root_packages` (`pyproject.toml:463`) is silently
unchecked — import-linter reports "7 contracts, 7 kept" while the new package imports whatever it
likes. Mitigation: adding a `packages/*` member means adding it to `root_packages` and giving it
either a `layers` contract or a place in the `independence` list in the same commit.

## `LlmStructuredProvider` — the structured-output port

**Producer:** `packages/atif-models/src/atif_models/domain/ports.py:116`

**Consumer(s):**

- `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:55` (annotated at
  `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:86`, `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:349`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:79` (`packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:127`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:163`, `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:428`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:73` (`packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:107`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:344`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:76` (`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:244`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:538`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:87` (`packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:141`,
  `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:364`)
- `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:28` — the provider
  factory and the usage/budget plumbing.
- `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:143` — the one production
  implementation.
- `packages/atif-analytics/tests/analytics_fixtures.py:237` — the deterministic double.

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
  and the pipelines route on exactly that split — a third error type would fall through to neither
  the refusal sidecar nor the retry queue.
- **The Protocol says nothing about token accounting, yet every consumer reads it off the concrete
  provider.** `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:70` and `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:129` both reach for `getattr(provider, "usage", None)` because
  `usage` is not on the port; a conforming adapter without that attribute silently reports zero
  spend and the `RunBudget` ceiling never trips.
- **`CallUsage` counts are PER CALL, and two of the four are subsets of the other two.**
  `reasoning_tokens` is a subset of `output_tokens` and `cached_tokens` a subset of `input_tokens`
  (`packages/atif-models/src/atif_models/domain/ports.py:64-70`), so `estimate_cost` at `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:73-77` prices only `input_tokens` and
  `output_tokens` — adding the other two would double-charge.
- **`UsageAccumulator` must be thread-safe, not task-safe.** The lock is a `threading.Lock`
  (`packages/atif-models/src/atif_models/domain/ports.py:87`) because adapters dispatch blocking `invoke_model` through `anyio.to_thread`,
  reasoning stated at `packages/atif-models/src/atif_models/domain/ports.py:16-22`.
- **The budget is a stop-dispatch trigger, not a cap.** `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:101-112` documents the
  overshoot bound as `max_cost_usd` plus at most `BUDGET_CHECK_BATCH` units in flight; a consumer
  treating `max_cost_usd` as a hard ceiling is wrong.
- **`pricing_in` / `pricing_out` on `ModelSpec` are USD per 1,000,000 tokens**, stated at
  `packages/atif-models/src/atif_models/domain/registry.py:42-43`, and `None` when unknown — which
  `estimate_cost` (`packages/atif-models/src/atif_models/domain/registry.py:125`) turns into a `None` cost rather than a zero.

- **`OpenAiBedrockProvider` never names the port either**, and no test binds it to one. The single
  static link is the annotated return of the factory:
  `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:34` declares
  `tuple[LlmStructuredProvider, ModelSpec]` and `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:45-57` returns the concrete adapter, so ty and
  pyright check conformance at that one return statement.

**Drift risk:** the port is `@runtime_checkable`, so an `isinstance` check passes on method names
alone and would accept an adapter with the wrong parameter kinds (positional instead of
keyword-only), and no `isinstance` guard against it exists in the workspace anyway
(`grep -rn 'isinstance(.*LlmStructuredProvider' packages/` returns nothing). Mitigation: the
keyword-only signature at `packages/atif-models/src/atif_models/domain/ports.py:123-125` is the
contract, and the factory's annotated return at
`packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:34` is where a
mismatched adapter fails the typecheck gate.

## The `edges.jsonl` line shape

**Producer:** `packages/atif-converter/src/atif_converter/domain/edges.py:22-32`

**Consumer(s):**

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:107-117` — `_EDGE_COLUMNS`, the
  same nine keys in the same order with DuckDB types.
- `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:300-301` — reads the
  `uuid` field for the conflicts pipeline's returned-uuid validity guard.
- `packages/atif-corpus/src/atif_corpus/application/materialize.py:224-227` — writes the lines and
  owns line termination.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:330` — the `messages` view is defined as
  exactly this surface.

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
  `packages/atif-corpus/src/atif_corpus/domain/ports.py:30-33` and honoured at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:224-227`; a producer that terminated its own lines would double them.
- **`parent_uuid` is a nullable string, and atif-duck declares it `VARCHAR` outright** rather than
  letting JSON union inference decide — the reason is recorded at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:103-106`.
- **`tool_use_ids` means two different things by record type**: tool_use block ids for assistant
  records, the `tool_use_id` of tool_result blocks for user records (`packages/atif-converter/src/atif_converter/domain/edges.py:11-13`). One field,
  two semantics, discriminated by the sibling `type` column.
- **`edges.jsonl` is the only source of raw-record identity.** The trajectory cannot supply uuids
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:38-40`, fidelity gap 7 `UUID_NOT_PRESERVED`), which is why the `messages` view is
  reconstructed from edges rather than from steps.
- **`records_total` in the loss report equals this file's line count.** Asserted at
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:91-95` — both derive from the same
  raw census, so a consumer may cross-check one against the other.

**Drift risk:** the nine keys are declared twice in packages that cannot import each other, and a
tenth key added on the producer side is simply invisible to `_EDGE_COLUMNS` — a projection reader
drops unknown keys without erroring. Mitigation: `EDGE_FIELDS` is a single tuple; changing it means
editing `packages/atif-duck/src/atif_duck/infrastructure/registry.py:107-117` in the same commit.

## `EmbeddingProvider`, `VectorStorePort`, `TextRowsPort`

**Producer:** `packages/atif-embed/src/atif_embed/domain/ports.py:31`, `packages/atif-embed/src/atif_embed/domain/ports.py:59`, `packages/atif-embed/src/atif_embed/domain/ports.py:87`

**Consumer(s):**

- `packages/atif-embed/src/atif_embed/application/embed.py:35` — imports all three; annotated at
  `packages/atif-embed/src/atif_embed/application/embed.py:46-47` and `packages/atif-embed/src/atif_embed/application/embed.py:65-67`.
- `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:285` — the
  `EmbeddingProvider` adapter.
- `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:377` — the `VectorStorePort`
  adapter.
- `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:156` — the `TextRowsPort`
  adapter.
- `packages/atif-embed/tests/embed_fixtures.py:171` — the deterministic `EmbeddingProvider` double.
- `packages/atif-embed/tests/test_embed_use_case.py:458` — a recording `TextRowsPort` double.

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
```

**Assumptions consumers make:**

- **`embed_documents` returns one slot per input text in input order, and the caller enforces it.**
  `packages/atif-embed/src/atif_embed/application/embed.py:191` zips with `strict=True`, so a provider returning a different length raises
  `ValueError` instead of misattributing vectors to texts.
- **A `None` slot means "not embedded this run", not "failed the run".** The port documents bounded
  loss at `packages/atif-embed/src/atif_embed/domain/ports.py:47-51`; `packages/atif-embed/src/atif_embed/application/embed.py:192-198` filters the `None`s, counts them as skipped, and
  leaves those uuids for the next pass.
- **The store's `(model, dim)` stamp is checked BEFORE any append**, `packages/atif-embed/src/atif_embed/application/embed.py:166-175`, using the
  identity `table_identity()` returns — `None` there means empty and any provider may claim it.
- **`add_chunk` accepts a 7-column polars frame with a fixed-size `pl.Array`, not a `pl.List`.**
  The schema is built at `packages/atif-embed/src/atif_embed/application/embed.py:216-238` and the reason is stated at `packages/atif-embed/src/atif_embed/application/embed.py:212-215`: a variable-size
  list is rejected by Lance for indexing.
- **`iter_unembedded`'s laziness bounds only the CALLER's residency.** The port says so explicitly
  at `packages/atif-embed/src/atif_embed/domain/ports.py:111-114` — peak resident text inside an implementation is each adapter's own
  problem, and `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:54-60` establishes it by batching on BYTES (4 MiB) rather than
  on file count.
- **Staleness is decided by hash, not by uuid presence.** A uuid under a different `text_hash` is
  yielded with `replaces_existing=True` (`packages/atif-embed/src/atif_embed/domain/ports.py:105-109`), and the caller must delete before
  appending or the uuid fans out to two vectors — `packages/atif-embed/src/atif_embed/application/embed.py:203-209`.
- **The CLI does not inject these ports.** `packages/atif-cli/src/atif_cli/app.py:826-831` calls
  `run_backfill` with only `corpus_root`, `settings`, `limit`, and `dry_run`; the use case
  constructs each default adapter itself under a deferred import (`packages/atif-embed/src/atif_embed/application/embed.py:103-110`, `packages/atif-embed/src/atif_embed/application/embed.py:157-159`)
  so a dry run never loads boto3.

- **No test binds any of the three adapters to its Protocol.** Unlike `ConverterPort`, these three
  have no conformance assertion anywhere in `packages/atif-embed/tests/`; the only static link is
  the defaulting assignment inside `run_backfill`, where each concrete class is assigned to a
  parameter already annotated with the port
  (`packages/atif-embed/src/atif_embed/application/embed.py:103-110` and `packages/atif-embed/src/atif_embed/application/embed.py:157-159` against the
  annotations at `packages/atif-embed/src/atif_embed/application/embed.py:65-67`). ty and pyright
  check those three assignments; if the defaulting branch were ever refactored to construct the
  adapters elsewhere, nothing would check conformance at all.

**Drift risk:** `dimension` is read once per run and stamped on every row (`packages/atif-embed/src/atif_embed/application/embed.py:161-162`), so
a provider whose width depends on the input rather than on configuration would write a store whose
rows disagree with their own `dim` column. Mitigation: `dimension` is a property fixed at adapter
construction from settings — `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:305-307` reads it from
`packages/atif-embed/src/atif_embed/infrastructure/settings.py:31`, never from a response.

## `ConverterPort` and `ConversionOutput`

**Producer:** `packages/atif-corpus/src/atif_corpus/domain/ports.py:41` (`ConverterPort`) and `packages/atif-corpus/src/atif_corpus/domain/ports.py:21`
(`ConversionOutput`)

**Consumer(s):**

- `packages/atif-corpus/src/atif_corpus/application/materialize.py:92` — imports the port;
  annotated at `packages/atif-corpus/src/atif_corpus/application/materialize.py:193` and `packages/atif-corpus/src/atif_corpus/application/materialize.py:480`.
- `packages/atif-cli/src/atif_cli/converter_adapter.py:38-69` — `RealConverter`, the production
  adapter and the only module importing both atif-converter and atif-corpus.
- `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:16-65` — the test adapter
  that ships in `src/`, not in `tests/`.
- `packages/atif-cli/tests/test_converter_adapter.py:17` — asserts the mapping and binds
  `RealConverter` to the port annotation at `packages/atif-cli/tests/test_converter_adapter.py:91`.
- `packages/atif-cli/src/atif_cli/app.py:399` — wires `RealConverter()` into the use case.

**Shape:**

```python
@dataclass(frozen=True, slots=True)
class ConversionOutput:
    trajectory_dict: dict[str, Any]
    loss_report_dict: dict[str, Any]
    edges_lines: list[str]


class ConverterPort(Protocol):
    """Anything that can turn one session JSONL into corpus artifacts.

    Implementations may raise any exception: the materialize use case
    records the failure against the session and continues — one broken
    transcript must never abort a corpus sync.
    """

    def convert(self, session_jsonl: Path) -> ConversionOutput:
        """Convert one session (main JSONL + its side-files) to artifacts."""
        ...
```

**Assumptions consumers make:**

- **`RealConverter` never names the Protocol it implements.** It imports only the return-value
  dataclass (`packages/atif-cli/src/atif_cli/converter_adapter.py:38`), so nothing in either package
  links the class to the port. The single static link in the workspace is the annotated assignment
  `converter: ConverterPort = RealConverter()` at
  `packages/atif-cli/tests/test_converter_adapter.py:91`, whose own comment
  (`packages/atif-cli/tests/test_converter_adapter.py:89-90`) states that the assignment is what ty
  verifies. Delete that test and a signature change on either side becomes a runtime
  `AttributeError` at materialize time.
- **The port is typed to `docs/CONTRACT.md`'s artifact shapes, not to converter internals** — three
  loosely-typed `dict[str, Any]` / `list[str]` fields instead of the converter's own
  `ConversionResult`. The docstring at `packages/atif-corpus/src/atif_corpus/domain/ports.py:3-10` names the independence contract as the
  reason, so the weak typing is the contract, not an omission.
- **Exceptions cross this port by design.** `packages/atif-corpus/src/atif_corpus/domain/ports.py:44-47` says an implementation may raise
  anything and the use case records the failure and continues. `RealConverter` relies on that: it
  raises `TrajectoryValidationError` for an invalid trajectory rather than materializing it
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:63-64`, reasoning at `packages/atif-cli/src/atif_cli/converter_adapter.py:21-25`).
- **`trajectory_dict` is the ENRICHED trajectory and is NOT yet compact-serialized.** The writer
  owns `separators=(",", ":")` — the mapping decision is stated at `packages/atif-cli/src/atif_cli/converter_adapter.py:13-14` and
  the writer applies it at `packages/atif-corpus/src/atif_corpus/application/materialize.py:222`.
- **`loss_report_dict` is `LossReport.to_json()`-shaped with enum members flattened to strings**
  (`packages/atif-cli/src/atif_cli/converter_adapter.py:15-16`), which is what makes it readable by atif-duck's
  `_LOSS_REPORT_COLUMNS` projection at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:122-131`.
- **Importing the adapter drags harbor.** `packages/atif-cli/src/atif_cli/converter_adapter.py:27-29` forbids importing it at
  `atif_cli.app` module scope, and `packages/atif-cli/src/atif_cli/app.py:387` obeys by importing inside the command body —
  enforced by the fresh-interpreter lean-import test named at `pyproject.toml:164`.

**Drift risk:** the three `dict[str, Any]` fields mean a converter that renames a trajectory key
type-checks perfectly and produces artifacts atif-duck's explicit projections silently null out;
and because the implementer never names the port, a `convert` signature change on either side is
caught by exactly one assertion. Mitigation:
`packages/atif-cli/tests/test_converter_adapter.py:88-92` is that assertion — treat it as part of
the contract, not as a redundant smoke test — and
`packages/atif-cli/tests/test_converter_adapter.py:17-60` pins the field mapping while atif-duck's
`DESCRIBE` drift test catches a projection that stops matching.

## Token semantics and the pricing table

**Producer:** `packages/atif-duck/src/atif_duck/infrastructure/registry.py:41-44` (the semantics)
and `packages/atif-duck/src/atif_duck/domain/catalog.py:405` (`DEFAULT_PRICING`)

**Consumer(s):**

- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1066-1083` — the `cost_estimate`
  macro, which consumes both.
- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1020` — `register_macros` resolves
  the pricing override against `DEFAULT_PRICING`.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:72-75` — the `steps` view schema publishes
  `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_creation` to every SQL consumer.
- `packages/atif-duck/src/atif_duck/domain/catalog.py:347-350` — the agent-facing `cost_estimate`
  description carries the trust condition.

**Shape:**

```python
# Model pricing per 1M tokens (in_rate, out_rate) at public list rates from
# Anthropic's published pricing page.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    ...
}
```

**Assumptions consumers make:**

- **`prompt_tokens` is a CUMULATIVE TOTAL, per step: non-cached + cache_read + cache_creation.**
  Stated at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:41-44` and read from `metrics.prompt_tokens` at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:376-377`. The
  identifier does not say so, so a consumer summing `prompt_tokens + cached_tokens` double-counts
  every cache read.
- **`cached_tokens` is the cache-READ subset of `prompt_tokens`**, not an additional quantity
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:380-381`). `cost_estimate` therefore prices `prompt_tokens - cached_tokens` and
  documents that identity at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1043-1049`; cache reads are charged nothing.
- **`in_rate` / `out_rate` are USD per 1,000,000 tokens.** The unit appears in neither name — only
  the comment at `packages/atif-duck/src/atif_duck/domain/catalog.py:395` and the `/ 1e6` at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1071` carry it.
- **`est_cost_usd` is USD and covers the PRICED steps only.** It is meaningful only when
  `unpriced_steps = 0`, stated three times: `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1053-1058`, `packages/atif-duck/src/atif_duck/domain/catalog.py:347-350`, and the
  `LEFT JOIN` shape itself at `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1079`.
- **`unpriced_steps` counts only steps with a non-NULL `model_name`**, because user steps carry no
  model and cost nothing — `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1060-1063`. Counting them would put every conversation
  above zero and mask real pricing gaps.
- **Cache write and read multipliers are NOT modelled.** `packages/atif-duck/src/atif_duck/domain/catalog.py:402-404` states that the 1.25x
  / 2x / 0.1x tiers are out of scope, so `est_cost_usd` under-reports a cache-heavy session.
- **A step's `model_name` matches a pricing row by dated-suffix-stripping prefix**
  (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1080`), so `claude-haiku-4-5-20251001` prices as `claude-haiku-4-5`.

**Drift risk:** `cached_tokens` exists in two coordinate spaces under one name — atif-duck's
per-step cache-read count (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:380`) and atif-models' per-call
`CallUsage.cached_tokens` (`packages/atif-models/src/atif_models/domain/ports.py:75`) — and neither
name states its scope, so a cross-plane join or a copied formula silently mixes them. Mitigation:
attribute the field by module path, and read the producer docstring
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:41-44` or
`packages/atif-models/src/atif_models/domain/ports.py:64-70`) before using either in arithmetic.

## The `EmbeddingProviderMismatch` guard, declared twice

**Producer:** two coordinate declarations, coupled by contract rather than by import —
`packages/atif-embed/src/atif_embed/domain/errors.py:29` with the rule at
`packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38`, and
`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33` with the rule at `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:46`

**Consumer(s):**

- `packages/atif-embed/src/atif_embed/application/embed.py:166-175` — the WRITE path calls
  `ensure_store_matches` before appending.
- `packages/atif-duck/src/atif_duck/infrastructure/registry.py:830-861` — the READ/bind path calls
  it before binding `message_embeddings` (guard-before-bind).
- `packages/atif-cli/src/atif_cli/duck_errors.py:26-31` — puts atif-duck's copy in
  `REGISTRATION_ERRORS` and maps it to exit 65 at `packages/atif-cli/src/atif_cli/duck_errors.py:74-81`.
- `packages/atif-embed/tests/test_guard_twin_pin.py:93` — reads both twin modules as source text
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
  so — `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-11` and
  `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:15-18` — and the pin test is what
  makes it more than a comment.
- **The message text must be constructed INSIDE the `raise`.** Hoisting it to a local satisfies
  ruff's `EM102` / `TRY003` and defeats the twin pin, so both rules are suppressed on purpose at
  `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:67-74`.
- **A `None` on either stored value means "empty store, any provider may claim it"** — not
  "unknown, be careful" (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:61-62`).
- **`expected_dim=None` trusts `model_id` alone**, and dim is checked only because Cohere's single
  model id can emit different Matryoshka widths
  (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:57-59`).
- **The two classes have different base classes and that matters at the CLI.** atif-duck's derives
  from `Exception` (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33`) while atif-embed's derives from that package's
  `DomainError` (`packages/atif-embed/src/atif_embed/domain/errors.py:29`), so a bare
  `except duckdb.Error` on the registration path would let atif-duck's escape as exit 1 — the
  reason `REGISTRATION_ERRORS` widens the caught set, stated at
  `packages/atif-cli/src/atif_cli/duck_errors.py:14-18`.
- **The recovery hint must not name a fixed home directory.** The store lives at
  `<corpus_root>/embeddings_lance` by default, and both copies record that naming the wrong path
  makes the operator delete nothing
  (`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:21-24` and
  `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:25-29`).

**Drift risk:** the twin pin checks that the hint constant reaches the `raise`; it does not check
that the two hint STRINGS are equal, so the copies could diverge in wording while both tests pass.
Mitigation: treat the two `RECOVERY_HINT` literals as one value — change both in the same commit,
which is what both docstrings instruct.

## harbor's public ATIF surface, consumed by our ported converters

**Producer:** upstream, `harbor.models.trajectories` (RFC 0001 data classes) and
`harbor.utils.trajectory_validator`, pinned `harbor>=0.22.0,<1` at
`packages/atif-converter/pyproject.toml:26`

**Consumer(s):**

- `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:75` — `convert_claude_code_records`, our Claude Code converter, a parity port of harbor
  0.22.0's, building `Trajectory` / `Step` / `ToolCall` / `Metrics` from the public models.
- `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:781` — `convert_codex_records`, the Codex counterpart.
- `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:68` — `validate_trajectory`, the only call into harbor's validator.
- `packages/atif-converter/tests/test_harbor_public_surface_guard.py:29` — the `ast` guard that pins the allowlist to those two modules.
- `packages/atif-converter/tests/harbor_oracle.py:94` and `:111` — the parity ORACLE: harbor's private converters, reached from the tests
  only, with frozen goldens and a live-corpus diff.

**Shape:**

```python
#: The public harbor surface atif-converter is allowed to depend on.
PUBLIC_HARBOR_MODULES: frozenset[str] = frozenset(
    {
        "harbor.models.trajectories",
        "harbor.utils.trajectory_validator",
    }
)
```

**Assumptions consumers make:**

- **The data classes are the contract, and they are versioned by ATIF.** Our converters write
  `schema_version` explicitly (`packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:72`, `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:74`), so a harbor release that adds a newer
  default still validates what we emit; a release that changes a field fails the drift tests.
- **Parity is measured, not assumed.** `test_harbor_oracle.py` asserts the live oracle equals the
  frozen goldens, and `test_parity_*` asserts our output equals both. A harbor bump that changes
  conversion behavior surfaces as a named JSON-path diff (`packages/atif-converter/tests/harbor_oracle.py:142`), which is a decision to
  record in the fidelity policy before any re-freeze.
- **Side-file discovery is ours.** Every `*.jsonl` under a session's side directory is read,
  workflow-nested ones included, with nested path parts joined by `__`
  (`packages/atif-converter/src/atif_converter/infrastructure/claude_code_converter.py:57`); harbor's
  own discovery cannot see those files, which is why the oracle stages them flat the same way.
- **harbor ships no `py.typed`**, so every import from it carries an `import-untyped` ignore.

## The `atif-sql` distribution's `==0.1.0` sibling pins

**Producer:** `packages/atif-cli/pyproject.toml:32-36` and
`packages/atif-analytics/pyproject.toml:24`

**Consumer(s):**

- `pyproject.toml:446-452` — commitizen's `version_files`, which rewrites every pin on each bump.
- `packages/atif-cli/pyproject.toml:56-60` — `[tool.uv.sources]`, the local-workspace resolution
  that these pins deliberately do NOT express.
- `packages/atif-analytics/pyproject.toml:56` — the same pairing for its single sibling dependency.
- `pyproject.toml:399-406` — `[tool.commitizen] version = "0.1.0"`, the one version this repo
  publishes.
- `pyproject.toml:17-18` — the distribution is named `atif-sql` at the workspace root, while the
  console script's module stays `atif_cli` in a member.

**Shape:**

```toml
dependencies = [
    "atif-analytics==0.1.0",
    "atif-converter==0.1.0",
    "atif-corpus==0.1.0",
    "atif-duck==0.1.0",
    "atif-embed==0.1.0",
    "cyclopts>=4.10.2",
    "loguru>=0.7.3",
]
```

```toml
version_files = [
    'packages/*/pyproject.toml:^version',
    'packages/atif-cli/pyproject.toml:^\s*"atif-',
    'packages/atif-analytics/pyproject.toml:^\s*"atif-models==',
]
```

**Assumptions consumers make:**

- **`[tool.uv.sources]` is invisible to an external installer.** uv's build backend does not
  translate a workspace source into a version constraint, so a bare name would ship as
  `Requires-Dist: atif-duck` and resolve from PyPI to whatever the newest release of that name is,
  owned by whoever owns it — the reasoning is inline at `packages/atif-cli/pyproject.toml:24-31`.
- **One version covers the whole repository.** commitizen reads its own `version` key rather than
  the PEP 621 field (`pyproject.toml:401-403`), and `version_files` propagates it to the published
  `[project] version` and to all seven member manifests; seven numbers for one artifact is the
  shape being refused.
- **The `version_files` entries are per-file rather than globbed on purpose**, because
  `--check-consistency` requires a hit in every matched file and five of the seven members carry no
  dev pin between members (`pyproject.toml:436-439`).
- **The lockfile must land in the same commit as the versions it resolves.** `pre_bump_hooks` run
  `uv lock` and `git add uv.lock` after the rewrite and before the commit
  (`pyproject.toml:422-433`), because `uv.lock` records every member's version and
  `mise run lock:check` would otherwise fail on the release commit itself.
- **A breaking change moves 0.1.0 to 0.2.0, not 1.0.0**, because `major_version_zero = true`
  (`pyproject.toml:416`) — reaching 1.0.0 is a decision, not a side effect of a `!` in a subject
  line.

**Drift risk:** a hand-edited pin, or a renamed member whose pin line stops matching its
`version_files` regex, goes stale silently until a release. Mitigation: `--check-consistency` fails
rather than skipping a file that no longer contains the current version — the guarantee is stated at
`pyproject.toml:434-436`.

## Other contracts

- **`LossReport.to_json()`** — producer
  `packages/atif-converter/src/atif_converter/domain/fidelity.py:114`, whose docstring calls the
  keys wire contract; consumed by `packages/atif-duck/src/atif_duck/infrastructure/registry.py:122-131`
  as `_LOSS_REPORT_COLUMNS` and by `packages/atif-cli/src/atif_cli/converter_adapter.py:67`.
  `record_counts` and `gaps_observed` stay `JSON` because they are an enum-keyed dict and a sorted
  list of enum values.
- **The `meta.json` provenance record** — written at
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:230-237`, projected at
  `packages/atif-duck/src/atif_duck/infrastructure/registry.py:134-141`. `harbor_version` and
  `converter_version` are supplied by the CLI (`packages/atif-cli/src/atif_cli/app.py:401-402`), so
  the corpus records which converter built it without atif-corpus importing either.
- **The embeddings-store row shape** — 7 Arrow fields written at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:115-121`, of which the
  `message_embeddings` view exposes 5
  (`packages/atif-duck/src/atif_duck/domain/catalog.py:218-224`). `text_hash` and `truncated` are
  unreachable from SQL, so a query cannot distinguish a head-only-embedded row from a complete one.
- **The Lance schema version sidecar** — `SCHEMA_VERSION = 2` and `schema_version.json` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:61-67`, kept as a sidecar rather
  than a column because reading it must not require the table.
- **The analytics parquet layout** — 11 artifact names at
  `packages/atif-analytics/src/atif_analytics/domain/layout.py:22-41`, of which atif-duck pins 9 as
  `_ANALYTICS_SOURCES` (`packages/atif-duck/src/atif_duck/infrastructure/analytics.py:53-64`).
  `REFUSALS_DIRNAME` (`packages/atif-analytics/src/atif_analytics/domain/layout.py:32`) has no view, so the refusal audit its docstring calls
  queryable is not reachable from SQL.
- **The CLI exit-code taxonomy** — `EXIT_CODES` at
  `packages/atif-cli/src/atif_cli/errors.py:25-39`, consumed at 12 sites in
  `packages/atif-cli/src/atif_cli/app.py` and mapped from DuckDB exceptions at
  `packages/atif-cli/src/atif_cli/duck_errors.py:34-63`. Code 78 is the load-bearing one: it means
  an operator must act, and unattended lanes suppress retries on it
  (`packages/atif-cli/src/atif_cli/errors.py:35-37`).
- **`PendingText` and the text stamp** — `packages/atif-embed/src/atif_embed/domain/text_stamp.py:50`,
  with `MAX_EMBEDDABLE_CHARS = 50_000` at `packages/atif-embed/src/atif_embed/domain/text_stamp.py:35` in CHARACTERS per text; the same constant governs
  both the row's `truncated` flag and the adapter's wire-level clip so the two can never disagree
  (`packages/atif-embed/src/atif_embed/domain/text_stamp.py:31-34`).
- **The `steps`-view rendering semantics, mirrored without a shared symbol** —
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:23-33` reproduces four
  of atif-duck's `steps` rules by hand (message-union flattening, `extra.source_uuids[0]` as the
  step key, `agent` → `assistant`, error recovery from
  `observation.results[].extra.tool_result_metadata.is_error`), and
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:12-25` reproduces two of
  them again.
- **The `ATIF_SQL_` settings prefix** — the one env namespace across all seven members;
  `packages/atif-embed/src/atif_embed/infrastructure/settings.py:31` pins
  `output_dimension: Literal[256, 512, 1024, 1536] = 1024`, which is the value the static catalog
  hardcodes as `FLOAT[1024]`.
- **The lean-import contract** — `pyproject.toml:164` and `pyproject.toml:181` record that `atif_cli.app` must not
  import duckdb, harbor, lancedb, boto3, or polars at module scope, asserted in a fresh interpreter
  by `packages/atif-cli/tests/test_lean_import.py`; 182 deferred-import sites exist because of it.

## See also

- [impact analysis](impact-analysis.md) — 49 shared source citations
- [module map](../architecture/module-map.md) — 37 shared source citations
- [processes](../behavior/processes.md) — 35 shared source citations
- [business logic](business-logic.md) — 33 shared source citations
- [components](../diagrams/architecture/components.md) — 20 shared source citations
