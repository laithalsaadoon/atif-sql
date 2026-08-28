# SPDX-License-Identifier: Apache-2.0

"""Domain layer: the static view/macro catalog for atif-duck.

Pure value objects — no duckdb import, no I/O. The catalog is the
agent-facing contract (`atif-sql schema` answers from here in <50ms with
zero DuckDB bind cost); drift against the actual DDL is caught by the
tests in ``tests/test_duck_views.py``.
"""

from atif_duck.domain.catalog import (
    DEFAULT_PRICING,
    DESCRIPTIONS,
    MACRO_NAMES,
    MACRO_SIGNATURES,
    TABLE_MACRO_NAMES,
    VIEW_NAMES,
    VIEW_SCHEMA,
)
from atif_duck.domain.examples import ExampleQuery, build_examples

__all__ = [
    "DEFAULT_PRICING",
    "DESCRIPTIONS",
    "MACRO_NAMES",
    "MACRO_SIGNATURES",
    "TABLE_MACRO_NAMES",
    "VIEW_NAMES",
    "VIEW_SCHEMA",
    "ExampleQuery",
    "build_examples",
]
