---
title: For agents
description: Which atif-sql surface to fetch for which question, what not to assume about the corpus, and the shortest path from nothing to a row of results.
---

This page is addressed to an AI agent working with atif-sql, and it is written so the person reading
over your shoulder can read it too. Every claim on it points at behavior the repository's own tests
check.

## 1. The catalog outranks this page

`atif-sql schema` prints the whole query contract: every view with its columns, and every macro with
its signature. It reads a static catalog rather than the corpus, so it answers in well under a tenth
of a second, needs no materialized data, and doubles as a liveness check on the install. Its output
ends with a pointer to the examples command.

`atif-sql examples` — the same thing as `atif-sql query --examples` — prints a runnable query per view
and per macro. Those queries are **derived from the catalog**, never hardcoded per object, and every
one is executed against a fixture corpus by `atif-duck`'s test suite. Filter with
`--requires core|analytics|vss` and `--category view|table-macro|scalar-macro`. Piped output is JSON.

:::agent

**For an agent.** Prefer `atif-sql schema` and `atif-sql examples` to this page wherever the two could
disagree. Both are generated from the catalog, so they are right and this page is stale. This page
adds what a schema cannot state: what a silence means, and which surface you are on.

:::

## 2. Assumptions to drop

- **The materialized corpus is what the views read, not `~/.claude`.** `atif-sql` converts each
  session to ATIF once, writes it under the corpus root, and every view reads those artifacts. A
  session that has not been materialized is absent from every view — not empty, absent. `atif-sql
  materialize` is what makes it visible, and `atif-sql status` replays the same planning decision
  read-only, so it answers "what would a materialize pass do right now" without converting anything.

- **A session still being written is skipped on purpose.** Materialization is gated on quiescence: a
  session is converted when its newest source file has been silent for longer than the quiesce
  threshold and is newer than the recorded watermark. So the most interesting session — the one open
  in another terminal — is the one deliberately not in the corpus yet. `--force` overrides the
  watermark; nothing overrides physics, so wait for the write to settle.

- **`requires` on an example is a precondition, not a hint.** An example tagged `analytics` needs
  `atif-sql analyze` to have run; one tagged `vss` needs an embedding run. Query one before its
  pipeline has run and you get a catalog miss with exit 65, which is a different problem from a query
  that legitimately matched nothing.

- **A bare `atif-sql embed` exits 64 rather than doing the obvious thing.** A real embedding run calls
  Bedrock once per unembedded step, so it requires an explicit scope: `--limit N` or `--all`. The
  refusal exists so a mistyped command cannot start a full backfill. `--dry-run` needs no scope and
  spends nothing.

- **Conversion loss is accounted for, not hidden.** Every materialized session carries a loss report
  beside its trajectory, and each known upstream conversion gap is a named type in the converter's
  fidelity policy rather than a paragraph in a changelog. A field absent from the ATIF document is not
  evidence that it was absent from the transcript — read the loss report before concluding that.

- **Branch on the exit code, never on the message text.** The codes are a stable wire contract: `64`
  for malformed input or malformed SQL, `65` for a catalog miss or a store written by another embedding
  provider, `70` for anything the database or an adapter raises, `78` for a state only an operator can
  clear, `127` for a missing harbor install, and `2` for a parsed-but-empty result an automated caller
  should treat as "nothing to do". The message beside the code is prose a maintainer rewrites freely.

## 3. Which surface you are on

The test for each row is free — readable from your tool list, your shell, or the repository — and
never a call that has to fail first.

| Signal | Surface | Entry point |
| --- | --- | --- |
| You can run a shell command | CLI | `atif-sql schema`, then the command you need |
| You can execute Python in this workspace | In-process library | Import the package that owns the capability: `atif_duck` to register views on a connection, `atif_corpus` to materialize |
| Neither | Read-only | You are reading documentation. Fetch the Markdown, not the HTML — section 7 has the URLs |

