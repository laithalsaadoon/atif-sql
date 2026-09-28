# SPDX-License-Identifier: Apache-2.0

"""atif-embed's ``LakeStepsPort``, implemented over atif-duck's lake.

atif-embed and atif-duck may not import each other, so the composition root
joins them: :class:`DuckLakeSteps` opens the corpus's steps through
:func:`atif_duck.infrastructure.lake_steps.open_lake_steps` and hands them to
atif-embed in the port's own types. Imported only inside command bodies, so
the fast paths never load DuckDB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from loguru import logger

from atif_embed.domain.ports import LakePosition

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType

    from atif_duck.infrastructure.lake import LakeLayout
    from atif_duck.infrastructure.lake_steps import LakeSteps


@dataclass(slots=True)
class _Snapshot:
    """One open :class:`~atif_duck.infrastructure.lake_steps.LakeSteps`, in the port's types."""

    steps: LakeSteps

    @property
    def position(self) -> LakePosition:
        return LakePosition(lineage=self.steps.lineage, snapshot_id=self.steps.snapshot_id)

    def can_read_changes_since(self, since: LakePosition) -> bool:
        return since.lineage == self.steps.lineage and self.steps.can_read_changes_since(
            since.snapshot_id
        )

    def step_texts(
        self, *, min_chars: int, changed_since: LakePosition | None
    ) -> Iterator[tuple[str, str]]:
        since = None if changed_since is None else changed_since.snapshot_id
        return self.steps.step_texts(min_chars=min_chars, since_snapshot=since)

    def step_keys(self) -> frozenset[str]:
        return self.steps.step_keys()

    def close(self) -> None:
        self.steps.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class DuckLakeSteps:
    """``LakeStepsPort`` over the lake at ``layout``."""

    layout: LakeLayout
    memory_limit_bytes: int | None = None

    def open_steps(self, corpus_root: Path) -> _Snapshot | None:
        """The corpus's steps at the published snapshot, or ``None`` (reason logged) to fall back."""
        from atif_duck.infrastructure.lake_steps import LakeSteps, open_lake_steps

        opened = open_lake_steps(
            self.layout, corpus_root, memory_limit_bytes=self.memory_limit_bytes
        )
        if not isinstance(opened, LakeSteps):
            logger.info("embed: {}; reading the per-session artifacts instead", opened.reason)
            return None
        return _Snapshot(opened)


def lake_steps_port() -> DuckLakeSteps:
    """The port over the lake ``ATIF_SQL_LAKE_ROOT`` names, capped like ``query``."""
    from atif_cli.app import query_memory_limit_bytes
    from atif_duck.infrastructure.lake import LakeLayout
    from atif_duck.infrastructure.lake_settings import LakeSettings

    return DuckLakeSteps(
        layout=LakeLayout(LakeSettings().lake_root),
        memory_limit_bytes=query_memory_limit_bytes(),
    )


__all__ = ["DuckLakeSteps", "lake_steps_port"]
