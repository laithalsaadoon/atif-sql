# atif-sql · Impact analysis

The reader's question: *if I touch X, what else do I have to think about?*

**What "high-impact surface" means here, and why it is not inbound-import count.** The packet's
default rule is the top 8 modules by inbound reference count. That rule mis-ranks this workspace, and
the substitution is deliberate. Seven import-linter contracts forbid five of the seven packages from
importing each other (`pyproject.toml:514-517`), so the surfaces that cost the most to change are
exactly the ones with the *fewest* inbound imports — a corpus filename string has four independent
readers and zero import edges. Ranking by import count would put `atif_analytics`' 92 intra-package
edges on top and leave every cross-package contract off the list.

A surface is high-impact here when **changing it forces a coordinated edit in a file that no import
edge connects to it, and a named gate fails if you miss one.** Surfaces are ordered by how many
distinct enforcement mechanisms fire (a test, an import-linter contract, a CI step, a `KeyError` at
build time) and how many packages the edit spans.

**How the consumer sets below were derived.** There is no code index in this repo — no `.codegraph/`,
no LSP index, no symbol graph — so no count here came from one. Each set was built by grepping import
statements for the symbol and then **reading every hit's import line** to confirm which package the
name resolved to. Three properties of this codebase make that confirmation step load-bearing rather
than pedantic:

- **Every cross-package import is indented** — inside a function body or an `if TYPE_CHECKING:` block,
  because the lean-import contract defers them (`packages/atif-cli/src/atif_cli/app.py:22-26`). A
  line-anchored `^from atif_` grep returns zero cross-package consumers. The census behind this file
  allowed leading whitespace and then read each enclosing scope.
- **Names collide across packages.** `DomainError` is declared independently in three packages and
  `EmbeddingProviderMismatch` in two — `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33`
  derives from bare `Exception`, `packages/atif-embed/src/atif_embed/domain/errors.py:29` from
  atif-embed's own `DomainError`. Near-names compound it:
  `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:44` imports
  `EmbeddingProviderUnavailable`, which is not the `EmbeddingProvider` Protocol.
- **A Protocol implementation imports nothing.** Structural satisfaction leaves no edge in either
  direction, so the adapters are invisible to any import-graph query. See the Protocols section.

The internal graph is a star: `atif-cli` to its five declared siblings, plus exactly one
`atif-analytics → atif-models` edge. atif-cli neither declares nor imports atif-models
(`packages/atif-cli/pyproject.toml:32-36` lists five, none of them atif-models). The CLI surface was
enumerated from the `@app.command` decorator sites, not from route literals.

## The static DuckDB catalog

Defined at: `packages/atif-duck/src/atif_duck/domain/catalog.py:28`

