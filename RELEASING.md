# Releasing

`atif-sql` ships **one thing**: a fully loaded CLI a user installs with one command.

```bash
uv tool install atif-sql     # or: uvx atif-sql schema
```

Versions are cut by [commitizen][cz] from Conventional Commit subjects, and published to PyPI
with [Trusted Publishing][tp] — OIDC, with no API token in any secret and no long-lived
credential to rotate or leak.

[cz]: https://commitizen-tools.github.io/commitizen/
[tp]: https://docs.pypi.org/trusted-publishers/

## One version for the repository

The seven directories under `packages/` are **internal organisation**, not seven products.
They exist so `import-linter` can enforce layer boundaries (`application > infrastructure >
domain`) and the independence contract that keeps `atif_converter`, `atif_corpus`,
`atif_duck`, `atif_models`, and `atif_embed` from importing each other. Nobody is expected to
depend on one of them, and nothing in this repository supports doing so.

So there is one version number, and every member moves with it. There is no per-package
changelog and no independent version history, because there is no independently consumed
package to have one. `[tool.commitizen] version` in the root `pyproject.toml` is the single
source of truth, and its `version_files` list rewrites the published `[project] version`, the
seven member manifests, and the dev pins between them from that one value.

## The release tool is commitizen, not release-please

commitizen already gates every commit message — `lefthook.yml`'s `commit-msg` hook runs
`cz check`, configured by `[tool.commitizen]`. Deriving the version bump from the same
implementation that accepted the commit is the whole argument:

- **One Conventional Commits parser, not two.** A second tool means a commit that `cz check`
  accepts and the release tool classifies differently, which surfaces as a version that is
  quietly wrong rather than as a failure.
- **The bump is reproducible on a laptop.** `uv run cz bump --dry-run` computes exactly what
  CI computes, from the locked commitizen in `uv.lock`. A GitHub Action's release logic cannot
  be run locally at all, so its answer can only be observed after it has been committed.
- **`version_files` rewrites arbitrary text in arbitrary files**, which is what a version living
  in ten places needs: one bump moves the published `[project] version`, `version = "0.2.0"` in
  seven member manifests, and `"atif-duck==0.2.0"` in the two that carry dev pins. Keeping those
  in step by hand is the failure this repository is most likely to ship.
- **No new dependency.** commitizen is already in `[dependency-groups] dev`.

What release-please would add is a bot-authored release PR as a review surface. That surface
is weaker than it looks: GitHub starts no workflow run from an event raised by the default
`GITHUB_TOKEN`, so the release PR would run **no CI**, and the review would be of a diff no
gate had seen. `release.yml` gets the same review value by running `mise run check` on the tree
it is about to tag, and the human decision points live where they matter — dispatching the
workflow, publishing the draft release, and approving the `pypi` environment.

## What actually gets published: one distribution

**One distribution goes to PyPI.** `atif-sql` is a single wheel carrying all seven module
trees — `atif_cli`, `atif_analytics`, `atif_converter`, `atif_corpus`, `atif_duck`,
`atif_embed`, `atif_models` — so `uvx atif-sql` claims one name, needs one Trusted Publisher,
and resolves no sibling from an index. The packages under `packages/*` stay workspace members
for development: that is what keeps the layer and independence contracts enforceable and each
package's tests where they belong. Only the publishing shape is singular.

The published requirement list is the **union** of every member's third-party requirements,
declared once in the root `[project.dependencies]`. Nothing derives it at build time, so
`packages/atif-cli/tests/test_distribution.py` asserts it: the union equals the declaration
exactly, no `atif-*` requirement reaches the metadata, every module tree appears in
`[tool.hatch.build.targets.wheel] packages` with a `py.typed` beside it, and no two members
declare the same package under different constraints. Each of those five assertions was
verified to fail against the defect it names.

### Why hatchling and not uv_build

uv_build resolves every module under a single `module-root` (default `src`), and these seven
live under `packages/*/src/`. Probed 2026-08-28 against `uv_build>=0.11.14,<0.13`, driven
through `uv build --force-pep517`:

