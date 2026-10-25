# atif-sql · Public API

**The supported entry point is the `atif-sql` command, not an import.** This repository ships exactly
one installable distribution, `atif-sql`, whose only console script is
`atif-sql = "atif_cli.app:main"` (`pyproject.toml:75`). Every workspace member ships inside that
one wheel rather than as install targets, and the published name is deliberately split from the
`atif_cli` module that provides the entry point (`pyproject.toml:16-18`). Install with `uvx atif-sql` or
`uv tool install atif-sql`; `docs/reference/cli.md` is the primary reference for the surface a user
actually calls.

What follows documents the **internal seam**: the highest-traffic symbols that one workspace member
imports from another. That seam is enforced rather than conventional: the root `pyproject.toml`
declares `[tool.importlinter]` over every root package at `pyproject.toml:547-548`, with an
`independence` contract at `pyproject.toml:599-602` forbidding atif-converter, atif-corpus, atif-duck,
atif-models, and atif-embed from importing each other at all, and a `forbidden` contract at
`pyproject.toml:604-608` limiting atif-analytics to atif-models alone. Every symbol below is a real
declaration read at the cited line; none of them is a supported import path for code outside this
workspace, and no compatibility promise attaches to any of them. The entries also cover the
atif-analytics prompt builders that every LLM stage shares inside that member.

These facts about the seam shape are worth carrying into every entry below. First, the boundary
abstractions are `typing.Protocol` classes, and all of them sit under a `domain/` package:
`ConverterPort` at `packages/atif-corpus/src/atif_corpus/domain/ports.py:101`, `EmbeddingProvider`,
`VectorStorePort`, and `TextRowsPort` at
`packages/atif-embed/src/atif_embed/domain/ports.py:37`, `:65`, and `:93`, and
`LlmStructuredProvider` at `packages/atif-models/src/atif_models/domain/ports.py:131`. No
`abstractmethod` exists anywhere in the workspace, so adapters satisfy a port structurally. Second,
the name `DomainError` is declared independently more than once (one base class per erroring package, at
`packages/atif-converter/src/atif_converter/domain/errors.py:14`,
`packages/atif-embed/src/atif_embed/domain/errors.py:14`, and
`packages/atif-models/src/atif_models/domain/ports.py:39`), and they are unrelated types that
share only a spelling.

There is no HTTP or RPC surface: a grep for route decorators, `FastAPI(`, `APIRouter`, `add_route`,
and `uvicorn` across every source file under `packages/*/src` returns no hits, and no module
imports fastapi, starlette, flask, or uvicorn.

### AnalyticsSettings

```py
class AnalyticsSettings(BaseSettings):
```

Env-driven configuration for the analytics pipelines, read under the `ATIF_SQL_` prefix with `.env`
support, carrying the corpus root and every pipeline knob. `transcript_caps()` projects the
transcript caps into the frozen `TranscriptCaps` at
`packages/atif-analytics/src/atif_analytics/domain/config.py:17-34`. With `session_text_tool_result_bodies` off, the
default, each tool result renders as a one-line size stub and its content never reaches the model;
`ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES=true` sends the bodies, each clipped to
`session_text_tool_result_max_chars`.

`packages/atif-analytics/src/atif_analytics/infrastructure/settings.py:60-116`

### AnthropicBedrockProvider

```py
class AnthropicBedrockProvider:
```

The `anthropic`-family `LlmStructuredProvider` adapter. It sends an Anthropic Messages body whose
`output_config.format` carries the schema after the Anthropic SDK's `transform_schema`, joins only
the `text` blocks of the response (it may open with a `thinking` block), and folds a string enum
value that differs from the schema only in letter case onto the declared member before pydantic
validation. A `refusal` stop reason raises `RefusalError`, a `max_tokens` stop retries once at one
effort step lower, and any other stop reason but `end_turn` raises `ProviderUnavailable`.

`packages/atif-models/src/atif_models/infrastructure/anthropic_bedrock.py:130-288`

### build_examples

```py
def build_examples() -> tuple[ExampleQuery, ...]:
```

Derives the full example inventory from the static catalogs in a fixed order (core views, the VSS
view, analytics views, then core macros, the VSS macro, analytics macros), so the CLI listing and the
JSON array are deterministic across runs.

