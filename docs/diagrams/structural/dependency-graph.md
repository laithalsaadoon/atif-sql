# atif-sql · Dependency graph

The internal modules and their highest-frequency external dependencies. Internal nodes are
the uv workspace members declared by `[tool.uv.workspace] members = ["packages/*"]`
(`pyproject.toml:99-100`); external nodes are first-order distributions taken from a member's own
`[project.dependencies]`, never from the installed transitive closure.

The internal direction is enforced rather than conventional. `pyproject.toml:462` declares
`[tool.importlinter]` over all seven root packages (`:366`), and `mise run lint:imports`
(`mise.toml:159-162`) is one of the nine gates `mise run check` depends on (`mise.toml:197-211`). Two of
those contracts fix the shape drawn here: the `independence` contract at `pyproject.toml:514-517`
forbids any import edge among atif_converter, atif_corpus, atif_duck, atif_models, and atif_embed, and
the `forbidden` contract at `:422-426` allows atif_analytics to import atif_models and nothing else
among the seven.

```mermaid
flowchart LR
    cli[atif-cli]
    analytics[atif-analytics]
    converter[atif-converter]
    corpus[atif-corpus]
    duck[atif-duck]
    embed[atif-embed]
    models[atif-models]

    loguru[(loguru)]:::external
    polars[(polars)]:::external
    pydantic[(pydantic)]:::external
    duckdb[(duckdb)]:::external
    pydsettings[(pydantic-settings)]:::external
    tenacity[(tenacity)]:::external
    pyarrow[(pyarrow)]:::external
    lancedb[(lancedb)]:::external
    anyio[(anyio)]:::external
    cyclopts[(cyclopts)]:::external
    boto3[(boto3)]:::external
    harbor[(harbor)]:::external

    cli --> analytics
    cli --> converter
    cli --> corpus
    cli --> duck
    cli --> embed
    analytics --> models

    cli --> cyclopts
    converter --> harbor
    duck --> duckdb
    converter -->|all members| loguru
    analytics --> polars
    analytics --> pyarrow
    embed --> lancedb
    embed --> pydsettings
    embed --> tenacity
    embed --> boto3
    models --> pydantic
    models --> anyio

    classDef external stroke-dasharray: 3 3
```

## Legend (overflow)

Some declared external distributions are left off the diagram. Importing modules are found by
grepping every `*.py` under `packages/*/src` for a `from X` or `import X` line at any indentation.

| elided node | importing module | declared at | import site |
| --- | --- | --- | --- |
| litellm | atif-converter | `packages/atif-converter/pyproject.toml:30` | `packages/atif-converter/src/atif_converter/domain/pricing.py:353`, a lazy import behind the bundled pricing table |
| pytz | none | `packages/atif-duck/pyproject.toml:29` | no import site; DuckDB's own client imports it to materialize TIMESTAMPTZ, per the comment at `:26-28` |

## Internal edges

Exactly six ordered pairs of members import each other. Counts are importing-file counts under the
source member's `src/`.

| edge | files | contract that permits it |
| --- | --- | --- |
| atif-cli to atif-analytics | 1 | atif-cli is the composition root; it is absent from both restrictive contracts (`pyproject.toml:517`, `:426`) |
| atif-cli to atif-converter | 2 | as above; declared `packages/atif-cli/pyproject.toml:33` |
| atif-cli to atif-corpus | 2 | as above; declared `packages/atif-cli/pyproject.toml:34` |
| atif-cli to atif-duck | 2 | as above; declared `packages/atif-cli/pyproject.toml:35` |
| atif-cli to atif-embed | 1 | as above; declared `packages/atif-cli/pyproject.toml:36` |
| atif-analytics to atif-models | 7 | the `forbidden` contract's single permitted edge (`pyproject.toml:519-523`); declared `packages/atif-analytics/pyproject.toml:24` |

Two absences carry meaning. atif-cli imports atif_models in zero source files even though it composes
everything else — it reaches the model registry through atif-analytics, and its
`[project.dependencies]` list omits atif-models accordingly (`packages/atif-cli/pyproject.toml:32-39`).
And no edge exists in either direction between atif-converter, atif-corpus, atif-duck, atif-embed, or
atif-models: they communicate only by writing and reading the corpus on disk. Adding any such edge
fails gate 5 of `mise run check`.

The five inter-member dependencies are `==0.1.0`-pinned rather than bare
(`packages/atif-cli/pyproject.toml:32-36`) because `[tool.uv.sources]` at `:59-64` is a local source an
external installer never sees; a bare name would ship as a `Requires-Dist` resolved from the public
PyPI namespace.

## External edges and where each is sourced

One edge per external distribution, drawn from the member whose files import it most often. Ties break
on import-site count, then on the member's own src line count, descending.

