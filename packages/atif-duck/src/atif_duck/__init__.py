# SPDX-License-Identifier: Apache-2.0

"""atif-duck: DuckDB views over the materialized ATIF corpus.

Layered: ``infrastructure`` (DuckDB registry) > ``domain`` (static
view/macro catalog). ``register(con, corpus_root)`` wires a connection to a
CONTRACT-shaped corpus (``<root>/sessions/<id>/{trajectory.json,
edges.jsonl, loss_report.json, meta.json}``) and exposes the core query
surface: the 16 views in :data:`VIEW_NAMES` (sessions / steps / messages /
tool_calls / ...) plus the 9 macros in :data:`MACRO_NAMES`. The v2
analytics surface (``ANALYTICS_VIEW_NAMES``,
``ANALYTICS_MACRO_SIGNATURES``) registers separately and needs
``atif-sql analyze`` to have run.
"""

from atif_duck.domain.catalog import (
    DEFAULT_PRICING,
    MACRO_NAMES,
    MACRO_SIGNATURES,
    VIEW_NAMES,
    VIEW_SCHEMA,
)
from atif_duck.infrastructure.registry import (
    RawSources,
    register,
    register_macros,
    register_raw,
    register_views,
)

__all__ = [
    "DEFAULT_PRICING",
    "MACRO_NAMES",
    "MACRO_SIGNATURES",
    "VIEW_NAMES",
    "VIEW_SCHEMA",
    "RawSources",
    "register",
    "register_macros",
    "register_raw",
    "register_views",
]
