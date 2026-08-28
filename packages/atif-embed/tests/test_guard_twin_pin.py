# SPDX-License-Identifier: Apache-2.0

"""Drift pin for the deliberate ``embedding_guard`` twins.

``atif_embed.domain.embedding_guard`` and ``atif_duck.domain.embedding_guard``
carry the same pure rule and the same operator recovery instruction, and the
independence contract forbids either package from importing the other. So the
atif-duck copy is read as SOURCE TEXT (AST, never imported) and both sides are
compared against one expected string held here.

The hint names ``<corpus-root>/embeddings_lance`` because that is where the
store actually is; a hint naming a home directory has the operator delete
nothing and stay broken, and the read/search path is the dominant one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from atif_embed.domain.embedding_guard import RECOVERY_HINT

#: The one recovery instruction both twins must carry, verbatim. The embed
#: command carries the full explicit scope — a bare ``atif-sql embed`` exits
#: 64, so a hint prescribing it hands the operator a command that fails.
EXPECTED_RECOVERY_HINT = (
    "Re-embed under the new provider: rm -rf the Lance store directory "
    "(ATIF_SQL_LANCE_URI, or <corpus-root>/embeddings_lance by default) "
    "and re-run `atif-sql embed --all --no-dry-run` (a bare `atif-sql embed` "
    "exits 64: a real run needs an explicit scope)."
)

#: packages/atif-embed/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_DUCK_GUARD = _PACKAGES_DIR / "atif-duck" / "src" / "atif_duck" / "domain" / "embedding_guard.py"
_EMBED_GUARD = _PACKAGES_DIR / "atif-embed" / "src" / "atif_embed" / "domain" / "embedding_guard.py"


def _duck_guard_source() -> str:
    assert _DUCK_GUARD.is_file(), f"twin module missing at {_DUCK_GUARD}"
    return _DUCK_GUARD.read_text(encoding="utf-8")


def _embed_guard_source() -> str:
    assert _EMBED_GUARD.is_file(), f"twin module missing at {_EMBED_GUARD}"
    return _EMBED_GUARD.read_text(encoding="utf-8")


def _module_constant(source: str, name: str) -> str:
    """Return a module-level string constant's value, parsed not imported."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = ast.literal_eval(node.value)
                assert isinstance(value, str)
                return value
    msg = f"no module-level string constant {name!r} in the twin source"
    raise AssertionError(msg)


class TestRecoveryHintTwinsAgree:
    def test_embed_side_matches_expected(self) -> None:
        assert RECOVERY_HINT == EXPECTED_RECOVERY_HINT

    def test_duck_side_matches_expected(self) -> None:
        assert _module_constant(_duck_guard_source(), "RECOVERY_HINT") == EXPECTED_RECOVERY_HINT

    @pytest.mark.parametrize(
        "read_source", [_duck_guard_source, _embed_guard_source], ids=["duck", "embed"]
    )
    def test_mismatch_message_interpolates_the_hint(self, read_source: Callable[[], str]) -> None:
        """The constant must reach the raised message, not just sit in the module.

        Checked on BOTH raise sites: a side that inlines its own path string
        keeps the module constant in agreement while shipping a different
        instruction to the operator, which is the drift this pin exists for.
        """
        source = read_source()
        raise_site = source[source.index("raise EmbeddingProviderMismatch") :]
        assert "{RECOVERY_HINT}" in raise_site
        assert "embeddings_lance" not in raise_site

    def test_neither_twin_names_a_home_directory_store(self) -> None:
        assert "~/.atif-sql" not in RECOVERY_HINT
        assert "~/.atif-sql" not in _duck_guard_source()
        assert "~/.atif-sql" not in _embed_guard_source()

    @pytest.mark.parametrize("fragment", ["ATIF_SQL_LANCE_URI", "<corpus-root>/embeddings_lance"])
    def test_both_twins_name_the_real_store_locations(self, fragment: str) -> None:
        assert fragment in RECOVERY_HINT
        assert fragment in _module_constant(_duck_guard_source(), "RECOVERY_HINT")
        assert fragment in _module_constant(_embed_guard_source(), "RECOVERY_HINT")
