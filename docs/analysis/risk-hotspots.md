# atif-sql · Risk hotspots

Risk here is a score over signals a command can produce, each countable and each tied to a
line of source: `2 × T + 2 × E + 0.5 × W + 1 × C + 1 × H + U/10`. `T` counts defects a type
checker located in the file; `E` and `W` count `error`- and `warn`-severity scanner findings from
the scanner report tier (`mise.toml:596-611`); `C` counts complexity ratchets the ruff config
sets at a function living in this file (`pyproject.toml:258-281`); `H` counts distinct unguarded
concurrency or IO windows, where a window whose own docstring names its recovery does not count;
and `U` is uncovered units — missed lines plus missed branches — under the branch-coverage config
at `pyproject.toml:535`. Combined line-and-branch coverage is measured against the `fail_under = 89` floor at
`pyproject.toml:560`, so `U/10` puts a
file's share of the remaining uncovered units on the same scale as one located defect.

Churn is rejected as a signal. The log sits under a single bot
identity, `bgagent`, with no human authors, so commit frequency measures authoring order rather
than defect density and ranking by it would produce confident noise. The `Trend` column below still
runs the mechanical 30-day slope rule — `↑ rising` is anything more than one standard deviation
above the median commit count — but it contributes zero weight to the score, and
`↓ falling` is unreachable because every file in the repo has more than one commit. `Top owner` is
likewise `bgagent` on every row and carries no bus-factor information. Further limits: the
scanner tier yields no `error`-severity findings workspace-wide, so `E` never discriminates, and
`packages/atif-embed/src/atif_embed/domain/ports.py` is excluded from the ranking despite reading
as uncovered, because `exclude_also` drops a Protocol's `...` body while still measuring its `def` line
(`pyproject.toml:561-568`) — that is a measurement artifact, not a gap.

Note (2026-09-27): the scores, LOC figures, and coverage below were measured before the trajectory
and structural pipelines were cut. `atif_analytics.application.use_cases.trajectory`, `.community`,
and `.cluster` no longer exist, and `parquet_cache` lost `replace_sessions` (the trajectory
pipeline was its one caller) and with it `_shard_may_hold`, so part of that file's drill-down
describes code that's gone. The ranking hasn't been re-measured.

