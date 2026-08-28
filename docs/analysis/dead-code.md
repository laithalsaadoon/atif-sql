# atif-sql · Dead code

Measured 2026-08-28.

**Nothing in this workspace is deletable code.** Of 314 public top-level
definitions across the 100 files under `packages/*/src`, 34 have no reference
outside their own file, and **zero** have no reference anywhere. Every one of the
34 resolves to a call, construction, or annotation site inside its own module.
What the two tables below record is a narrower defect: an **export declaration
with no consumer** — a name in an `__all__` that nothing imports.

**Technique, and what it cannot see.** No code index covers this repo (there is no `.codegraph/`, no LSP index, no AST
symbol graph), and no dead-code analyzer is wired into the project: `grep -n
"vulture|dead|unused|deptry|knip"` over `pyproject.toml` and `mise.toml` returns
one unrelated hit, ty's `unused-ignore-comment` at `pyproject.toml:292`. The
finding set is therefore derived, in three passes:

1. **AST enumeration.** `ast.parse` every file under `packages/*/src/**/*.py`,
   collecting every top-level `FunctionDef` / `AsyncFunctionDef` / `ClassDef` /
   module-level assignment whose name does not begin with `_`, together with its
   `__all__` membership. 314 definitions across 100 files; 56 of the 100 modules
   declare an `__all__`.
2. **Unanchored reference index.** For each name, the whole-word pattern
   `(?<![\w.])NAME(?![\w])` against every line of every git-tracked text file —
   `git ls-files -z` yields 224 paths, all 224 readable as text — excluding the
   defining file. Driving the search from `git ls-files` is what guarantees that
   no citation here lands in a gitignored path and that `.md`, `.toml`, and
   `.yml` surfaces are searched, not only Python.
3. **Read-back at the use site.** Every candidate opened and its intra-file
   reference read, and every claimed absence re-checked against the specific
   import form a consumer would have to write.

`vulture` 2.16 was run as a cross-check (`uvx vulture packages
--min-confidence 80`, exit 3, 19 findings) and contributes **no rows**: 17 are
pytest fixture parameters requested for their side effect, one is a boto3
keyword-only parameter on a signature-matching stub, and one is the
`@classmethod` receiver of a pydantic `@field_validator`. Its unit of analysis is
one file, so it cannot answer the cross-module question.

Four limits of this derivation, stated because no index backs it:

- **Name resolution is not namespace-qualified.** A bare name match
  cross-attributes symbols that share a name across packages, and this repo has
  such collisions on purpose: `DomainError` is declared independently in
  atif-models, atif-converter, and atif-embed, and `EmbeddingProviderMismatch`
  in two. The direction of that error is conservative — it inflates reference
  counts, so it can hide a dead symbol but cannot invent one.
- **A textual match is not a semantic reference.** Every reference that keeps a
  symbol off these tables was read at its site rather than counted.
- **Import position is irrelevant to the method, deliberately.** Because
  `PLC0415` is off (`pyproject.toml:73`) to satisfy the lean-import assertion at
  `packages/atif-cli/tests/test_lean_import.py:17-32`, atif-cli's cross-package
  imports sit *inside* command bodies —
  `packages/atif-cli/src/atif_cli/app.py:201`,
  `packages/atif-cli/src/atif_cli/app.py:253`, and
  `packages/atif-cli/src/atif_cli/app.py:615` are all indented. The import graph
  uses `ast.walk`, which descends into function
  bodies, and the reference regex is unanchored, so a deferred import counts as
  an inbound edge.
- **Dynamic dispatch was swept by hand.** No first-party module is imported by
  string: the only `importlib` uses are
  `packages/atif-cli/src/atif_cli/app.py:83` and
  `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:128`.
  No `getattr` targets a module object — all six sites take an instance or an
  upstream class, including
  `packages/atif-models/src/atif_models/infrastructure/settings.py:73` and
  `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:178`.

Two enforced gates are why the list is short rather than thin. ruff runs
`select = ["ALL"]` (`pyproject.toml:55`) at zero, so `F401`, `F811`, `F841` and
`ERA001` leave no unused import, no redefinition, no unused local, and no
commented-out code — with exactly one carve-out, `pyproject.toml:133`
(`"**/__init__.py" = ["F401", "E402"]`). pyright runs
`typeCheckingMode = "strict"` (`pyproject.toml:498`) over all seven `src/` and
`tests/` trees (`pyproject.toml:499-515`) at zero errors, which enforces
`reportUnusedFunction` / `reportUnusedClass` / `reportUnusedVariable` for every
`_`-prefixed definition unused inside its own file. Neither gate can see a
*public* symbol that no other module imports, and that gap is exactly what the
tables below fill.

## Unreferenced exports

26 names appear in their own module's `__all__` and in no other tracked file.
The symbol is live inside its module — the unreferenced thing is the export
declaration, so the fix is to narrow `__all__`, not to delete code. Each row's
intra-module use site is named after the table.