`packages/atif-duck/src/atif_duck/domain/examples.py:191-252`

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
    generation_stale_session_ids: Collection[str] = (),
) -> MaterializationPlan:
```

Partitions scanned sessions into the plan buckets: the pure decision at the center of
materialization, taking the watermark and the quiescence policy as data. A session named in
`generation_stale_session_ids` (its recorded converter or columnar schema differs from this pass's)
is stale even though no source byte moved.

`packages/atif-corpus/src/atif_corpus/domain/sessions.py:169-228`

### build_structured_provider

```py
def build_structured_provider(
    spec: ModelSpec,
    *,
    region: str,
    concurrency: int = 16,
    usage: UsageAccumulator | None = None,
) -> LlmStructuredProvider:
```

Returns the adapter for `spec.family`, bound to `spec` at construction: `AnthropicBedrockProvider`
for `anthropic` and `OpenAiBedrockProvider` otherwise. Each adapter module is imported only when
its family is asked for, so building one provider never loads the other SDK.

`packages/atif-models/src/atif_models/infrastructure/providers.py:20-34`

### ConversionOutput

```py
@dataclass(frozen=True, slots=True)
class ConversionOutput:
```

Everything one conversion yields that the corpus writes to disk: the ATIF trajectory dict, the
loss-report dict, one already-serialized `edges.jsonl` line per raw record without trailing
newlines (the writer owns line termination), the same for each kept non-message record of
`session_events.jsonl`, the raw source files the converter archived, and the inline attachments
it replaced with placeholders.

`packages/atif-corpus/src/atif_corpus/domain/ports.py:65-98`

### convert_and_audit

```py
def convert_and_audit(
    session_jsonl: Path,
    *,
    include_subagents: bool = True,
    archive: SourceArchiveWriter | None = None,
) -> tuple[ConversionResult, LossReport]:
```

Converts one Claude Code session JSONL to ATIF and produces its loss accounting in the same call,
returning the enriched trajectory and a `LossReport` whose `records_dropped` is an upper bound on what
the materialized trajectory is missing. With `archive`, the same verifying re-read also streams
every source file into that writer.

`packages/atif-converter/src/atif_converter/application/convert_and_audit.py:220-294`

### CorpusLayout

```py
@dataclass(frozen=True, slots=True)
class CorpusLayout:
```

Computes every contract path under one `corpus_root` as pure path arithmetic that never touches the
filesystem, so the layout can be asserted against the contract without a tmpdir.

`packages/atif-corpus/src/atif_corpus/domain/layout.py:64-168`

### corpus_slug

```py
def corpus_slug(corpus_root: Path | str) -> str:
```

Maps one corpus root to a stable, human-legible directory key.

`packages/atif-corpus/src/atif_corpus/domain/slug.py:49-72`

### CorpusSettings

```py
class CorpusSettings(BaseSettings):
```

Env-driven settings for corpus materialization, read under the `ATIF_SQL_` prefix.

`packages/atif-corpus/src/atif_corpus/infrastructure/settings.py:88-130`

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

Env-driven settings for the embedding pipeline: the provider (`cohere` or `gemma`), each provider's
model, the output width, batch size, concurrency, and the LanceDB URI.

`packages/atif-embed/src/atif_embed/infrastructure/settings.py:46-146`

### EmbeddingProviderMismatch

```py
class EmbeddingProviderMismatch(Exception):  # noqa: N818 — names a store state, not an "*Error"
```

Raised on a stamped-versus-active `(model, dim)` mismatch, and terminal by design: vectors from two
embedding models occupy incompatible spaces, so the store must be dropped and re-embedded rather than
queried across the switch.

`packages/atif-duck/src/atif_duck/domain/embedding_guard.py:33-48`

### EmptySessionError

```py
class EmptySessionError(DomainError):
```

Harbor produced no trajectory because the session held no convertible events.

`packages/atif-converter/src/atif_converter/domain/errors.py:27-28`

### estimate_cost

```py
def estimate_cost(
    spec: ModelSpec,
    *,
    input_tokens: int,
    output_tokens: int,
    long_input_tokens: int = 0,
    long_output_tokens: int = 0,
) -> float | None:
```

USD estimate for one call or an accumulated pipeline against the prices carried on `spec`, returning
`None` when either price is unknown, which a caller must render as pricing unavailable, never as
zero. `long_input_tokens` and `long_output_tokens` are the part of the totals from calls over the
spec's long-context threshold; they bill at the long rates, the rest at the standard rates, and a
spec without long rates bills them at the standard rates.

`packages/atif-models/src/atif_models/domain/registry.py:180-210`

### fence_id

```py
def fence_id(text: str) -> str:
```

The id a prompt fence around `text` carries: the first 8 hex digits of its SHA-256. It's derived
from the text, so a prompt is a pure function of its input and a body can't close its own fence.

`packages/atif-analytics/src/atif_analytics/application/prompts.py:732-739`

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
resolved from configuration. Settings load refuses an `llm_family` outside `RUNNABLE_FAMILIES`
(`openai` and `anthropic`), the families with a wired adapter.

`packages/atif-models/src/atif_models/infrastructure/settings.py:28-82`

### LlmStructuredProvider

```py
@runtime_checkable
class LlmStructuredProvider(Protocol):
```

The port for one structured-output call, one adapter per backend, deliberately narrow: its single
`classify_structured` method takes system text, a prompt, and a schema type, and raises `RefusalError`
terminally or `ProviderUnavailable` retryably.

`packages/atif-models/src/atif_models/domain/ports.py:130-147`

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
    "step_author": ("src", "msg"),
}
```

