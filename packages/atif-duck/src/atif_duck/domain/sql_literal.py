# SPDX-License-Identifier: Apache-2.0

"""Single-quoted SQL literal escaping, and the type that marks text as SQL.

Two things live here because they are the two ends of the same boundary.

:data:`SqlFragment` is a ``NewType`` over ``str``: a value typed as one is
text that may be spliced into a statement. Only three kinds of code produce it:
:func:`sql_literal` here, the catalog constants, and the projection module's
column expressions. A path, a session id, or anything else that arrived from
outside is a plain ``str`` and reaches DuckDB as a bound parameter instead. The
type is advisory at runtime (a NewType is its underlying ``str``); the AST test
over the registry modules is what enforces the rule.

:func:`sql_literal` is for the few statement kinds DuckDB refuses to prepare:
``ATTACH``, and the producer's one-row session projection. It doubles every
embedded single quote, which is the SQL standard's own escape. It lives in
``domain`` so every module that still needs it reaches the SAME implementation.

Pure module: stdlib only, no duckdb.
"""

from __future__ import annotations

from typing import NewType

#: Text that may be spliced into a SQL statement. See the module docstring.
SqlFragment = NewType("SqlFragment", str)


def sql_literal(value: str) -> SqlFragment:
    """Escape ``value`` as a single-quoted SQL string literal.

    Doubles every embedded single quote, per the SQL standard's own escape.

    Parameters
    ----------
    value
        Value to embed in a statement.

    Returns
    -------
    SqlFragment
        ``value`` wrapped in single quotes, embedded quotes doubled.
    """
    escaped = value.replace("'", "''")
    return SqlFragment(f"'{escaped}'")


__all__ = ["SqlFragment", "sql_literal"]
