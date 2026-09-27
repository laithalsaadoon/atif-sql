# SPDX-License-Identifier: Apache-2.0

"""Drift pin for the deliberate authorship twin.

``atif_duck.domain.authorship`` renders the ``step_author`` SQL macro and
``atif_analytics.domain.authorship`` classifies the steps the LLM pipelines
read. The independence contract forbids atif-analytics from importing
atif-duck, so the analytics copy is read as SOURCE TEXT (AST, never imported)
and its tables compared with the ones this package renders. Same tables, same
prefix semantics: a SQL reader and a Python pipeline cannot disagree on who
wrote a step.
"""

from __future__ import annotations

import ast
from pathlib import Path

from atif_duck.domain.authorship import (
    AUTHOR_PREFIX_RULES,
    AUTHOR_STRIP_CHARS,
    AUTHOR_VALUES,
    INTERRUPT_PREFIXES,
)

#: packages/atif-duck/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_ANALYTICS_AUTHORSHIP = (
    _PACKAGES_DIR / "atif-analytics" / "src" / "atif_analytics" / "domain" / "authorship.py"
)

_PINNED = ("AUTHOR_VALUES", "AUTHOR_STRIP_CHARS", "AUTHOR_PREFIX_RULES", "INTERRUPT_PREFIXES")


def _literal_constants(source: str) -> dict[str, object]:
    """Module-level ``NAME = <literal>`` / ``NAME: T = <literal>`` bindings."""
    constants: dict[str, object] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = [t for t in node.targets if isinstance(t, ast.Name)], node.value
        else:
            continue
        try:
            literal = ast.literal_eval(value)
        except ValueError:
            continue
        for target in targets:
            constants[target.id] = literal
    return constants


def test_twin_source_is_present() -> None:
    assert _ANALYTICS_AUTHORSHIP.is_file(), f"twin module missing at {_ANALYTICS_AUTHORSHIP}"


def test_twin_defines_every_pinned_table_as_a_literal() -> None:
    twin = _literal_constants(_ANALYTICS_AUTHORSHIP.read_text(encoding="utf-8"))
    missing = [name for name in _PINNED if name not in twin]
    assert not missing, f"twin lost (or stopped writing as a literal): {missing}"


def test_tables_match_exactly() -> None:
    twin = _literal_constants(_ANALYTICS_AUTHORSHIP.read_text(encoding="utf-8"))
    assert twin["AUTHOR_VALUES"] == AUTHOR_VALUES
    assert twin["AUTHOR_STRIP_CHARS"] == AUTHOR_STRIP_CHARS
    assert twin["AUTHOR_PREFIX_RULES"] == AUTHOR_PREFIX_RULES
    assert twin["INTERRUPT_PREFIXES"] == INTERRUPT_PREFIXES