Hand-maintained parameter names for every core macro, kept static because DuckDB's
`duckdb_functions()` returns NULL `parameters` for table macros and cannot recover them at runtime.

`packages/atif-duck/src/atif_duck/domain/catalog.py:343-354`

### main

```py
def main() -> None:
```

The console-script entry point behind the `atif-sql` command; it replaces loguru's default DEBUG sink
with WARNING-and-up (`ATIF_SQL_LOG_LEVEL` widens it) so routine reads keep stderr quiet, then hands
off to the cyclopts app and turns a cyclopts usage error into exit `64`.

`packages/atif-cli/src/atif_cli/app.py:2409-2442`

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
    source_layout: SourceLayout = CLAUDE_CODE_LAYOUT,
    workers: int = 1,
    worker_setup: Callable[[], None] | None = None,
    artifact_producer: ArtifactProducer | None = None,
    expected_meta: Mapping[str, object] | None = None,
    converter_schema: int | None = None,
    session_sink: SessionSink | None = None,
    sink_batch_size: int = DEFAULT_SINK_BATCH_SIZE,
    convert_clock: Callable[[], float] = time.perf_counter,
) -> MaterializationReport:
```

Runs one materialization pass over the raw transcript corpus, taking conversion as an injected
`ConverterPort` so tests substitute a fake and atif-cli injects `RealConverter`, its adapter over
atif-converter.
`workers` above `1` runs the convert+write stage on a spawn-context process pool that pickles
`converter` into each worker; `worker_setup` is the composition root's per-process hook (atif-cli
uses it to install its stderr log sink). The default `1` is the inline path.

`packages/atif-corpus/src/atif_corpus/application/materialize.py:1497-1826`

### MaterializationReport

```py
@dataclass(frozen=True, slots=True)
class MaterializationReport:
```

What one materialization pass did, for logs and the CLI status line. `convert_seconds` is the
per-session sum and `workers` the pool size the pass used, so with several workers
`convert_seconds` can exceed `total_seconds`, the wall clock.

`packages/atif-corpus/src/atif_corpus/application/materialize.py:219-317`

### message_prompt

```py
def message_prompt(message: str) -> str:
```

The friction stage's user message: the one human message inside a `<message id="...">` fence whose
id is `fence_id(message)`, then the `FRICTION_REQUEST` line.

`packages/atif-analytics/src/atif_analytics/application/prompts.py:753-756`

### ModelSpec

```py
@dataclass(frozen=True, slots=True)
class ModelSpec:
    family: Family
    size: Size
    model_id: str
    pricing_in: float | None
    pricing_out: float | None
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    reasoning_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT
    supports_strict_json: bool = True
    long_context_threshold: int | None = None
    long_pricing_in: float | None = None
    long_pricing_out: float | None = None
    chars_per_token: float = 4.0
