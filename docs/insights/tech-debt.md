# atif-sql · Tech debt

**There are zero `TODO`, `FIXME`, `HACK`, and `XXX` markers in this repository.** Three
scans confirm it: over `packages/*/src`, over `packages/` plus `scripts/`, and over every
file `git ls-files` names. That means the marker channel carries no signal here. It does
not mean the codebase is debt-free, and this page exists because the debt is real and
lives elsewhere.

So the register below is assembled from five sources rather than from comments. Every
citation on this page carries its full path, so no reference resolves through a nearby
antecedent.

1. **The suppression config, read as the debt register someone already wrote.** `select =
   ["ALL"]` with exactly 12 ignores (`pyproject.toml:62-93`), four pyright rules switched
   off (`pyproject.toml:570-581`), ty at `all = "error"` with no rule disabled
   (`pyproject.toml:279-280`), and per-site
   `# noqa` / `# pyright: ignore` counts taken by grep. Almost every config entry carries
   its own measured count and its cost-of-removal analysis inline, which is a better debt
   record than a comment marker and is quoted here rather than re-derived.
2. **Version ceilings and the reason attached to each**, read at the pin in the member
   manifests, not inferred from the lockfile.
3. **Install weight and platform coverage**, computed independently from `uv.lock` by
   walking the `atif-sql` dependency graph twice — once whole, once with `harbor` blocked —
   following extras as well as direct dependencies.
4. **Asymmetric enforcement between sibling surfaces**: a coverage floor enforced in CI but
   not in the declared definition of done, so a local green run does not measure it.
5. **Coverage and test-shape gaps**, per module. Coverage figures are measured from the
   generated report, which is gitignored, so every figure is cited to the source module it
   describes.

Category and cost vocabularies are closed. Categories: `marker`, `wrong abstraction`,
`error handling`, `dead code adjacent`, `deprecated pattern`, `version pin`, `duplicated
logic`, `missing tests`. Cost: `S`, `M`, `L`. No row carries the `marker` category, for
the reason in the first paragraph. Rank is `cost-to-fix × consequence-of-leaving`, so a
cheap fix for a harmless problem sinks and an expensive fix for a structural problem
rises.

## Ranked register

