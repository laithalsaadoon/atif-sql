# atif-sql · Risk hotspots

Risk here is a score over signals a command can produce, each countable and each tied to a
line of source: `2 × T + 2 × E + 0.5 × W + 1 × C + 1 × H + U/10`. `T` counts defects a type
checker located in the file; `E` and `W` count `error`- and `warn`-severity scanner findings from
the scanner report tier (`mise.toml:675`); `C` counts complexity ratchets the ruff config
sets at a function living in this file (`pyproject.toml:324-347`); `H` counts distinct unguarded
concurrency or IO windows, where a window whose own docstring names its recovery does not count;
and `U` is uncovered units (missed lines plus missed branches) under the branch-coverage config
at `pyproject.toml:620`. Combined line-and-branch coverage is measured against the `fail_under = 89` floor at
`pyproject.toml:645`, so `U/10` puts a
file's share of the remaining uncovered units on the same scale as one located defect.

Churn is rejected as a signal. One author wrote 88 of the 93 commits (the other five are three
`dependabot[bot]` and two `github-actions[bot]` commits, none touching `packages/*/src`), and the history opens
with one commit on 2026-08-28 that added 276 files, 89 of the 154 Python files under
`packages/*/src` among them, so commit frequency measures authoring order rather
than defect density and ranking by it would produce confident noise. The `Trend` column below still
runs the mechanical 30-day slope rule over those 154 files (`↑ rising` is a count of commits in
the 30 days ending 2026-10-12 more than one standard deviation above the median: the median is 1 and the
population standard deviation 2.27, so four commits or more), but it contributes zero weight to the
score, and `↓ falling` is unreachable because one standard deviation below the median is less than
zero. `Top owner` is likewise the sole author on every row and carries no bus-factor information.
Further limits: both type checkers pass at zero (`ty check` reports no diagnostic and pyright
strict reports 0 errors and 0 warnings), so `T` is 0 on every file; bandit's one open finding
workspace-wide is `LOW` severity, so `E` and `W` never discriminate either, and the order rests on
`C`, `H`, and `U`. `U` comes from a branch-coverage run of the suite rather than from the tree, so
it's the one input here that a reading of the code, git, and the linters can't re-derive.
`packages/atif-embed/src/atif_embed/domain/ports.py` is excluded from the ranking despite reading
as uncovered, because `exclude_also` drops a Protocol's `...` body while still measuring its `def` line
(`pyproject.toml:646-653`); that is a measurement artifact, not a gap.

| File | Trend | Open findings | Top owner | Citation |
| --- | --- | --- | --- | --- |
| `atif_duck.infrastructure.registry` | ↑ rising | none | sole author | `packages/atif-duck/src/atif_duck/infrastructure/registry.py` |
| `atif_analytics.infrastructure.parquet_cache` | → flat | none | sole author | `packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py` |
| `atif_cli.app` | ↑ rising | none | sole author | `packages/atif-cli/src/atif_cli/app.py` |
| `atif_converter.domain.enrichment` | → flat | none | sole author | `packages/atif-converter/src/atif_converter/domain/enrichment.py` |
| `atif_analytics.application.use_cases.friction` | → flat | none | sole author | `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py` |
| `atif_analytics.infrastructure.corpus_reader` | ↑ rising | none | sole author | `packages/atif-analytics/src/atif_analytics/infrastructure/corpus_reader.py` |
| `atif_duck.domain.examples` | → flat | none | sole author | `packages/atif-duck/src/atif_duck/domain/examples.py` |
| `atif_analytics.application.analyze` | → flat | none | sole author | `packages/atif-analytics/src/atif_analytics/application/analyze.py` |
| `atif_cli.output` | → flat | none | sole author | `packages/atif-cli/src/atif_cli/output.py` |

