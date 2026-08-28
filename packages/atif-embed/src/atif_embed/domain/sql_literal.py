# SPDX-License-Identifier: Apache-2.0

"""Single-quoted SQL literal escaping for the two inlined-SQL adapters.

DuckDB rejects prepared parameters as table-function arguments — the same
constraint that makes atif-duck inline its globs — so ``corpus_text_rows``
inlines the trajectory paths as an escaped list literal. LanceDB's delete
predicate takes a string, not parameters, so ``lance_store`` escapes the uuids
the same way. Escaping is a correctness requirement of both statements, and it
lives in ``domain`` so the two adapters share ONE implementation instead of a
copy each.

atif-embed may never import atif-duck (the import-linter independence
contract), so this module is deliberately atif-embed's own rather than a reuse
of atif-duck's twin.

Pure module: stdlib only, no duckdb, no lancedb.
"""

from __future__ import annotations


def sql_literal(value: str) -> str:
    """Escape ``value`` as a single-quoted SQL string literal.

    Doubles every embedded single quote, per the SQL standard's own escape.

    Parameters
    ----------
    value
        Value to embed in a statement or predicate.

    Returns
    -------
    str
        ``value`` wrapped in single quotes, embedded quotes doubled.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


__all__ = ["sql_literal"]
