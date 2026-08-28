# SPDX-License-Identifier: Apache-2.0

"""Single-quoted SQL literal escaping for the inlined DDL.

DuckDB rejects prepared parameters where atif-duck needs them most — a table
function's arguments, and the glob patterns a view is defined over — so the
corpus paths are inlined into the statement text instead. Escaping is
therefore a correctness requirement of the DDL, not a convenience, and it
lives in ``domain`` so both the core registry and the analytics registry reach
the SAME implementation rather than each carrying a copy that could drift.

Pure module: stdlib only, no duckdb.
"""

from __future__ import annotations


def sql_literal(value: str) -> str:
    """Escape ``value`` as a single-quoted SQL string literal.

    Doubles every embedded single quote, per the SQL standard's own escape.

    Parameters
    ----------
    value
        Value to embed in a statement.

    Returns
    -------
    str
        ``value`` wrapped in single quotes, embedded quotes doubled.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


__all__ = ["sql_literal"]
