# SPDX-License-Identifier: Apache-2.0

"""Find, and on request delete, the stored vectors no lake step names.

A row in the Lance store is an orphan when its uuid is the primary uuid of no
step the lake holds for the corpus: its session was deleted before the corpus
kept removed sessions, or a converter change re-keyed the step. Search joins
hits to steps, so an orphan never shows up in a result, but it still takes
space and still competes for the top-k before the join drops it.

The lake is the reference because it holds every session the corpus holds,
retained ones included. The caller must make sure no session is still
waiting for a lake write (atif-cli checks the corpus's pending list), since
such a session's vectors would look orphaned until its rows land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

    from atif_embed.domain.ports import LakeStepsPort, VectorStorePort

#: uuids per Lance delete predicate.
_DELETE_CHUNK = 1000


def prune_orphans(
    corpus_root: Path,
    *,
    lake: LakeStepsPort,
    store: VectorStorePort,
    dry_run: bool = True,
) -> dict[str, Any] | None:
    """Count the store's orphan rows, and delete them unless ``dry_run``.

    Returns ``None`` when there is no lake to check against. Otherwise
    ``{pipeline, stored, lake_keys, orphans, deleted, dry_run}``. A corpus
    the lake holds no steps for is refused (nothing deleted, ``orphans``
    still counted) rather than read as "every row is an orphan".
    """
    snapshot = lake.open_steps(corpus_root)
    if snapshot is None:
        return None
    with snapshot:
        keys = snapshot.step_keys()
        position = snapshot.position
    stored = store.get_embedded_hashes()
    orphans = sorted(uuid for uuid in stored if uuid not in keys)
    report: dict[str, Any] = {
        "pipeline": "prune_orphans",
        "lake_snapshot": position.snapshot_id,
        "stored": len(stored),
        "lake_keys": len(keys),
        "orphans": len(orphans),
        "deleted": 0,
        "dry_run": dry_run,
    }
    if not keys:
        logger.warning(
            "prune: the lake holds no steps for {}; refusing to treat every stored row as an orphan",
            corpus_root,
        )
        report["refused"] = "the lake holds no steps for this corpus"
        return report
    if dry_run or not orphans:
        return report
    for start in range(0, len(orphans), _DELETE_CHUNK):
        store.delete_uuids(orphans[start : start + _DELETE_CHUNK])
    store.optimize()
    report["deleted"] = len(orphans)
    logger.info("prune: deleted {} orphan rows from the store", len(orphans))
    return report


__all__ = ["prune_orphans"]
