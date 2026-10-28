# atif-sql · Tech debt

**There are no `TODO`, `FIXME`, `HACK`, or `XXX` markers in this repository's code.** Scans
over `packages/*/src` and over `packages/` plus `scripts/` return nothing, and a scan over
every file `git ls-files` names finds the words only in Vale style lists and in docs pages
that discuss them. That means the marker channel carries no signal here. It does
not mean the codebase is debt-free, and this page exists because the debt is real and
lives elsewhere.

So the register below is assembled from these sources rather than from comments. Every
citation on this page carries its full path, so no reference resolves through a nearby
antecedent.

1. **The suppression config, read as the debt register someone already wrote.** `select = ["ALL"]`
   with a short list of ignores (`pyproject.toml:202-240`), some pyright rules switched
   off (`pyproject.toml:721-764`), ty at `all = "error"` with no rule turned off over `src/`
   (`pyproject.toml:440-441`; the overrides below it relax a few rules for the tests trees
   and the fuzz entrypoints only), and per-site
   `# noqa` / `# pyright: ignore` counts taken by grep. Almost every config entry carries
   its own measured count and its cost-of-removal analysis inline, which is a better debt
   record than a comment marker. It's quoted here, with a measurement on this tree beside
   any count the tree doesn't match.
2. **Version ceilings and the reason attached to each**, read at the pin in the member
   manifests, not inferred from the lockfile.
3. **Platform coverage**, read from the wheel and source-distribution entries `uv.lock` records for each
   native package in the runtime closure and checked against the release notes in `RELEASING.md`.
4. **Asymmetric enforcement between sibling surfaces**: a coverage floor enforced in CI but
   not in the declared definition of done, so a local green run does not measure it.
5. **Test-shape gaps**, read from the test modules themselves: which tests reach real
   transcripts and which run against a double.

Category and cost vocabularies are closed. Categories: `marker`, `wrong abstraction`,
`error handling`, `dead code adjacent`, `deprecated pattern`, `version pin`, `duplicated
logic`, `missing tests`. Cost: `S`, `M`, `L`. No row carries the `marker` category, for
the reason in the first paragraph. Rank is `cost-to-fix × consequence-of-leaving`, so a
cheap fix for a harmless problem sinks and an expensive fix for a structural problem
rises.

## Ranked register