Four catalogs (`VIEW_NAMES` 16, `MACRO_NAMES` 9, `ANALYTICS_VIEW_NAMES` 12,
`ANALYTICS_MACRO_SIGNATURES` 13) plus `VIEW_SCHEMA`, `TABLE_MACRO_NAMES`, `DESCRIPTIONS`, and
`DEFAULT_PRICING`. Adding one view or macro is a five-place edit, and each place has a gate.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `atif_duck.domain.examples` — derives every example from all four catalogs | direct import | yes | `packages/atif-duck/src/atif_duck/domain/examples.py:35-43` |
| `DESCRIPTIONS` — one entry per object; `_description()` raises `KeyError` without it | direct import | yes | `packages/atif-duck/src/atif_duck/domain/catalog.py:326` and `packages/atif-duck/src/atif_duck/domain/examples.py:166-175` |
| `ARG_EXEMPLARS` — needed only when a macro introduces a NEW parameter name; `_macro_sql()` raises `KeyError` without it | direct import | yes | `packages/atif-duck/src/atif_duck/domain/examples.py:75-92` and `packages/atif-duck/src/atif_duck/domain/examples.py:148-158` |
| `TABLE_MACRO_NAMES` — membership required when the DDL says `AS TABLE`, or the derived SQL uses the wrong call shape | direct import | yes | `packages/atif-duck/src/atif_duck/domain/catalog.py:308-317` |
| `atif_duck.infrastructure.registry` — owns the real DDL and imports `DEFAULT_PRICING` | direct import | yes | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:53` |
| `atif_duck.infrastructure.analytics` — `register_analytics` and `register_analytics_macros` own the DDL the two analytics catalogs describe | indirect | yes | `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:124` and `packages/atif-duck/src/atif_duck/infrastructure/analytics.py:221` |
| `atif_cli.app.schema` — deferred import of `VIEW_SCHEMA` + `MACRO_SIGNATURES` | direct import | no | `packages/atif-cli/src/atif_cli/app.py:1099` |
| `atif_cli.app.examples` — deferred import of `build_examples` + the two value tuples | direct import | no | `packages/atif-cli/src/atif_cli/app.py:1022-1026` |
| `test_examples.py::test_every_example_executes` — parametrized over every derived example, `fetchall()` forces materialization | test | yes | `packages/atif-duck/tests/test_examples.py:73-79` |
| `test_examples.py` drift catchers — description coverage, exemplar coverage, `AS TABLE` set equality, example-or-exclusion coverage | test | yes | `packages/atif-duck/tests/test_examples.py:160-211` |
| `test_duck_views.py::test_view_schema_matches_describe` — `DESCRIBE` output equals `VIEW_SCHEMA` column-for-column | test | yes | `packages/atif-duck/tests/test_duck_views.py:50-59` |
| `test_duck_views.py::test_macro_signatures_match_ddl` — regex-parses the DDL and asserts equality | test | yes | `packages/atif-duck/tests/test_duck_views.py:66-87` |
| `test_analytics_views.py::test_analytics_macro_signatures_match_ddl` — same for the analytics half | test | yes | `packages/atif-duck/tests/test_analytics_views.py:313-328` |
| `test_duck_views.py::test_default_pricing_matches_published_list_rates_exactly` — oracle table, key-for-key | test | yes | `packages/atif-duck/tests/test_duck_views.py:459-467` |
| `test_app.py::TestSchema` / `TestExamples` — assert the CLI payload against the catalog, not literals | test | no | `packages/atif-cli/tests/test_app.py:77-84` and `packages/atif-cli/tests/test_app.py:94-111` |

### Blast-radius notes

- **`EXCLUSIONS` is the only sanctioned way to add a catalog object without an example, and it is
  policed in both directions.** The dict is currently empty
  (`packages/atif-duck/src/atif_duck/domain/examples.py:99`), and a stale key — or a key that still
  emits an example — fails `test_exclusions_reference_real_catalog_objects`
  (`packages/atif-duck/tests/test_examples.py:170-175`).
- **`atif-sql schema` prints only the two CORE catalogs.** It reads `VIEW_SCHEMA` and
  `MACRO_SIGNATURES` and nothing else (`packages/atif-cli/src/atif_cli/app.py:1099`), so a new
  analytics view appears in `atif-sql examples` output (`packages/atif-duck/src/atif_duck/domain/examples.py:200-211`)
  and never in `atif-sql schema` output. This asymmetry is pinned, not accidental:
  `packages/atif-cli/tests/test_app.py:82` asserts set equality against `VIEW_SCHEMA` alone.
- **Column ORDER in `VIEW_SCHEMA` is load-bearing.** The drift test asserts tuple equality against
  `DESCRIBE`, so reordering a `SELECT` list in the DDL without reordering the catalog entry fails CI
  (`packages/atif-duck/src/atif_duck/domain/catalog.py:47-50`).

## harbor's public surface, and the parity oracle behind the ported converters

Defined at: `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:75` (Claude Code) and `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:781` (Codex)

Production code depends on harbor for two public things — the ATIF data classes in
`harbor.models.trajectories` and `harbor.utils.trajectory_validator` — pinned `harbor>=0.22.0,<1`
(`packages/atif-converter/pyproject.toml:26`). The conversion is a parity port of harbor 0.22.0's
private converters, and those private methods are reachable from the tests only, as the oracle
(`packages/atif-converter/tests/harbor_oracle.py:94`, `packages/atif-converter/tests/harbor_oracle.py:111`), frozen to goldens under `packages/atif-converter/tests/goldens/`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `test_harbor_public_surface_guard.py` — `ast` allowlist of the two public modules | test | yes | `packages/atif-converter/tests/test_harbor_public_surface_guard.py:29` |
| `harbor_adapter.convert_session()` / `codex_adapter.convert_codex_session()` — the seams: converter in, validated dict out | direct import | yes | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:83`, `packages/atif-converter/src/atif_converter/infrastructure/codex_adapter.py:63` |
| `FidelityGap` / `CodexFidelityGap` — the losses the port inherited on purpose; fixing one is a decision to diverge from the oracle | indirect | yes | `packages/atif-converter/src/atif_converter/domain/fidelity.py:52`, `packages/atif-converter/src/atif_converter/domain/codex_fidelity.py:66` |
| `test_harbor_oracle.py` — live oracle equals frozen goldens; a harbor bump that changes conversion fails here first | test | yes | `packages/atif-converter/tests/test_harbor_oracle.py:1` |
| `test_parity_live.py` — newest N local sessions of each agent, both converters, every diverging path | test | yes | `packages/atif-converter/tests/test_parity_live.py:1` |
| `atif_converter.domain.enrichment` / `codex_enrichment` — re-derive step identity by replicating the converter's normalization order | indirect | yes | `packages/atif-converter/src/atif_converter/domain/enrichment.py:1`, `packages/atif-converter/src/atif_converter/domain/codex_enrichment.py:1` |
| `EXIT_CODES["harbor_missing"] = 127` — retired, kept so the table never renumbers | config | no | `packages/atif-cli/src/atif_cli/errors.py:47` |
| `atif_cli.app.materialize` — stamps `_version_of("harbor")` into every `meta.json` | direct import | likely | `packages/atif-cli/src/atif_cli/app.py:426` |

