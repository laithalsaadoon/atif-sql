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

Python 3.13 or newer. The install is substantial and deliberately so, because the analytics and
vector paths carry `polars`, `pyarrow`, `lancedb`, and `duckdb`. Conversion itself needs none of
that weight: the ATIF models are vendored from Harbor and step pricing reads a vendored copy of
litellm's price data, so neither `harbor` nor `litellm` is installed. Prebuilt wheels cover CPython
3.13 on manylinux x86_64 and aarch64, macOS arm64, and Windows x86_64. Alpine and other musl targets
are not supported. [RELEASING.md](RELEASING.md) says how to measure the install.

`atif-sql analyze`, `atif-sql embed`, and `atif-sql search` call Amazon Bedrock and cost money.
`analyze` is a dry run until you pass `--no-dry-run`, `embed` refuses to run without `--limit` or
`--all` (and `--dry-run` previews it), and `search` embeds one query per call. Nothing else in the
tool needs a credential.

## What you can do

Start with the question you want to answer or the work you want to keep. Each command links
to its flags and exit codes in [the CLI reference](docs/reference/cli.md).

Analysis, embedding, and text search use Amazon Bedrock. `analyze` previews by default;
`embed` needs `--dry-run` to preview an explicit scope; `search` charges for a query embedding
on each successful call. Rows marked **costs money** describe paid execution.