| Rank | Debt item | Category | Cost to fix | Citation |
| --- | --- | --- | --- | --- |
| 1 | The entire Claude Code → ATIF conversion path runs through one PRIVATE upstream method, `ClaudeCode._convert_events_to_trajectory`, verified against harbor 0.22.0 only — so the manifest carries a minor-version ceiling and the unit tests are the drift alarm. Nothing upstream promises the method exists in 0.23. | `version pin` | L | `packages/atif-converter/pyproject.toml:19-23` |
| 2 | Seven upstream conversion losses are catalogued as `FidelityGap` enum members rather than fixed. Gap 3 flattens the `parentUuid` tree by timestamp sort, losing branch and rewind structure; gap 7 drops the event `uuid`, making step-to-raw-record identity unrecoverable from the trajectory — recovered out of band through a parallel `edges.jsonl` sidecar the corpus layout mandates. | `wrong abstraction` | L | `packages/atif-converter/src/atif_converter/domain/fidelity.py:44-79`, `docs/CONTRACT.md:26-28` |
| 3 | 63 of the 113 runtime distributions reach this project only through `harbor` — `fastapi`, `uvicorn`, `starlette`, the whole `supabase` client stack, `litellm`, `openai`, `tiktoken`, `tokenizers`, `huggingface-hub`, `cryptography`, `aiohttp` — for exactly one private method call. A CLI that converts JSONL ships a web server and a database client. 57% of the roster, 155 MiB. | `version pin` | L | `RELEASING.md:193-200` |
| 4 | 415 pyright findings sit behind three disabled Unknown-propagation rules, and the config names the fix it has not built: a `TypedDict` model of the `~/.claude` JSONL record and the ATIF trajectory would close 144 of them. Until that model exists, every JSON-shaped value in the hottest modules is `dict[str, Any]` narrowed by `isinstance`, with the type checker's opinion switched off. The two heaviest concentrations are the converter's enrichment module at 59 findings and the analytics corpus reader at 33. | `wrong abstraction` | L | `pyproject.toml:543-573` |
| 5 | 15 SQL statements are built by string interpolation through a hand-rolled quote-doubling escape, because DuckDB rejects prepared parameters as table-function arguments. ruff's `# noqa: S608` silences ruff only — bandit reports the same 15 as B608 and those findings upload to code scanning, so the suppression is not portable across the two scanners that both implement the rule. | `deprecated pattern` | L | `pyproject.toml:639-641`, `packages/atif-duck/src/atif_duck/infrastructure/registry.py:172`, `packages/atif-embed/src/atif_embed/infrastructure/corpus_text_rows.py:108` |
| 6 | No test reads a real `~/.claude` corpus. The whole integration tier is two tests over two synthetic 4-event sessions, and parity oracles against the predecessor implementation are explicitly not part of this repo. The seven fidelity gaps are policy about real-world JSONL shapes that nothing real-world exercises. | `missing tests` | L | `packages/atif-cli/tests/test_integration.py:3-9`, `packages/atif-cli/tests/test_integration.py:24-34`, `AGENTS.md:92-94` |
| 7 | `hdbscan 0.8.44` has never published an aarch64 wheel, so a first install on Graviton, an ARM CI runner, or a `linux/arm64` container compiles five Cython extensions and needs a C toolchain. musl is not merely slow but impossible: `lancedb 0.37.1` publishes no sdist at all, so there is nothing to build from. | `version pin` | L | `RELEASING.md:181-191` |
| 8 | The four size ratchets and the complexity ratchet are set at the measured worst function in the tree — `max-statements = 119` for `_friction_async`, `max-complexity = 38` for `enrich_trajectory`, `max-args = 19` for the `search` command. The ratchet stops growth and permanently blesses the current outliers; nothing in the config plans their reduction. | `wrong abstraction` | L | `pyproject.toml:167-190`, `packages/atif-analytics/src/atif_analytics/application/use_cases/friction.py:240`, `packages/atif-converter/src/atif_converter/domain/enrichment.py:198` |
| 9 | `TC001`/`TC002`/`TC003` are ignored on the strength of a manual probe: moving 58 imports under `TYPE_CHECKING` broke `typing.get_type_hints` on 159 first-party functions and classes. The 182-site `PLC0415` ignore beside it has a regression test proving its property in a fresh interpreter; the `get_type_hints` property has none, so the argument that justifies the ignore is not locked. | `missing tests` | M | `pyproject.toml:79-92`, `packages/atif-cli/tests/test_lean_import.py:35` |
| 10 | Every one of the nine security scanners has its findings exit code swallowed at eight sites, by a stated contract: findings do not fail the job, only a scanner that produced no usable report does. Gating is delegated entirely to GitHub code-scanning alerts and branch protection, so nothing inside the repo fails on a new vulnerability. | `error handling` | M | `mise.toml:217-223`, `mise.toml:366`, `mise.toml:482` |
| 11 | The provider/dimension guard exists as two deliberate 83-line twins because the independence contract forbids either package importing the other. The drift pin reads the atif-duck twin as SOURCE TEXT, parses it with `ast`, and asserts substrings — including a raw `source.index("raise EmbeddingProviderMismatch")` that silently changes meaning if the raise site is renamed or a second raise appears. The two docstrings have already diverged; the pin covers the recovery string and the interpolation, not the rule. | `duplicated logic` | M | `packages/atif-embed/tests/test_guard_twin_pin.py:3-13`, `packages/atif-embed/tests/test_guard_twin_pin.py:93-95`, `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16` |
| 12 | The clustering path is the least-tested code in the tree and it is also the least portable: the structural clustering module at 22% (23 of 30 lines unexecuted), the Lance reader at 31%, the cluster use case at 51% — against an 89.98% whole-tree combined figure. This is exactly the UMAP/HDBSCAN code behind the aarch64 wheel gap. | `missing tests` | M | `packages/atif-analytics/src/atif_analytics/domain/structure/cluster.py:88`, `packages/atif-analytics/src/atif_analytics/infrastructure/lance_reader.py:38-49`, `packages/atif-analytics/src/atif_analytics/application/use_cases/cluster.py:50` |
| 13 | Both binding contract documents disclaim describing the code. CONTRACT-V2 states outright that when it disagrees with the code, the code wins and the file is design intent "not a description of current behavior"; CONTRACT v1 calls its own scope section historical. A binding document that pre-emptively surrenders authority cannot be used to detect drift, which is the only thing a contract document is for. *judgment-call* — the disclaimers are honest, and honesty about staleness is still staleness. | `dead code adjacent` | M | `docs/CONTRACT-V2.md:3-8`, `docs/CONTRACT.md:5-8` |
| 14 | Two version constraints are unexplained or inconsistent across manifests where every other pin in the repo justifies itself: `lancedb>=0.30,<0.38` is declared twice with no comment on either side, and `boto3` is floored at `>=1.42.91` in one member and `>=1.40.0` in another for the same Bedrock client work. | `version pin` | S | `packages/atif-analytics/pyproject.toml:27`, `packages/atif-embed/pyproject.toml:20-22`, `packages/atif-models/pyproject.toml:20` |
| 15 | `FakeConverter` — a scriptable test double — ships inside the installable `atif-corpus` wheel under `infrastructure/`, and it is the converter the end-to-end materialize suite runs against. A fake that both mutates and reads corpus state stands in for the real harbor adapter in exactly the tests that would catch a state-transition bug. | `wrong abstraction` | S | `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:3-9`, `packages/atif-corpus/tests/test_materialize.py:25` |
| 16 | Three independently declared `DomainError(Exception)` bases with the same name and no shared ancestor, so a caller composing two members cannot write one `except` clause and any symbol-name search cross-attributes all three. | `duplicated logic` | S | `packages/atif-converter/src/atif_converter/domain/errors.py:14`, `packages/atif-embed/src/atif_embed/domain/errors.py:14`, `packages/atif-models/src/atif_models/domain/ports.py:39` |
| 17 | The 89% coverage floor is not part of the declared definition of done. `mise run check` depends on nine gates and `test:cov` is not among them, so a local green run can drop coverage below the floor and only the CI job notices. | `missing tests` | S | `mise.toml:197-212`, `pyproject.toml:462`, `.github/workflows/check.yml:78` |