| Symbol | Path | Last modified |
| --- | --- | --- |
| `CHARS_PER_TOKEN` | `packages/atif-analytics/src/atif_analytics/domain/costs.py:23` | 2026-08-28 |
| `CLASSIFICATIONS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:22` | 2026-08-28 |
| `TRAJECTORY_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:23` | 2026-08-28 |
| `CONFLICTS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:24` | 2026-08-28 |
| `USER_FRICTION_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:25` | 2026-08-28 |
| `PERCEIVED_ERRORS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:26` | 2026-08-28 |
| `REFUSALS_DIRNAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:32` | 2026-08-28 |
| `CLUSTERS_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:36` | 2026-08-28 |
| `CLUSTER_TERMS_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:37` | 2026-08-28 |
| `COMMUNITIES_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:38` | 2026-08-28 |
| `COMMUNITY_PROFILE_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:39` | 2026-08-28 |
| `STATE_DB_FILENAME` | `packages/atif-analytics/src/atif_analytics/domain/layout.py:41` | 2026-08-28 |
| `TOOL_INPUT_PREVIEW_CHARS` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:50` | 2026-08-28 |
| `UUID_HEADER_ESCAPE` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:61` | 2026-08-28 |
| `main_chain` | `packages/atif-analytics/src/atif_analytics/domain/transcript.py:132` | 2026-08-28 |
| `PART_GLOB` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:25` | 2026-08-28 |
| `is_sharded_dir` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:35` | 2026-08-28 |
| `MAX_ATTEMPTS_DEFAULT` | `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:42` | 2026-08-28 |
| `LaneStatus` | `packages/atif-cli/src/atif_cli/cron.py:68` | 2026-08-28 |
| `emit_row_batches` | `packages/atif-cli/src/atif_cli/output.py:177` | 2026-08-28 |
| `Category` | `packages/atif-duck/src/atif_duck/domain/examples.py:53` | 2026-08-28 |
| `MIN_TEXT_CHARS` | `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:45` | 2026-08-28 |
| `write_schema_version` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:159` | 2026-08-28 |
| `migrate_pre_stamp_table` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:166` | 2026-08-28 |
| `optimize_if_needed` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:285` | 2026-08-28 |
| `ReasoningEffort` | `packages/atif-models/src/atif_models/domain/registry.py:31` | 2026-08-28 |

Intra-module use sites, one per file, all opened and read. Where a symbol is used
more than once, the citation carries the first site and the remaining line
numbers follow it as plain numbers.

- `CHARS_PER_TOKEN` is the divisor at
  `packages/atif-analytics/src/atif_analytics/domain/costs.py:30`.
- The eleven `*_DIRNAME` / `*_FILENAME` constants are each joined onto
  `analytics_dir` by one property in
  `packages/atif-analytics/src/atif_analytics/domain/layout.py:58-108`, in
  declaration order.
- `TOOL_INPUT_PREVIEW_CHARS` is a default argument at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:90`;
  `UUID_HEADER_ESCAPE` is the substitution at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:73`;
  `main_chain` is called at
  `packages/atif-analytics/src/atif_analytics/domain/transcript.py:162`.
- `PART_GLOB` is globbed at
  `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:86`
  and `is_sharded_dir` is called at
  `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:97`.
- `MAX_ATTEMPTS_DEFAULT` is a default argument four times, first at
  `packages/atif-analytics/src/atif_analytics/infrastructure/sqlite_state/retry_queue.py:150`
  and again on lines 164, 252, and 258 of that file.
- `LaneStatus` annotates `packages/atif-cli/src/atif_cli/cron.py:226` and is
  constructed at `packages/atif-cli/src/atif_cli/cron.py:237`.
- `emit_row_batches` is the shared writer both public emitters call, at
  `packages/atif-cli/src/atif_cli/output.py:151` and again on line 174 of that
  file.
- `Category` feeds `get_args` at
  `packages/atif-duck/src/atif_duck/domain/examples.py:56` and annotates
  `packages/atif-duck/src/atif_duck/domain/examples.py:124`.
- `MIN_TEXT_CHARS` is interpolated into SQL at
  `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:106`.
- `write_schema_version` is called at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:185` and
  again on lines 207 and 210; `migrate_pre_stamp_table` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:205`;
  `optimize_if_needed` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:412`.
- `ReasoningEffort` types `DEFAULT_REASONING_EFFORT` at
  `packages/atif-models/src/atif_models/domain/registry.py:34` and a model field
  at `packages/atif-models/src/atif_models/domain/registry.py:56`.

Eight further public-by-naming symbols have no outside reference and are **not**
listed, because their modules make no export claim about them. Seven live in
modules that declare no `__all__` at all —
`packages/atif-converter/src/atif_converter/domain/edges.py:60`,
`packages/atif-converter/src/atif_converter/domain/edges.py:93`,
`packages/atif-converter/src/atif_converter/infrastructure/raw_records.py:45`,
`packages/atif-corpus/src/atif_corpus/application/materialize.py:108`,
`packages/atif-corpus/src/atif_corpus/domain/layout.py:22`,
`packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:35`, and
`packages/atif-corpus/src/atif_corpus/infrastructure/atomic.py:43` — so
they are module-internal helpers. The eighth,
`packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:77`
(`DistanceMetric`), sits in a module that does declare an `__all__` and is
deliberately absent from it, which is the module saying it is not exported.