| Task | Command | Outcome |
| --- | --- | --- |
| Keep a history of your Claude Code work | [`atif-sql materialize`](docs/reference/cli.md#materialize) | Saves quiet sessions for later queries and keeps them when source transcripts disappear. Re-runs update changed sessions and those with an older converter generation. |
| Bring your Codex work into the same analysis workflow | [`atif-sql materialize --agent codex`](docs/reference/cli.md#materialize) | Saves Codex rollouts in a separate corpus with the same query surface as Claude Code. |
| Inspect or share one conversation as ATIF | [`atif-sql convert <session.jsonl>`](docs/reference/cli.md#convert) | Prints its trajectory and loss report. Add `--trajectory-out` to save the trajectory and adjacent `edges.jsonl` file. |
| Know whether your recent work is included | [`atif-sql status`](docs/reference/cli.md#status) | Shows corpus freshness, session counts, and what another `materialize` pass would update, without changing data. |
| Query a large history without rereading every transcript | [`atif-sql lake rebuild`](docs/reference/cli.md#lake-rebuild) | Builds the shared DuckLake query store. Later `materialize` passes keep it current. |
| Check that query results match your saved conversations | [`atif-sql lake verify`](docs/reference/cli.md#lake-verify) | Compares stored rows with session artifacts and exits 65 if they differ. |
| Diagnose missing or stale query data | [`atif-sql lake status`](docs/reference/cli.md#lake-status) | Shows whether the lake exists, is current, and contains the expected corpora, plus its last write. |
| Recover disk space from query storage | [`atif-sql lake compact`](docs/reference/cli.md#lake-compact) | Merges small files, expires old snapshots, and removes unreferenced files while keeping current rows. |
| Find the fields you need to answer a question | [`atif-sql schema`](docs/reference/cli.md#schema) | Lists columns and macro signatures, tagged by required data: `core`, `analytics`, or `vss`. No corpus needed. |
| Start with a query that already works | [`atif-sql examples`](docs/reference/cli.md#examples) | Gives tested queries to copy or adapt. Filters select the required data and query type. |
| Answer your own question about past sessions | [`atif-sql query '<sql>'`](docs/reference/cli.md#query) | Returns a table to read or JSON to pipe into another tool, through a read-only sandboxed connection. |
| Compare work across Claude Code and Codex | [`atif-sql query --all-corpora 'SELECT agent, count(*) FROM sessions GROUP BY agent'`](docs/reference/cli.md#query) | Counts sessions by agent across all corpora in the lake. Other queries can use `sessions.corpus` to identify each source. Needs the lake. |
| See which tools your agents rely on most | [`atif-sql query 'SELECT * FROM tool_rank(30) LIMIT 10'`](docs/reference/cli.md#query) | Ranks tool use by call count over the last 30 days. |
| Estimate the cost of a session | [`atif-sql query 'SELECT * FROM cost_estimate((SELECT session_id FROM sessions LIMIT 1)) LIMIT 10'`](docs/reference/cli.md#query) | Gives a lower-bound USD estimate and priced/unpriced step counts. Unpriced steps and cache-read charges are excluded. Replace the subquery to choose a session. |
| Find recurring moments when you correct your agents | [`atif-sql query 'SELECT * FROM friction_counts(30) LIMIT 10'`](docs/reference/cli.md#query) | Counts detected friction labels over the last 30 days, after paid analysis has run. |
| Plan a review of agent interactions before paying | [`atif-sql analyze`](docs/reference/cli.md#analyze) | Previews the work and estimated cost. **Costs money** with `--no-dry-run`, which fills the classifications, conflicts, user friction, and perceived errors views. |
| Preview how much history to prepare for semantic search | [`atif-sql embed --limit 100 --dry-run`](docs/reference/cli.md#embed) | Previews embedding up to 100 steps. Removing `--dry-run` **costs money** and prepares those steps for search. |
| Find prior work when you remember the topic, not the words | [`atif-sql search '<text>'`](docs/reference/cli.md#search) | **Costs money** for one query embedding. Finds similar steps with session identifiers and snippets. Needs existing embeddings from `embed`. |
| Find related work across both agent histories | [`atif-sql search --all-corpora '<text>'`](docs/reference/cli.md#search) | **Costs money.** Searches all corpora's embedding stores and identifies each result's corpus. Needs the lake and existing embeddings. |
| Free space in a corpus from an older version | [`atif-sql corpus slim`](docs/reference/cli.md#corpus-slim) | Previews byte savings. `--no-dry-run` compresses JSON and removes old parquet only after lake verification succeeds. |
| Keep your history refreshed without remembering each run | [`atif-sql cron install --script <refresh-script>`](docs/reference/cli.md#cron-install) | Prints a schedule to review and install manually. The schedule includes paid embedding and analytics. Obtain `scripts/atif-sql-refresh.sh` from a repo checkout; packaged installs need its explicit path. |
| Check whether scheduled refreshes ran | [`atif-sql cron status --script <refresh-script>`](docs/reference/cli.md#cron-status) | Shows each lane's last completion, skip, and current lock holder from the refresh script's logs. |

## How the source is organized

The directories under `packages/` are internal structure, not separate installs. They
exist so `import-linter` can enforce the layer and independence contracts at the source level;
the only thing documented as installable is the `atif-sql` CLI above.

| Directory | What |
| --- | --- |
| `atif-converter` | Claude Code and Codex CLI transcript → ATIF converters (ours, built on Harbor's ATIF models, vendored) + per-agent fidelity policy (loss accounting per session) |
| `atif-corpus` | Corpus materialization: discovery, watermarks, quiescence, atomic artifact writes |
| `atif-duck` | DuckDB views + macros over the materialized corpus (core surface plus the v2 analytics surface) |
| `atif-models` | Model alias registry + structured-output LLM client; no other package hardcodes a model id |
| `atif-analytics` | the LLM pipelines `atif-sql analyze` runs: classify, conflicts, friction, and perceived |
| `atif-embed` | Cohere Embed v4 on Bedrock + LanceDB vector store + embedding backfill |
| `atif-cli` | the composition root, and the source of the `atif-sql` command (see [What you can do](#what-you-can-do)) |

## Quick start

Build the corpus from the local transcripts, then query it:

```bash
atif-sql materialize                   # discover sessions, convert, write the corpus
atif-sql lake rebuild                  # load every corpus into the DuckLake once
atif-sql status                        # corpus and lake freshness, read-only
atif-sql query 'SELECT * FROM sessions LIMIT 5'
```

The lake lives at `~/.atif-sql/lake/` (`ATIF_SQL_LAKE_ROOT` moves it) and holds every corpus. Without
one, `query` warns once and reads the per-session files.

Each session is stored once. `materialize` writes the ATIF document as `trajectory.json.zst`,
and `edges.jsonl` and `session_events.jsonl` compressed the same way, beside a plain `meta.json`,
`loss_report.json`, and the raw source archive. The lake holds the queryable rows, and it loads a
session from its compressed trajectory. `zstd -dc trajectory.json.zst` prints the plain ATIF
document. Without a lake, `query --no-lake` still works, but it parses every session's trajectory
on each run, so it's slow on a large corpus.

A corpus written by an earlier version keeps working as it is; `status` counts its sessions under
`layout`, and `atif-sql corpus slim` converts it.

`materialize` converts sessions across a process pool, `min(8, cpu_count)` workers by default.
`--workers N` (or `ATIF_SQL_MATERIALIZE_WORKERS`) sets the size, and `--workers 1` runs the
single-process path. The pool doesn't change a byte of output: every worker writes the same
artifacts through the same per-session staging directory and atomic rename.

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
pick which, or `--agent codex` to get the Codex default. `--all-corpora` reads
every corpus the lake holds at once, and `sessions.corpus` says which one a
row came from.

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

Setup, the gates `mise run check` runs, the import-linter contracts you will trip, and the
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

`atif-sql query` runs agent-composed SQL, so it runs it in a box: sized to the host before the
corpus is registered (override with `ATIF_SQL_QUERY_MEMORY_LIMIT` and `ATIF_SQL_QUERY_THREADS`),
a private spill directory outside the corpus that's removed on exit, no extension installs at
query time (`atif-sql embed --install-extension` is where the lance extension comes from), file
facing statements (`COPY`, `EXPORT`, `ATTACH`, `INSTALL`, `LOAD`, `PREPARE`, `EXECUTE`) refused
before they run, and a refusal to run as root unless `ATIF_SQL_ALLOW_ROOT=1` says so. The details
and the one accepted disclosure (`duckdb_settings()` lists the granted paths) are in
[docs/reference/cli.md](docs/reference/cli.md#query).

## License

[Apache License 2.0](LICENSE). Each module directory carries the same license
file, so a published distribution ships it too.
