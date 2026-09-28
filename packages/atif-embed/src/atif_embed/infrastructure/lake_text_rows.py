# SPDX-License-Identifier: Apache-2.0

"""Embed discovery from the DuckLake, reading only what changed since the last complete pass.

The per-session reader (:mod:`atif_embed.infrastructure.corpus_text_rows`)
parses every session's ``trajectory.json`` on every run, which on a large
corpus is minutes and gigabytes to find a handful of new rows. The lake
already holds each step's flattened text and primary uuid in its ``steps``
table, so this reader asks the lake instead (through
:class:`~atif_embed.domain.ports.LakeStepsPort`, which atif-cli implements
over atif-duck).

The watermark
-------------
``lake_watermark.json`` sits beside the Lance table, inside the store's own
directory, so deleting the store deletes it too. It records the lake
position (lineage and snapshot id) of the last pass that ran to its end and
whose every yielded row was embedded, the text floor that pass used, and how
many rows the store held afterwards. The next pass reads only the rows whose
uuid was inserted or deleted in a lake snapshot after that position.

Why that's the same row set a full read gives: a uuid that no step row
touched since the watermark has the same rows, in the same order, as it had
then, so a full read would pick the same text for it, and that text was
embedded when the watermark was written. A uuid a changed row touched is
re-read in full, every occurrence in corpus order, so the first-wins rule
lands on the same text a full read would.

A full read runs instead whenever the watermark can't be trusted: there is
none, it names another lineage (the lake was rebuilt), the snapshots after
it were expired, the text floor changed, or the store's row count isn't the
one recorded (something outside a complete pass wrote or deleted rows). With
no usable lake at all, discovery falls back to the per-session reader.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_embed.domain.discovery import PendingSelection
from atif_embed.domain.ports import LakePosition
from atif_embed.infrastructure.corpus_text_rows import MIN_TEXT_CHARS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from atif_embed.domain.ports import LakeStepSnapshot, LakeStepsPort, TextRowsPort
    from atif_embed.domain.text_stamp import PendingText

#: The watermark's filename inside the Lance store directory.
LAKE_WATERMARK_FILE = "lake_watermark.json"

#: Bump when what a watermark means changes (the row key, the order).
WATERMARK_VERSION = 1


def read_watermark(path: Path) -> dict[str, Any] | None:
    """The recorded watermark, or ``None`` when absent or unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_watermark(path: Path, payload: dict[str, Any]) -> None:
    """Write the watermark atomically (a temporary file renamed over it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def watermark_position(payload: dict[str, Any] | None) -> LakePosition | None:
    """The position a watermark records, when it's one this code wrote."""
    if payload is None or payload.get("version") != WATERMARK_VERSION:
        return None
    lineage, snapshot = payload.get("lineage"), payload.get("snapshot_id")
    if not isinstance(lineage, str) or not isinstance(snapshot, int) or isinstance(snapshot, bool):
        return None
    return LakePosition(lineage=lineage, snapshot_id=snapshot)


class LakeTextRows:
    """:class:`~atif_embed.domain.ports.TextRowsPort` over the lake, with a watermark.

    ``fallback`` serves every call the lake can't (no lake, a stale one, a
    corpus it doesn't hold). :attr:`discovery` says which path the last call
    took: ``corpus`` (the fallback), ``lake-full``, ``lake-incremental`` or
    ``lake-unchanged`` (the watermark is already at the lake's snapshot).
    """

    def __init__(
        self, *, lake: LakeStepsPort, watermark_path: Path, fallback: TextRowsPort
    ) -> None:
        self._lake = lake
        self._watermark_path = watermark_path
        self._fallback = fallback
        self._completed: LakePosition | None = None
        self._last_source: TextRowsPort | None = None
        self.discovery = "none"

    def iter_unembedded(
        self,
        corpus_root: Path,
        *,
        embedded: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[PendingText]:
        """Yield the texts needing embedding, from the lake when there is one to read."""
        self._completed = None
        snapshot = self._lake.open_steps(corpus_root)
        if snapshot is None:
            self._last_source = self._fallback
            self.discovery = "corpus"
            yield from self._fallback.iter_unembedded(corpus_root, embedded=embedded, limit=limit)
            return
        self._last_source = None
        with snapshot:
            position = snapshot.position
            since = self._trusted_since(snapshot, stored_rows=len(embedded or {}))
            selection = PendingSelection(embedded=embedded, limit=limit)
            if since is not None and since == position:
                self.discovery = "lake-unchanged"
                rows: Iterator[tuple[str, str]] = iter(())
            else:
                self.discovery = "lake-full" if since is None else "lake-incremental"
                rows = snapshot.step_texts(min_chars=MIN_TEXT_CHARS, changed_since=since)
            logger.info(
                "embed discovery: {} at lake snapshot {}{}",
                self.discovery,
                position.snapshot_id,
                f" (changes after snapshot {since.snapshot_id})" if since is not None else "",
            )
            yield from selection.select(rows)
            if selection.exhausted:
                self._completed = position

    def _trusted_since(
        self, snapshot: LakeStepSnapshot, *, stored_rows: int
    ) -> LakePosition | None:
        """The watermark to read changes after, or ``None`` for a full read (with the reason logged)."""
        payload = read_watermark(self._watermark_path)
        since = watermark_position(payload)
        reason: str | None = None
        if payload is None:
            reason = "no watermark yet"
        elif since is None:
            reason = "the watermark was written by another version"
        elif payload.get("min_text_chars") != MIN_TEXT_CHARS:
            reason = "the text floor changed"
        elif payload.get("stored_rows") != stored_rows:
            reason = (
                f"the store holds {stored_rows} rows, not the {payload.get('stored_rows')} "
                "the watermark recorded"
            )
        elif since.lineage != snapshot.position.lineage:
            reason = "the lake was rebuilt since the watermark"
        elif since.snapshot_id > snapshot.position.snapshot_id:
            reason = "the watermark is ahead of the lake"
        elif since != snapshot.position and not snapshot.can_read_changes_since(since):
            reason = "the lake no longer holds the snapshots after the watermark"
        if reason is not None:
            logger.info("embed discovery: full lake read ({})", reason)
            return None
        return since

    def commit(self, *, stored_rows: int) -> None:
        """Advance the watermark to the position the last complete pass read."""
        if self._last_source is not None:
            self._last_source.commit(stored_rows=stored_rows)
            return
        if self._completed is None:
            return
        write_watermark(
            self._watermark_path,
            {
                "version": WATERMARK_VERSION,
                "lineage": self._completed.lineage,
                "snapshot_id": self._completed.snapshot_id,
                "min_text_chars": MIN_TEXT_CHARS,
                "stored_rows": stored_rows,
            },
        )
        logger.info(
            "embed discovery: watermark now at lake snapshot {}", self._completed.snapshot_id
        )
        self._completed = None


__all__ = [
    "LAKE_WATERMARK_FILE",
    "WATERMARK_VERSION",
    "LakeTextRows",
    "read_watermark",
    "watermark_position",
    "write_watermark",
]