## Unreferenced files

`_none_`

The import graph reports 18 of 100 source modules with no inbound first-party
import. All 18 are framework-dispatched and none is removable, so the bucket is
empty:

- **Seventeen package `__init__.py` files.** The interpreter executes a package's
  `__init__.py` on *any* submodule import, so
  `import atif_analytics.domain.costs` runs
  `packages/atif-analytics/src/atif_analytics/__init__.py`. That inbound edge is
  structural and invisible to an import-statement graph; deleting the file breaks
  every import of the package.
- **One module entry point**, dispatched by the interpreter's `-m` switch.
  `packages/atif-cli/src/atif_cli/__main__.py:3` names itself the
  `python -m atif_cli` entry and
  `packages/atif-cli/src/atif_cli/__main__.py:5-8` calls `atif_cli.app.main`. It
  is exercised in-repo by a subprocess test at
  `packages/atif-cli/tests/test_integration.py:121`.

## Dead imports

`__init__.py` is the only tree where `F401` is off (`pyproject.toml:133`), so it
is the only place an import with no consumer survives lint. atif-duck is the only
member whose `__init__.py` files re-export anything, and **all 22 re-exports are
consumed by nothing** — in-repo or out. Each name is listed in its file's
`__all__`, which is what keeps the statement alive to the linter; no tracked file
imports any of them from the package root, and the six members are not
independently installable, so no external consumer exists either.

| Path | Symbol | Imported from |
| --- | --- | --- |
| `packages/atif-duck/src/atif_duck/__init__.py:17` | `DEFAULT_PRICING` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:18` | `MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:19` | `MACRO_SIGNATURES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:20` | `VIEW_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:21` | `VIEW_SCHEMA` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:24` | `register` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:25` | `register_macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:26` | `register_raw` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/__init__.py:27` | `register_views` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:12` | `DEFAULT_PRICING` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:13` | `DESCRIPTIONS` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:14` | `MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:15` | `MACRO_SIGNATURES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:16` | `TABLE_MACRO_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:17` | `VIEW_NAMES` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:18` | `VIEW_SCHEMA` | `packages/atif-duck/src/atif_duck/domain/catalog.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:20` | `ExampleQuery` | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `packages/atif-duck/src/atif_duck/domain/__init__.py:20` | `build_examples` | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:6` | `register` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:7` | `register_macros` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:8` | `register_raw` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `packages/atif-duck/src/atif_duck/infrastructure/__init__.py:9` | `register_views` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |

The absence is checked against the exact import form a consumer would have to
write, over all 224 tracked files:

- `from atif_duck import ...` — no site.
- `from atif_duck.domain import ...` — no site.
- `from atif_duck.infrastructure import ...` — two sites, and both import the
  **submodule** `analytics`, never one of the four re-exported `register*` names:
  `packages/atif-duck/tests/test_analytics_views.py:23` and
  `packages/atif-duck/tests/test_examples.py:42`.
- No `import atif_duck` followed by attribute access, and no test anywhere
  asserts an `__all__` (`grep -rn '__all__' packages/*/tests scripts` is empty),
  so `__all__` membership is not itself pinned.

Every real consumer bypasses the facade for the deep module:

- `packages/atif-cli/src/atif_cli/app.py:615` and
  `packages/atif-cli/src/atif_cli/app.py:906` —
  `from atif_duck.infrastructure.registry import register`.
- `packages/atif-cli/src/atif_cli/app.py:1022` —
  `from atif_duck.domain.examples import ...`.
- `packages/atif-cli/src/atif_cli/app.py:1099` —
  `from atif_duck.domain.catalog import MACRO_SIGNATURES, VIEW_SCHEMA`.
- `packages/atif-cli/src/atif_cli/duck_errors.py:26`,
  `packages/atif-cli/tests/test_app.py:78`, and
  `packages/atif-cli/tests/test_app.py:98`.

The one dynamic reference to the package,
`packages/atif-cli/tests/test_lean_import.py:21`, names
`"atif_duck.infrastructure"` inside the `_FORBIDDEN_EAGER_IMPORTS` tuple at
`packages/atif-cli/tests/test_lean_import.py:17-32` — an assertion that the
module must **not** load on `import atif_cli.app`, which is the opposite of a
consumer.

Each `__init__.py` presents the facade as intentional:
`packages/atif-duck/src/atif_duck/__init__.py:6` documents
`register(con, corpus_root)` as the package entry point. Read against
`packages/atif-cli/pyproject.toml:8` — one distribution named `atif-sql`, with
the other six members shipped as `==0.1.0`-pinned internal dependencies rather
than install targets — the facade has no addressable consumer, and the CLI is
the public contract instead.

## See also

- [module map](../architecture/module-map.md) — 21 shared source citations
- [processes](../behavior/processes.md) — 17 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 17 shared source citations
- [business logic](../insights/business-logic.md) — 16 shared source citations
- [contract map](../insights/contract-map.md) — 16 shared source citations
