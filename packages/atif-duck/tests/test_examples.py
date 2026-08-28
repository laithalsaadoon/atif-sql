# SPDX-License-Identifier: Apache-2.0

"""THE TEST IS THE CONTRACT for the derived example queries.

Every example :func:`atif_duck.domain.examples.build_examples` emits is
EXECUTED here against the fixture corpus — analytics parquets populated,
tiny Lance store attached — so the CLI's "test-executed against this
version" claim is literally this file. Drift catchers pin the derivation
seams: DESCRIPTIONS coverage, exemplar coverage, the AS TABLE macro set,
and example coverage of every catalog macro/view (or a documented
exclusion).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator

import duckdb
import pytest
from duck_fixtures import build_corpus
from test_analytics_views import _populate_analytics
from test_vss import _write_lance

from atif_duck.domain.catalog import (
    ANALYTICS_MACRO_SIGNATURES,
    ANALYTICS_VIEW_NAMES,
    DESCRIPTIONS,
    MACRO_SIGNATURES,
    TABLE_MACRO_NAMES,
    VIEW_NAMES,
)
from atif_duck.domain.examples import (
    ARG_EXEMPLARS,
    CATEGORY_VALUES,
    EXCLUSIONS,
    REQUIRES_VALUES,
    ExampleQuery,
    build_examples,
)
from atif_duck.infrastructure import analytics as analytics_mod, registry as registry_mod
from atif_duck.infrastructure.registry import register

EXAMPLES = build_examples()


@pytest.fixture(scope="module")
def full_con(tmp_path_factory: pytest.TempPathFactory) -> Iterator[duckdb.DuckDBPyConnection]:
    """One connection over the COMPLETE fixture surface: core + analytics + vss.

    Module-scoped: the examples are read-only SELECTs, so one registration
    pass serves every parametrized case. The corpus is rebuilt here (rather
    than via the function-scoped ``corpus_root`` fixture) to allow the wider
    scope.
    """
    root = tmp_path_factory.mktemp("examples-corpus")
    corpus = build_corpus(root / "corpus")
    _populate_analytics(corpus)
    lance = _write_lance(root / "lance")
    con = duckdb.connect(":memory:")
    register(con, corpus, lance_uri=lance)
    yield con
    con.close()


# ---------------------------------------------------------------------------
# The contract: every example EXECUTES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", EXAMPLES, ids=[e.name for e in EXAMPLES])
def test_every_example_executes(example: ExampleQuery, full_con: duckdb.DuckDBPyConnection) -> None:
    """Each derived example must run without error on the fixture corpus."""
    cursor = full_con.execute(example.sql)
    assert cursor.description, f"{example.name}: example produced no result shape"
    # fetchall() forces full materialization — a lazily-failing projection
    # (bad JSON path, missing column) surfaces here, not at bind time.
    cursor.fetchall()


def test_view_examples_return_rows(full_con: duckdb.DuckDBPyConnection) -> None:
    """Core + analytics view examples yield DATA on the fixture corpus.

    (VSS aside — the lance fixture's uuids overlap ``messages`` not every
    view.) Empty results would mean the exemplar corpus no longer exercises
    the surface and the executing test has gone hollow.
    """
    for example in EXAMPLES:
        if example.category != "view" or example.requires == "vss":
            continue
        rows = full_con.execute(example.sql).fetchall()
        assert rows, f"{example.name}: view example returned zero rows on the fixture corpus"


def test_semantic_search_example_returns_rows(full_con: duckdb.DuckDBPyConnection) -> None:
    """The VSS example runs the real macro over the tiny Lance fixture."""
    example = next(e for e in EXAMPLES if e.name == "semantic_search")
    rows = full_con.execute(example.sql).fetchall()
    assert rows, "semantic_search example returned zero rows over the Lance fixture"
    # Self-probe: the stored embedding used as query_vec is its own nearest
    # neighbor, so the top hit has sim == 1.
    assert float(rows[0][1]) == pytest.approx(1.0)


def test_query_vec_exemplar_is_unit_norm(full_con: duckdb.DuckDBPyConnection) -> None:
    """The SHIPPED query_vec exemplar must build a unit-norm probe.

    Guards ``ARG_EXEMPLARS['query_vec']`` itself, not a copy of its SQL: a raw
    stored vector (norm 1200 on the fixture store) is its own nearest neighbor
    under every metric, so the semantic_search example would score sim == 1.0
    whether the macro ranked by cosine or by L2 and could not distinguish a
    correct macro from a broken one.
    """
    row = full_con.execute(
        f"SELECT sqrt(list_sum(list_transform({ARG_EXEMPLARS['query_vec']}, x -> x::DOUBLE * x)))"
    ).fetchone()
    assert row is not None
    assert float(row[0]) == pytest.approx(1.0, abs=1e-6)


def test_query_vec_exemplar_discriminates_cosine_from_l2(
    full_con: duckdb.DuckDBPyConnection,
) -> None:
    """The exemplar probe must rank cosine and L2 DIFFERENTLY.

    This is what makes the semantic_search example an oracle rather than a
    smoke test. Fixture vectors are un-normalized on purpose, so a unit-norm
    probe makes the two metrics disagree at every rank; a raw-magnitude probe
    collapses them onto the same order and the example stops discriminating.
    """
    probe = ARG_EXEMPLARS["query_vec"]
    cosine_order = [
        r[0] for r in full_con.execute(f"SELECT uuid FROM semantic_search({probe}, 4)").fetchall()
    ]
    l2_order = [
        r[0]
        for r in full_con.execute(
            f"SELECT uuid FROM message_embeddings ORDER BY list_distance(embedding, {probe}) LIMIT 4"
        ).fetchall()
    ]
    assert len(cosine_order) == 4
    assert cosine_order != l2_order


# ---------------------------------------------------------------------------
# Drift catchers on the derivation seams
# ---------------------------------------------------------------------------


def _all_catalog_objects() -> set[str]:
    return (
        set(VIEW_NAMES)
        | set(ANALYTICS_VIEW_NAMES)
        | set(MACRO_SIGNATURES)
        | set(ANALYTICS_MACRO_SIGNATURES)
    )


def test_every_catalog_object_has_example_or_exclusion() -> None:
    """Coverage drift: a new view/macro must gain an example or a documented exclusion."""
    example_names = {e.name for e in EXAMPLES}
    covered = example_names | set(EXCLUSIONS)
    missing = _all_catalog_objects() - covered
    assert not missing, (
        f"catalog objects with neither an example nor an EXCLUSIONS entry: {sorted(missing)}"
    )


def test_exclusions_reference_real_catalog_objects() -> None:
    """An exclusion for a renamed/removed object is stale — fail it."""
    stale = set(EXCLUSIONS) - _all_catalog_objects()
    assert not stale, f"EXCLUSIONS entries not in any catalog: {sorted(stale)}"
    overlap = set(EXCLUSIONS) & {e.name for e in EXAMPLES}
    assert not overlap, f"EXCLUSIONS entries that still emit examples: {sorted(overlap)}"


def test_descriptions_cover_catalog_exactly() -> None:
    """DESCRIPTIONS (the only hand-maintained prose) matches the catalogs 1:1."""
    objects = _all_catalog_objects()
    missing = objects - set(DESCRIPTIONS)
    stale = set(DESCRIPTIONS) - objects
    assert not missing, f"catalog objects without a DESCRIPTIONS entry: {sorted(missing)}"
    assert not stale, f"DESCRIPTIONS entries for unknown objects: {sorted(stale)}"
    assert all(DESCRIPTIONS[k].strip() for k in DESCRIPTIONS), "empty description text"


def test_every_macro_param_has_exemplar() -> None:
    """A new macro parameter name needs an ARG_EXEMPLARS literal."""
    params = {
        p for sig in (*MACRO_SIGNATURES.values(), *ANALYTICS_MACRO_SIGNATURES.values()) for p in sig
    }
    missing = params - set(ARG_EXEMPLARS)
    assert not missing, f"macro parameters without an ARG_EXEMPLARS entry: {sorted(missing)}"


def test_table_macro_names_match_ddl() -> None:
    """TABLE_MACRO_NAMES must equal the macros whose DDL says ``AS TABLE``."""
    source = inspect.getsource(registry_mod.register_macros) + inspect.getsource(
        analytics_mod.register_analytics_macros
    )
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+MACRO\s+(\w+)\s*\([^)]*\)\s*AS\s+(TABLE)?",
        re.IGNORECASE,
    )
    parsed_table = {m.group(1) for m in pattern.finditer(source) if m.group(2)}
    assert parsed_table == set(TABLE_MACRO_NAMES), (
        f"TABLE_MACRO_NAMES diverges from the DDL.\n"
        f"  DDL AS TABLE: {sorted(parsed_table)}\n"
        f"  static:       {sorted(TABLE_MACRO_NAMES)}"
    )


def test_example_fields_are_well_formed() -> None:
    """Names unique; requires/category values valid; sql mentions the object."""
    names = [e.name for e in EXAMPLES]
    assert len(names) == len(set(names)), "duplicate example names"
    for example in EXAMPLES:
        assert example.requires in REQUIRES_VALUES
        assert example.category in CATEGORY_VALUES
        assert example.name in example.sql
        assert example.description == DESCRIPTIONS[example.name]