| Attempt | Result |
| --- | --- |
| `module-name = ["mod_a", "mod_b"]` with both under one `module-root` | Works — one wheel, both modules at top level. The list form is real. |
| `module-root = "packages"`, `module-name = ["atif_cli", "atif_duck"]` against the real `packages/<dist>/src/<module>` layout | `IO error for operation on .../packages/atif_cli: No such file or directory`. Every entry resolves as `<module-root>/<name>` and `module-root` is a single directory, so seven separate `src` roots cannot be reached. |
| `module-name = ["packages.atif-cli.src.atif_cli"]` (the documented dotted form) | Builds, and the wheel is broken: the module lands at `site-packages/packages/atif-cli/src/atif_cli/`, so `import atif_cli` fails. It fails silently, which is worse than the error above. |
| `[tool.uv.build-backend.data] purelib = "packages/atif-duck/src"` | Gathers **one** sibling tree into `<dist>.data/purelib/`, which installers unpack into site-packages. A list is rejected: `invalid type: sequence, expected path string`. One directory cannot cover six. |

hatchling's `[tool.hatch.build.targets.wheel] packages` takes a path per module, so one wheel
spans all seven trees with **no source movement** — which is what keeps `[tool.ruff] src`,
`[tool.ty]`, `[tool.pyright]`, `[tool.coverage.run]`, `[tool.bandit]`, the import-linter
contracts, and every doc citation valid. Verified 2026-08-28: the wheel carries 100 `.py`
files, all seven top-level modules, the `atif-sql` console script, and 19 `Requires-Dist`
entries with no `atif-*` among them.

## What the publish preflight asserts

`publish.yml` checks four properties before it uploads anything, and fails with a pointer to
this file when one is missing. Each is a defect `twine check` passes over — the metadata stays
structurally valid and the damage is semantic — and each becomes permanent the moment a first
release goes out under the wrong name or an unpinned requirement.

**1. The distribution name is the command name.** The console script is `atif-sql`, so the
distribution is `atif-sql`: `uv tool install <name>` has to install a tool whose command is
`<name>`, and a mismatch also puts the tool directory under the wrong name
(`~/.local/share/uv/tools/<distribution>`). The directory and the module stay `atif_cli`. A
distribution name and a module name are independent by design, and renaming the module would
touch `[tool.ruff] src`, `[tool.ty]`, `[tool.pyright]`, `[tool.coverage.run]`, `[tool.bandit]`,
and the import-linter contracts for no gain.

**2. The wheel carries every module, and requires no sibling.** Seven module trees reach PyPI
inside one file, so the two ways that goes wrong are a tree missing from
`[tool.hatch.build.targets.wheel] packages` and a requirement on an unpublished sibling. Both
install cleanly and fail on the user's machine:

- **A missing tree is invisible to every other gate.** Development puts all seven modules on
  `sys.path` whether or not the wheel would carry them, so tests, type checkers, and
  import-linter all pass on a manifest that would ship six. The preflight reads the archive
  itself and asserts each of the seven top-level modules is inside it, with its `py.typed`
  beside it — a marker whose absence hides a package's annotations from consumers under PEP 561,
  which is how `atif_embed` shipped `Typing :: Typed` and no marker.
- **A sibling requirement resolves from a namespace this project does not own.** `[tool.uv.sources]
  <pkg> = { workspace = true }` is a **local** source no PyPI installer sees, so a member listed
  in `[project.dependencies]` becomes a real `Requires-Dist` naming a project that was never
  published. Measured against real PyPI on 2026-08-28, that gave: `Because atif-analytics was not
  found in the package registry and atif-cli==0.1.0 depends on atif-analytics, we can conclude
  that atif-cli==0.1.0 cannot be used.` After a first publish it would be worse than an error —
  a bare name binds to whatever the newest release under that name happens to be, owned by
  whoever registered it.

`packages/atif-cli/tests/test_distribution.py` pins the manifest side of both in `mise run
check`; the preflight pins the artifact side, because a manifest that is right and a wheel that
is wrong are different failures.