## The materialized corpus artifact layout

Defined at: `packages/atif-corpus/src/atif_corpus/domain/layout.py:18-22`, specified by
`docs/CONTRACT.md:21-31`

atif-corpus writes it; atif-duck, atif-embed, and atif-analytics each read it **independently**,
because the independence contract forbids them a shared reader. So the layout strings exist in four
places with no import edge between them.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `atif_corpus.application.materialize` — the writer, routed through `CorpusLayout` | direct import | yes | `packages/atif-corpus/src/atif_corpus/application/materialize.py:75-76` and `packages/atif-corpus/src/atif_corpus/application/materialize.py:530` |
| `atif_duck.infrastructure.registry` — builds four globs under `corpus_root / "sessions"` | indirect | yes | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:209-213` |
| `atif_embed.infrastructure.corpus_text_rows` — its own `sessions/` walk, gated on `meta.json` | indirect | yes | `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:112-123` |
| `atif_analytics.infrastructure.corpus_reader` — re-declares three filename constants | indirect | yes | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:53-55` and `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:163-164` |
| `atif_cli.app.convert` — writes `edges.jsonl` beside a `--out` trajectory on the one-shot path | indirect | likely | `packages/atif-cli/src/atif_cli/app.py:276` |
| `atif_cli.app.status` — reads the watermark through the producer's public `read_watermark` | direct import | likely | `packages/atif-cli/src/atif_cli/app.py:460-470` |
| `docs/CONTRACT.md:21-31` — the specification; the file declares itself orchestrator-owned and changeable only via itself | config | yes | `docs/CONTRACT.md:3` |
| `test_domain.py::TestCorpusLayout.test_contract_paths` — pins all five contract strings | test | yes | `packages/atif-corpus/tests/test_domain.py:245-249` |
| Four independent fixture-corpus builders, one per consuming package | test | yes | `packages/atif-duck/tests/duck_fixtures.py:482`, `packages/atif-embed/tests/embed_fixtures.py:120`, `packages/atif-analytics/tests/analytics_fixtures.py:218`, `packages/atif-corpus/tests/corpus_fixtures.py:77` |

### Blast-radius notes

