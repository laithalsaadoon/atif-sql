# atif-sql · Documentation

Seventeen documents covering the architecture, reference surface, runtime behavior, and the things
this codebase quietly assumes. Every factual claim carries a `path:LOC` citation into source.

Prose is generated; structure is mechanical. Cross-references are deterministic.

Measured 2026-08-28: 1,768 full-path citations plus 314 shorthands, resolving into 136
distinct source files. A validator rejects any citation naming a file that does not exist, a line
outside that file's range, or a shorthand with no resolvable antecedent, and it exits non-zero on the
first one — so a citation here resolves or the tree does not ship.

## Start here

| If you want to | Read |
| --- | --- |
| Understand what this is and how it fits together | [System overview](architecture/system-overview.md) |
| Run it | [CLI](reference/cli.md) |
| Know what happens when a command runs | [Processes](behavior/processes.md) |
| Change something without breaking it | [Impact analysis](insights/impact-analysis.md) |
| Debug a failure | [Debugging guide](insights/debugging-guide.md) |

## Architecture

- [System overview](architecture/system-overview.md) — the two-paragraph version, the stack, and one
  diagram of the seven workspace members.
- [Module map](architecture/module-map.md) — one section per member, ordered by source size, with the
  eight files in each that carry the weight.
- [Data flow](architecture/data-flow.md) — three flows (`materialize`, `query`, `analyze`) traced end
  to end, each as a sequence diagram.

## Reference

- [CLI](reference/cli.md) — all ten commands with verbatim usage, every flag, and the exit-code
  taxonomy. This is the public contract: atif-sql ships one installable distribution and the command
  is its supported entry point.
- [Public API](reference/public-api.md) — the internal module surface, ranked by the enforced
  inter-package seam. No symbol here is a supported import path or carries a compatibility promise.

## Behavior

- [Processes](behavior/processes.md) — eight processes with numbered, cited steps, plus six minor
  flows.
- [State machines](behavior/state-machines.md) — the three genuine multi-state lifecycles: corpus
  session materialization, embed-store schema version, and the retry-queue row.

## Analysis

- [Risk hotspots](analysis/risk-hotspots.md) — ranked by type-checker defects, scanner findings,
  complexity ratchets, unguarded IO windows, and uncovered units. Commit churn is deliberately
  excluded and the document says why.
- [Dead code](analysis/dead-code.md) — nothing in this workspace is deletable code. What the two
  populated tables record instead is export declarations with no consumer.

## Diagrams

- [Components](diagrams/architecture/components.md) — one class diagram over the seven members.
- [Dependency graph](diagrams/structural/dependency-graph.md) — internal members and external
  distributions on one page, with the enforced direction stated.
- [Sequences](diagrams/behavioral/sequences.md) — call order for the top three processes.

## Insights

- [Impact analysis](insights/impact-analysis.md) — eight high-impact surfaces, each with the
  downstream effects and the gate that catches you for missing one.
- [Debugging guide](insights/debugging-guide.md) — a failure-mode index, the log and error surfaces,
  and a first-checks ladder ordered cheapest first.
- [Contract map](insights/contract-map.md) — eleven contracts with producer, consumer, and shape,
  including the units, base, and scope of every numeric field crossing a seam.
- [Business logic](insights/business-logic.md) — the domain rules: validations, invariants,
  calculations, and policies, with the test that pins each one where a test exists.
- [Tech debt](insights/tech-debt.md) — a ranked register with cost of removal. The workspace carries
  zero `TODO`/`HACK`/`FIXME` markers, so the register is built from version ceilings, justified lint
  suppressions, install weight, and platform gaps instead.

## Two documents this tree deliberately omits

- **`analysis/ownership.md`** — the history is 97 commits over six days under a single bot identity,
  with zero human authors. A per-person ranked table and a bus-factor list would be noise dressed as
  analysis.
- **`reference/rpc-tools.md`** — there is no RPC, MCP, or HTTP surface. `fastapi`, `uvicorn`,
  `starlette`, and the supabase client stack do appear in the installed dependency closure, arriving
  transitively through harbor, and no first-party module imports any of them.

An empty file in either slot would imply absence by omission rather than by silence, which is why
neither exists.

## Hand-written documents that outrank inference

[`CONTRACT.md`](CONTRACT.md) and [`CONTRACT-V2.md`](CONTRACT-V2.md) are authored, not generated. Where
a generated document disagrees with one of them, the generated document cites source and flags the
disagreement rather than picking a side.