## Explicit markers

**None.** This section is empty because the repository contains no comment markers, not
because the search was narrow.

- Scanned `packages/*/src` for `\bTODO\b`, `\bFIXME\b`, `\bHACK\b`, `\bXXX\b`: 0 matches.
- Scanned `packages/` plus `scripts/` with the same pattern: 0 matches.
- Scanned every file `git ls-files` reports: 0 matches.
- No `@deprecated` decorator and no `# DEPRECATED` banner sits on any first-party symbol.
  Every `deprecat*` hit in the tree describes an UPSTREAM deprecation, and the two members
  reading the LanceDB store name the same one independently — `db.table_names()` at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:90` and at
  `packages/atif-analytics/src/atif_analytics/infrastructure/lance_reader.py:43`, because the
  independence contract means neither can read the other's warning. The other two are a
  lancedb kwarg family at
  `packages/atif-embed/src/atif_embed/infrastructure/lance_store.py:274` and an osv-scanner
  flag at `mise.toml:362`.
- The closest thing to a marker is `RELEASING.md:193`, which names the harbor dependency
  subtree "the standing follow-up" in prose — a declined-scope note in the release record
  rather than a comment in code. It is register row 3.

The absence is enforced, not incidental. `select = ["ALL"]` at `pyproject.toml:55` leaves
flake8-fixme and flake8-todos on, and neither appears among the 12 ignores at
`pyproject.toml:62-93`.
Probed 2026-08-28 by dropping one `# TODO: ...` comment into `packages/atif-duck/src/`:
`ruff check` exits 2 with `TD002` (missing author), `TD003` (missing issue link), and
`FIX002` (line contains TODO). A marker cannot reach `main` past `mise run lint`, so the
debt lives in the five channels the register draws on instead of in markers. A sixth channel
exists and contributes no row: a comment explaining the code by reference to an
implementation no installer of this package can obtain. `grep -riE 'claude-sql|predecessor'
packages/*/src` returns 0 lines. No lint can police that one, so it is empty by review
rather than by gate, which is why it is worth naming here.

