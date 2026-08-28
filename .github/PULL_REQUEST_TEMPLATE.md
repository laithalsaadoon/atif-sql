<!--
Every box below corresponds to something this repository actually enforces — a
mise task, a lefthook hook, an import-linter contract, or a drift test. Nothing
here is aspirational, and nothing that CI checks on its own is repeated here.
Delete the sections that do not apply; do not delete the first two.
-->

## What changes and why

<!-- One paragraph. The "why" is the part review cannot reconstruct from the diff. -->

## Gates

- [ ] **`mise run check` is green locally.** Five tasks, all of which must pass:
      `lint` (ruff), `fmt` (`ruff format --check`; `mise run fmt:write` applies
      it), `typecheck` (`ty`, with `all = "error"` and `error-on-warning = true`,
      so a warning fails too), `lint:imports` (import-linter), and `test`
      (pytest over every member's `tests/`).
- [ ] **Every commit subject is a Conventional Commit** (`type(scope): subject`,
      from `feat|fix|chore|docs|refactor|test|perf|ci`). The `commit-msg` hook
      runs `cz check`, and the release flow derives the version increment from
      these subjects — a `feat:` mislabelled `chore:` silently costs a minor bump.

## If you touched layers or package boundaries

- [ ] The **layer DAG** still holds for each package you touched
      (`application` > `infrastructure` > `domain`; `atif-duck` and `atif-models`
      are `infrastructure` > `domain`), and `mise run lint:imports` proves it.
- [ ] The **independence contract** still holds: `atif-converter`,
      `atif-corpus`, `atif-duck`, `atif-models`, and `atif-embed` do not import
      each other. `atif-cli` is the only composition root; `atif-analytics` may
      import `atif-models` and nothing else among the seven.
- [ ] A new cross-package dependency is declared in **both** the member's
      `[project.dependencies]` **and** its `[tool.uv.sources]` as
      `{ workspace = true }`, and carries a version constraint — an unconstrained
      intra-workspace name resolves to a stranger's package once published.

## If you added a DuckDB view or macro

- [ ] `DESCRIPTIONS` entry in `atif_duck/domain/catalog.py`.
- [ ] `ARG_EXEMPLARS` entry for every new parameter name.
- [ ] `TABLE_MACRO_NAMES` membership if the DDL is `AS TABLE`.
- [ ] The derived example **executes** against the fixture corpus
      (`packages/atif-duck/tests/test_examples.py`), or there is an `EXCLUSIONS`
      entry saying why it cannot.

## If you changed dependencies

- [ ] `uv.lock` is regenerated and committed (`uv lock`), and `mise run
      lock:check` passes. The `uv-lock-sync` pre-commit job fails on a stale
      lockfile.
- [ ] A `harbor` version change re-ran the **atif-converter** suite. Those tests
      pin upstream behavior and are the drift alarm for the private
      `ClaudeCode._convert_events_to_trajectory` call; the pin is
      `>=0.22.0,<0.23` and the fidelity gaps in `domain/fidelity.py` need
      re-auditing past it.
- [ ] No new hardcoded Bedrock model id. `atif-models` owns the alias registry
      and no other package may name a model.

## Not in scope for a pull request

- [ ] **No version numbers were hand-edited.** The seven members move in
      lockstep and `.github/workflows/release.yml` is the only thing that writes
      a version — see `RELEASING.md`. A hand-edited version desynchronizes the
      intra-workspace pins and the first symptom is an unresolvable
      `pip install`.
- [ ] `CHANGELOG.md` was not hand-edited; it is generated from commit subjects.
