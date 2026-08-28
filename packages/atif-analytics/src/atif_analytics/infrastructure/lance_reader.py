# SPDX-License-Identifier: Apache-2.0

"""Read-only access to the atif-embed lance store for the structural stages.

The VSS branch owns the store writer; this module codes against the
DOCUMENTED table contract (CONTRACT-V2 §Ports & state):
table ``embeddings`` with columns ``{uuid, model, dim, embedding,
embedded_at}``. When the store is absent (dataset dir missing, or no
``embeddings`` table yet), :func:`load_embeddings` returns ``None`` and the
structural stages SKIP gracefully with a log — an unembedded corpus is a
normal state, not an error.

lancedb is lazy-imported inside the function so importing this module never
drags its ~2.6 s import subtree onto the CLI fast path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from loguru import logger

#: Table name fixed by the store contract (CONTRACT-V2).
TABLE_NAME = "embeddings"


def load_embeddings(lance_uri: Path) -> tuple[list[str], np.ndarray] | None:
    """Read the lance ``embeddings`` table → ``(uuid_list, float32 matrix)``.

    Returns ``None`` when the store is absent or empty — callers log and
    skip. Matrix shape is ``(N, dim)``, contiguous float32 (HDBSCAN /
    numpy-centroid ready).
    """
    if not lance_uri.exists():
        logger.info("lance store absent at {} — structural stages will skip", lance_uri)
        return None
    import lancedb

    db = lancedb.connect(str(lance_uri))
    try:
        # `list_tables()`, not the deprecated `table_names()` (which emits a
        # DeprecationWarning as of lancedb 0.30). The response is a pydantic
        # model whose `.tables` is the name list; the local namespace is small
        # enough that its `page_token` never comes into play.
        names = set(db.list_tables().tables)
    except Exception as exc:  # noqa: BLE001 — any store-shape surprise means "no embeddings yet"
        logger.warning("lance store at {} unreadable ({}) — skipping", lance_uri, exc)
        return None
    if TABLE_NAME not in names:
        logger.info(
            "lance store at {} has no '{}' table — structural stages will skip",
            lance_uri,
            TABLE_NAME,
        )
        return None
    tbl = db.open_table(TABLE_NAME)
    arrow = tbl.to_arrow().select(["uuid", "embedding"])
    uuids = [str(u) for u in arrow.column("uuid").to_pylist()]
    if not uuids:
        logger.info("lance store at {} is empty — structural stages will skip", lance_uri)
        return None
    emb_raw = arrow.column("embedding").to_numpy(zero_copy_only=False)
    emb = np.stack(list(emb_raw)) if emb_raw.ndim == 1 else emb_raw
    return uuids, np.ascontiguousarray(emb, dtype=np.float32)


__all__ = ["TABLE_NAME", "load_embeddings"]