| Rank | Debt item | Category | Cost to fix | Citation |
| --- | --- | --- | --- | --- |
| 1 | Six conversion losses inherited from harbor's Claude Code converter are catalogued as `FidelityGap` members rather than fixed, because the converter is a parity port and fixing one means diverging from the oracle on purpose. Gap 3 flattens the `parentUuid` tree by timestamp sort, losing branch and rewind structure, and every conversion reports it. Gap 7 is the converter never writing the event `uuid` into `Step.extra`: enrichment restores step-to-raw-record identity as `step.extra["source_uuids"]`, the corpus layout keeps a parallel `edges.jsonl` sidecar with every raw record's uuid and parent, and the gap is reported only where enrichment refuses to attribute a step. | `wrong abstraction` | L | `packages/atif-converter/src/atif_converter/domain/fidelity.py:55-99`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:79`, `packages/atif-converter/src/atif_converter/application/convert_and_audit.py:124`, `docs/CONTRACT.md:29-31` |
| 2 | Three Unknown-propagation rules are off, and the config names the fix it hasn't built: its record traces 415 findings to their origins and says a `TypedDict` model of the `~/.claude` JSONL record and the ATIF trajectory would close 144 of them. No such model exists, so every JSON-shaped value in the hottest modules is `dict[str, Any]` narrowed by `isinstance`, with the type checker's opinion switched off. Turned back on over this tree, the three rules report 1,106 findings outside `fuzz/`, 818 of them under `packages/*/src`. The heaviest files are the converter's Claude Code conversion module at 101, its result-signal module at 92, its Codex conversion module at 77, and its enrichment module at 61. | `wrong abstraction` | L | `pyproject.toml:721-755` |
| 3 | SQL statements are assembled as f-strings at 70 sites, each carrying a per-line `# noqa: S608` and `# nosec B608` that names what it interpolates. Paths from outside reach `read_json` as bound parameters, and the hand-rolled quote-doubling escape `sql_literal` remains for the statement kinds DuckDB won't prepare, such as `ATTACH` and the producer's one-row session projection. Both scanners that implement the rule are silenced at every site, so neither flags a new unsafe interpolation. The guard is an AST audit test over the eleven modules that build SQL text, which hold 67 of the 70 sites (the other three, in `atif_duck.domain.examples`, interpolate catalog view and macro names). | `deprecated pattern` | L | `pyproject.toml:205-208`, `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:93-97`, `packages/atif-duck/src/atif_duck/domain/sql_literal.py:15-18`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:1712`, `packages/atif-duck/src/atif_duck/infrastructure/columnar.py:536`, `packages/atif-duck/tests/test_sql_text_boundaries.py:457` |
| 4 | No test CI runs reads a real `~/.claude` corpus. The live parity tests read local transcripts and skip wherever there are none, CI included; the integration tier is a few tests over two small synthetic sessions; and the migration parity oracles `AGENTS.md` mentions aren't part of this repo. The six fidelity gaps are policy about real-world JSONL shapes that nothing in CI exercises. | `missing tests` | L | `packages/atif-converter/tests/test_parity_live.py:3-9`, `packages/atif-converter/tests/test_parity_live.py:107`, `packages/atif-cli/tests/test_integration.py:3-9`, `packages/atif-cli/tests/test_integration.py:24-34`, `AGENTS.md:462-464` |
| 5 | Alpine and other musl targets can't install the package, and not merely slowly: `duckdb` and `lancedb` publish no `musllinux` wheels, and `lancedb 0.39.0` publishes no sdist at all, so there is nothing to build from. The `manylinux`, macOS, and Windows targets have a prebuilt wheel for every native package in the closure. | `version pin` | L | `RELEASING.md:217-219`, `RELEASING.md:211-213` |
| 6 | The four size ratchets and the complexity ratchet are set at the measured worst function in the tree: `max-statements = 119` for `_friction_async`, `max-branches = 39` and `max-complexity = 38` for `enrich_trajectory`, and `max-args = 19` for `materialize` (the config's comment names the `search` command, which takes seven). The ratchet stops growth and permanently blesses the current outliers; nothing in the config plans their reduction. | `wrong abstraction` | L | `pyproject.toml:320-343`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:210`, `packages/atif-converter/src/atif_converter/domain/enrichment.py:198`, `packages/atif-corpus/src/atif_corpus/application/materialize.py:1497` |
| 7 | `TC001`/`TC002`/`TC003` are ignored on the strength of a manual probe the config records: moving 58 imports under `TYPE_CHECKING` broke `typing.get_type_hints` on 159 first-party functions and classes (the three rules flag 33 imports under `packages/*/src` on this tree). The `PLC0415` ignore beside it (182 sites by the config's count, 178 under `packages/*/src` on this tree) has a regression test proving its property in a fresh interpreter; the `get_type_hints` property has none, so the argument that justifies the ignore is not locked. | `missing tests` | M | `pyproject.toml:220-239`, `packages/atif-cli/tests/test_lean_import.py:41` |
| 8 | Every scanner in the `security` tier has its findings exit code swallowed, by a stated contract: findings do not fail the job, only a scanner that produced no usable report does. Gating on those findings is delegated to GitHub code-scanning alerts and branch protection. Inside the repo, only dependency review fails on a vulnerability, and only on a pull request whose dependency changes carry a high or critical advisory; a new advisory against a locked dependency, a new secret, or a new SAST finding fails nothing. | `error handling` | M | `mise.toml:270-276`, `mise.toml:443`, `mise.toml:561`, `.github/workflows/dependency-review.yml:49` |
| 9 | The provider/dimension guard exists as two deliberate twins, 88 lines in atif-duck and 83 in atif-embed, because the independence contract forbids either package importing the other. The drift pin reads the atif-duck twin as SOURCE TEXT, parses it with `ast`, and asserts substrings, including a raw `source.index("raise EmbeddingProviderMismatch")` that silently changes meaning if the raise site is renamed or a second raise appears. The two docstrings differ; the pin covers the recovery string and the interpolation, not the rule. | `duplicated logic` | M | `packages/atif-embed/tests/test_guard_twin_pin.py:3-13`, `packages/atif-embed/tests/test_guard_twin_pin.py:93-95`, `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16` |
| 10 | CONTRACT-V2, the binding document for the analytics pipelines, VSS, the model registry, and the cron lanes, disclaims describing the code: when it disagrees with the code, the code is authoritative, and the reader is told to use it "for design intent, not as a description of current behavior" instead. CONTRACT v1 binds the corpus layout, the enrichment pass, the transcript-derived surface, and the CLI shapes, and hands everything on top of them to V2. So the surfaces V2 covers have no binding document that can detect drift, which is the only thing a contract document is for. *judgment-call*: the disclaimer warns the reader, and the drift it warns about stays. | `dead code adjacent` | M | `docs/CONTRACT-V2.md:3-8`, `docs/CONTRACT.md:5-8` |
| 11 | `FakeConverter`, a scriptable test double, ships inside the installable `atif-sql` wheel under `atif_corpus/infrastructure/`, and it's the converter the atif-corpus end-to-end materialize suite runs against. It fabricates contract-shaped output from the session id without converting the transcript, and it stands in for the real converter adapter in exactly the tests that would catch a state-transition bug. | `wrong abstraction` | S | `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:3-9`, `pyproject.toml:98`, `packages/atif-corpus/tests/test_materialize.py:3`, `packages/atif-cli/src/atif_cli/converter_adapter.py:80` |
| 12 | Independently declared `DomainError(Exception)` bases with the same name and no shared ancestor, so a caller composing members cannot write one `except` clause and any symbol-name search cross-attributes them all. | `duplicated logic` | S | `packages/atif-converter/src/atif_converter/domain/errors.py:14`, `packages/atif-embed/src/atif_embed/domain/errors.py:14`, `packages/atif-models/src/atif_models/domain/ports.py:39` |
| 13 | The 89% coverage floor is not part of the declared definition of done. `mise run check` depends on its gates and `test:cov` is not among them, so a local green run can drop coverage below the floor and only the CI job notices. | `missing tests` | S | `mise.toml:245-265`, `pyproject.toml:641`, `.github/workflows/check.yml:90` |

## Explicit markers

**None in code.** This section is empty because the code contains no comment markers, not
because the search was narrow.

- Scanned `packages/*/src` for `\bTODO\b`, `\bFIXME\b`, `\bHACK\b`, `\bXXX\b`: no matches.
- Scanned `packages/` plus `scripts/` with the same pattern: no matches.
- Scanned every file `git ls-files` reports: the only matches are the Vale style lists that
  name the markers (`.vale/styles/proselint/Annotations.yml:6-8`,
  `.vale/styles/Google/Acronyms.yml:56`) and docs pages that discuss them, this one included.
- No `@deprecated` decorator and no `# DEPRECATED` banner sits on any first-party symbol.
  Every `deprecat*` hit in code and config names an UPSTREAM deprecation or is data:
  `db.table_names()` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:90`, a
  lancedb kwarg family at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:273`, an osv-scanner
  flag at `mise.toml:431-432`, short SPDX ids at `.github/workflows/dependency-review.yml:67`,
  model retirement dates in the bundled price table, and example text in an analytics prompt
  at `packages/atif-analytics/src/atif_analytics/application/prompts.py:260`.
