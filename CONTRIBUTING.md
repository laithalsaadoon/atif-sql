# Contributing to atif-sql

atif-sql is a uv workspace whose root is also the one published distribution:
`pyproject.toml` carries both a `[project]` table (the single `atif-sql` wheel,
which bundles every module tree) and `[tool.uv.workspace] members =
["packages/*"]`. The packages under `packages/` are development members,
not install targets. Everything below is read off the
checked-in config (`mise.toml`, `pyproject.toml`, `lefthook.yml`, and
`AGENTS.md`), so if a claim here and a config file disagree, the config file
wins and this document is the bug.

## First-time setup

```bash
mise trust && mise install && mise run install
```

`mise install` provisions the toolchain pinned in `mise.toml` (`[tools]`:
Python 3.13 and the latest `uv`; `min_version = "2025.1.0"`). `mise run install`
is `uv sync --all-packages` and depends on `hooks:install`, so it also installs
the lefthook git hooks in the same step.

Python is 3.13 rather than 3.14 on purpose: `mise.toml`'s header records that
harbor (a dev dependency, for the test oracles) floors at
`Requires-Python >=3.12` and 3.13 is the safe pick while harbor's dependency chain
settles on 3.14.

Drive every command through `mise run <task>` rather than calling `uv`, `ruff`,
or `ty` directly. The mise `[env]` block sets `PYTHONDONTWRITEBYTECODE=1` and
`UV_LINK_MODE=copy`, and `[settings] python.uv_venv_auto = "create|source"`
creates and sources the workspace venv; a bare tool invocation can miss all of
that.

## Definition of done: `mise run check`

`[tasks.check]` in `mise.toml` depends on the gates below, and all of them must pass:

| Task | Command | What fails you |
| --- | --- | --- |
| `lint` | `uv run ruff check .` | the broad `select` list in `[tool.ruff.lint]` |
| `fmt` | `uv run ruff format --check .` | formatting; `mise run fmt:write` applies it |
| `typecheck` | `uv run ty check` | `[tool.ty.rules] all = "error"`, and `[tool.ty.terminal] error-on-warning = true` means a warning is also a failure |
| `lint:imports` | `uv run lint-imports` | the import contracts below |
| `lint:pnpm-lock` | `python3 scripts/verify_single_yaml_document.py site/pnpm-lock.yaml` | `site/pnpm-lock.yaml` holding more than one YAML document, which GitHub's dependency graph cannot read |
| `security:vex:check` | `uv run python scripts/vex_to_osv_config.py ... --check` | `osv-scanner.toml` or the `allow-ghsas:` line of `.github/workflows/dependency-review.yml` drifting from `security/atif-sql.openvex.json`, or a ledger PURL version that `uv.lock` or `site/pnpm-lock.yaml` no longer locks; `mise run security:vex` re-renders both |
| `docs:prose` | `python3 scripts/vale_gate.py` | an error-level Vale alert in `docs/**/*.md`, `site/authored/**/*.md`, `README.md`, `AGENTS.md` or `CONTRIBUTING.md`, a run that checked fewer files than that scope holds, or a glob that matched fewer files than its floor in `scripts/vale_gate.py` (`docs/**/*.md` at least 15, `site/authored/**/*.md` at least 2, each root file at least 1), or a rule whose warnings rose past its count in `.vale-baseline.json` (or that warns and is not in it); a baseline that is missing, empty, or does not parse fails as well. `.vale.ini` names the rules and why each one below error sits there, and a new term goes in `.vale/styles/config/vocabularies/atif-sql/accept.txt` |
| `test` | `uv run pytest --no-header -q` | `[tool.pytest.ini_options] testpaths = ["packages/*/tests"]` |

Most gates declare `sources`, so mise skips one whose inputs have not moved by
mtime (`lint:pnpm-lock` and `docs:prose` run every time). Run `mise run --force check` when you want them all to execute anyway.

The Vale warnings are a ratchet, not a free pass. `.vale-baseline.json` holds each rule's warning
count over the scope in the table, and `docs:prose` names any rule that rose. Reword the sentence that
added the warning; `vale <file>` lists them. When a change lowers a count, the gate passes and
prints `lower the baseline: mise run docs:prose:baseline`: run that task and commit the file with
the change. A baseline only goes down in review, so a diff that raises a count in it needs the
same scrutiny as a widened `ignore` list.

`Google.EmDash` is an error, not a ratchet: a dash with a space on each side (a line break counts as
a space) fails `docs:prose` on its own. Write a colon, comma, period, or parentheses in its place,
whichever keeps the sentence's sense; `.vale.ini` says why the rule sits at error.

## The docs site: `mise run docs:gate`

A change under `docs/` or `site/` also needs `mise run docs:install && mise run docs:gate`, which
`check` leaves out because it needs node and a site build. CI runs it in `.github/workflows/docs.yml`,
and the pre-push hook runs its link crawl when a push touches either tree.

