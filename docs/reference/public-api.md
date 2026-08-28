# atif-sql · Public API

**The supported entry point is the `atif-sql` command, not an import.** This repository ships exactly
one installable distribution, `atif-sql`, whose only console script is
`atif-sql = "atif_cli.app:main"` — `packages/atif-cli/pyproject.toml:43`. The other six workspace
members ship as that distribution's pinned dependencies and are described in the manifest itself as
"internal module boundaries", with the distribution name deliberately split from the `atif_cli` module
name — `packages/atif-cli/pyproject.toml:2-7`. Install with `uvx atif-sql` or
`uv tool install atif-sql`; `docs/reference/cli.md` is the primary reference for the surface a user
actually calls.

What follows documents the **internal seam**: the 30 highest-traffic symbols that one workspace member
imports from another. That seam is enforced rather than conventional — the root `pyproject.toml`
declares `[tool.importlinter]` over all seven root packages at `pyproject.toml:364-365`, with an
`independence` contract at `pyproject.toml:416-419` forbidding atif-converter, atif-corpus, atif-duck,
atif-models, and atif-embed from importing each other at all, and a `forbidden` contract at
`pyproject.toml:421-425` limiting atif-analytics to atif-models alone. Every symbol below is a real
declaration read at the cited line; none of them is a supported import path for code outside this
workspace, and no compatibility promise attaches to any of them.

Two facts about the seam shape are worth carrying into every entry below. First, the boundary
abstractions are `typing.Protocol` classes and there are exactly five, all under a `domain/` package:
`ConverterPort` at `packages/atif-corpus/src/atif_corpus/domain/ports.py:41`, `EmbeddingProvider`,
`VectorStorePort`, and `TextRowsPort` at
`packages/atif-embed/src/atif_embed/domain/ports.py:31`, `:59`, and `:87`, and
`LlmStructuredProvider` at `packages/atif-models/src/atif_models/domain/ports.py:116`. No
`abstractmethod` exists anywhere in the workspace, so adapters satisfy a port structurally. Second,
the name `DomainError` is declared three independent times — one base class per erroring package, at
`packages/atif-converter/src/atif_converter/domain/errors.py:14`,
`packages/atif-embed/src/atif_embed/domain/errors.py:14`, and
`packages/atif-models/src/atif_models/domain/ports.py:39` — and the three are unrelated types that
share only a spelling.

There is no HTTP or RPC surface: a grep for route decorators, `FastAPI(`, `APIRouter`, `add_route`,
and `uvicorn` across all 100 source files under `packages/*/src` returns zero hits, and no module
imports fastapi, starlette, flask, or uvicorn.

### AnalyticsSettings

```py
class AnalyticsSettings(BaseSettings):
```

Env-driven configuration for the analytics pipelines, read under the `ATIF_SQL_` prefix with `.env`
support, carrying the corpus root, the Lance URI, and every pipeline knob.

`packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:68-191`

### build_examples

```py
def build_examples() -> tuple[ExampleQuery, ...]:
```

Derives the full example inventory from the static catalogs in a fixed order — core views, the VSS
view, analytics views, then core macros, the VSS macro, analytics macros — so the CLI listing and the
JSON array are deterministic across runs.

`packages/atif-duck/src/atif_duck/domain/examples.py:178-239`

### build_plan

```py
def build_plan(
    sessions: Sequence[SessionSource],
    *,
    watermark: Mapping[str, int],
    policy: QuiescencePolicy,
    now_ns: int,
    force: bool = False,
    unmaterialized_session_ids: Collection[str] = (),
) -> MaterializationPlan:
```

Partitions scanned sessions into the three plan buckets — the pure decision at the centre of
materialization, taking the watermark and the quiescence policy as data.

`packages/atif-corpus/src/atif_corpus/domain/sessions.py:159-211`

### ConversionOutput

```py
@dataclass(frozen=True, slots=True)
class ConversionOutput:
```

Everything one conversion yields that the corpus writes to disk: the ATIF trajectory dict, the
loss-report dict, and one already-serialized `edges.jsonl` line per raw record without trailing
newlines, because the writer owns line termination.

`packages/atif-corpus/src/atif_corpus/domain/ports.py:21-38`

### convert_and_audit

```py
def convert_and_audit(
    session_jsonl: Path,
    *,
    include_subagents: bool = True,
) -> tuple[ConversionResult, LossReport]:
```

Converts one Claude Code session JSONL to ATIF and produces its loss accounting in the same call,
returning the enriched trajectory and a `LossReport` whose `records_dropped` is an upper bound on what
the materialized trajectory is missing.

`packages/atif-converter/src/atif_converter/application/convert_and_audit.py:105-155`

