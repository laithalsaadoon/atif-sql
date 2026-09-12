# SPDX-License-Identifier: Apache-2.0

"""Single-quoted SQL literal escaping for the one predicate that needs it.

LanceDB's delete predicate takes a string, not parameters, so ``lance_store``
escapes the uuids it deletes with this. The DuckDB reader in
``corpus_text_rows`` no longer needs it: its trajectory paths reach
``read_json`` as a bound list parameter. Escaping is a correctness requirement
of the predicate, and it lives in ``domain`` so a second caller would share
ONE implementation instead of carrying a copy.

:data:`SqlFragment` marks text that may be spliced into a statement; a value
from outside the process is a plain ``str`` until it passes through here.

atif-embed may never import atif-duck (the import-linter independence
contract), so this module is deliberately atif-embed's own rather than a reuse
of atif-duck's twin.

Pure module: stdlib only, no duckdb, no lancedb.
"""

from __future__ import annotations

from typing import NewType

#: Text that may be spliced into a SQL statement or predicate.
SqlFragment = NewType("SqlFragment", str)


def sql_literal(value: str) -> SqlFragment:
    """Escape ``value`` as a single-quoted SQL string literal.

    Doubles every embedded single quote, per the SQL standard's own escape.

    Parameters
    ----------
    value
        Value to embed in a statement or predicate.

    Returns
    -------
    SqlFragment
        ``value`` wrapped in single quotes, embedded quotes doubled.
    """
    escaped = value.replace("'", "''")
    return SqlFragment(f"'{escaped}'")


__all__ = ["SqlFragment", "sql_literal"]
