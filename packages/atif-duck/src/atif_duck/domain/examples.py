# SPDX-License-Identifier: Apache-2.0

"""Derived, test-executed example queries for the atif-duck SQL surface.

Operator's principle: DON'T hardcode example strings — they rot the moment a
macro signature or view changes. Every example here is DERIVED from the
static catalogs (:data:`~atif_duck.domain.catalog.VIEW_NAMES`,
:data:`~atif_duck.domain.catalog.ANALYTICS_VIEW_NAMES`,
:data:`~atif_duck.domain.catalog.MACRO_SIGNATURES`,
:data:`~atif_duck.domain.catalog.ANALYTICS_MACRO_SIGNATURES`) plus one small
exemplar table mapping parameter NAMES to literal SQL argument text
(:data:`ARG_EXEMPLARS`). The catalogs are already drift-pinned against the
real DDL by the tests in ``tests/test_duck_views.py`` /
``tests/test_analytics_views.py``, so the examples inherit that freshness.

THE TEST IS THE CONTRACT: ``tests/test_examples.py`` EXECUTES every example
:func:`build_examples` emits against the fixture corpus (analytics parquets
registered, tiny Lance store attached). An example that stops running stops
shipping — the CLI surface (``atif-sql examples``) can honestly claim
"test-executed against this version".

The only hand-maintained prose is
:data:`~atif_duck.domain.catalog.DESCRIPTIONS` in ``catalog.py``; a coverage
drift test forces one description per catalog object.

Pure domain module: no duckdb import, no I/O — safe on the CLI's lean
import path (``schema`` / ``examples`` / ``--help``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast, get_args

from atif_duck.domain.catalog import (
    ANALYTICS_MACRO_SIGNATURES,
    ANALYTICS_VIEW_NAMES,
    DESCRIPTIONS,
    MACRO_SIGNATURES,
    TABLE_MACRO_NAMES,
    VIEW_NAMES,
    VIEW_SCHEMA,
)

#: What an example needs registered before it runs: the base corpus views
#: (``core``), the analytics parquet views (``analytics``), or the
#: Lance-backed embeddings surface (``vss``).
Requires = Literal["core", "analytics", "vss"]

#: Structural kind, derived from which catalog the object lives in and
#: whether its DDL is ``AS TABLE`` (see
#: :data:`~atif_duck.domain.catalog.TABLE_MACRO_NAMES`).
Category = Literal["view", "table-macro", "scalar-macro"]

REQUIRES_VALUES: tuple[str, ...] = cast("tuple[str, ...]", get_args(Requires))
CATEGORY_VALUES: tuple[str, ...] = cast("tuple[str, ...]", get_args(Category))

#: Exemplar SQL argument text per macro parameter NAME. The single seam
#: between the derived examples and real macro signatures: a new parameter
#: name lands here (one line) or ``build_examples`` fails loud — and the
#: executing test in ``tests/test_examples.py`` fails with it.
#:
#: * ``sid`` — a real session id, picked from the corpus itself so the
#:   example runs verbatim on ANY materialized corpus.
#: * ``query_vec`` — a stored embedding L2-NORMALIZED into a unit-norm probe,
#:   matching what ``embed_query`` returns, so the example runs on any corpus
#:   without fabricating a dim-wide literal. The normalization is not
#:   cosmetic: stored vectors are un-normalized, and a RAW stored vector is
#:   its own nearest neighbor under every metric, so an un-normalized probe
#:   would make this example score sim == 1.0 no matter which metric the macro
#:   ranks by — it could not tell a correct macro from a broken one. For TEXT
#:   queries the flow is two-step: embed the query text first (``atif-sql
#:   search`` wraps both steps); see
#:   :data:`~atif_duck.domain.catalog.DESCRIPTIONS`.
ARG_EXEMPLARS: dict[str, str] = {
    "sid": "(SELECT session_id FROM sessions LIMIT 1)",
    "since_days": "30",
    "last_n_days": "30",
    "window_days": "30",
    "k": "10",
    "n": "10",
    "label_name": "'correction'",
    "signal_name": "'correction'",
    "cid": "0",
    "interval_text": "'7 days'",
    "query_vec": (
        "(SELECT list_transform("
        "me.embedding, v -> (v / sqrt(list_sum("
        "list_transform(me.embedding, x -> x::DOUBLE * x))))::FLOAT"
        ") FROM message_embeddings me LIMIT 1)"
    ),
}

#: Catalog objects DELIBERATELY without an example, mapped to the reason.
#: The coverage drift test treats these as documented exclusions; everything
#: else in the catalogs MUST yield an example. Currently empty — every view
#: and macro is exampled — but the seam stays so a future exclusion is a
#: reviewed one-liner instead of a silent coverage hole.
EXCLUSIONS: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class ExampleQuery:
    """One runnable example query, derived from the catalog.

    Attributes
    ----------
    name
        The catalog object (view or macro) the example demonstrates.
    sql
        A complete, runnable statement for ``atif-sql query``.
    description
        Agent-facing prose from :data:`~atif_duck.domain.catalog.DESCRIPTIONS`.
    requires
        ``core`` | ``analytics`` | ``vss`` — what must be registered/populated.
    category
        ``view`` | ``table-macro`` | ``scalar-macro``.
    """

    name: str
    sql: str
    description: str
    requires: Requires
    category: Category


def _view_requires(name: str) -> Requires:
    """Core views are corpus-only; ``message_embeddings`` needs the store."""
    return "vss" if name == "message_embeddings" else "core"


def _view_sql(name: str) -> str:
    """Canonical top-N SELECT for one view.

    When the static :data:`~atif_duck.domain.catalog.VIEW_SCHEMA` declares a
    timestamp column, the example orders by the FIRST one descending
    (newest-first is the agent's default question); otherwise a plain LIMIT.
    Analytics views have no static schema entry, so they take the plain form.
    """
    schema = VIEW_SCHEMA.get(name, ())
    ts_col = next((col for col, typ in schema if typ.startswith("TIMESTAMP")), None)
    if ts_col is not None:
        # The view name and its timestamp column both come from the static catalog.
        return f"SELECT * FROM {name} ORDER BY {ts_col} DESC LIMIT 10"  # noqa: S608
    return f"SELECT * FROM {name} LIMIT 10"  # noqa: S608


def _macro_sql(name: str, params: tuple[str, ...]) -> str:
    """Runnable invocation for one macro, args filled from the exemplars."""
    try:
        args = ", ".join(ARG_EXEMPLARS[param] for param in params)
    except KeyError as exc:
        msg = (
            f"macro {name!r} has parameter {exc.args[0]!r} with no entry in "
            "ARG_EXEMPLARS — add an exemplar literal so build_examples() can "
            "derive a runnable example"
        )
        raise KeyError(msg) from exc
    if name in TABLE_MACRO_NAMES:
        # The macro name comes from MACRO_SIGNATURES and args from ARG_EXEMPLARS.
        return f"SELECT * FROM {name}({args}) LIMIT 10"  # noqa: S608
    return f"SELECT {name}({args}) AS {name}"


def _description(name: str) -> str:
    """DESCRIPTIONS lookup that fails loud on a missing catalog object."""
    try:
        return DESCRIPTIONS[name]
    except KeyError as exc:
        msg = (
            f"catalog object {name!r} has no entry in "
            "atif_duck.domain.catalog.DESCRIPTIONS — add one (the drift test "
            "in tests/test_examples.py pins this coverage)"
        )
        raise KeyError(msg) from exc


def build_examples() -> tuple[ExampleQuery, ...]:
    """Derive the full example inventory from the catalogs.

    Order: core views, VSS view, analytics views, then core macros, VSS
    macro, analytics macros — stable, so the CLI listing and the JSON array
    are deterministic across runs.
    """
    examples: list[ExampleQuery] = []

    for view in VIEW_NAMES:
        if view in EXCLUSIONS:
            continue
        examples.append(
            ExampleQuery(
                name=view,
                sql=_view_sql(view),
                description=_description(view),
                requires=_view_requires(view),
                category="view",
            )
        )

    for view in ANALYTICS_VIEW_NAMES:
        if view in EXCLUSIONS:
            continue
        examples.append(
            ExampleQuery(
                name=view,
                sql=_view_sql(view),
                description=_description(view),
                requires="analytics",
                category="view",
            )
        )

    for macro, params in MACRO_SIGNATURES.items():
        if macro in EXCLUSIONS:
            continue
        examples.append(
            ExampleQuery(
                name=macro,
                sql=_macro_sql(macro, params),
                description=_description(macro),
                requires="vss" if macro == "semantic_search" else "core",
                category="table-macro" if macro in TABLE_MACRO_NAMES else "scalar-macro",
            )
        )

    for macro, params in ANALYTICS_MACRO_SIGNATURES.items():
        if macro in EXCLUSIONS:
            continue
        examples.append(
            ExampleQuery(
                name=macro,
                sql=_macro_sql(macro, params),
                description=_description(macro),
                requires="analytics",
                category="table-macro" if macro in TABLE_MACRO_NAMES else "scalar-macro",
            )
        )

    return tuple(examples)


__all__ = [
    "ARG_EXEMPLARS",
    "CATEGORY_VALUES",
    "EXCLUSIONS",
    "REQUIRES_VALUES",
    "Category",
    "ExampleQuery",
    "Requires",
    "build_examples",
]