Every `B608` hardcoded-SQL site carries a `# nosec B608` that names its reason, so bandit
skips all 69 of them (each `MEDIUM` severity at `LOW` or `MEDIUM` confidence when the markers are
ignored). Its one open finding is a `LOW`-severity `B105` on a record-type constant at
`packages/atif-converter/src/atif_converter/domain/codex_fidelity.py:37`, a file outside the table,
which is why `Open findings` reads `none` on every row. The build-configuration
rule that asks `[tool.uv]` for a dependency cooldown (`uv-missing-dependency-cooldown`) is met:
`pyproject.toml:125` sets a seven-day `exclude-newer` window, and the comment at `:116-124` names
the rule. The SQL sites are the ones that ruff suppresses per line (`ruff check --select S608 packages/` exits clean while the same run with
`--ignore-noqa` over `packages/*/src` reports 70 sites against bandit's 69, the two sets differing
by three statements the tools parse differently), and each `# noqa: S608` names what it
interpolates (`pyproject.toml:209-212`). The scanner tier therefore carries no unaudited exposure,
which is why coverage, complexity, and concurrency carry the ranking instead.

## Per-file breakdown

### `atif_duck.infrastructure.registry`

**What's there.** The DuckDB view and macro registry that binds a connection to the materialized
`<corpus_root>/sessions/<id>/` tree and exposes it as the stable SQL surface, with raw readers built
as `CREATE TEMP TABLE` over `read_json` under an explicit strict-projection `columns` filter
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:3-20`). Every registration function
follows a register-or-fail-loud contract: log through `logger.exception` and re-raise on any DDL
failure (`:62-63`, `:989-992`, `:1561-1564`, `:2106-2109`).

**Recent activity.** 12 commits in the 30-day window against a median of 1, so `↑ rising` under the
mechanical rule, which here measures how often the file was edited, not whether it is
destabilizing.

**Owners.** The sole author at 100% (all 14 commits touching the path; the history has no merge
commits).

**Findings.** More of the workspace's `B608` sites live here than in any other file (28 of 69), each
carrying a per-line `# nosec B608` that names its interpolation: a module-constant reader or table
with catalog columns (`:342`, `:395`), a glob or file list the statement binds as a parameter
(`:762`, `:864`), the corpus name through `sql_literal` (`:1009`), local regex literals (`:1379`),
an `int()`-coerced width (`:1771`), an int-derived alias (`:1826`), and the pricing rates
(`:1989`). The file holds no type-checker defect; `_pricing_values_clause` escapes the model name
through `sql_literal` and coerces both rates with `float()` (`:1860-1887`, specifically `:1884`),
because the `pricing` parameter is reachable by an in-process embedding caller even though the CLI
never passes one, and a `str` rate there would get past the type checkers (`:1877-1882`). Its
uncovered units sit in `register_views` (`:1018-1564`), `register_macros` (`:1890-2109`),
`register_vss` (`:1635-1776`), `_gate_session_dirs` (`:516-567`), and `_pricing_values_clause`
itself, although tests drive each of those paths: 14 test files call `register`, which runs
`register_views` and `register_macros`, seven call `register_vss`, and
`packages/atif-duck/tests/test_duck_views.py:663` passes a hostile model name through the pricing
clause. The `register_vss` gap is the one that matters most: the ATTACH-failure arm that degrades
a Lance directory to an empty-store fallback and logs rather than raising
(`packages/atif-duck/src/atif_duck/infrastructure/registry.py:1715-1719`) is untested silent
degradation: no test calls `register_vss` on a directory that fails to ATTACH.

### `atif_analytics.infrastructure.parquet_cache`

**What's there.** The sharded parquet cache behind the analytics pipelines: `write_part` appends a
new shard and `read_all` unions every whole shard
(`packages/atif-analytics/src/atif_analytics/infrastructure/parquet_cache.py:90-111`,
`:114-129`). `iter_part_files` is the one choke point both readers share, and it drops any shard
too small to carry parquet framing (`:71-87`).

**Recent activity.** One commit in the window, at the median, so `→ flat`. Churn adds nothing to
its score.

**Owners.** The sole author at 100% (both commits touching the path).

**Findings.** No scanner findings, no complexity ratchet, and one unguarded IO window.
`write_part`'s sharded branch drops `part-<time_ns>.parquet` straight into the live
directory (`:102-103`) that `iter_part_files` globs (`:86`), with no tmp-then-rename; the code
anticipates concurrency explicitly, since the nanosecond suffix exists to avoid collisions "when two
part-writes land in the same millisecond under concurrency" (`:99-101`). The read path does apply
the size guard: `MIN_PARQUET_BYTES`' own docstring states the size check "is what keeps a torn
artifact from failing a pipeline that could skip it" (`:27-32`), `iter_part_files` filters every
globbed part through it (`:87`), and the legacy single-file branch applies it too (`:107`). The
guard catches a stub of 16 bytes or fewer, not a half-written file, so a reader that globs while
polars is still writing a larger shard hands it to `pl.read_parquet` (`:129`) and fails there.