- The closest thing to a marker is a comment at `scripts/atif-sql-refresh.sh:86-89`, which
  calls leaving embedding out of the Codex pass a deliberate deferral and semantic search over
  that corpus a follow-up. It names a scope choice rather than a defect, so it has no register
  row.

The absence is enforced, not incidental. `select = ["ALL"]` at `pyproject.toml:202` leaves
flake8-fixme and flake8-todos on, and neither appears among the ignores at
`pyproject.toml:209-240`.
Probed 2026-10-12 by piping a module with one `# TODO: ...` comment to `ruff check` under a
`packages/atif-duck/src/` filename: it exits 1 with `TD002` (missing author), `TD003` (missing
issue link), and `FIX002` (line contains `TODO`). A marker cannot reach `main` past `mise run lint`, so the
debt lives in the channels the register draws on instead of in markers. One more channel
exists and contributes no row: a comment explaining the code by reference to an
implementation no installer of this package can obtain. `grep -riE 'claude-sql|predecessor'
packages/*/src` returns one line,
`packages/atif-converter/src/atif_converter/domain/codex_edges.py:30`, where the word names a
record's previous record in a rollout log. No lint can police that one, so it is empty by review
rather than by gate, which is why it is worth naming here.

## Pattern-level smells

### The toolchain config is the real debt register, and it is load-bearing

