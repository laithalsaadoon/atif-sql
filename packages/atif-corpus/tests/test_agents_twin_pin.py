# SPDX-License-Identifier: Apache-2.0

"""Drift pin for the deliberate ``AgentSource`` twins.

``atif_corpus.domain.agents`` and ``atif_converter.domain.agents`` carry the
same enum, and the independence contract forbids either package from importing
the other. So the atif-converter copy is read as SOURCE TEXT (AST, never
imported — importing it would drag harbor into atif-corpus's test run) and its
members are compared against the copy this package actually uses.

The values are wire contract three times over: the ``--agent`` flag's accepted
spellings, harbor's ``Trajectory.agent.name``, and the ``agent`` key in
``meta.json``. A twin that drifts would make one of those three disagree with
the other two, which is exactly the failure this test exists to prevent.
"""

from __future__ import annotations

import ast
from pathlib import Path

from atif_corpus.domain.agents import DEFAULT_AGENT, AgentSource

#: packages/atif-corpus/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_CONVERTER_AGENTS = (
    _PACKAGES_DIR / "atif-converter" / "src" / "atif_converter" / "domain" / "agents.py"
)


def _enum_members(source: str, class_name: str) -> dict[str, str]:
    """``{member name: str value}`` for one enum class, parsed not imported."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members: dict[str, str] = {}
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                target = statement.targets[0]
                value = statement.value
                if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                    assert isinstance(value.value, str), f"{target.id} is not a string member"
                    members[target.id] = value.value
            return members
    msg = f"class {class_name} not found in the twin source"
    raise AssertionError(msg)


def _module_default(source: str, name: str) -> str:
    """The member name a module-level ``AgentSource`` constant points at."""
    for node in ast.parse(source).body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if name not in target_names:
            continue
        assert isinstance(value, ast.Attribute), f"{name} must be an AgentSource member reference"
        return value.attr
    msg = f"constant {name} not found in the twin source"
    raise AssertionError(msg)


def test_twin_source_is_present() -> None:
    assert _CONVERTER_AGENTS.is_file(), f"twin module missing at {_CONVERTER_AGENTS}"


def test_members_and_values_match_exactly() -> None:
    twin = _enum_members(_CONVERTER_AGENTS.read_text(encoding="utf-8"), "AgentSource")
    mine = {member.name: member.value for member in AgentSource}
    assert twin == mine


def test_default_agent_matches() -> None:
    twin_default = _module_default(_CONVERTER_AGENTS.read_text(encoding="utf-8"), "DEFAULT_AGENT")
    assert twin_default == DEFAULT_AGENT.name
