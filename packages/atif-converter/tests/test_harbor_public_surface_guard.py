# SPDX-License-Identifier: Apache-2.0

"""Production code may import harbor's PUBLIC surface and nothing else.

The port away from ``ClaudeCode._convert_events_to_trajectory`` and
``Codex._convert_events_to_trajectory`` is only durable if nothing under
``src/`` can quietly reach back into ``harbor.agents``. import-linter cannot
express "this external package, these two modules only" (it squashes an
external package to its top level), so the boundary is pinned here the way the
twin enums are: by reading the source tree's imports with ``ast``.

The allowlist IS the contract: the ATIF data classes and the validator, the
two things harbor documents (docs/agents/trajectory-format, RFC 0001). Adding
a third module here is a decision about the dependency, so make it in a
review, not by widening a test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "atif_converter"

#: The public harbor surface atif-converter is allowed to depend on.
PUBLIC_HARBOR_MODULES: frozenset[str] = frozenset(
    {
        "harbor.models.trajectories",
        "harbor.utils.trajectory_validator",
    }
)


def _harbor_imports(path: Path) -> list[tuple[int, str]]:
    """Every ``harbor...`` module a source file imports, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "harbor"
        ):
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.split(".")[0] == "harbor"
            )
    return found


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_only_public_harbor_modules_are_imported(path: Path) -> None:
    offending = [
        f"{path.relative_to(SRC)}:{line} imports {module}"
        for line, module in _harbor_imports(path)
        if module not in PUBLIC_HARBOR_MODULES
    ]
    assert not offending, "\n".join(offending)


def test_the_guard_sees_imports_at_all() -> None:
    """The allowlist is meaningless if the scan finds nothing: at least the
    converters and the validator seam must import harbor."""
    importing = [p for p in _source_files() if _harbor_imports(p)]
    assert len(importing) >= 3, [str(p) for p in importing]


def test_harbor_agents_is_the_named_forbidden_surface() -> None:
    """A direct statement of what the port removed, so a reader of this test
    file learns the rule without deriving it from the allowlist."""
    for path in _source_files():
        for line, module in _harbor_imports(path):
            assert not module.startswith("harbor.agents"), f"{path}:{line} imports {module}"
