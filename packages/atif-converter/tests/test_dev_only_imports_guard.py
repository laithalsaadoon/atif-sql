# SPDX-License-Identifier: Apache-2.0

"""Production code imports nothing from harbor or litellm.

Both are DEV dependencies: harbor for the parity oracle and the conformance
test that holds the vendored ATIF models (``atif_converter.domain.atif``) to
upstream, litellm for the pricing identity test. Neither is installed with
atif-sql, so a ``src/`` import of either is an ``ImportError`` on a user's
machine that every test here would miss, because the dev environment has both.
import-linter can't express "this external package, nowhere", so the boundary
is pinned the way the twin enums are: by reading every member's ``src/`` tree
with ``ast``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2]

#: Distributions that are dev-only, so no production module may import them.
DEV_ONLY: frozenset[str] = frozenset({"harbor", "litellm"})


def _dev_only_imports(path: Path) -> list[tuple[int, str]]:
    """Every dev-only module a source file imports, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in DEV_ONLY:
                found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.split(".")[0] in DEV_ONLY
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"import_module", "find_spec"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.split(".")[0] in DEV_ONLY
        ):
            found.append((node.lineno, node.args[0].value))
    return found


def _source_files() -> list[Path]:
    return sorted(PACKAGES.glob("*/src/**/*.py"))


def test_the_scan_reads_every_member() -> None:
    """The guard is meaningless if the glob finds nothing to read."""
    members = {path.relative_to(PACKAGES).parts[0] for path in _source_files()}
    assert {"atif-converter", "atif-cli", "atif-corpus", "atif-duck"} <= members, members


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(PACKAGES)))
def test_no_dev_only_distribution_is_imported(path: Path) -> None:
    offending = [
        f"{path.relative_to(PACKAGES)}:{line} imports {module}"
        for line, module in _dev_only_imports(path)
    ]
    assert not offending, "\n".join(offending)


def test_the_scan_catches_each_import_shape(tmp_path: Path) -> None:
    """GUARD: each spelling of the forbidden import is detected, so a pass means something."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import harbor\n"
        "from harbor.models.trajectories import Trajectory\n"
        "import importlib\n"
        "importlib.import_module('litellm')\n"
        "importlib.util.find_spec('litellm')\n"
        "from atif_converter.domain.atif import Trajectory as Ours\n",
        encoding="utf-8",
    )
    assert [line for line, _ in _dev_only_imports(probe)] == [1, 2, 4, 5]