Every suppression in `pyproject.toml` carries its measured count and its cost-of-removal
analysis, which makes the config the most honest debt document in the repo, and also
means a large amount of unresolved work is encoded as a rule that is off rather than as an
issue anyone tracks. The disabled Unknown-propagation rules cover a large block of findings and
the config names what would let them back on, none of which exists.
The `TC*` rules are off because a probe found that fixing them breaks
`typing.get_type_hints` on first-party functions. Per-site `# noqa: N818` markers flag
error classes deliberately not named `*Error`. The size limits are pinned at the current
worst function. Reading the config top to bottom is a fuller account of what this
codebase owes than reading its comments, and none of it appears in any tracker.

Shows up in:

- `pyproject.toml:209-240`: the ignores, each with its site count.
- `pyproject.toml:721-764`: the findings traced to origin, plus the stub gap.
- `pyproject.toml:320-343`: the ratchets set at the measured worst.
- `pyproject.toml:440-441`: `[tool.ty.rules] all = "error"` with nothing turned off for
  `src/`, which is what the pattern looks like when it holds: a suppression here would need the
  same measured argument every entry above carries.

Cost: L. The analysis is done and written; the work it defers is a `TypedDict` model of
the JSONL record, stub distributions, and refactors of named functions.

### The converter is a parity port, so it keeps upstream's losses on purpose

`atif-converter` converts Claude Code and Codex transcripts itself, on the public ATIF data
classes vendored under `atif_converter.domain.atif`. harbor's private converters survive only in
the tests, as the parity oracle the port is measured against, live while harbor still ships
them and frozen to goldens after that. The data-loss gaps stay typed as enum members because
the port is a PARITY port: fixing one means deciding to diverge from the oracle. The live
oracle also rests on private upstream methods, so the day harbor drops them, every test that
needs one skips and the frozen goldens are the only reference left.

Shows up in:

- `packages/atif-converter/src/atif_converter/domain/claude_code_conversion.py:981` and
  `packages/atif-converter/src/atif_converter/domain/codex_conversion.py:924`: the two
  conversion entry points, one per agent.
- `packages/atif-converter/tests/harbor_oracle.py:118` and
  `packages/atif-converter/tests/harbor_oracle.py:358`: the live oracle call and the diff the
  port is held to.
- `packages/atif-converter/tests/harbor_oracle.py:83`: the skip that takes over when the
  private method is gone.
- `packages/atif-converter/src/atif_converter/domain/fidelity.py:55-62`: the enum docstring
  that names the gaps as kept by choice.

Cost: L. It's the work of register row 1: each gap closed is a deliberate divergence from the
oracle that has to be decided and pinned on its own.

### The independence contract is paid for in copies, and the copies are pinned by text