**3. `[tool.commitizen]` carries the version wiring.** commitizen's `version_provider` defaults
to reading `version` out of `[tool.commitizen]` itself, which stays the source of truth; the
root's `[project] version` is what a wheel carries and moves through `version_files`. Both hold
one string. `release.yml` asserts the commitizen key exists, because without it `cz bump` has
nothing to read and silently has nothing to do.

**4. The CLI reports the distribution's version.** cyclopts resolves a default `--version` by
looking the calling module's name up in the installed metadata. The module is `atif_cli` and the
distribution is `atif-sql`, so `importlib.metadata.version("atif_cli")` raises
`PackageNotFoundError` and cyclopts answers **`0.0.0`**. `atif_cli.app` therefore names the
distribution explicitly, through a callable so `importlib.metadata` stays off the import path
until `--version` is actually asked for (which the lean-import test pins). `publish.yml`
compares the installed CLI's `--version` against the tag, so a regression here cannot reach PyPI
quietly.

The configuration implementing all four lives in `packages/atif-cli/pyproject.toml`,
`packages/atif-analytics/pyproject.toml`, root `pyproject.toml`, and
`packages/atif-cli/src/atif_cli/app.py`, each with the reasoning in a comment beside the line it
governs. Two properties of `[tool.commitizen]` are worth knowing before editing it:

- `pre_bump_hooks = ["uv lock", "git add uv.lock"]`. `uv.lock` records every member's version,
  so rewriting the seven manifests leaves the lockfile describing the previous release and
  `mise run lock:check` fails on the release commit itself. These hooks run after the version
  files are rewritten and before the commit, the only window where the re-resolved lockfile can
  land in the same commit as the versions it resolves. `git add` is separate because commitizen
  stages only the files it rewrote.
- `version_files` entries are per-file rather than globbed, because `--check-consistency`
  requires a hit in every matched file and five of the seven members carry no intra-workspace
  pin.

Verify the wiring without committing anything — `--version-files-only` rewrites the files and
stops, so `git diff` shows exactly which lines the next real bump moves. Measured 2026-08-28:
15 changed lines across 8 files — the `[tool.commitizen] version` key, the published
`[project] version`, seven member `version =` lines, and the six dev pins.

`--version-files-only` does more than rewrite those files, and undoing it needs all three steps.
Measured 2026-08-28: `pre_bump_hooks` re-resolves `uv.lock` at the new version and **stages** it,
and `update_changelog_on_bump` writes `CHANGELOG.md`. So commit first — a dirty tree here is
work a restore will take with it — and clean up deliberately rather than with `git restore -p`,
which is interactive and silently does nothing if you answer `n`.

```bash
git status --short                      # must be empty; commit anything here FIRST
uv run cz bump --version-files-only --check-consistency --yes
git diff --stat                          # expect 15 lines across 8 files

git checkout -- .                        # the version rewrites
git restore --staged --worktree uv.lock  # the staged re-lock
rm -f CHANGELOG.md                       # written even in this mode
git status --short                       # must be empty again
```

### The lockfile moves with the distribution name

`uv.lock` records the distribution name, so renaming one invalidates it and `uv lock --check` —
which `check.yml` runs before the gate — fails until it is re-resolved. A rename and its `uv
lock` must land in ONE commit, or CI fails on a lockfile instead of on the rename.

## Install weight is a property of the release

One install carries every capability. There are no capability-gating extras, no
`atif-sql[embed]`, and nothing to install afterwards to make a command work — so the weight
below is what every user pays, and it belongs in the release record rather than in a
surprise. Measured 2026-08-28 against `uv.lock`, CPython 3.13, linux x86_64:

| | |
| --- | --- |
| Third-party runtime packages | **113** (linux and macOS). 115 counting `colorama` and `win32-setctime`, which are gated `sys_platform == 'win32'`; 120 including the seven members. |
| Wheels downloaded | **381 MiB** |
| Installed on disk | **1.15 GiB** (1.29 GiB once bytecode is compiled) |
| Heaviest five | `polars-runtime-32` 206 MiB, `llvmlite` 171, `lancedb` 155, `pyarrow` 150, `scipy` 107 — 67% of the install |

