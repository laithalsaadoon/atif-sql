# SPDX-License-Identifier: Apache-2.0

"""The published distribution is ONE wheel, and its metadata cannot drift.

atif-sql ships as a single distribution carrying all seven module trees, so the
root manifest's `[project.dependencies]` is the ONLY thing an installer sees. It
cannot be derived at build time — hatchling reads it verbatim — which makes it a
hand-maintained list, and a hand-maintained list with no test is the drift
surface that kept this repository on seven distributions instead.

These tests are that test. Each one pins a property whose failure mode is a
broken install rather than a broken build:

* a member that adds a third-party dependency the root does not declare ships a
  wheel that imports something it never required — an ImportError at run time,
  on the user's machine, for a package that resolved cleanly;
* an `atif-*` requirement reaching the root means an installer tries to fetch a
  sibling from an index where, by design, nothing was published;
* a module tree missing from the wheel's `packages` list is absent from the
  wheel entirely, and every gate here still passes because the source is on
  `sys.path` during development.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]

#: The one published distribution name.
DISTRIBUTION = "atif-sql"


# `Any` rather than `object`: a decoded TOML document is genuinely dynamic, and
# `object` would only move every cast one call inward without adding a guarantee.
def _manifest(path: Path) -> dict[str, Any]:
    """One parsed `pyproject.toml`."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _requirement_name(spec: str) -> str:
    """The bare package name from a PEP 508 requirement string."""
    return str(re.split(r"[><=!~\[;]", spec, maxsplit=1)[0]).strip()


def _member_manifests() -> dict[str, dict[str, Any]]:
    """Every member manifest, keyed by its directory name under `packages/`."""
    paths = sorted((ROOT / "packages").glob("*/pyproject.toml"))
    assert paths, f"no member manifests under {ROOT / 'packages'}"
    return {p.parent.name: _manifest(p) for p in paths}


def _root_project() -> dict[str, Any]:
    """The root manifest's `[project]` table — the published metadata."""
    project: Any = _manifest(ROOT / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    return project


def _third_party_union() -> dict[str, set[str]]:
    """Every non-`atif-*` requirement any member declares, keyed by package name."""
    union: dict[str, set[str]] = {}
    for manifest in _member_manifests().values():
        project = manifest["project"]
        assert isinstance(project, dict)
        deps = project.get("dependencies", [])
        assert isinstance(deps, list)
        for dep in deps:
            name = _requirement_name(str(dep))
            if not name.startswith("atif-"):
                union.setdefault(name, set()).add(str(dep))
    return union


def test_members_agree_on_every_shared_constraint() -> None:
    """Two members must not declare the same package with different constraints.

    A single distribution declares ONE requirement per package, so a
    disagreement forces a silent choice — and the looser floor is a resolution
    nothing in the workspace has ever tested.
    """
    conflicts = {
        name: sorted(specs) for name, specs in _third_party_union().items() if len(specs) > 1
    }
    assert not conflicts, f"members disagree on: {conflicts}"


def test_root_declares_exactly_the_members_union() -> None:
    """The published requirement list equals what the members actually need."""
    declared = {_requirement_name(str(d)): str(d) for d in _root_project()["dependencies"]}
    needed = {name: next(iter(specs)) for name, specs in _third_party_union().items()}

    missing = {n: s for n, s in needed.items() if n not in declared}
    assert not missing, f"a member needs these and the wheel would not require them: {missing}"

    extra = {n: s for n, s in declared.items() if n not in needed}
    assert not extra, f"the wheel requires these and no member declares them: {extra}"

    differing = {n: (declared[n], needed[n]) for n in needed if declared[n] != needed[n]}
    assert not differing, f"constraint differs between root and member: {differing}"


def test_root_requires_no_sibling_distribution() -> None:
    """Nothing `atif-*` may reach the wheel's metadata.

    The six sibling names are unpublished on purpose. A requirement on one is an
    install that fails outright before a first publish, and afterwards resolves
    whatever the global namespace happens to hold under that name.
    """
    siblings = [
        str(d)
        for d in _root_project()["dependencies"]
        if _requirement_name(str(d)).startswith("atif-")
    ]
    assert not siblings, f"the published distribution must not require a sibling: {siblings}"


def test_every_member_module_is_in_the_wheel() -> None:
    """The wheel's `packages` list covers all seven trees, and names them correctly.

    Development puts every module on `sys.path` regardless, so an omission here
    is invisible to every other gate and shows up only as a missing module in an
    installed wheel.
    """
    hatch = _manifest(ROOT / "pyproject.toml")["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert isinstance(hatch, dict)
    listed = {str(p) for p in hatch["packages"]}

    expected: set[str] = set()
    for member, manifest in _member_manifests().items():
        module = str(manifest["project"]["name"]).replace("-", "_")
        # atif-cli is the one member whose distribution name and module name
        # differ: the published name `atif-sql` lives at the root.
        if member == "atif-cli":
            module = "atif_cli"
        expected.add(f"packages/{member}/src/{module}")

    assert listed == expected, (
        f"wheel packages != member modules\n  only in wheel: {listed - expected}\n  only in members: {expected - listed}"
    )

    for entry in sorted(listed):
        assert (ROOT / entry).is_dir(), f"{entry} is listed in the wheel but is not a directory"
        assert (ROOT / entry / "py.typed").is_file(), (
            f"{entry} ships no py.typed, so PEP 561 hides its annotations from consumers"
        )


def test_the_console_script_is_the_distribution_name() -> None:
    """`uv tool install <name>` has to install a tool whose command is `<name>`."""
    project = _root_project()
    assert project["name"] == DISTRIBUTION
    scripts = project["scripts"]
    assert isinstance(scripts, dict)
    assert list(scripts) == [DISTRIBUTION], (
        f"expected exactly the {DISTRIBUTION} script, got {list(scripts)}"
    )
    assert scripts[DISTRIBUTION] == "atif_cli.app:main"


def test_no_member_claims_the_published_name() -> None:
    """Two projects named `atif-sql` in one workspace is a build that picks one."""
    claimants = [
        m for m, mf in _member_manifests().items() if mf["project"]["name"] == DISTRIBUTION
    ]
    assert not claimants, f"{DISTRIBUTION} is the root's name; also claimed by {claimants}"
