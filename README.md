# atif-sql

[![CI](https://github.com/laithalsaadoon/atif-sql/actions/workflows/check.yml/badge.svg?branch=main)](https://github.com/laithalsaadoon/atif-sql/actions/workflows/check.yml)
[![security](https://github.com/laithalsaadoon/atif-sql/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/laithalsaadoon/atif-sql/actions/workflows/security.yml)
[![CodeQL](https://github.com/laithalsaadoon/atif-sql/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/laithalsaadoon/atif-sql/actions/workflows/codeql.yml)
[![Scorecard](https://github.com/laithalsaadoon/atif-sql/actions/workflows/scorecard.yml/badge.svg?branch=main)](https://github.com/laithalsaadoon/atif-sql/actions/workflows/scorecard.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)
[![License Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

ATIF-native analytics over agent trajectories.

Claude Code sessions (`~/.claude/projects/**/*.jsonl`) and Codex CLI rollouts
(`~/.codex/sessions/**/rollout-*.jsonl`) are converted to ATIF (Harbor's Agent
Trajectory Interchange Format, see the Harbor ATIF RFC: RFC-0001 in
<https://github.com/laude-institute/harbor>), materialized as a corpus, and
queried through DuckDB views. Converting once at the boundary — with an
explicit, tested fidelity policy for what upstream drops — beats re-deriving
trajectory semantics inside every SQL view. Both agents land in the same views,
and `sessions.agent` says which one a row came from.

## Install

One command, one package, every capability. Conversion, materialization, the DuckDB surface,
the LLM analytics pipelines, and semantic search are all in the box — there are no extras to
choose and nothing to install afterwards to make a command work.

```bash
uv tool install atif-sql     # the CLI on PATH
uvx atif-sql schema          # or run it without installing
```

Python 3.13 or newer. The install is substantial and deliberately so: 113 runtime dependencies,
about 1.15 GiB on disk, because the analytics and vector paths carry `polars`, `pyarrow`,
`scipy`, `scikit-learn`, `umap-learn`, `hdbscan`, `lancedb`, and `duckdb`. Prebuilt wheels cover
CPython 3.13 on manylinux x86_64, macOS arm64, and Windows x86_64; Linux **aarch64** compiles
`hdbscan` from source, which needs a C toolchain. Alpine and other musl targets are not
supported. [RELEASING.md](RELEASING.md) carries the measurements.

`atif-sql analyze`, `atif-sql embed`, and `atif-sql search` call Amazon Bedrock and cost money
per invocation. Each one is dry-run by default and spends only when asked. Nothing else in the
tool needs a credential.

## How the source is organized

These seven directories under `packages/` are internal structure, not seven installs. They
exist so `import-linter` can enforce the layer and independence contracts at the source level;
the only thing documented as installable is the `atif-sql` CLI above.

| Directory | What |
| --- | --- |
| `atif-converter` | Claude Code and Codex CLI transcript → ATIF converters (ours, built on Harbor's public trajectory models) + per-agent fidelity policy (loss accounting per session) |
| `atif-corpus` | Corpus materialization: discovery, watermarks, quiescence, atomic artifact writes |
| `atif-duck` | DuckDB views + macros over the materialized corpus (core surface plus the v2 analytics surface) |
| `atif-models` | Model alias registry + structured-output LLM client; no other package hardcodes a model id |
| `atif-analytics` | eight v2 pipelines — five LLM (classify, trajectory, conflicts, friction, perceived) and three structural (cluster, terms, community) |
| `atif-embed` | Cohere Embed v4 on Bedrock + LanceDB vector store + embedding backfill |
| `atif-cli` | the composition root, and the source of the `atif-sql` command: `convert`, `materialize`, `status`, `query`, `analyze`, `embed`, `search`, `examples`, `schema`, `cron` |

## Quick start

Build the corpus from the local transcripts, then query it:

```bash
atif-sql materialize                   # discover sessions, convert, write the corpus
atif-sql status                        # corpus freshness, read-only
atif-sql query 'SELECT * FROM sessions LIMIT 5'
```

`materialize` also writes typed columnar artifacts (four parquet files per session) beside the
JSON ones, so `query` parses no JSON for those sessions; `--no-columnar` skips them, older corpora
keep working from `trajectory.json`, and `status` prints which path a corpus takes as `query path`.

For one session at a time, `atif-sql convert <session.jsonl>` converts and audits it in place.

### Codex CLI transcripts

`convert`, `materialize`, and `status` all take `--agent claude-code|codex`,
defaulting to `claude-code`. Pick `codex` and both roots move with it: the source
becomes `$CODEX_HOME` (default `~/.codex`) `/sessions`, and the corpus becomes
`~/.atif-sql/corpus/codex`. An explicit `--source-root`, `--corpus-root`, or the
matching `ATIF_SQL_*` env var still wins.

```bash
atif-sql materialize --agent codex
atif-sql status --agent codex
atif-sql query "SELECT agent, count(*) FROM sessions GROUP BY 1"
```

One corpus holds one agent, so a Codex corpus and a Claude Code corpus stay
separate directories, and one `query` reads one of them. Pass `--corpus-root` to
pick which, or `--agent codex` to get the Codex default.

Working on `atif-sql` itself is a different setup — a clone, `mise`, and `mise run check` as the
definition of done. `CONTRIBUTING.md` has it.

## Agent workflow: schema → examples → query

```bash
atif-sql schema                    # every view + macro signature (<50 ms)
atif-sql examples                  # tested example queries, grouped core/analytics/vss
atif-sql query 'SELECT * FROM tool_rank(30) LIMIT 10'
```

`atif-sql examples` (or `atif-sql query --examples`) emits runnable queries
derived from the catalog — not hardcoded strings — and every one is executed
by the test suite against a fixture corpus, so the listing cannot rot.
Piped output is JSON; filter with `--requires core|analytics|vss` and
`--category view|table-macro|scalar-macro`.

## Contributing

Setup, the five gates `mise run check` runs, the import-linter contracts you will trip, and the
Conventional Commit rule the `commit-msg` hook enforces: **[CONTRIBUTING.md](CONTRIBUTING.md)**.

Releases are cut by commitizen and published to PyPI over OIDC Trusted Publishing —
**[RELEASING.md](RELEASING.md)** covers the flow, the versioning model, and the install-weight
measurements.

## Security

Report a vulnerability privately through
[GitHub's advisory form](https://github.com/laithalsaadoon/atif-sql/security/advisories/new),
not in a public issue. Supported versions, the disclosure expectations, and what counts as a
vulnerability in a tool that reads local transcripts are in
**[SECURITY.md](SECURITY.md)**.

## License

[Apache License 2.0](LICENSE). Each of the seven module directories carries the same license
file, so a published distribution ships it too.