**Prebuilt wheel coverage is complete except for one combination.** 31 of the closure ship
native code; all 31 have cp313 wheels for manylinux x86_64, macOS arm64, and Windows x86_64.
The gap is `hdbscan==0.8.44` on **manylinux aarch64**: no hdbscan release has ever published
an aarch64 wheel, so there is no version to move to, and its sdist compiles five Cython
extensions. A first `uvx atif-sql` on Graviton, an ARM CI runner, or a `linux/arm64` container
needs a C toolchain and pays that build. macOS arm64 is unaffected — hdbscan's
`macosx_*_universal2` wheels cover it.

Alpine and other musl targets are worse and are not supported: `duckdb`, `hdbscan`,
`lancedb`, `llvmlite`, `numba`, and `scikit-learn` publish no musllinux wheels, and
`lancedb==0.37.1` publishes **no sdist at all**, so there is nothing to build from.

**The harbor subtree is the standing follow-up.** 63 of the 113 runtime packages reach this
project only through `harbor` — `fastapi`, `uvicorn`, `starlette`, the whole `supabase` client
stack, `litellm`, `openai`, `tiktoken`, `tokenizers`, `huggingface-hub`, `cryptography`,
`aiohttp` — while `atif-converter` uses harbor for exactly one private method,
`ClaudeCode._convert_events_to_trajectory`. That is 57% of the dependency roster and 13% of
the bytes (155 MiB): a CLI that converts JSONL ships a web server and a database client to do
it. It is a supply-chain and install-weight question, not a release blocker, and it does not
change the shape of a release.

## The normal path

1. Land Conventional Commits on `main`. `feat:` moves the minor, `fix:` the patch, and a `!`
   or a `BREAKING CHANGE:` footer moves the minor too while `major_version_zero` holds.
2. **Dry run.** `release` is `workflow_dispatch`-only and `dry_run` defaults to true, so the
   first press is always free:

   ```bash
   gh workflow run release.yml
   ```

   It runs `mise run check`, computes the next version, and writes the rendered changelog
   section to the job summary. Nothing is committed, tagged, or pushed.
3. **Cut it.** Dispatch again with `dry_run: false`. The job runs the gate again, then
   `cz bump --yes --changelog --check-consistency --annotated-tag`, pushes the bump commit and
   the annotated tag, and opens a **draft** GitHub release whose notes are the changelog
   section `cz` just wrote.

   ```bash
   gh workflow run release.yml -f dry_run=false
   ```
4. **Review the draft and press "Publish release".** That press is what starts `publish.yml` —
   see below for why it has to be a human and not the workflow.
5. **Approve the `pypi` environment.** `publish.yml` builds the sdist and the wheel, verifies
   them, installs them, and then waits. A reviewer approves once and all seven upload legs
   proceed.

### Why the release is a draft

GitHub starts no workflow run from an event raised by the default `GITHUB_TOKEN`. A release
that `release.yml` created *and published* would raise `release: published` into a void, and
`publish.yml` would never fire. The usual workaround is a Personal Access Token or a GitHub
App — a long-lived credential created to defeat a safety mechanism.

Leaving the release as a draft turns the constraint into the design. The human who reviews the
notes and presses Publish is the actor whose event starts the publish, and no token has to
exist. It also separates two decisions that deserve separating: "these are the release notes"
is reversible; "these bytes are on PyPI" is not, because a version can be yanked but never
replaced or reused.

## First publish: one pending publisher, no manual upload

Unlike npm, PyPI can bootstrap a project that does not exist. A **pending publisher** is
registered before the project, and the first successful publish creates the project and
converts the pending publisher into a normal one with nothing further to configure.

At <https://pypi.org/manage/account/publishing/> — the **account** page, not a project page,
because the project does not exist yet — add one GitHub pending publisher:

