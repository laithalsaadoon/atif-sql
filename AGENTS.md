# atif-sql — operating manual

ATIF-native analytics over agent trajectories, from two agents: Claude Code
(`~/.claude/projects/**/*.jsonl`) and Codex CLI
(`~/.codex/sessions/**/rollout-*.jsonl`).
Instead of SQL views over raw JSONL, this stack converts sessions to ATIF
(Harbor's Agent Trajectory Interchange Format), materializes a corpus of ATIF
documents, and layers DuckDB views on top. One corpus holds one agent, and
`sessions.agent` names it.

## Workspace layout

uv WORKSPACE (virtual root, members under `packages/*`):

- `packages/atif-converter` — wraps harbor's `ClaudeCode` and `Codex`
  adapters; owns the fidelity policy per agent (the seven known Claude Code
  gaps in `atif_converter.domain.fidelity`, the seven Codex ones in
  `atif_converter.domain.codex_fidelity`, whose values are namespaced
  `codex_*` so one `gaps_observed` array carries both). `domain.agents`
  holds the `AgentSource` enum, whose VALUES are the wire contract for the
  `--agent` flag, harbor's `Trajectory.agent.name`, and `meta.agent`.
  `domain.pricing` prices each step (`total_cost_usd`, Codex `cost_usd`) from
  litellm's bundled price table without importing litellm, and hands any
  shape it doesn't cover to litellm itself. Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-corpus` — corpus materialization: discovery, watermarks,
  quiescence, atomic artifact writes, and the `ArtifactProducer` port that
  lets the composition root add per-session files (atif-duck's columnar
  parquets) to the same atomic swap. Per-agent discovery lives in
  `domain.source_layout` (`transcript_depth` 1 for Claude Code, 3 for
  Codex's `<YYYY>/<MM>/<DD>` nesting), and `domain.agents` is an AST-pinned
  twin of the converter's enum, because the two packages may not import each
  other. Layered: `application` > `infrastructure` > `domain`.
- `packages/atif-duck` — DuckDB views + macros over the materialized corpus:
  16 core views and 9 macros, plus 12 analytics views and 13 analytics
  macros, all declared in a static drift-tested catalog. Also the
  `ColumnarArtifactProducer` that writes each session's typed parquet
  artifacts at materialize time, and the registry that reads them instead of
  `trajectory.json` when they're current (falling back per session). Layered:
  `infrastructure` > `domain`.
- `packages/atif-models` — model alias registry + structured-output LLM
  client. No other package hardcodes a Bedrock model id. Layered:
  `infrastructure` > `domain`.
- `packages/atif-analytics` — the eight v2 pipelines: five LLM pipelines
  that spend money at Bedrock and are checkpointed per session (classify,
  trajectory, conflicts, friction, perceived — see `PIPELINE_NAMES` in
  `infrastructure/sqlite_state/checkpointer.py`) plus three structural ones
  with no checkpoint (cluster, terms, community). Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-embed` — Cohere Embed v4 on Bedrock + LanceDB store + the
  backfill use case. Layered: `application` > `infrastructure` > `domain`.
- `packages/atif-cli` — cyclopts CLI composing the rest: 10 commands
  (`convert`, `materialize`, `status`, `query`, `analyze`, `embed`,
  `search`, `examples`, `schema`, `cron`).

Rules of the road:

- Independence contract (import-linter, `pyproject.toml`): converter /
  corpus / duck / models / embed may NEVER import each other. atif-cli is
  the composition root and may import them all. atif-analytics is the one
  exception — it may import atif-models and nothing else among our
  packages (a `forbidden` contract pins its other edges shut).
- Inter-package deps: declare in the member's `[project.dependencies]` AND
  `[tool.uv.sources] <pkg> = { workspace = true }`.
- harbor (`>=0.22.0,<1`) is used for its PUBLIC surface only: the ATIF data
  classes in `harbor.models.trajectories` and `harbor.utils.trajectory_validator`.
  The raw-JSONL → `Trajectory` conversion is ours
  (`atif_converter.domain.claude_code_conversion`, `domain.codex_conversion`,
  ported from 0.22.0 under Apache-2.0). Nothing under `harbor.agents` may be
  imported from `src/`; the private converters survive only in
  `packages/atif-converter/tests/harbor_oracle.py` as the parity oracle, with
  frozen goldens under `tests/goldens/` and a live-corpus parity test.
  A harbor bump is a lockfile edit plus reading the converter suite's failures
  as upstream-behavior reports (see CONTRIBUTING).
- litellm stays a declared dependency but is OFF the conversion hot path:
  `import litellm` took about four of the five seconds a 76 MB session
  needed, so `atif_converter.domain.pricing` reads litellm's own bundled
  `model_prices_and_context_window_backup.json` (found through
  `importlib.util.find_spec`, never `import litellm`) and repeats
  `litellm.cost_per_token`'s arithmetic in the same float order. The output
  is bit for bit identical; `packages/atif-converter/tests/test_pricing_identity.py`
  proves it against litellm over every covered table key and the corpus
  models. A shape the fast path doesn't replicate falls back to litellm, so a
  litellm bump still means re-running that test and reading its failures as
  upstream-pricing reports.
- loguru only, never stdlib logging (ruff banned-api enforces it).
- Settings via pydantic-settings, env prefix `ATIF_SQL_`. `agent` is applied
  at CONSTRUCTION, not copied in afterwards: both default roots derive from
  it, and only roots absent from `model_fields_set` re-derive, so an explicit
  `ATIF_SQL_SOURCE_ROOT` or `ATIF_SQL_CORPUS_ROOT` still wins. That override is
  also the one way to aim a pass at the other agent's corpus, which
  `meta.agent` catches: materialize refuses (exit 78) rather than deleting the
  other agent's sessions as ghosts.
- `materialize` runs its convert+write stage on a spawn-context process pool
  (`--workers N` / `ATIF_SQL_MATERIALIZE_WORKERS`, default `min(8, cpu_count)`).
  `--workers 1` is the single-process reference path and must stay
  byte-identical to the pool; the pool tests in
  `packages/atif-corpus/tests/test_materialize_parallel.py` pin that, and
  read worker pids back off disk so a pool that silently ran inline fails.
  The `ConverterPort` instance is pickled into each worker, so an adapter
  has to stay picklable.

## Agent query workflow

For an LLM agent driving `atif-sql`, the discovery loop is three commands:

1. `atif-sql schema` — every view (with columns) and macro signature, from
   the static catalog in <50 ms. Its output ends with an `examples_hint`.
2. `atif-sql examples` (alias: `atif-sql query --examples`) — runnable
   example queries for every view and macro, DERIVED from the catalog (never
   hardcoded per object) and each one EXECUTED by
   `packages/atif-duck/tests/test_examples.py` against a fixture corpus —
   the header's "test-executed against this version" is literal. Piped
   output is JSON `{note, examples: [{name, sql, description, requires,
   category}]}`; filter with `--requires core|analytics|vss` and
   `--category view|table-macro|scalar-macro`.
3. `atif-sql query '<sql>'` — run it. `--agent codex` points it at the Codex
   corpus without spelling the path. Copy an example verbatim (the `sid`
   exemplar is a subquery over `sessions`, so it works on any corpus) or
   adapt it. `requires: analytics` needs `atif-sql analyze` to have run;
   `requires: vss` needs `atif-sql embed --all --no-dry-run` (a bare `embed`
   exits 64: a real run needs an explicit scope). For semantic search over TEXT,
   prefer `atif-sql search 'query'` — it embeds the text first, then runs
   the same `semantic_search` kNN.

Adding a view/macro? The drift tests force: a `DESCRIPTIONS` entry in
`atif_duck/domain/catalog.py`, an `ARG_EXEMPLARS` entry for any new
parameter name, `TABLE_MACRO_NAMES` membership if the DDL is `AS TABLE`, and
the derived example must execute — or a documented `EXCLUSIONS` entry.

## Definition of done

`mise run check` fully green (lint + fmt + typecheck + lint:imports + lint:workflows +
lint:hooks + test). `mise run security` is the report-only tier beside it: findings do not fail
it, a scanner that produced no usable SARIF does.
First-time setup: `mise trust && mise install && mise run install`.

## Experiments

`experiments/` holds numbered experiment protocols (README per experiment).
Outputs go to `experiments/**/out/` (gitignored). No experiment is wired
into a gate — `mise run check` and pytest's `testpaths` both skip the
directory.

Migration parity oracles are not part of this repo. Parity is not a standing
gate: re-running one would need a reference implementation this workspace does
not depend on.