### CorpusLayout

```py
@dataclass(frozen=True, slots=True)
class CorpusLayout:
```

Computes every contract path under one `corpus_root` as pure path arithmetic that never touches the
filesystem, so the layout can be asserted against the contract without a tmpdir.

`packages/atif-corpus/src/atif_corpus/domain/layout.py:25-75`

### corpus_slug

```py
def corpus_slug(corpus_root: Path | str) -> str:
```

Maps one corpus root to a stable, human-legible directory key.

`packages/atif-corpus/src/atif_corpus/domain/slug.py:32-50`

### CorpusSettings

```py
class CorpusSettings(BaseSettings):
```

Env-driven settings for corpus materialization, read under the `ATIF_SQL_` prefix.

`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:44-58`

### DomainError

```py
class DomainError(Exception):
```

Base class for atif-embed domain errors, subclassed by the embed-specific failures declared beneath
it in the same module.

`packages/atif-embed/src/atif_embed/domain/errors.py:14-26`

### EmbedSettings

```py
class EmbedSettings(BaseSettings):
```

Env-driven settings for the embedding pipeline — model, batch size, concurrency, and the LanceDB URI.

`packages/atif-embed/src/atif_embed/infrastructure/settings.py:19-64`

### EmbeddingProviderMismatch

```py
class EmbeddingProviderMismatch(Exception):  # noqa: N818 — names a store state, not an "*Error"
```

Raised on a stamped-versus-active `(model, dim)` mismatch, and terminal by design: vectors from two
embedding models occupy incompatible spaces, so the store must be dropped and re-embedded rather than
queried across the switch.

`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33-43`

### EmptySessionError

```py
class EmptySessionError(DomainError):
```

Harbor produced no trajectory because the session held no convertible events.

`packages/atif-converter/src/atif_converter/domain/errors.py:27-28`

### estimate_cost

```py
def estimate_cost(spec: ModelSpec, *, input_tokens: int, output_tokens: int) -> float | None:
```

USD estimate for one call or an accumulated pipeline against the prices carried on `spec`, returning
`None` when either price is unknown — which a caller must render as pricing unavailable, never as
zero.

`packages/atif-models/src/atif_models/domain/registry.py:125-137`

### InvalidSessionInput

```py
class InvalidSessionInput(DomainError):  # noqa: N818 — named as a terminal input verdict, not "*Error"
```

The supplied path is not a Claude Code session JSONL file.

`packages/atif-converter/src/atif_converter/domain/errors.py:23-24`

### LlmSettings

```py
class LlmSettings(BaseSettings):
```

Env-driven model selection for the LLM analytics pipelines, and the only place a Bedrock model id is
resolved from configuration.

`packages/atif-models/src/atif_models/infrastructure/settings.py:28-81`

### LlmStructuredProvider

```py
@runtime_checkable
class LlmStructuredProvider(Protocol):
```

The port for one structured-output call, one adapter per backend, deliberately narrow: its single
`classify_structured` method takes system text, a prompt, and a schema type, and raises `RefusalError`
terminally or `ProviderUnavailable` retryably.

`packages/atif-models/src/atif_models/domain/ports.py:115-132`

### MACRO_SIGNATURES

```py
MACRO_SIGNATURES: dict[str, tuple[str, ...]] = {
    "ago": ("interval_text",),
    "model_used": ("sid",),
    "cost_estimate": ("sid",),
    "tool_rank": ("last_n_days",),
    "todo_velocity": ("sid",),
    "subagent_fanout": ("sid",),
    "semantic_search": ("query_vec", "k"),
    "skill_rank": ("last_n_days",),
    "skill_source_mix": ("last_n_days",),
}
```

Hand-maintained parameter names for all nine core macros, kept static because DuckDB's
`duckdb_functions()` returns NULL `parameters` for table macros and cannot recover them at runtime.

`packages/atif-duck/src/atif_duck/domain/catalog.py:242-257`

### main

```py
def main() -> None:
```

The console-script entry point behind the `atif-sql` command; it replaces loguru's default DEBUG sink
with WARNING-and-up so routine reads keep stderr quiet, then hands off to the cyclopts app.

`packages/atif-cli/src/atif_cli/app.py:1125-1135`

### materialize

```py
def materialize(
    *,
    source_root: Path,
    corpus_root: Path,
    converter: ConverterPort,
    materialized_at: str,
    harbor_version: str,
    converter_version: str,
    quiesce_seconds: int = 300,
    force: bool = False,
    now_ns: int | None = None,
    session_ids: Collection[str] | None = None,
) -> MaterializationReport:
```

