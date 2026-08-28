# atif-sql — operating manual

ATIF-native analytics over Claude Code agent trajectories.
Instead of SQL views over raw `~/.claude/projects` JSONL, this stack converts
sessions to ATIF (Harbor's Agent Trajectory Interchange Format), materializes a
corpus of ATIF documents, and layers DuckDB views on top.

## Workspace layout

uv WORKSPACE (virtual root, members under `packages/*`):

- `packages/atif-converter` — wraps harbor's `ClaudeCode` adapter; owns the
  fidelity policy (the seven known upstream conversion gaps live as types in
  `atif_converter.domain.fidelity`). Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-corpus` — corpus materialization: discovery, watermarks,
  quiescence, atomic artifact writes. Layered: `application` >
  `infrastructure` > `domain`.
- `packages/atif-duck` — DuckDB views + macros over the materialized corpus:
  16 core views and 9 macros, plus 12 analytics views and 13 analytics
  macros, all declared in a static drift-tested catalog. Layered:
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
- harbor is pinned `>=0.22.0,<0.23` — we call the private
  `ClaudeCode._convert_events_to_trajectory`, verified against 0.22.0 only.
  The unit tests in atif-converter pin upstream behavior and are the drift
  alarm for version bumps.
- loguru only, never stdlib logging (ruff banned-api enforces it).
- Settings via pydantic-settings, env prefix `ATIF_SQL_`.

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
3. `atif-sql query '<sql>'` — run it. Copy an example verbatim (the `sid`
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