| Task | Command | What fails you |
| --- | --- | --- |
| `docs:build` | `pnpm run build` in `site/` | a relative link the build left unresolved (`starlight-links-validator` with `errorOnRelativeLinks`), a fragment naming no heading, or a citation to a path absent at the pinned commit |
| `docs:links` | `python3 scripts/docs_links.py dist` | any internal `href` or `src` in `site/dist` (pages, head, raw `.md` twins, the `llms.txt` bundles) that GitHub Pages would answer with a 404, hidden paths included, or a fragment naming no `id`; also 0 pages, 0 internal links, or a page of the content collection with no route or twin in the build |
| `docs:gate` | `pnpm run check && pnpm run test` in `site/`, after `docs:build` and `docs:links` | `astro check` and the `vitest` probes over `site/dist` |

`mise run docs:links:live` runs the same crawl over the deployed site, from its sitemap. The
`live-links` job in `docs.yml` runs it after every deploy and every Monday, and fails on any
internal link that does not answer 200.

Never reach green by relaxing a gate. Widening an `ignore` list, adding a
blanket per-file ignore, or deleting a contract is a change to the project's
standards, not a fix to your branch. Raise it in the pull request instead.

## Commits

Commit messages must be [Conventional Commits](https://www.conventionalcommits.org):
`type(scope): subject` with `type` one of `feat|fix|chore|docs|refactor|test|perf|ci`.
This is enforced, not advisory: `lefthook.yml`'s `commit-msg` hook runs
`uv run cz check --allow-abort --commit-msg-file {1}`, configured by
`[tool.commitizen]` in
`pyproject.toml` (`name = "cz_conventional_commits"`, with
`allowed_prefixes = ["Merge", "Revert", "Pull request", "fixup!", "squash!"]`
as the only exemptions).

The other hooks in `lefthook.yml` run a subset of the gate early, so a clean
`mise run check` before you commit means the hooks have nothing to say:

- **pre-commit**, on staged `**/*.py`: `ruff check --fix` and `ruff format`
  (both `stage_fixed: true`, so fixes are re-staged for you), `ty check`, and
  `lint-imports`. Plus `uv lock --check` whenever a `pyproject.toml` or
  `uv.lock` is staged.
- **pre-push**: `pytest --no-header -q`, plus `mise run docs:links` (a site build and the
  crawl of `site/dist`) when the push touches `docs/` or `site/`.

`ty`, `lint-imports`, and the pre-push pytest are skipped during a merge or
rebase (`skip: [merge, rebase]`); `mise run check` is what covers those.

Work on a branch, and open a pull request against `main`.

## The import contracts you will trip

`[tool.importlinter]` in `pyproject.toml` declares contracts over the
root packages. These shape the whole workspace:

1. **Independence.** `atif_converter`, `atif_corpus`, `atif_duck`,
   `atif_models`, and `atif_embed` may never import each other. If you need
   two of them in one flow, compose them in `atif_cli`; it is the only
   composition root and may import them all.
2. **`atif_analytics` composes `atif_models` and nothing else of ours.** A
   `forbidden` contract pins its remaining edges shut: it may not import
   `atif_converter`, `atif_corpus`, `atif_duck`, `atif_embed`, or `atif_cli`.
   That is why `atif_embed` reads the corpus through its own DuckDB adapter
   over the documented corpus layout rather than through `atif_duck`.

The rest are `layers` contracts:
`application > infrastructure > domain` for `atif_converter`, `atif_corpus`,
`atif_embed`, and `atif_analytics`, and `infrastructure > domain` for
`atif_models`. Dependencies point inward: a `domain` module importing from
`infrastructure` fails `lint:imports`.

Other rules live in ruff rather than import-linter:

- **stdlib `logging` is banned workspace-wide.** `[tool.ruff.lint.flake8-tidy-imports.banned-api]`
  maps it to "Use loguru via `from loguru import logger`".
- **Relative imports are banned entirely** (`ban-relative-imports = "all"`).
  Import by absolute package path.

## Adding things

**A dependency.** Use `uv add --package <member> <dep>` (or
`uv add --dev <dep>` for the shared dev group in the root `pyproject.toml`) so
`pyproject.toml` and `uv.lock` move together. Commit both: `uv lock --check`
runs in the pre-commit hook and as `mise run lock:check`, and a stale lock is a
failure.

**A dependency on a sibling package.** Declare it in the member's
`[project.dependencies]` *and* add `[tool.uv.sources] <pkg> = { workspace = true }`
(both, per `AGENTS.md`). The independence contract above still applies.

**A new package.** `packages/*` is globbed as a workspace member, but these
places in the root `pyproject.toml` list packages explicitly and will not pick
it up on their own: `[tool.ruff] src`, `[tool.ruff.lint.isort]
known-first-party`, `[tool.ty.environment] root` plus `[tool.ty.src] include`,
and `[tool.importlinter] root_packages` (with its layers contract).

The new member also needs the license wiring every existing member has:
`license = "Apache-2.0"` with `license-files = ["LICENSE"]` in `[project]`, and
a `LICENSE` symlink to the repo root (`ln -s ../../LICENSE
packages/<name>/LICENSE`). `license-files` globs are resolved inside the
member's own directory and `..` is rejected, so the symlink is what puts the
license text into the wheel's `dist-info/licenses/`; Apache-2.0 §4(a) requires
it, and `uv build` fails outright if the file is missing.

**A DuckDB view or macro.** The catalog is static and drift-tested. Per
`AGENTS.md`, adding an object means adding a `DESCRIPTIONS` entry in
`atif_duck/domain/catalog.py`, an `ARG_EXEMPLARS` entry for any new parameter
name, `TABLE_MACRO_NAMES` membership if the DDL is `AS TABLE`, and a derived
example that actually executes (or a documented `EXCLUSIONS` entry). The tests
in `packages/atif-duck/tests/` fail until that is true.

## harbor and litellm are test oracles, not dependencies

Neither is installed with atif-sql. Both sit in the root `[dependency-groups] dev`,
and `packages/atif-converter/tests/test_dev_only_imports_guard.py` fails if any
member's `src/` imports either one.

**The ATIF models are vendored.** The data classes from `harbor.models.trajectories`
(RFC 0001) and `harbor.utils.trajectory_validator` live in
`atif_converter.domain.atif`, copied from harbor 0.24.0 under Apache-2.0. Each file
carries an attribution header and is otherwise upstream's file byte for byte, with
the import path rewritten; ruff and ty skip the directory so no formatter rewrites
it into ours. `UPSTREAM_VERSION` in its `__init__` records the release, and `meta.json`
stamps it as `harbor_version`. The conversion from a Claude Code session or a Codex
rollout to a `Trajectory` is ours, in `atif_converter.domain.claude_code_conversion`
and `atif_converter.domain.codex_conversion`, ported from harbor and held to parity
with harbor 0.24.0.

harbor itself is used in two places, both tests:

- `tests/test_vendored_atif.py` compares every vendored file to the installed
  harbor's source, compares the two JSON Schemas, and round-trips the goldens and
  the converter's enriched output through both sets of models and both validators,
  valid and broken inputs alike.
- `tests/harbor_oracle.py` reaches harbor's private converters as the PARITY
  ORACLE, and three things hang off it:
  - `tests/goldens/*.trajectory.json`: the oracle's output for each synthetic
    fixture, frozen. `test_harbor_oracle.py` asserts the live oracle still equals
    the frozen one, so an upstream behavior change surfaces as a named JSON-path
    diff, not as a port that mysteriously "fails parity".
  - `test_parity_claude_code.py` / `test_parity_codex.py`: our converters equal the
    oracle on the fixtures AND the goldens. The golden half never skips, so parity
    stays checkable the day harbor removes the private method.
  - `test_parity_live.py`: the newest `ATIF_PARITY_LIMIT` local sessions of each
    agent (`0` = all), converted both ways and diffed. Skips in CI; run it with
    `ATIF_PARITY_LIMIT=0` before a release, and after any harbor bump.

So a harbor bump is a lockfile edit plus one reading: run the atif-converter suite
and read its failures as upstream-behavior reports. A model change fails
`test_vendored_atif.py`, naming the file; re-vendor it (copy upstream's file under
the existing header, rewrite the import path) and move `UPSTREAM_VERSION`. A
conversion behavior change fails the golden test. Each is a decision about whether
the port follows upstream. Re-freeze the goldens only after that decision:
`ATIF_FREEZE_GOLDENS=1 uv run pytest packages/atif-converter/tests/test_harbor_oracle.py -k freeze`.

**Prices are vendored data.** `atif_converter/domain/model_prices.json` is a filtered
copy of litellm's public `model_prices_and_context_window.json` (MIT; the notice and
the source ref travel in its `meta` block), holding only the Claude and OpenAI text
models our transcripts name, plus local overrides for models litellm doesn't price
yet. Refresh it by hand, never by editing the JSON:

