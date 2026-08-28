# Contributing to atif-sql

atif-sql is a uv workspace whose root is also the one published distribution:
`pyproject.toml` carries both a `[project]` table (the single `atif-sql` wheel,
which bundles all seven module trees) and `[tool.uv.workspace] members =
["packages/*"]`. The seven packages under `packages/` are development members,
not install targets. Everything below is read off the
checked-in config — `mise.toml`, `pyproject.toml`, `lefthook.yml`, and
`AGENTS.md` — so if a claim here and a config file disagree, the config file
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
`harbor==0.22.0` floors at `Requires-Python >=3.12` and 3.13 is the safe pick
while harbor's dependency chain settles on 3.14.

Drive every command through `mise run <task>` rather than calling `uv`, `ruff`,
or `ty` directly. The mise `[env]` block sets `PYTHONDONTWRITEBYTECODE=1` and
`UV_LINK_MODE=copy`, and `[settings] python.uv_venv_auto = "create|source"`
creates and sources the workspace venv — a bare tool invocation can miss all of
that.

## Definition of done: `mise run check`

`[tasks.check]` in `mise.toml` depends on five gates, and all five must pass:

| Task | Command | What fails you |
| --- | --- | --- |
| `lint` | `uv run ruff check .` | the broad `select` list in `[tool.ruff.lint]` |
| `fmt` | `uv run ruff format --check .` | formatting; `mise run fmt:write` applies it |
| `typecheck` | `uv run ty check` | `[tool.ty.rules] all = "error"`, and `[tool.ty.terminal] error-on-warning = true` means a warning is also a failure |
| `lint:imports` | `uv run lint-imports` | the import contracts below |
| `test` | `uv run pytest --no-header -q` | `[tool.pytest.ini_options] testpaths = ["packages/*/tests"]` |

Each gate declares `sources`, so mise skips one whose inputs have not moved by
mtime. Run `mise run --force check` when you want them all to execute anyway.

Never reach green by relaxing a gate. Widening an `ignore` list, adding a
blanket per-file ignore, or deleting a contract is a change to the project's
standards, not a fix to your branch — raise it in the pull request instead.

## Commits

Commit messages must be [Conventional Commits](https://www.conventionalcommits.org):
`type(scope): subject` with `type` one of `feat|fix|chore|docs|refactor|test|perf|ci`.
This is enforced, not advisory — `lefthook.yml`'s `commit-msg` hook runs
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
- **pre-push**: `pytest --no-header -q`.

`ty`, `lint-imports`, and the pre-push pytest are skipped during a merge or
rebase (`skip: [merge, rebase]`); `mise run check` is what covers those.

Work on a branch, and open a pull request against `main`.

## The import contracts you will trip

`[tool.importlinter]` in `pyproject.toml` declares seven contracts over the
seven root packages. Two of them shape the whole workspace:

1. **Independence.** `atif_converter`, `atif_corpus`, `atif_duck`,
   `atif_models`, and `atif_embed` may never import each other. If you need
   two of them in one flow, compose them in `atif_cli` — it is the only
   composition root and may import them all.
2. **`atif_analytics` composes `atif_models` and nothing else of ours.** A
   `forbidden` contract pins its remaining edges shut: it may not import
   `atif_converter`, `atif_corpus`, `atif_duck`, `atif_embed`, or `atif_cli`.
   That is why `atif_embed` reads the corpus through its own DuckDB adapter
   over the documented corpus layout rather than through `atif_duck`.

The other five are `layers` contracts:
`application > infrastructure > domain` for `atif_converter`, `atif_corpus`,
`atif_embed`, and `atif_analytics`, and `infrastructure > domain` for
`atif_models`. Dependencies point inward — a `domain` module importing from
`infrastructure` fails `lint:imports`.

Two more rules live in ruff rather than import-linter:

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
— both, per `AGENTS.md`. The independence contract above still applies.

**A new package.** `packages/*` is globbed as a workspace member, but four
places in the root `pyproject.toml` list packages explicitly and will not pick
it up on their own: `[tool.ruff] src`, `[tool.ruff.lint.isort]
known-first-party`, `[tool.ty.environment] root` plus `[tool.ty.src] include`,
and `[tool.importlinter] root_packages` (with its layers contract).

The new member also needs the license wiring every existing member has:
`license = "Apache-2.0"` with `license-files = ["LICENSE"]` in `[project]`, and
a `LICENSE` symlink to the repo root (`ln -s ../../LICENSE
packages/<name>/LICENSE`). `license-files` globs are resolved inside the
member's own directory and `..` is rejected, so the symlink is what puts the
license text into the wheel's `dist-info/licenses/` — Apache-2.0 §4(a) requires
it, and `uv build` fails outright if the file is missing.

**A DuckDB view or macro.** The catalog is static and drift-tested. Per
`AGENTS.md`, adding an object means adding a `DESCRIPTIONS` entry in
`atif_duck/domain/catalog.py`, an `ARG_EXEMPLARS` entry for any new parameter
name, `TABLE_MACRO_NAMES` membership if the DDL is `AS TABLE`, and a derived
example that actually executes — or a documented `EXCLUSIONS` entry. The tests
in `packages/atif-duck/tests/` fail until that is true.

## The harbor pin is load-bearing

`packages/atif-converter/pyproject.toml` pins `harbor>=0.22.0,<0.23`.
`atif_converter.infrastructure.harbor_adapter` calls
`ClaudeCode._convert_events_to_trajectory`, a private upstream method, verified
against 0.22.0 only. The unit tests in `packages/atif-converter/tests/` pin
upstream's observed behavior and are the drift alarm for that call.

So a harbor version bump is not a lockfile edit. Widen the pin, then re-run the
atif-converter tests and read the failures as upstream-behavior reports: they
tell you what changed in the private method and in the seven fidelity gaps
typed in `atif_converter.domain.fidelity`.

## Never spend money in a test

Two paths call Amazon Bedrock and bill the caller: `atif-sql analyze` (the LLM
pipelines) and `atif-sql embed` (Cohere Embed v4). Both are guarded, and the
guards are part of the contract:

- `analyze` is dry-run by default; `--no-dry-run` is what makes it spend.
- `embed` refuses an unscoped real run — no `--limit` and no `--all` exits 64.

The test suite must stay offline: fake the port, never the credential. If a
change makes a Bedrock call reachable from `pytest`, that is a defect in the
change.

## Experiments

`experiments/` holds numbered protocols with a README (and, once run, a REPORT)
per experiment. Nothing there is wired into a gate — pytest's `testpaths` only
collects `packages/*/tests`, and outputs are written to `experiments/**/out/`,
which `.gitignore` excludes. An experiment's recorded numbers are measurements;
if you re-run one, add your measurement rather than editing someone else's.