The CLI and the library are the same implementation: `atif-cli` is a composition root and holds no
query logic of its own, so a fact learned on one surface transfers to the other. The layering is
enforced rather than documented — import-linter fails the build when a leaf package imports another
leaf package.

:::agent

**For an agent.** Read your own tool list and environment to find your surface, rather than running a
command and inspecting the failure. `atif-sql schema` needs no corpus and no credentials, so it is the
cheapest probe that distinguishes "installed" from "not installed".

:::

## 4. The shortest path to a row of results

```bash
mise trust && mise install && mise run install   # first-time setup, from the repository root
atif-sql materialize                             # scan, convert, write; skips non-quiescent sessions
atif-sql schema                                  # the contract: views, columns, macro signatures
atif-sql query 'SELECT * FROM tool_rank(30) LIMIT 10'
```

Two habits change the shape of everything after. Output adapts to the channel: a TTY gets a plain
table, a pipe gets JSON — so redirect when you intend to parse, and never parse the table. And an
example is meant to be copied verbatim before it is adapted: the session-id exemplar in every example
is a subquery over `sessions`, so a pasted example runs against any corpus rather than needing an id
you do not have yet.

Errors arrive on stderr as a JSON envelope when the channel is a pipe, carrying a kind, a message and
a hint. Exit 0 is success. An exit in the sixties is fixed by changing the call; an exit of 70 or 78 is
fixed by changing the environment.

## 5. What to avoid

- **Scraping these pages.** Every one of them is served as Markdown at its own path with `.md`
  appended, and section 7 has the URLs. Scraping the rendered HTML buys navigation chrome, a search
  widget and a theme toggle in exchange for reading three paragraphs.

- **Re-deriving trajectory semantics in SQL.** The whole reason the conversion happens at the boundary
  is that a view over raw transcript JSON re-implements the adapter, badly, once per view. If a fact
  about a trajectory is missing, it belongs in the converter or in the catalog, not in your query.

- **Reading an analytics or vector view before its pipeline has run.** Point back at section 2: that is
  a catalog miss, and the fix is a command rather than a different query.

- **Naming a Bedrock model id anywhere outside `atif-models`.** The alias registry exists so a model
  swap is one edit. A hardcoded id elsewhere is a second source of truth that nothing reconciles.

- **Adding a view or macro without its catalog entries.** The drift tests require a description entry,
  an argument exemplar for any new parameter name, table-macro membership when the DDL declares one,
  and a derived example that actually executes. A view added without them fails the suite rather than
  shipping undocumented.

- **Embedding without a scope, or with `--all` on a corpus you have not sized.** `--dry-run` prints the
  plan for free. Read it first.

## 6. Read next

This table is built at publish time from the set of pages the site actually wrote, so a page added to
the documentation tree appears here without an edit and a removed page cannot leave a row behind. The
right-hand column is that page's raw Markdown twin — fetch that instead of the page.

GENERATED-READ-NEXT-TABLE

## 7. The machine surfaces

Any page here is available as Markdown: append `.md` to its path. `/agents/` is served at
[`/agents.md`](/agents.md). That holds for every page on the site, generated ones included, and each
page links its own twin from `<head>` with `rel="alternate" type="text/markdown"`. The media type on the
response is the static host's to send, so read the path convention and the head link as the contract and
the header as a courtesy.

The whole site comes three ways. [`llms.txt`](/llms.txt) is the index, and it lists this page first.
[`llms-full.txt`](/llms-full.txt) is every page in one file. [`llms-small.txt`](/llms-small.txt) is the
same corpus with non-essential content removed, for a tighter context.

Citations on the generated pages are repository permalinks pinned to one commit, so a line anchor keeps
naming the line it was written about. The raw twins deliberately keep the bare `path:line` form
instead: that is the string you can hand to a grep.

:::agent

**For an agent.** Fetch `llms-small.txt` before `llms-full.txt`. When you already know which page you
want, fetch that page's `.md` twin instead of either bundle — a fraction of the tokens for the same
text.

:::
