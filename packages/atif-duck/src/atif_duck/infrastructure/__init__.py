# SPDX-License-Identifier: Apache-2.0

"""Infrastructure layer: DuckDB registry over the materialized ATIF corpus.

Also the columnar producer (:class:`ColumnarArtifactProducer`) that writes
the typed per-session parquet artifacts the registry prefers to read, and
:func:`columnar_coverage`, which reports how many sessions carry them.
"""

from atif_duck.infrastructure.columnar import (
    ColumnarArtifactProducer,
    ColumnarCoverage,
    columnar_coverage,
    session_has_columnar,
)
from atif_duck.infrastructure.registry import (
    RawSources,
    register,
    register_macros,
    register_raw,
    register_views,
)

__all__ = [
    "ColumnarArtifactProducer",
    "ColumnarCoverage",
    "RawSources",
    "columnar_coverage",
    "register",
    "register_macros",
    "register_raw",
    "register_views",
    "session_has_columnar",
]
