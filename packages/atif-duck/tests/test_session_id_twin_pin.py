# SPDX-License-Identifier: Apache-2.0

"""Drift pin for the deliberate ``session_id`` twin.

``atif_duck.domain.session_id`` and ``atif_corpus.domain.session_id`` carry
the same pattern and limit, and the independence contract forbids either
package from importing the other. The atif-corpus copy is read as SOURCE TEXT
(AST, never imported) and compared with the constants this package uses. The
same rule on both sides is what lets the registry trust that a directory it
accepts is one the corpus writer would have written, and refuse one it would
not have.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from atif_duck.domain.session_id import (
    SESSION_ID_MAX_CHARS,
    SESSION_ID_PATTERN,
    is_valid_session_id,
    session_id_rejection,
)

#: packages/atif-duck/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_CORPUS_SESSION_ID = (
    _PACKAGES_DIR / "atif-corpus" / "src" / "atif_corpus" / "domain" / "session_id.py"
)


def _module_constants(source: str) -> dict[str, object]:
    constants: dict[str, object] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant):
                constants[node.target.id] = node.value.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                    constants[target.id] = node.value.value
    return constants


def test_twin_source_is_present() -> None:
    assert _CORPUS_SESSION_ID.is_file(), f"twin module missing at {_CORPUS_SESSION_ID}"


def test_pattern_and_limit_match_exactly() -> None:
    twin = _module_constants(_CORPUS_SESSION_ID.read_text(encoding="utf-8"))
    assert twin["SESSION_ID_PATTERN"] == SESSION_ID_PATTERN
    assert twin["SESSION_ID_MAX_CHARS"] == SESSION_ID_MAX_CHARS


def test_pattern_is_the_documented_one() -> None:
    assert SESSION_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    assert SESSION_ID_MAX_CHARS == 255


@pytest.mark.parametrize(
    "name",
    ["4b108d1c-0915-4e3d-93a0-45f661b01ae2", "019e9e38-39a6-7170-86fb-46e6f1cb1931", "a" * 255],
)
def test_real_ids_pass(name: str) -> None:
    assert is_valid_session_id(name)


@pytest.mark.parametrize(
    "name",
    ["o'brien", 'say "hi"', "a;b", "--comment", "$1", "two words", "sessión", "a" * 256, ""],
)
def test_adversarial_shapes_fail(name: str) -> None:
    assert session_id_rejection(name) is not None
