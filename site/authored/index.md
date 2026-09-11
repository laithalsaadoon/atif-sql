---
title: atif-sql
description: ATIF-native analytics over Claude Code and Codex CLI agent trajectories — convert sessions to ATIF, materialize a corpus, query it through DuckDB views.
---

atif-sql reads Claude Code session transcripts and Codex CLI rollouts, converts each session to
[ATIF](https://github.com/laude-institute/harbor) — Harbor's Agent Trajectory Interchange Format —
materializes the results as a corpus of ATIF documents on disk, and layers DuckDB views and macros
over that corpus. Converting once at the boundary, with an explicit and tested fidelity policy for
what the upstream adapter drops, replaces re-deriving trajectory semantics inside every SQL view.

Pick the agent with `--agent claude-code|codex` on `convert`, `materialize`, `status` and `query`.
One corpus holds one agent, the two default to separate roots, and `sessions.agent` names which one a
row came from.

It is a uv Python workspace, Apache-2.0, and it replaces `claude-sql`.

```mermaid The conversion pipeline: a Claude Code session JSONL or a Codex CLI rollout JSONL is converted once to an ATIF corpus on disk, and every DuckDB view, analytics pipeline and embedding run reads that corpus.
flowchart LR
  jsonl[Claude Code session JSONL] --> convert[atif-sql convert]
  rollout[Codex CLI rollout JSONL] --> convert
  convert --> corpus[ATIF corpus on disk]
  corpus --> duck[DuckDB views and macros]
  duck --> query[atif-sql query]
  corpus --> analyze[atif-sql analyze]
  analyze --> duck
  corpus --> embed[atif-sql embed]
  embed --> duck
```

## The packages

| Package | What it owns |
| --- | --- |
| `atif-converter` | The Harbor `ClaudeCode` and `Codex` adapter wrappers, and the per-agent fidelity policy that accounts for every known upstream conversion gap per session |
| `atif-corpus` | Corpus materialization: source discovery, watermarks, quiescence, atomic artifact writes |
| `atif-duck` | The DuckDB views and macros over the materialized corpus, declared in a drift-tested static catalog |
| `atif-models` | The model alias registry and the structured-output LLM client. No other package names a Bedrock model id |
| `atif-analytics` | The v2 pipelines: classify, trajectory, conflicts, friction, cluster, terms, community |
| `atif-embed` | Cohere Embed v4 on Bedrock, a LanceDB vector store, and the embedding backfill |
| `atif-cli` | The cyclopts CLI that composes the rest |

Each package is layered, and the independence contract is enforced by import-linter rather than by
convention: converter, corpus, duck, models and embed may never import one another, and `atif-cli` is
the only composition root.

## The discovery loop

```bash
atif-sql schema     # every view with its columns, and every macro signature, from the static catalog
atif-sql examples   # runnable example queries, derived from that catalog and executed by the tests
atif-sql query 'SELECT * FROM tool_rank(30) LIMIT 10'
```

`atif-sql examples` derives its output from the catalog rather than carrying hardcoded strings, and
every example is executed against a fixture corpus by `atif-duck`'s own test suite — so the listing
cannot drift from the schema it documents.

:::agent

**For an agent.** Start at [For agents](/agents/), which names which surface to fetch for which
question and what not to assume about this repository. Prefer `atif-sql schema` and
`atif-sql examples` to anything on this site: both are generated from the catalog, so they are right
and a page can be stale.

:::