The independent packages may never import each other, so shared logic is duplicated instead of
extracted. SQL-literal escapers with identical bodies exist in atif-duck and atif-embed, the
composition root carries a third, and the same pure provider/dimension guard exists as twins.
The escapers in the two packages
are the contract's price paid honestly: each package owns one copy, in `domain/`, and no
import would be legal. The guard is the price paid dangerously. The mechanism keeping the
twins honest is a test that reads the other package's file as text, parses it with `ast`,
and asserts substrings, including one raw `str.index` on a `raise` keyword. That test is
fragile in a way an import never is: it passes when the twins agree on a string and
disagree on their logic, and it breaks on a rename that changes nothing.

Shows up in:

- `packages/atif-duck/src/atif_duck/domain/sql_literal.py:31` and
  `packages/atif-embed/src/atif_embed/domain/sql_literal.py:30`: the domain-layer escapers,
  each in its own package's `domain/`, each reached by that package's adapters.
- `packages/atif-cli/src/atif_cli/app.py:134`: the composition root's copy, which
  could legally import either of those above and does not.
- `packages/atif-embed/tests/test_guard_twin_pin.py:93-95`: the substring assertions that
  stand in for a shared import.
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16`: the twin whose
  docstring differs from its counterpart's.

Cost: M. Collapsing the guard twins needs a decision about a shared-kernel package the
contract forbids. The escapers need no decision: a shared kernel would absorb them,
and short of one, one copy per package is the contract working rather than failing.

### The scanner tier never fails on a finding

Every scanner in the `security` tier has its findings exit code swallowed (most
sites use a trailing `|| true`, the rest an equivalent flag) under an explicit contract: a
finding does not fail the task, a missing report does. The reasoning is sound, because a
non-zero exit kills the CI step before the SARIF upload. The consequence is that a new secret,
a new SAST finding, or a new advisory against a locked dependency fails no gate inside this
repository, and enforcement for those lives in GitHub code-scanning alerts and branch
protection. Two gates do fail: dependency review fails a pull request whose dependency changes
carry a high or critical advisory, and `security:vex:check` fails `mise run check` when the
OpenVEX suppression ledger drifts from the lockfile. The same asymmetry appears in the
coverage floor: `fail_under = 89` exists, and the task that reads it is not among the gates
`mise run check` depends on, so the declared definition of done does not measure coverage.
bandit's `B608` findings never reach code scanning at all, because every SQL site carries
`# nosec B608`, so the SQL review lives in per-line comments rather than in any scanner's
output.

Shows up in:

- `mise.toml:270-276`: the contract, stated plainly.
- `mise.toml:443` and `mise.toml:561`: swallow sites, the second one bandit's.
- `.github/workflows/dependency-review.yml:49`: the severity at which a pull request fails.
- `mise.toml:245-265`: the gates, with `security:vex:check` and without `test:cov`.
- `pyproject.toml:641`: the floor that only `.github/workflows/check.yml:90` enforces.

Cost: M. Splitting each scanner into "run and upload" plus "assert the finding delta
against a baseline" is mechanical; agreeing on the baseline is the work.

### The end-to-end materialize suite runs against a double

The atif-corpus end-to-end materialize suite runs against a fake converter that ships in the
wheel and fabricates its output from the session id, so the state transitions that matter
most are exercised against a double. The tests that push transcripts through the real
converter are the CLI integration tier, over two small synthetic sessions, and the opt-in live
parity tests, which read local transcripts and skip wherever there are none, CI included.

Shows up in:

- `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:3-9`: the double
  the materialize suite uses instead of the real adapter.
- `packages/atif-cli/tests/test_integration.py:3-9`: the whole integration tier: a few
  small synthetic sessions.
- `packages/atif-converter/tests/test_parity_live.py:3-9`: the live parity tests, and where
  they skip.

Cost: M. A corpus fixture built from real, scrubbed `~/.claude` records is the whole of
it, and it's what would put the fidelity gaps in front of CI, where no real transcript
reaches them.

## See also

- [impact analysis](impact-analysis.md)
- [contract map](contract-map.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [business logic](business-logic.md)