| File | Trend | Open findings | Top owner | Citation |
| --- | --- | --- | --- | --- |
| `atif_duck.infrastructure.registry` | ↑ rising | warn only | bgagent | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `atif_analytics.infrastructure.parquet_cache` | → flat | none | bgagent | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py` |
| `atif_cli.app` | ↑ rising | warn only | bgagent | `packages/atif-cli/src/atif_cli/app.py` |
| `atif_converter.domain.enrichment` | ↑ rising | none | bgagent | `packages/atif-converter/src/atif_converter/domain/enrichment.py` |
| `atif_analytics.application.use_cases.friction` | ↑ rising | none | bgagent | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py` |
| `atif_analytics.application.use_cases.trajectory` | ↑ rising | none | bgagent | `packages/atif-analytics/src/atif_analytics/application/use_cases/trajectory.py` |
| `atif_analytics.application.use_cases.community` | → flat | none | bgagent | `packages/atif-analytics/src/atif_analytics/application/use_cases/community.py` |
| `atif_analytics.application.use_cases.cluster` | → flat | none | bgagent | `packages/atif-analytics/src/atif_analytics/application/use_cases/cluster.py` |
| `atif_analytics.infrastructure.corpus_reader` | → flat | none | bgagent | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py` |
| `atif_duck.domain.examples` | → flat | warn only | bgagent | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `atif_analytics.application.analyze` | ↑ rising | none | bgagent | `packages/atif-analytics/src/atif_analytics/application/analyze.py` |
| `atif_cli.output` | → flat | none | bgagent | `packages/atif-cli/src/atif_cli/output.py` |

The open findings are `B608` hardcoded-SQL sites plus a build-configuration finding against
`[tool.uv]` at `pyproject.toml:102`, which asks for an `exclude-newer` dependency cooldown. All of them
map to `warn`: `B608` carries `MEDIUM` severity at `LOW` or `MEDIUM` confidence with rule precision
`low`, and the cooldown rule's default level is `warning`. The SQL sites are exactly the ones that
ruff suppresses per line — `ruff check --select S608 packages/` exits clean while the same run with
`--ignore-noqa` over `packages/*/src` reports those same sites — and each `# noqa: S608` names what it
interpolates (`pyproject.toml:150-152`). The scanner tier therefore carries no unaudited exposure,
which is why coverage, complexity, and concurrency carry the ranking instead.

## Per-file drill-down

### `atif_duck.infrastructure.registry`

**What's there.** The DuckDB view and macro registry that binds a connection to the materialized
`<corpus_root>/sessions/<id>/` tree and exposes it as the stable SQL surface, with raw readers built
as `CREATE TEMP TABLE` over `read_json` under an explicit strict-projection `columns` filter
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:3-31`). Every registration function
follows a register-or-fail-loud contract: log through `logger.exception` and re-raise on any DDL
failure (`:31-32`, `:816-819`, `:1217-1220`).

**Recent activity.** Commits in the 30-day window sit well above the median, so `↑ rising` under the
mechanical rule — which here means the file was written and rewritten during the short authoring
run, not that it is destabilizing.

**Owners.** `bgagent` at 100% — every commit touching the path, merges included —
a bot identity.

**Findings.** Most of the workspace's `B608` sites live here, each carrying a per-line
`# noqa: S608` that names its interpolation: a module-constant table name (`:185`, `:237`), a glob
escaped through `sql_literal` (`:254`, `:277`, `:300`, `:319`), local regex literals (`:743`), an
`int()`-coerced width (`:962`), and the pricing rates (`:1099`). The file also holds one of the
type-checker defects: `_pricing_values_clause` escapes the model name through `sql_literal` and coerces
both rates with `float()` (`:975-1002`, specifically `:999`), because the `pricing` parameter is
reachable by an in-process embedding caller even though the CLI never passes one (`:992-998`). Its
uncovered units cluster in exactly the paths no test drives: `register_views` (`:333-819`),
`register_macros` (`:1005-1220`), `register_vss` (`:846-967`),
`_warn_incomplete_session_dirs` (`:175-193`), and `_pricing_values_clause` itself. The
`register_vss` gap is the load-bearing one — the ATTACH-failure arm that degrades a Lance directory
to an empty-store fallback and logs rather than raising (`:906-910`) is untested silent degradation.

### `atif_analytics.infrastructure.parquet_cache`

**What's there.** The sharded parquet cache behind the analytics pipelines: `write_part` appends a
new shard, `read_all` unions every shard, and `replace_sessions` drops a session's prior rows so a
re-flush cannot duplicate them
(`packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:90-111`, `:85-100`,
`:159-221`). `_shard_may_hold` prunes shards by parquet footer statistics and fails open on missing
or unreadable stats (`:116-156`), which is the safe direction.

**Recent activity.** Commits sit at the median, so `→ flat`. This file ranks second on defect
signals alone with no help from churn, which is the clearest demonstration that the two are
independent here.

**Owners.** `bgagent` at 100% — every commit touching the path — a bot identity.

**Findings.** No scanner findings, and several of the workspace's unguarded IO windows, all in the
same file. `write_part`'s sharded branch drops `part-<time_ns>.parquet` straight into the live
directory (`:73-74`) that `iter_part_files` globs (`:57`), with no tmp-then-rename; the code
anticipates concurrency explicitly, since the nanosecond suffix exists to avoid collisions "when two
part-writes land in the same millisecond under concurrency" (`:70-72`). `read_all` then hands every
globbed part to `pl.read_parquet` with no size or completeness check (`:100`), while
`MIN_PARQUET_BYTES`' own docstring states the size check "is what keeps a torn artifact from failing
a pipeline that could skip it" (`:26-31`) and the legacy single-file branch does apply it (`:78`).
A reader can therefore pick up a shard polars is still writing, and the guard the module documents
protects only the path it is not on. `replace_sessions` compounds it by rewriting shards in place
(`:213`) and unlinking emptied ones (`:209`). The coverage gap lands on the same functions:
a large share of its uncovered units are in `replace_sessions` and `write_part`. It also owns the
`max-returns = 5` ratchet through `_shard_may_hold` (`pyproject.toml:268`, `:182`; the function at
`packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:145`).

### `atif_cli.app`

**What's there.** The cyclopts composition root and the only module importing atif-converter,
atif-corpus, and atif-duck together, wiring every cross-package seam — the `ConverterPort` adapter,
the clock, version pins, the DuckDB connection (`packages/atif-cli/src/atif_cli/app.py:3-15`). Heavy
imports are deferred into the command bodies so the `schema` / `--help` fast path stays lean, a
property pinned by a fresh-interpreter test rather than by a lint (`:22-26`).

**Recent activity.** The most commits of any file in the repo, so `↑ rising` — expected of a
composition root that gains a wiring line whenever any member changes.

**Owners.** `bgagent` at 100% — every commit touching the path, merges included —
a bot identity.

**Findings.** A `B608` at the `search` kNN query, whose `# noqa` records that `dim` is
`len(vector)` while the session id, `k`, and the vector itself are `?`-bound (`:925-937`). It
carries the `max-args = 19` ratchet through `search`, whose parameters are the CLI flags
cyclopts binds (`pyproject.toml:264`, `:180`; the function at
`packages/atif-cli/src/atif_cli/app.py:860`). Its uncovered units — the largest single-file gap
in the workspace — concentrate in the commands that reach outward: `analyze`
(`:627-725`), `convert` (`:225-291`), and `status` (`:412-488`), with a few in `search`
(`:828-948`) and `main` (`:1093-1103`). `analyze`, `embed`, and `search` are the
billable commands that call Bedrock, so the least-covered command body in the tree is also the one
that spends money; the uncovered region inside `analyze` is the settings-override chain that
applies the budget ceilings a crontab line depends on (`:691-707`).

### `atif_converter.domain.enrichment`

**What's there.** A pure function over a trajectory dict plus raw records — no harbor import, no IO —
that repairs some of the named fidelity gaps by re-running harbor's deterministic
normalization order (`packages/atif-converter/src/atif_converter/domain/enrichment.py:3-30`). It
exists because harbor 0.22.0 puts no record identity in `step.extra`: it reads `requestId` off the
message dict and `id` off the event dict, while real transcripts carry both on the event, so nothing
lands in extra and there is nothing to join on (`:15-20`).

**Recent activity.** Commits well above the median, so `↑ rising`.

**Owners.** `bgagent` at 100% — every commit touching the path — a bot identity.

**Findings.** No scanner findings, no IO windows — this is a pure function — and both
complexity ratchets in the workspace that a single function sets: `max-branches = 39` and
`max-complexity = 38` are both pinned at `enrich_trajectory` (`pyproject.toml:267`, `:181`,
`:185-190`; the function at
`packages/atif-converter/src/atif_converter/domain/enrichment.py:198`). Each branch is one named
fidelity gap, which is why the count is a ratchet rather than a target. Its uncovered units
sit mostly inside `enrich_trajectory` (`:198-412`) and `_visible_user_text` (`:113-146`),
the helper that replicates harbor's rules for which user records produce a visible text message.
Both untested regions are alignment-failure paths: the user-step mismatch arm that logs a warning
and truncates attribution from that step onward (`:350-360`) is the behavior a reader most needs
pinned, since it silently narrows enrichment coverage rather than failing.

### `atif_analytics.application.use_cases.friction`

**What's there.** A tiered friction detector over short user-role messages: a pre-filter, a regex
fast path at confidence 0.9, deterministic stamp rules, and an LLM tier for everything else
(`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:3-27`). The `source`
column is the row's provenance tier rather than the engine that computed it, and downstream atif-duck
views bind to those literal values, so the vocabulary is a fixed contract (`:29-33`).

**Recent activity.** Commits well above the median, so `↑ rising`.

**Owners.** `bgagent` at 100% — every commit touching the path — a bot identity.

**Findings.** No scanner findings, and the `max-statements = 119` ratchet, set at `_friction_async`
(`pyproject.toml:269`, `:183`; the function at
`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:240`). The coverage
gap and the complexity outlier are the same region: most of the file's uncovered units sit inside
`_friction_async` (`:240-528`), with the rest in `detect_user_friction` (`:531-635`) and
`deterministic_stamps` (`:178-232`). This is the file where the two signals coincide most tightly —
the longest function in the workspace is also the least-exercised, and it is the function that
decides which candidate messages cross into the billable LLM tier. The budget-guard posture that
governs that decision assumes every candidate reaches the LLM even though roughly half survive the
fast tiers (`:583-585`), and the third-party disclosure is explicit: short user message bodies leave
the machine on tier 4 (`:35-36`).

## See also

- [impact analysis](../insights/impact-analysis.md)
- [tech debt](../insights/tech-debt.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [components](../diagrams/architecture/components.md)
