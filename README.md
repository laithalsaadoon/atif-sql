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

Every `atif-sql` command, by task. Each command links to its flags and exit codes in
[the CLI reference](docs/reference/cli.md). Rows marked **costs money** call Amazon Bedrock and
are dry runs until you ask them to spend.

| Task | Command | Outcome |
| --- | --- | --- |
| Build the corpus from Claude Code transcripts | [`atif-sql materialize`](docs/reference/cli.md#materialize) | Scans `~/.claude/projects`, converts each quiet session to ATIF, and writes it to the corpus under `~/.atif-sql/corpus`. Re-runs convert only sessions that changed. |
| Build the corpus from Codex CLI rollouts | [`atif-sql materialize --agent codex`](docs/reference/cli.md#materialize) | The same pass over `$CODEX_HOME/sessions` into a separate Codex corpus. |
| Convert one transcript | [`atif-sql convert <session.jsonl>`](docs/reference/cli.md#convert) | One ATIF trajectory plus its loss report and edges, on stdout or at `--trajectory-out`. |
| Check corpus freshness | [`atif-sql status`](docs/reference/cli.md#status) | Read-only report of watermark age, session counts, bytes, and what the next `materialize` would convert. |
| Load every corpus into the lake | [`atif-sql lake rebuild`](docs/reference/cli.md#lake-rebuild) | Builds the DuckLake beside the old one and swaps it in. Afterward `materialize` keeps it current and `query` reads it instead of per-session files. |
| Check the lake against the corpus | [`atif-sql lake verify`](docs/reference/cli.md#lake-verify) | Compares each session's lake rows with its artifacts. Exits 65 on a difference. |
| See the lake's state | [`atif-sql lake status`](docs/reference/cli.md#lake-status) | Whether the lake exists and is current, its corpora, snapshots, files, and last write. |
| Keep the lake small | [`atif-sql lake compact`](docs/reference/cli.md#lake-compact) | Merges small files, expires old snapshots, and removes files nothing references. |
| List the views and macros | [`atif-sql schema`](docs/reference/cli.md#schema) | Every view with its columns and every macro signature, each tagged `core`, `analytics`, or `vss`. No corpus needed. |
| Get runnable example queries | [`atif-sql examples`](docs/reference/cli.md#examples) | Tested queries for every view and macro, filtered by `--requires` and `--category`. |
| Run any SQL over your sessions | [`atif-sql query '<sql>'`](docs/reference/cli.md#query) | Rows as a table on a terminal and JSON on a pipe, from a read-only sandboxed connection. |
| Query every corpus at once | [`atif-sql query --all-corpora '<sql>'`](docs/reference/cli.md#query) | The views span Claude Code and Codex; `sessions.corpus` says which a row came from. Needs the lake. |
| Rank the most-used tools | [`atif-sql query 'SELECT * FROM tool_rank(30) LIMIT 10'`](docs/reference/cli.md#query) | Tool call counts over the last 30 days, most used first. |
| Estimate what a session cost | [`atif-sql query 'SELECT * FROM cost_estimate((SELECT session_id FROM sessions LIMIT 1)) LIMIT 10'`](docs/reference/cli.md#query) | Estimated USD from token counts and prices. The priced and unpriced step counts say how far to trust it. |
| Count friction by label | [`atif-sql query 'SELECT * FROM friction_counts(30) LIMIT 10'`](docs/reference/cli.md#query) | Friction labels over the last 30 days. Needs `analyze` to have run. |
| Classify sessions and find conflicts, friction, and perceived errors | [`atif-sql analyze`](docs/reference/cli.md#analyze) | **Costs money.** A dry run by default: prints the plan and a cost estimate. `--no-dry-run` runs classify, conflicts, friction, and perceived, and fills the `session_classifications`, `session_conflicts`, `user_friction`, and `perceived_errors` views. |
| Embed steps for semantic search | [`atif-sql embed`](docs/reference/cli.md#embed) | **Costs money.** Needs `--limit N` or `--all`. `--dry-run` previews the plan; a real run embeds unembedded steps with Cohere Embed v4 into LanceDB. |
| Search sessions by meaning | [`atif-sql search '<text>'`](docs/reference/cli.md#search) | **Costs money** (one query embedding per call). Top-k nearest steps with `session_id`, `snippet`, and `sim`. |
| Search every corpus at once | [`atif-sql search --all-corpora '<text>'`](docs/reference/cli.md#search) | **Costs money.** The same search across every corpus's embedding store, with a `corpus` column. Needs the lake. |
| Shrink a corpus written by an older version | [`atif-sql corpus slim`](docs/reference/cli.md#corpus-slim) | Reports how many bytes it would free. `--no-dry-run` compresses the JSON files and then deletes the parquet files once the lake agrees. |
| Schedule the refresh | [`atif-sql cron install`](docs/reference/cli.md#cron-install) | Prints the crontab block for the refresh lanes. It never writes your crontab. |
| Check the scheduled refresh | [`atif-sql cron status`](docs/reference/cli.md#cron-status) | Each lane's lock holder and its last run and skip from the refresh log. |

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
