# SPDX-License-Identifier: Apache-2.0

"""Infrastructure layer: DuckDB registry over the materialized ATIF corpus."""

from atif_duck.infrastructure.registry import (
    register,
    register_macros,
    register_raw,
    register_views,
)

__all__ = [
    "register",
    "register_macros",
    "register_raw",
    "register_views",
]
