# SPDX-License-Identifier: Apache-2.0

"""The ``step_author`` SQL macro and its Python twin agree on every sample.

atif-duck renders the macro from its rule table; atif-analytics classifies
the steps its LLM pipelines read with a Python copy of the same table (the
independence contract keeps the two packages apart, and atif-duck's
``test_authorship_twin_pin.py`` pins the tables as source text). atif-cli is
the one package that imports both, so this is where the two IMPLEMENTATIONS
run side by side: one sample per rule with and without leading whitespace,
the rule prefix quoted inside human text, and the edge cases.
"""

from __future__ import annotations

import duckdb
import pytest

from atif_analytics.domain import authorship as py_authorship
from atif_duck.domain.authorship import AUTHOR_PREFIX_RULES, STEP_AUTHOR_CASE_SQL


def _samples() -> list[tuple[str, str | None]]:
    samples: list[tuple[str, str | None]] = []
    for _, prefix in AUTHOR_PREFIX_RULES:
        samples += [
            ("user", prefix),
            ("user", f"{prefix} and more"),
            ("user", f" \t\r\n{prefix}\nbody"),
            ("user", f"quoting it: {prefix}"),
            ("user", prefix[:-1]),
            ("agent", prefix),
        ]
    samples += [
        ("user", None),
        ("user", ""),
        ("user", " \n\t "),
        ("user", "go"),
        ("user", "\u00a0Stop hook feedback"),  # NBSP: stripped on neither side
        ("system", "hi"),
        ("agent", None),
    ]
    return samples


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute(f"CREATE MACRO step_author(src, msg) AS ({STEP_AUTHOR_CASE_SQL});")
    return connection


def test_sql_and_python_agree_on_every_sample(con: duckdb.DuckDBPyConnection) -> None:
    disagreements = []
    for source, message in _samples():
        row = con.execute("SELECT step_author(?, ?)", [source, message]).fetchone()
        assert row is not None
        sql = row[0]
        py = py_authorship.step_author(source, message)
        if sql != py:
            disagreements.append((source, message, sql, py))
    assert not disagreements, disagreements