| Field | Value |
| --- | --- |
| PyPI project name | `atif-sql` |
| Owner | `theagenticguy` |
| Repository name | `atif-sql` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

One, because one distribution. The six module boundaries under `packages/*` are not published
and need no registration.

Two properties of that form decide whether the first release works:

- **The environment name is optional to PyPI and mandatory here.** It is the claim that ties a
  publish to the reviewer-gated environment. Omitted, any workflow run in this repository
  could publish — and PyPI emails the project owners to point that out.
- **A pending publisher does not reserve the name.** It stays valid only until somebody else
  registers that project, at which point it is silently invalidated. `atif-sql` was unclaimed on
  PyPI and TestPyPI when probed on 2026-08-28.

`publish.yml` has a single publish job, and the count matters for a reason worth keeping: PyPI
mints one macaroon per OIDC exchange, carrying a `ProjectID` caveat listing the projects
attached to the matched publisher, and warehouse reifies at most **one** pending publisher per
exchange (read 2026-08-28 in `warehouse/oidc/views.py`). A repository publishing N projects
therefore needs N exchanges on its first release. N is 1 here.

Until the pending publisher exists, `publish.yml` fails with a 403 and a PyPI response naming
the publisher it could not find. That pair on a first release means "the bootstrap has not
happened yet", not "the workflow is wrong". The same pair afterwards means the publisher does
not match — check the workflow filename, the environment name, and the repository owner's
case.

## No TestPyPI rehearsal

A TestPyPI pass would double the trusted-publisher surface, for a
rehearsal that cannot rehearse the thing that matters.

- What it would catch — invalid metadata, a malformed sdist or wheel — `publish.yml` already
  catches offline and deterministically, with `twine check` plus an actual
  `uv tool install --find-links dist atif-sql` followed by running the installed command.
- What it cannot catch is a resolution against PyPI's index, because TestPyPI's index holds
  different packages. An install test there resolves a different graph than a user's.
- The remaining failure a rehearsal would find is a misconfigured publisher — and TestPyPI
  would only prove that *TestPyPI's* publisher is configured.

Against that, a second set of registrations is a standing chance that a drifted TestPyPI
publisher fails a real release for a reason unrelated to the release. The argument for adding
one would be a change that makes the artifact's shape uncertain — a build-backend switch, or
the fat-wheel restructure above. Neither is on the table.

## Repository settings this depends on

| Setting | Where | Why |
| --- | --- | --- |
| Environment `pypi` with **required reviewers** | Settings → Environments | The gate on the irreversible step. Without reviewers the environment exists, the OIDC claim matches, and the gate is decorative. Its name must match the `Environment name` on the trusted publisher. |
| **Private vulnerability reporting** enabled | Settings → Advanced Security | `SECURITY.md` and `.github/ISSUE_TEMPLATE/config.yml` both route reports to `/security/advisories/new`, which 404s while this is off. |
| **Require review from Code Owners** on `main` | Branch protection or ruleset | `.github/CODEOWNERS` only *requests* review until this is on; with it off the file is a notification list. |
| `default_workflow_permissions` = `read` | Settings → Actions | Every workflow here declares its own per-job permissions, so the default needs no write. |
| A push path to `main` for the release workflow | Branch protection or ruleset | See below. |
| Pages **Source = GitHub Actions** | Settings → Pages | `docs.yml` deploys through `actions/deploy-pages`, which needs the Actions source rather than a branch. `base: "/atif-sql/"` in `site/astro.config.ts` matches the resulting project-site path. |
| The AI-crawler policy, at the ORIGIN root | The `theagenticguy.github.io` user-pages repository | `robots.txt` is per-origin (RFC 9309 §2.3), so a project site served from a path segment cannot own one: Astro emits this site's copy to `/atif-sql/robots.txt`, which no crawler fetches. `site/public/robots.txt` is the decided policy and the exact text to install at `https://theagenticguy.github.io/robots.txt`. Until it is installed there, these pages inherit whatever that origin already serves — and absent a file, that is fully permissive. Outside this repository's reach, so no gate here can assert it. |