| external node | edge drawn from | declared at | import site |
| --- | --- | --- | --- |
| loguru | atif-converter | `packages/atif-converter/pyproject.toml:31` | `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:75` |
| polars | atif-analytics | `packages/atif-analytics/pyproject.toml:25` | `packages/atif-analytics/src/atif_analytics/application/use_cases/classify.py:41` |
| pydantic | atif-models | `packages/atif-models/pyproject.toml:26` | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:41` |
| duckdb | atif-duck | `packages/atif-duck/pyproject.toml:20` | `packages/atif-duck/src/atif_duck/infrastructure/registry.py:60` |
| pydantic-settings | atif-embed | `packages/atif-embed/pyproject.toml:27` | `packages/atif-embed/src/atif_embed/infrastructure/settings.py:16` |
| tenacity | atif-embed | `packages/atif-embed/pyproject.toml:28` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:36` |
| pyarrow | atif-analytics | `packages/atif-analytics/pyproject.toml:26` | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:137` |
| lancedb | atif-embed | `packages/atif-embed/pyproject.toml:22` | `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:42` |
| anyio | atif-models | `packages/atif-models/pyproject.toml:19` | `packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:29` |
| cyclopts | atif-cli | `packages/atif-cli/pyproject.toml:37` | `packages/atif-cli/src/atif_cli/app.py:35` |
| boto3 | atif-embed | `packages/atif-embed/pyproject.toml:20` | `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:136` |
| harbor | atif-converter | `packages/atif-converter/pyproject.toml:23` | `packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:161` |

Three readings the drawn edge deliberately compresses:

- **loguru is universal.** Every member declares it — `packages/atif-analytics/pyproject.toml:24`,
  `packages/atif-cli/pyproject.toml:43`, `packages/atif-converter/pyproject.toml:31`,
  `packages/atif-corpus/pyproject.toml:19`, `packages/atif-duck/pyproject.toml:21`,
  `packages/atif-embed/pyproject.toml:23`, `packages/atif-models/pyproject.toml:25` — and the edge is
  drawn from atif-converter only because more of the importing files are its than any other
  member's. The edge label states
  the real fan-out.
- **atif-corpus has no drawn external edge.** It declares three externals — loguru
  (`packages/atif-corpus/pyproject.toml:19`), pydantic (`:20`), pydantic-settings (`:21`) — and each is
  imported more often, or in more sites, by another member, so the attribution rule sources all three
  elsewhere. Its own import sites are real: `packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:19`
  and `packages/atif-corpus/src/atif_corpus/domain/sessions.py:29`.
- **harbor is a public-API edge now.** `packages/atif-converter/pyproject.toml:26` pins
  `harbor>=0.22.0,<1`, and production code imports two things from it: the ATIF data classes
  (`harbor.models.trajectories`, e.g. `packages/atif-converter/src/atif_converter/infrastructure/codex_converter.py:36`)
  and the validator (`packages/atif-converter/src/atif_converter/infrastructure/harbor_adapter.py:68`).
  The conversion itself is ours, ported from 0.22.0
  (`packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:75`,
  `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:781`). An `ast` guard pins
  the allowlist (`packages/atif-converter/tests/test_harbor_public_surface_guard.py:29`), and
  harbor's private converters are reachable from the parity oracle in the tests only
  (`packages/atif-converter/tests/harbor_oracle.py:94`). harbor ships no `py.typed` marker, so each
  import site carries an `import-untyped` ignore.

## Declared dependencies are not the installed closure

The diagram's external nodes are what a member asks for, and that is a strictly smaller set than what
`uv sync` installs. The gap includes a web stack this system never runs: harbor's own dependency block
at `uv.lock:1139-1164` lists `fastapi` (`:1082`), `supabase` (`:1097`), and `uvicorn` (`:1101`), so all
three are in the installed closure. No member declares any of them, and
`grep -rnE "^[[:space:]]*(from|import) +(fastapi|uvicorn|starlette|supabase)(\.| |$)" --include='*.py' packages/`
returns zero matches across every source and test file. atif-sql exposes no HTTP surface; it is one
console script, `atif-sql = "atif_cli.app:main"` (`packages/atif-cli/pyproject.toml:42`), over a set of
libraries.

Every storage engine in the graph is embedded, so no server node belongs here either: duckdb runs
in-process (`packages/atif-duck/pyproject.toml:20`) and lancedb is a local vector store
(`packages/atif-embed/pyproject.toml:22`). The only edge that leaves the machine is boto3 to Amazon
Bedrock, from `packages/atif-embed/src/atif_embed/infrastructure/cohere_bedrock.py:136` and
`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:31`.

## See also

- [module map](../../architecture/module-map.md) — 13 shared source citations
- [processes](../../behavior/processes.md) — 13 shared source citations
- [contract map](../../insights/contract-map.md) — 12 shared source citations
- [impact analysis](../../insights/impact-analysis.md) — 11 shared source citations
- [tech debt](../../insights/tech-debt.md) — 11 shared source citations