- **Nothing mechanical catches a layout change that updates one reader and misses the others.** No
  import edge connects them, so the only signal is each package's own fixture corpus going out of
  agreement with the writer. The `meta.json`-written-last torn-set rule is duplicated verbatim in all
  three readers — evidence of the duplication cost, and three places to edit if the completion marker
  ever moves (`packages/atif-duck/src/atif_duck/infrastructure/registry.py:188`,
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:28`,
  `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:14`).
- **`.staging` sits outside `sessions/` for a reader-visibility reason, not tidiness.** DuckDB's
  `read_json` glob matches dot-dirs, so moving staging under `sessions/` exposes half-written session
  dirs to every reader and to the ghost-removal walk
  (`packages/atif-corpus/src/atif_corpus/domain/layout.py:47-55`).
- **`LossReport.to_json()`'s keys are wire contract, not an internal shape.** atif-duck reads them
  back through `_LOSS_REPORT_COLUMNS`, so renaming a key breaks the `loss_reports` view with no type
  error anywhere (`packages/atif-converter/src/atif_converter/domain/fidelity.py:114-137`,
  `packages/atif-duck/src/atif_duck/infrastructure/registry.py:122`).

## The five domain Protocols

Defined at: `packages/atif-corpus/src/atif_corpus/domain/ports.py:41` (`ConverterPort`),
`packages/atif-embed/src/atif_embed/domain/ports.py:31`,
`packages/atif-embed/src/atif_embed/domain/ports.py:59`,
`packages/atif-embed/src/atif_embed/domain/ports.py:87` (`EmbeddingProvider`,
`VectorStorePort`, `TextRowsPort`), `packages/atif-models/src/atif_models/domain/ports.py:116`
(`LlmStructuredProvider`)

Five Protocols, all under `domain/`, all satisfied structurally — there is no `abstractmethod` and no
DI container in this workspace. Two of them exist because an import-linter contract forbids the direct
dependency: `ConverterPort` (atif-corpus may not import atif-converter) and `TextRowsPort` (atif-embed
may not import atif-duck). The other three are ordinary seams — atif-analytics is permitted to import
atif-models, and the two atif-embed adapters live in the same package as their ports.

**Every adapter satisfies its Protocol without importing it.** Each row below marked `indirect` names a
class whose only textual reference to the Protocol is a docstring, so no import-graph query reaches it.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `LlmStructuredProvider` ← six atif-analytics modules, every one importing it under `if TYPE_CHECKING:` | direct import | yes | `packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:28`, `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:55`, `packages/atif-analytics/src/atif_analytics/application/use_cases/conflicts.py:73`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:76`, `packages/atif-analytics/src/atif_analytics/application/use_cases/perceived.py:87`, `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py:79` |
| `OpenAiBedrockProvider` — the one real adapter; imports `CallUsage` / `ProviderUnavailable` / `RefusalError` / `SchemaT` / `UsageAccumulator` from the port module and NOT the Protocol | indirect | yes | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:49-55` and `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:143` |
| `FakeProvider` — the deterministic double, named in prose only | test | yes | `packages/atif-analytics/tests/analytics_fixtures.py:237` |
| `ConverterPort` ← `materialize` use case, three signature positions | direct import | yes | `packages/atif-corpus/src/atif_corpus/application/materialize.py:92`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:193`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:480` |
| `RealConverter` — the production adapter; imports `ConversionOutput` only, names `ConverterPort` in its docstring | indirect | yes | `packages/atif-cli/src/atif_cli/converter_adapter.py:38` and `packages/atif-cli/src/atif_cli/converter_adapter.py:44-45` |
| `FakeConverter` — shipped in `src/` rather than `tests/`; same shape, same non-import | indirect | yes | `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:16` and `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:37` |
| `ConversionOutput` — the three-field frozen dataclass that DOES cross the boundary as an import | direct import | yes | `packages/atif-corpus/src/atif_corpus/domain/ports.py:21-38` |
| `EmbeddingProvider` / `VectorStorePort` / `TextRowsPort` ← the one embed use case, under `if TYPE_CHECKING:` | direct import | yes | `packages/atif-embed/src/atif_embed/application/embed.py:35`, `packages/atif-embed/src/atif_embed/application/embed.py:46-47`, `packages/atif-embed/src/atif_embed/application/embed.py:65-67` |
| Three production adapters, one per embed port, none importing its Protocol | indirect | yes | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:285`, `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:377`, `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:156` |
| `test_converter_adapter.py` — the one place a Protocol name is imported and bound to an implementation | test | yes | `packages/atif-cli/tests/test_converter_adapter.py:17` and `packages/atif-cli/tests/test_converter_adapter.py:91` |
| `[tool.coverage.report] exclude_also` — a bare `...` line is excluded because a Protocol body is a signature | config | no | `pyproject.toml:561-564` |

### Blast-radius notes

- **Adding a method to a Protocol breaks implementations that name it nowhere.** Because no adapter
  imports its Protocol, the only mechanical signal is a `pyright`/`ty` error where the implementation is
  passed into a port-typed parameter — and for `RealConverter` that call site is in a different package
  from both the Protocol and the class (`packages/atif-cli/src/atif_cli/converter_adapter.py:44`,
  `packages/atif-corpus/src/atif_corpus/application/materialize.py:193`). `FakeConverter` lives in
  `src/`, so a missed update fails the type gate rather than only a test
  (`packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:37`).
- **`ConverterPort` deliberately permits any exception to cross it.** The docstring states that
  implementations may raise anything and the materialize use case records the failure against the
  session and continues, because one broken transcript must never abort a corpus sync. Tightening
  this into a typed-error contract changes materialize's control flow, not only a signature
  (`packages/atif-corpus/src/atif_corpus/domain/ports.py:41-47`).
- **`EmbeddingProvider.embed_documents` returns one slot per input, `None` for a failure, rather than
  raising.** That is what bounds loss: a terminally failing batch must not discard sibling batches
  whose embeddings were already billed
  (`packages/atif-embed/src/atif_embed/domain/ports.py:44-52`).

## The seven import-linter contracts

Defined at: `pyproject.toml:462-523`

Five `layers` contracts, one `independence` contract over converter / corpus / duck / models / embed,
one `forbidden` contract pinning atif-analytics to atif-models alone. These are the reason four of the
surfaces above exist as duplicated strings and Protocols instead of imports.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `mise run lint:imports` — `uv run lint-imports`, entry 5 of the 9 in `[tasks.check].depends` | config | yes | `mise.toml:162` and `mise.toml:204` |
| `root_packages` — all seven import packages must be listed, or a new member goes unchecked | config | yes | `pyproject.toml:463` |
| `atif_cli` — the composition root, absent from `independence.modules` and listed in `forbidden_modules`; it declares five siblings and NOT atif-models, so the comment granting it that permission describes an unused allowance | indirect | likely | `pyproject.toml:517`, `pyproject.toml:523`, `packages/atif-cli/pyproject.toml:32-36` |
| `ConverterPort` — exists because atif-corpus may not import atif-converter | indirect | yes | `packages/atif-corpus/src/atif_corpus/domain/ports.py:5-9` |
| `TextRowsPort` — exists because atif-embed may not import atif-duck | indirect | yes | `packages/atif-embed/src/atif_embed/domain/ports.py:11-15` |
| The `embedding_guard` twins — one rule, two copies, in atif-duck and atif-embed | direct import | yes | `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33` and `packages/atif-embed/src/atif_embed/domain/embedding_guard.py:38` |
| `test_guard_twin_pin.py` — reads the atif-duck copy as SOURCE TEXT via `ast`, never importing it | test | yes | `packages/atif-embed/tests/test_guard_twin_pin.py:41` and `packages/atif-embed/tests/test_guard_twin_pin.py:55-72` |
| The three independent corpus readers | indirect | yes | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:209`, `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:113`, `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py:53` |
| `[tool.coverage.run] source` — the same seven import packages, named rather than pathed | config | likely | `pyproject.toml:537-545` |

### Blast-radius notes

- **A new cross-package import fails gate 5 of 9 and nothing else.** `lint:imports` runs
  `uv run lint-imports` over `packages/*/src/**/*.py` plus `pyproject.toml`
  (`mise.toml:161-162`) — the type checkers and the test suite stay green, so the contract violation
  surfaces only when `mise run check` reaches that gate.
- **atif-duck declares no `layers` contract.** It has `domain/` and `infrastructure/` and no
  `application/`, so its layer direction is convention rather than enforcement; only the
  `independence` contract constrains it (`pyproject.toml:517`).
- **Working around the contract by copying a rule is a sanctioned move that carries its own gate.** The
  `embedding_guard` twins are the worked example: two copies of one pure rule and one operator hint,
  compared through an AST read of the other package's source because importing it would break the
  contract (`packages/atif-embed/tests/test_guard_twin_pin.py:3-14`).

## `EXIT_CODES` — the CLI wire contract

Defined at: `packages/atif-cli/src/atif_cli/errors.py:25-39`

Eleven keys mapping to seven distinct codes (0, 2, 64, 65, 70, 78, 127). This is the contract an agent
driving the CLI reads, so the dict is the surface — not the individual numbers, and not the messages.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `atif_cli.app` — the module-scope import; the dict is on the lean path | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:38` |
| `atif_cli.app.convert` — four exits: `invalid_input`, `empty_session`, `runtime_error`, `validation_error` | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:264-304` |
| `atif_cli.app.query` — `parse_error`, raised after the classified envelope is emitted | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:603-610` |
| `atif_cli.app.embed` — `invalid_input` plus a `EXIT_CODES[kind]` dispatch over the classifier's verdict | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:814-847` |
| `atif_cli.app.search` — `no_embeddings`, the exit that tells an operator to run `embed` | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:935-941` |
| `atif_cli.app.examples` — `invalid_input` on an unknown `--category` / `--requires` value | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:1036-1042` |
| `atif_cli.duck_errors` — classifies `duckdb.Error` into parse / catalog / runtime plus `embedding_mismatch` | direct import | yes | `packages/atif-cli/src/atif_cli/duck_errors.py:25` and `packages/atif-cli/src/atif_cli/duck_errors.py:40-86` |
| `atif_cli.cron` — the unattended lane, which suppresses retries on code 78 | direct import | likely | `packages/atif-cli/src/atif_cli/cron.py:32` and `packages/atif-cli/src/atif_cli/cron.py:175` |
| `ClassifiedError.to_payload()` — the JSON error envelope on non-TTY stderr | indirect | likely | `packages/atif-cli/src/atif_cli/errors.py:60-68` |
| `test_app.py` — asserts against the dict, never against a literal, except where the number itself is the claim | test | yes | `packages/atif-cli/tests/test_app.py:17` and `packages/atif-cli/tests/test_app.py:744` |
| `test_cron.py`, `test_integration.py`, `test_vss_commands.py` — three more suites keyed off the dict | test | yes | `packages/atif-cli/tests/test_cron.py:27`, `packages/atif-cli/tests/test_integration.py:99`, `packages/atif-cli/tests/test_vss_commands.py:20` |
| `docs/CONTRACT.md:62-66` §CLI — the documented command surface these codes are raised from | config | likely | `docs/CONTRACT.md:62-66` |

### Blast-radius notes

- **Two keys share code 2 and three share 65 on purpose.** `empty_session` and `no_embeddings` both
  exit 2; `catalog_error`, `validation_error`, and `embedding_mismatch` all exit 65. Renaming a key is
  safe for callers reading numbers and breaks every internal `EXIT_CODES["..."]` lookup, which is the
  opposite of the usual polarity (`packages/atif-cli/src/atif_cli/errors.py:26-38`).
- **Exit 78 is the only code with retry semantics attached.** `terminal_state` means an operator must
  act, so an unattended lane suppresses retries on it rather than burning identical ticks; a test pins
  both the mapping and the literal 78 (`packages/atif-cli/src/atif_cli/errors.py:35-37`,
  `packages/atif-cli/tests/test_app.py:744`).
- **A code outside the dict is the failure mode this surface exists to prevent.** An unhandled
  traceback exits 1, which appears nowhere in `EXIT_CODES`, and two tests assert that a lazily-bound
  Lance scan tripping mid-stream still classifies instead of exiting 1
  (`packages/atif-cli/src/atif_cli/duck_errors.py:15-17`,
  `packages/atif-cli/tests/test_app.py:657` and `packages/atif-cli/tests/test_app.py:1039`).

## The published `atif-sql` distribution

Defined at: `pyproject.toml:108-109`

One distribution carries every capability, as a single wheel bundling all seven module trees. The
packages under `packages/*` are development members and not install targets, so their manifests
are dev wiring: what each may import, where its tests live, and how uv installs it editable.
Nothing resolves a member from an index. The published name and the entry module diverge on
purpose: the distribution is `atif-sql`, the console script's module is `atif_cli`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `[project.dependencies]` — the union of every member's third-party requirements, and the ONLY thing an installer sees | config | yes | `pyproject.toml:132-152` |
| `[tool.hatch.build.targets.wheel] packages` — the seven module trees the wheel carries; an omission ships a wheel missing a module | config | yes | `pyproject.toml:173-182` |
| `test_distribution.py` — asserts the union, the absence of any `atif-*` requirement, and the module list with its `py.typed` markers | test | yes | `packages/atif-cli/tests/test_distribution.py:3-22` |
| Five dev pins in the CLI manifest and one in the analytics manifest, resolved locally through `[tool.uv.sources]` and never published | config | likely | `packages/atif-cli/pyproject.toml:32-36`, `packages/atif-analytics/pyproject.toml:24` |
| `[tool.commitizen] version_files` — four glob:regex entries rewriting the published version, all seven manifests, and both dev pin blocks | config | yes | `pyproject.toml:446-451` |
| `[tool.commitizen] version` — the single source of truth, because `version_provider` defaults to reading it | config | yes | `pyproject.toml:403-406` |
| `cz bump --check-consistency` in `release.yml` — fails when the current version is absent from any listed file | config | yes | `.github/workflows/release.yml:193` |
| `pre_bump_hooks` — `uv lock` then `git add uv.lock`, so the re-resolved lockfile lands in the bump commit | config | yes | `pyproject.toml:428-431` |
| `uv lock --check` — enforced as a lefthook pre-commit step and as a CI step | config | likely | `mise.toml:84`, `lefthook.yml:95`, `.github/workflows/check.yml:51` |
| The console script `atif-sql = "atif_cli.app:main"` | config | likely | `packages/atif-cli/pyproject.toml:42` |
| `tag_format = "v$version"` — `publish.yml` strips the leading `v` and `gh release create --verify-tag` fails if the formats diverge | config | likely | `pyproject.toml:411` |

### Blast-radius notes

- **The pin entries are per-file rather than globbed because `--check-consistency` requires a hit in
  every matched file**, and five of the seven members carry no dev pin at all. Adding a
  member-to-member dependency therefore needs a new `version_files` entry in the same change, or the
  next bump leaves that pin stale with nothing failing (`pyproject.toml:432-451`).
- **A third-party dependency added to a member is not added to the wheel.** hatchling reads the root's
  `[project.dependencies]` verbatim, so a member that imports something the root does not require
  produces a wheel that installs cleanly and raises `ModuleNotFoundError` at run time. The union
  assertion in `test_distribution.py` is what turns that into a failing gate
  (`packages/atif-cli/tests/test_distribution.py:92`).
- **`major_version_zero = true`, so a breaking change moves 0.1.0 to 0.2.0.** Reaching 1.0.0 is a
  decision, not a side effect of a `!` in a commit subject (`pyproject.toml:412-414`).

## The lean import path of `atif_cli.app`

Defined at: `packages/atif-cli/src/atif_cli/app.py:22-26`

A bare `import atif_cli.app` must not pull duckdb, harbor, lancedb, boto3, polars, or umap. The fast
path — `schema`, `examples`, `--help`, `--version` — needs none of them, and each costs hundreds of
milliseconds (lancedb alone ~2.6 s). Every heavy import is deferred into the command body that uses
it.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `test_lean_import.py` — an 11-module forbidden list checked in a FRESH interpreter via `subprocess` | test | yes | `packages/atif-cli/tests/test_lean_import.py:17-32` and `packages/atif-cli/tests/test_lean_import.py:43-49` |
| ruff `PLC0415` (import-outside-top-level) ignored workspace-wide, 182 measured sites | config | yes | `pyproject.toml:164` |
| `atif_cli.errors` — kept `atif_*`-free and duckdb-free so it stays on the lean path | direct import | yes | `packages/atif-cli/src/atif_cli/errors.py:13-15` |
| `atif_cli.duck_errors` — the concrete `duckdb.Error` classifier, split out for exactly that reason | direct import | yes | `packages/atif-cli/src/atif_cli/duck_errors.py:26` and `packages/atif-cli/src/atif_cli/duck_errors.py:31` |
| `atif_duck.domain.examples` — a pure domain module with no duckdb import, safe on the lean path | direct import | yes | `packages/atif-duck/src/atif_duck/domain/examples.py:26-27` |
| Nine deferred imports inside command bodies (`convert`, `materialize`, `status`, `query`, `analyze`, `embed`, `search`, `examples`, `schema`) | runtime dispatch | likely | `packages/atif-cli/src/atif_cli/app.py:615`, `packages/atif-cli/src/atif_cli/app.py:906`, `packages/atif-cli/src/atif_cli/app.py:1099` |
| `atif_cli.cron` — imported at module scope, so it must itself stay lean | direct import | yes | `packages/atif-cli/src/atif_cli/app.py:37` |
| `if TYPE_CHECKING:` blocks — the only way to name a heavy type in an annotation on this path | direct import | likely | `packages/atif-cli/src/atif_cli/app.py:33` |

### Blast-radius notes

- **This is the one surface where moving an import to the top of the file — the normal, lint-preferred
  shape — fails a test.** The deferred import IS the contract, which is why `PLC0415` is one of the 12
  workspace-wide ruff ignores and carries its measured site count inline (`pyproject.toml:164`).
- **The forbidden list names `atif_duck.infrastructure` but not `atif_duck.domain`.** That split is
  what lets `schema` and `examples` answer from the static catalog at module-import cost while `query`
  pays for duckdb only when it runs (`packages/atif-cli/tests/test_lean_import.py:21`).
- **A new `atif_*` package imported at `app.py` module scope needs a forbidden-list entry too.** The
  test enumerates modules, so a member absent from `_FORBIDDEN_EAGER_IMPORTS` can regress the fast path
  without failing anything (`packages/atif-cli/tests/test_lean_import.py:17-32`).

## Other notable surfaces

- **`DEFAULT_PRICING`** (`packages/atif-duck/src/atif_duck/domain/catalog.py:405`) — 11 models with
  published list rates. A model absent here lands in `cost_estimate`'s `unpriced_steps` rather than
  being dropped, and two tests pin the table: an exact oracle
  (`packages/atif-duck/tests/test_duck_views.py:459`) and a corpus-coverage gate
  (`packages/atif-duck/tests/test_duck_views.py:533`).
- **The model alias registry** (`packages/atif-models/src/atif_models/domain/registry.py:60`) — six
  `(family, size)` entries resolved through `resolve()` at
  `packages/atif-models/src/atif_models/domain/registry.py:109`. `ModelSpec` reaches five
  atif-analytics pipelines through the shared provider builder
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/_shared.py:34`), and
  `spec.model_id` is written into every pipeline's output row, so a re-alias changes parquet content
  (`packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:335`).
- **`RECOVERY_HINT`** (`packages/atif-embed/src/atif_embed/domain/embedding_guard.py:23` and its
  atif-duck twin) — one operator instruction, four assertions, including one that forbids naming a
  home directory and one that forbids inlining the path at the raise site
  (`packages/atif-embed/tests/test_guard_twin_pin.py:97-106`).
- **The 15 `# noqa: S608` sites** — `S608` is enforced, and each suppression carries its
  static-catalog reason inline; two of them are in the examples generator, where the interpolated
  name comes from the catalog rather than from user input
  (`packages/atif-duck/src/atif_duck/domain/examples.py:144`,
  `packages/atif-duck/src/atif_duck/domain/examples.py:161`, `pyproject.toml:211-212`).
- **Watermark and quiescence** (`packages/atif-corpus/src/atif_corpus/domain/watermark.py:28`) — the
  freshness rule that decides what `materialize` re-converts, surfaced to `status` through the
  producer's public `read_watermark` (`packages/atif-cli/src/atif_cli/app.py:460`) rather than a
  private reader.

## See also

- [contract map](contract-map.md) — 49 shared source citations
- [module map](../architecture/module-map.md) — 38 shared source citations
- [processes](../behavior/processes.md) — 35 shared source citations
- [business logic](business-logic.md) — 34 shared source citations
- [tech debt](tech-debt.md) — 24 shared source citations