Runs one materialization pass over the raw transcript corpus, taking conversion as an injected
`ConverterPort` so tests substitute a fake and atif-cli injects the harbor-backed adapter.

`packages/atif-corpus/src/atif_corpus/application/materialize.py:476-645`

### MaterializationReport

```py
@dataclass(frozen=True, slots=True)
class MaterializationReport:
```

What one materialization pass did, for logs and the CLI status line.

`packages/atif-corpus/src/atif_corpus/application/materialize.py:117-157`

### ModelSpec

```py
@dataclass(frozen=True, slots=True)
class ModelSpec:
```

One concrete model behind a `(family, size)` alias, carrying its Bedrock model id, USD-per-1M-token
prices that may be `None`, and whether the family supports native strict-JSON structured output.

`packages/atif-models/src/atif_models/domain/registry.py:38-57`

### OpenAiBedrockProvider

```py
class OpenAiBedrockProvider:
```

The default `LlmStructuredProvider` adapter, satisfying the port structurally rather than by
inheritance.

`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:142-311`

### QuiescencePolicy

```py
@dataclass(frozen=True, slots=True)
class QuiescencePolicy:
```

The rule for when a session is settled enough to convert: quiescent once its newest source mtime is
at least `quiesce_seconds` in the past, an observed rather than announced signal that the writer
finished.

`packages/atif-corpus/src/atif_corpus/domain/sessions.py:70-104`

### RefusalError

```py
class RefusalError(DomainError):
```

Terminal error for a model refusal — a content filter or a refusal finish reason — as distinct from a
retryable provider fault.

`packages/atif-models/src/atif_models/domain/ports.py:43-48`

### register

```py
def register(
    con: duckdb.DuckDBPyConnection,
    corpus_root: Path,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    skip_vss: bool = False,
    lance_uri: Path | None = None,
    expected_model: str | None = None,
    expected_dim: int | None = None,
) -> None:
```

Registers raw readers, views, VSS, and macros over `corpus_root` in dependency order on one DuckDB
connection; every call re-scans the whole corpus into TEMP tables, so the cost is O(corpus) per
connection and a caller should reuse one connection per process.

`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1212-1278`

### run_analyze

```py
def run_analyze(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = 30,
    limit: int | None = None,
    dry_run: bool = True,
    structural_only: bool = False,
    llm_only: bool = False,
    skip_cluster: bool = False,
    skip_terms: bool = False,
    skip_community: bool = False,
    skip_classify: bool = False,
    skip_trajectory: bool = False,
    skip_conflicts: bool = False,
    skip_friction: bool = False,
    skip_perceived: bool = False,
    force_cluster: bool = False,
    force_community: bool = False,
) -> dict[str, Any]:
```

Runs the analytics pipeline end to end, structure first and then the LLM stages, returning a
per-stage summary dict; `structural_only` and `llm_only` are mutually exclusive lane selectors and the
`skip_*` flags subtract individual stages from whichever lane runs.

`packages/atif-analytics/src/atif_analytics/application/analyze.py:36-207`

### run_backfill

```py
async def run_backfill(
    *,
    corpus_root: Path,
    settings: EmbedSettings,
    embedder: EmbeddingProvider | None = None,
    text_rows: TextRowsPort | None = None,
    store: VectorStorePort | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> int | dict[str, Any]:
```

Discovers unembedded steps, embeds them, and appends to the Lance store; all three ports default to
None and are constructed lazily so a dry run never loads boto3.

`packages/atif-embed/src/atif_embed/application/embed.py:61-262`

### scan_source_root

```py
def scan_source_root(source_root: Path) -> tuple[SessionSource, ...]:
```

The sessions found under `source_root`, discarding the per-scan diagnostics that the fuller
`scan_sources` returns.

`packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:211-222`

### TrajectoryValidationError

```py
class TrajectoryValidationError(DomainError):
```

The converted trajectory failed harbor's `TrajectoryValidator`, and the exception carries the
validator's error list so a caller can report the exact schema violations.

`packages/atif-converter/src/atif_converter/domain/errors.py:31-40`

### VIEW_SCHEMA

```py
VIEW_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
```

The hand-maintained column schema for all 16 core views, where column order is load-bearing: a drift
test asserts tuple equality against DuckDB `DESCRIBE` output, so editing view DDL without updating
this dict fails CI instead of surfacing as a runtime mystery.

`packages/atif-duck/src/atif_duck/domain/catalog.py:47-225`

## See also

- [module map](../architecture/module-map.md) — 23 shared source citations
- [processes](../behavior/processes.md) — 22 shared source citations
- [business logic](../insights/business-logic.md) — 20 shared source citations
- [contract map](../insights/contract-map.md) — 18 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 18 shared source citations
