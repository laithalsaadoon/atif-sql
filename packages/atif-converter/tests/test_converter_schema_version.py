# SPDX-License-Identifier: Apache-2.0

"""A converter code change must be a conscious decision about ``CONVERTER_SCHEMA_VERSION``.

Materialization re-converts every session whose recorded ``converter_schema``
differs from :data:`atif_converter.domain.schema_version.CONVERTER_SCHEMA_VERSION`,
so forgetting the bump leaves the corpus on the old output forever, and bumping
on a no-op refactor re-converts the whole corpus for nothing. This test pins a
digest of the converter's code next to the version it was reviewed at. Any code
change turns it red; the fix is to bump the version if the change can alter any
artifact byte for any input, and then re-pin the digest either way.

The digest covers every module under ``src/atif_converter`` except
``domain/schema_version.py`` itself, as ``ast.unparse`` of the tree with
docstrings removed, so comments, docstrings and formatting don't count and code
(annotations included) does. Two branches that each bump from the same value
still conflict here when they merge, on the pinned digest, which forces a fresh
decision instead of letting two behavior changes share one version.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from atif_converter.domain.schema_version import CONVERTER_SCHEMA_VERSION

#: The reviewed pair. Update BOTH in the commit that changes converter code:
#: the version only when output can change, the digest always.
PINNED_VERSION = 3
PINNED_SOURCE_DIGEST = "62440bf01e29d613d690baedb96ab1f756f73f272e3a702168d697e584ca7ddf"

_SRC = Path(__file__).resolve().parents[1] / "src" / "atif_converter"


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def converter_source_digest(src: Path = _SRC) -> str:
    """sha256 over every converter module's docstring-free, comment-free code."""
    digest = hashlib.sha256()
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        if relative == "domain/schema_version.py":
            continue
        code = ast.unparse(_strip_docstrings(ast.parse(path.read_text(encoding="utf-8"))))
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(code.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def test_converter_code_matches_the_reviewed_digest() -> None:
    actual = converter_source_digest()
    assert actual == PINNED_SOURCE_DIGEST, (
        "atif-converter code changed since CONVERTER_SCHEMA_VERSION was last reviewed.\n"
        "If this change can alter any artifact byte for any input, bump "
        "CONVERTER_SCHEMA_VERSION in src/atif_converter/domain/schema_version.py "
        "(every corpus session re-converts on the next materialize). Then set, in "
        "this file:\n"
        f"    PINNED_VERSION = {CONVERTER_SCHEMA_VERSION}\n"
        f'    PINNED_SOURCE_DIGEST = "{actual}"'
    )


def test_the_pinned_version_is_the_running_version() -> None:
    """Re-pinning the digest without looking at the version is caught here."""
    assert CONVERTER_SCHEMA_VERSION == PINNED_VERSION


def test_docstrings_and_comments_do_not_move_the_digest(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text('"""Doc."""\n\ndef f():\n    """Doc."""\n    return 1\n')
    before = converter_source_digest(tmp_path)
    (tmp_path / "a.py").write_text(
        '"""Other doc."""\n\n# a comment\ndef f():\n    """Changed."""\n    return 1  # why\n'
    )
    assert converter_source_digest(tmp_path) == before


def test_a_code_change_moves_the_digest(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    before = converter_source_digest(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    return 2\n")
    assert converter_source_digest(tmp_path) != before