## Pattern-level smells

### The toolchain config is the real debt register, and it is load-bearing

Every suppression in `pyproject.toml` carries its measured count and its cost-of-removal
analysis, which makes the config the most honest debt document in the repo — and also
means a large amount of unresolved work is encoded as a rule that is off rather than as an
issue anyone tracks. The three disabled Unknown-propagation rules cover 415 findings and
the config names the two things that would let them back on, neither of which exists.
Three `TC*` rules are off because a probe found that fixing them breaks
`typing.get_type_hints` on 159 first-party functions. Nine per-site `# noqa: N818` mark
error classes deliberately not named `*Error`. Five size limits are pinned at the current
worst function. Reading the config top to bottom is a more complete account of what this
codebase owes than reading its comments, and none of it appears in any tracker.

Shows up in:

- `pyproject.toml:62-93` — the 12 ignores, each with its site count.
- `pyproject.toml:543-582` — 415 findings traced to origin, plus the 23-warning stub gap.
- `pyproject.toml:167-190` — the five ratchets set at the measured worst.
- `pyproject.toml:279-280` — `[tool.ty.rules] all = "error"` with nothing disabled beneath
  it, which is what the pattern looks like when it holds: a suppression here would need the
  same measured argument every entry above carries.

Cost: L — the analysis is done and written; the work it defers is a `TypedDict` model of
the JSONL record, four stub distributions, and three refactors of named functions.

### One private upstream method carries the whole product

`atif-converter` exists to call `ClaudeCode._convert_events_to_trajectory`, a private
harbor method. Four consequences compound. The manifest carries a minor-version ceiling
that only a manual re-audit can lift. Seven data losses are documented as enum members
because the private method's behavior cannot be changed, and two of them — flattened
parent chains, dropped event uuids — are worked around by writing a parallel `edges.jsonl`
sidecar for every session, so the corpus layout itself is shaped by an upstream defect.
The dependency arrives with 63 other distributions including a web framework, an ASGI
server, and a hosted-database client, none of which this code imports.

And it forecloses a supply-chain control the rest of the toolchain would take. semgrep's
`uv-missing-dependency-cooldown` asks for `exclude-newer` under `[tool.uv]`, so a freshly
published version waits before it can be resolved — the standard mitigation for a
compromised release. Probed 2026-08-28 with `uv lock --dry-run --exclude-newer "7 days"`:
the workspace becomes UNSATISFIABLE, because no distribution inside
`harbor>=0.22.0,<0.23` is older than the cutoff. The control is declined for that measured
reason rather than overlooked, and `uv.lock` plus `uv sync --locked` remain what actually
pins the resolution.

Shows up in:

- `packages/atif-converter/pyproject.toml:19-23` — the ceiling and the reason.
- `packages/atif-converter/src/atif_converter/domain/fidelity.py:44-79` — the seven gaps.
- `docs/CONTRACT.md:26-28` — `edges.jsonl`, the sidecar that exists because of gaps 3 and 7.
- `RELEASING.md:193-200` — 63 of 113 packages, 155 MiB, for one method.

Cost: L — either vendor the conversion the method performs and drop harbor entirely, or get
a public entry point upstreamed. Both are real projects.

### The independence contract is paid for in copies, and the copies are pinned by text

Five packages may never import each other, so shared logic is duplicated instead of
extracted. Three byte-identical SQL-literal escapers exist, one per package that inlines
SQL, and the same pure provider/dimension guard exists as two 83-line twins. The escapers
are the contract's price paid honestly: each package owns one copy, in `domain/`, and no
import would be legal. The guard is the price paid dangerously. The mechanism keeping the
twins honest is a test that reads the other package's file as text, parses it with `ast`,
and asserts substrings — including one raw `str.index` on a `raise` keyword. That test is
fragile in a way an import never is: it passes when the twins agree on a string and
disagree on their logic, and it breaks on a rename that changes nothing.