```bash
uv run scripts/update_prices.py --ref v1.103.2   # a litellm tag, branch, or commit
```

The script drops an override once upstream prices the same key and says so; delete
that override from the script then. Move the dev `litellm` pin to the same release
when you can, because `tests/test_pricing_identity.py` compares arithmetic only over
entries the installed litellm shares with the table and fails when too few do. Every
changed rate changes `total_cost_usd` for that model's sessions on their next
materialize, so read the diff as a pricing change.

## Never spend money in a test

These paths call Amazon Bedrock and bill the caller: `atif-sql analyze` (the LLM
pipelines) and `atif-sql embed` (Cohere Embed v4). Each is guarded, and the
guards are part of the contract:

- `analyze` is dry-run by default; `--no-dry-run` is what makes it spend.
- `embed` refuses a real run with no scope: no `--limit` and no `--all` exits 64.

The test suite must stay offline: fake the port, never the credential. If a
change makes a Bedrock call reachable from `pytest`, that is a defect in the
change.

## Experiments

`experiments/` holds numbered protocols with a README (and, once run, a REPORT)
per experiment. Nothing there is wired into a gate: pytest's `testpaths` only
collects `packages/*/tests`, and outputs are written to `experiments/**/out/`,
which `.gitignore` excludes. An experiment's recorded numbers are measurements;
if you re-run one, add your measurement rather than editing the one already recorded.
