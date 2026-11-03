# SPDX-License-Identifier: Apache-2.0

"""Nothing under ``packages/`` imports ``lancedb``, and nothing installs it.

atif-embed writes its Lance store through DuckDB's lance extension, the same
extension atif-duck reads it with, so the store has one engine. ``lancedb``
was a second engine for the same files plus three packages in every install
(``lancedb``, ``lance-namespace``, ``lance-namespace-urllib3-client``). These
tests keep it from coming back by accident: a new import fails the first, a
new requirement or lock entry fails the second.

The scan reads the source as an AST rather than grepping, so a docstring or
comment that names the package (the history in ``lance_store``) is fine, and
an import that hides behind ``importlib.import_module("lancedb")`` is not.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]

#: The package that must stay out.
BANNED = "lancedb"

#: Fewer files than this means the scan looked in the wrong place, not that
#: the tree is clean. The tree held 275 `.py` files under packages/ on
#: 2026-11-03.
_MIN_SCANNED = 200

#: Calls that import a module named by their first argument.
_DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _is_banned(module: str | None) -> bool:
    return module is not None and module.split(".")[0] == BANNED


def _dynamic_target(node: ast.Call) -> str | None:
    """The module a ``importlib.import_module("x")`` / ``__import__("x")`` call names."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in _DYNAMIC_IMPORTS or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def banned_imports(path: Path) -> list[str]:
    """Every import of :data:`BANNED` in ``path``, as ``"<path>:<line>: <statement>"``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names if _is_banned(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""] if _is_banned(node.module) else []
        elif isinstance(node, ast.Call):
            target = _dynamic_target(node)
            names = [target] if _is_banned(target) else []
        else:
            continue
        found.extend(f"{path.relative_to(ROOT)}:{node.lineno}: imports {name}" for name in names)
    return found


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "packages").rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    )


def test_nothing_under_packages_imports_lancedb() -> None:
    files = _python_files()
    assert len(files) >= _MIN_SCANNED, f"scanned only {len(files)} files under packages/"
    found = [hit for path in files for hit in banned_imports(path)]
    assert found == [], (
        "lancedb is not a dependency: write the Lance store through DuckDB's lance "
        "extension (atif_embed.infrastructure.lance_store) instead.\n" + "\n".join(found)
    )


def _requirement_name(spec: str) -> str:
    """The bare, normalized package name from a PEP 508 requirement string."""
    name = re.split(r"[><=!~\[;@ ]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_no_manifest_requires_lancedb_and_the_lock_holds_none_of_it() -> None:
    manifests = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]
    requiring: list[str] = []
    for path in manifests:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        specs = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            specs.extend(extra)
        requiring.extend(
            f"{path.relative_to(ROOT)}: {spec}"
            for spec in specs
            if _requirement_name(spec) == BANNED
        )
    assert requiring == []

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {str(package["name"]) for package in lock["package"]}
    assert "duckdb" in locked  # the lock parsed, and the store's real engine is in it
    assert sorted(name for name in locked if name == BANNED or name.startswith("lance-")) == []