Shows up in:

- `packages/atif-duck/src/atif_duck/domain/sql_literal.py:18` and
  `packages/atif-embed/src/atif_embed/domain/sql_literal.py:24` — two of the three escapers,
  each in its own package's `domain/`, each reached by that package's adapters.
- `packages/atif-cli/src/atif_cli/app.py:106` — the third, in the composition root, which
  could legally import either of the two above and does not.
- `packages/atif-embed/tests/test_guard_twin_pin.py:93-95` — the substring assertions that
  stand in for a shared import.
- `packages/atif-duck/src/atif_duck/domain/embedding_guard.py:5-16` — the twin whose
  docstring has already drifted from its counterpart's.

Cost: M — collapsing the guard twins needs a decision about a shared-kernel package the
contract forbids. The three escapers need no decision: a shared kernel would absorb them,
and short of one, one copy per package is the contract working rather than failing.

### Findings never fail anything

Nine security scanners run, and every one has its findings exit code swallowed — eight
sites use a trailing `|| true`, the rest an equivalent flag — under an explicit contract: a
finding does not fail the task, a missing report does. The reasoning is sound, because a
non-zero exit kills the CI step before the SARIF upload. The consequence is that no gate
inside this repository fails on a new
vulnerability, a new secret, or a new SAST finding, and enforcement lives entirely in
GitHub code-scanning alerts and branch protection. The same asymmetry appears in the
coverage floor: `fail_under = 89` exists, and the task that reads it is not among the nine
`mise run check` depends on, so the declared definition of done does not measure coverage.
The suppression ledger this tier generates is currently empty, which reads identically to
"nothing needed suppressing" and to "nobody has looked".

Shows up in:

- `mise.toml:217-223` — the contract, stated plainly.
- `mise.toml:366` and `mise.toml:482` — two of the eight swallow sites, one of them
  bandit's, whose 15 B608 findings therefore reach code scanning permanently.
- `mise.toml:197-212` — the nine gates, without `test:cov`.
- `pyproject.toml:462` — the floor that only `.github/workflows/check.yml:78` enforces.

Cost: M — splitting each scanner into "run and upload" plus "assert the finding delta
against a baseline" is mechanical; agreeing on the baseline is the work.

### The code the type checkers guard hardest is the code the tests reach least

Combined line-and-branch coverage is 89.98% and the misses are not spread evenly — they
concentrate in one path, the numeric clustering one.
`packages/atif-analytics/src/atif_analytics/domain/structure/cluster.py:88` is at 22%, and
it is the module carrying a `pyright: ignore` on the UMAP call and depending on the one
package with no aarch64 wheel.
`packages/atif-analytics/src/atif_analytics/infrastructure/lance_reader.py:38-49` is at 31%,
so the store-shape branches deciding whether the structural stages run at all are
unexecuted. Above all of it, the end-to-end materialize suite runs against a fake converter
that ships in the wheel, so the state transitions that matter most are exercised against a
double.

Shows up in:

- `packages/atif-analytics/src/atif_analytics/domain/structure/cluster.py:88` — 22% covered,
  and the site of the UMAP `pyright: ignore`.
- `packages/atif-analytics/src/atif_analytics/infrastructure/lance_reader.py:38-49` — 31%
  covered, including the `noqa: BLE001` that turns any store-shape surprise into a skip.
- `packages/atif-corpus/src/atif_corpus/infrastructure/fake_converter.py:3-9` — the double
  the materialize suite uses instead of the real adapter.
- `packages/atif-cli/tests/test_integration.py:3-9` — the whole integration tier: two
  synthetic 4-event sessions.

Cost: M — a corpus fixture built from real, scrubbed `~/.claude` records is the whole of
it, and it is what would exercise both the clustering path and the fidelity gaps nothing
real-world currently touches.

## See also

- [impact analysis](impact-analysis.md) — 24 shared source citations
- [contract map](contract-map.md) — 19 shared source citations
- [module map](../architecture/module-map.md) — 18 shared source citations
- [processes](../behavior/processes.md) — 16 shared source citations
- [business logic](business-logic.md) — 14 shared source citations