### `atif_cli.app`

**What's there.** The cyclopts composition root and the only module importing atif-converter,
atif-corpus, and atif-duck together, wiring every cross-package seam: the `ConverterPort` adapter,
the clock, version pins, the DuckDB connection (`packages/atif-cli/src/atif_cli/app.py:3-9`). Heavy
imports are deferred into the command bodies so the `schema` / `--help` fast path stays lean, a
property pinned by a fresh-interpreter test rather than by a lint (`:50-53`).

**Recent activity.** The most commits of any source file (19, 16 of them in the window), so
`↑ rising`, which is expected of a composition root that gains a wiring line whenever any member
changes.

**Owners.** The sole author at 100% (all 19 commits touching the path).

**Findings.** No `B608` site: the `search` kNN statement in `search_rows` binds the vector, the
session id, and `k` as parameters and formats only the width `len(query_vector)` and constant
fragments into `_SEARCH_SQL` (`:2223`, `:2240-2247`). It sets no complexity ratchet: `search` takes
seven parameters and the widest command, `analyze`, takes 13, while `max-args = 19` sits at
`atif_corpus.application.materialize.materialize`
(`packages/atif-corpus/src/atif_corpus/application/materialize.py:1497`). Its uncovered units
concentrate in the commands that reach outward: `analyze`
(`packages/atif-cli/src/atif_cli/app.py:1656-1757`), `convert` (`:690-772`), and `status`
(`:1162-1311`), with a few in `search` (`:2040-2198`) and `main` (`:2431-2464`). `analyze` always
calls Bedrock, and `embed` and `search` do with the default Cohere provider, so the least-covered
command body in the tree is also the one that spends money; the uncovered region inside `analyze`
is the settings-override chain that applies the budget ceilings a crontab line depends on
(`:1722-1736`).

### `atif_converter.domain.enrichment`

**What's there.** A pure function over a trajectory dict plus raw records (no harbor import, no IO)
that repairs some of the named fidelity gaps by re-running harbor's deterministic
normalization order (`packages/atif-converter/src/atif_converter/domain/enrichment.py:3-30`). It
exists because harbor 0.22.0 puts no record identity in `step.extra`: it reads `requestId` off the
message dict and `id` off the event dict, while real transcripts carry both on the event, so nothing
lands in extra and there is nothing to join on (`:15-20`).

**Recent activity.** One commit in its history and none in the window, below the median, so
`→ flat`.

**Owners.** The sole author at 100% (the one commit touching the path).

**Findings.** No scanner findings, no IO windows (this is a pure function), and two of the
workspace's complexity ratchets set by one function: `max-branches = 39` and
`max-complexity = 38` are both pinned at `enrich_trajectory` (`pyproject.toml:338`, `:333`,
`:342-347`; the function at
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
(`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:3-28`). The `source`
column is the row's provenance tier rather than the engine that computed it, and downstream atif-duck
views bind to those literal values, so the vocabulary is a fixed contract (`:34-38`).

**Recent activity.** Two commits in the window, under the four the rule needs, so `→ flat`.

**Owners.** The sole author at 100% (all three commits touching the path).

**Findings.** No scanner findings, and the `max-statements = 119` ratchet, set at `_friction_async`
(`pyproject.toml:340`, `:335`; the function at
`packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:210`). The coverage
gap and the complexity outlier are the same region: most of the file's uncovered units sit inside
`_friction_async` (`:210-498`), with the rest in `detect_user_friction` (`:501-606`) and
`deterministic_stamps` (`:146-202`). This is the file where the two signals coincide most tightly:
the function with the most statements in the workspace is also the least-exercised, and it is the
function that decides which candidate messages cross into the billable LLM tier. The budget-guard
posture that governs that decision assumes every candidate reaches the LLM even though roughly half
survive the fast tiers (`:554-556`), and the third-party disclosure is explicit: short user message
bodies leave the machine on tier 4 (`:40-41`).

## See also

- [impact analysis](../insights/impact-analysis.md)
- [tech debt](../insights/tech-debt.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [components](../diagrams/architecture/components.md)