```

One concrete model behind a `(family, size)` alias, carrying its Bedrock model id, USD-per-1M-token
prices that may be `None`, and whether the family supports native strict-JSON structured output.
Past `long_context_threshold` input tokens the vendor bills the whole request at `long_pricing_in`
and `long_pricing_out`, and `None` means one rate across the window. `chars_per_token` is the
family's measured characters per token on rendered transcripts, which the dry-run plans use to turn
a prompt length into tokens.

`packages/atif-models/src/atif_models/domain/registry.py:49-85`

### OpenAiBedrockProvider

```py
class OpenAiBedrockProvider:
```

The default `LlmStructuredProvider` adapter, for the `openai` family, satisfying the port structurally
rather than by inheritance. It sends a chat-completions body with a strict `response_format` JSON
schema, and a `length` finish reason retries once at one `reasoning_effort` step lower.

`packages/atif-models/src/atif_models/infrastructure/openai_bedrock.py:81-246`

### QuiescencePolicy

```py
@dataclass(frozen=True, slots=True)
class QuiescencePolicy:
```

The rule for when a session is settled enough to convert: quiescent once its newest source mtime is
at least `quiesce_seconds` in the past, an observed rather than announced signal that the writer
finished.

`packages/atif-corpus/src/atif_corpus/domain/sessions.py:70-114`

### RefusalError

```py
class RefusalError(DomainError):
```

Terminal error for a model refusal (a content filter, or a refusal `finish_reason` or `stop_reason`),
as distinct from a retryable provider fault.

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
    lake: LakeReader | None = None,
    lance_uris: Sequence[Path] | None = None,
) -> RawSources:
```

Registers raw readers, views, VSS, core macros, the authorship surface (`step_author`, `user_steps`,
`human_turns`, `session_outcomes`), and the analytics views and macros over `corpus_root` in
dependency order on one DuckDB connection. Without `lake`, every call re-scans the corpus and parses
each session that has no current per-session parquet into TEMP tables, so the cost is O(JSON sessions)
per connection and a caller should reuse one connection per process. With `lake`, the raw relations
are views over the lake's tables and nothing loads eagerly.

`packages/atif-duck/src/atif_duck/infrastructure/registry.py:2095-2194`

### run_analyze

```py
def run_analyze(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = 30,
    limit: int | None = None,
    dry_run: bool = True,
    skip_classify: bool = False,
    skip_conflicts: bool = False,
    skip_friction: bool = False,
    skip_perceived: bool = False,
    source: SessionSource | None = None,
) -> dict[str, Any]:
```

Runs the LLM stages in order (classify, conflicts, friction, perceived) over one shared corpus
reader and one run budget, returning a per-stage summary dict; the `skip_*` flags subtract individual
stages, and `source` says where session data comes from (atif-cli passes the lake's when the lake
holds the corpus; the default reads the per-session files).

`packages/atif-analytics/src/atif_analytics/application/analyze.py:37-155`

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

Discovers unembedded steps, embeds them, and appends to the Lance store; all its ports default to
None and are constructed lazily so a dry run never loads boto3.

`packages/atif-embed/src/atif_embed/application/embed.py:61-277`

### scan_source_root

```py
def scan_source_root(
    source_root: Path,
    layout: SourceLayout = CLAUDE_CODE_LAYOUT,
) -> tuple[SessionSource, ...]:
```

The sessions found under `source_root`, discarding the per-scan diagnostics that the fuller
`scan_sources` returns.

`packages/atif-corpus/src/atif_corpus/infrastructure/scanner.py:302-316`

### TrajectoryValidationError

```py
class TrajectoryValidationError(DomainError):
```

The converted trajectory failed harbor's `TrajectoryValidator`, and the exception carries the
validator's error list so a caller can report the exact schema violations.

`packages/atif-converter/src/atif_converter/domain/errors.py:31-40`

### transcript_prompt

```py
def transcript_prompt(transcript: str, *, request: str) -> str:
```

The user message for the classify, conflicts, and perceived stages: the transcript inside a
`<transcript id="...">` fence whose id is `fence_id(transcript)`, then the one-line `request`. Each
stage passes its own request constant, `CLASSIFY_REQUEST`, `CONFLICTS_REQUEST`, or
`PERCEIVED_REQUEST`, declared at
`packages/atif-analytics/src/atif_analytics/application/prompts.py:726-728`.

`packages/atif-analytics/src/atif_analytics/application/prompts.py:742-750`

### VIEW_SCHEMA

```py
VIEW_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
```

The hand-maintained column schema for every core view, where column order is load-bearing: a drift
test asserts tuple equality against DuckDB `DESCRIBE` output, so editing view DDL without updating
this dict fails CI instead of surfacing as a runtime mystery.

`packages/atif-duck/src/atif_duck/domain/catalog.py:56-316`

## See also

- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [business logic](../insights/business-logic.md)
- [contract map](../insights/contract-map.md)
- [impact analysis](../insights/impact-analysis.md)