### The release push and branch protection

`release.yml` pushes the bump commit and the tag with `GITHUB_TOKEN`, which acts as
`github-actions[bot]`. A ruleset on `main` that requires a pull request refuses that push with
`GH006: Protected branch update failed`, and **a ruleset bypass list cannot name it** — the
eligible bypass actors are repository/organisation/enterprise admins, the maintain and write
roles or custom roles based on write, teams, GitHub Apps, and Dependabot. `github-actions[bot]`
is none of those.

Two supported resolutions:

1. **Grant the push.** Keep the pull-request requirement off `main`, or add a bypass actor the
   token can act as. The repository is single-maintainer and every change still goes through a
   pull request by convention; the gate that matters is `check.yml`.
2. **Cut the version locally.** The tool is the same one CI runs, from the same lockfile, so
   the outcome is identical:

   ```bash
   git switch main && git pull --ff-only
   mise run check
   uv run cz bump --changelog --check-consistency --annotated-tag
   git push --follow-tags
   gh release create "v$(uv run cz version --project)" --draft --verify-tag \
     --title "v$(uv run cz version --project)" \
     --notes-file <(uv run cz changelog --dry-run "$(uv run cz version --project)")
   ```

   From there the flow is identical: review the draft, press Publish, approve `pypi`.

## Cutting a specific version

`release.yml` takes an `increment` input (`PATCH`, `MINOR`, `MAJOR`) that overrides what
commitizen derives from the commit subjects. Prefer it to a `Release-As:` trailer: the input is
recorded on the workflow run, while a trailer has to survive a squash-merge to be read at all.

```bash
gh workflow run release.yml -f dry_run=false -f increment=MINOR
```

## Retrying a publish

A publish fails for reasons that have nothing to do with the bytes — a pending publisher that
was never registered, a PyPI outage, a runner without a tool. Retry against the existing tag
rather than spending another version:

```bash
gh workflow run publish.yml -f tag=v0.2.0
```

The publish steps set `skip-existing: true`, so a re-run after a partial upload completes the
legs that failed instead of taking a 400 on the ones that succeeded. PyPI refuses to overwrite
an existing filename either way, so this cannot replace bytes.

## Failure signatures

| Symptom | Cause |
| --- | --- |
| `[tool.commitizen] in pyproject.toml carries no 'version' key` | The `version` key was removed from `[tool.commitizen]`. Property 3 above. |
| `no wheel built for: atif-sql` / `unexpected distribution(s) built: atif-cli` | `packages/atif-cli/pyproject.toml` declares a `[project] name` other than `atif-sql`. Property 1 above. |
| `intra-workspace requirement 'atif-duck' is not pinned to ==<version>` | A sibling dependency lost its `==` pin. Property 2 above. |
| `Version is '0.1.0', tag says '0.2.0'` | The tag and the manifests disagree — a `cz bump` that partially applied, or a hand-moved tag. |
| `the installed CLI reports version '0.0.0'` | `cyclopts.App` has no explicit `version=`, so it looked up the module name instead of the distribution name. |
| `EOFError` from `cz bump`, no other output | `--get-next` without `--yes` opens a prompt and dies on a non-TTY. |
| `mise run lock:check` fails on the release commit | `pre_bump_hooks` is missing from `[tool.commitizen]`, so `uv.lock` still records the previous versions. |
| `commitizen found no version-bumping commit since the last tag` | Nothing to release. Land a `feat:`/`fix:`, or dispatch with an explicit `increment`. |
| `CurrentVersionNotFoundError: Current version ... is not found in <path>` | A `version_files` entry points at a file whose version was hand-edited. `--check-consistency` is doing its job. |
| `GH006: Protected branch update failed` | See "The release push and branch protection". |
| 403 from PyPI naming a publisher it cannot find | On a first release, the pending publisher does not exist yet. Afterwards, the publisher does not match the claims. |
| `expected one sdist and one wheel for <project>, staged 0` | `uv build --all-packages` did not produce that project — usually a member removed from `[tool.uv.workspace] members`. |
