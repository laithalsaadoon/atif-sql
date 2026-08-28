# SPDX-License-Identifier: Apache-2.0

"""Pure watermark math: which source files moved between two scans.

Compare two ``{path: mtime_ns}`` maps and partition into added / modified /
removed. ``touched`` (added then modified) are the sessions whose artifacts
must be re-materialized; ``removed`` are sessions whose source vanished. Pure
so the delta can be tested without a filesystem and so two identical scans
always produce the same plan.

Clock discipline: every ``mtime_ns`` here is a filesystem modification time
in **epoch nanoseconds** (``os.stat().st_mtime_ns``). Nothing in this module
reads a clock; "now" is always passed in by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class SourceDelta:
    """Which source files moved between two watermark snapshots.

    All three partitions are sorted tuples so a delta is deterministic and
    comparable — the materialization plan embeds these paths, and set
    ordering would make two identical scans plan different work orders.
    """

    #: Paths present in the current scan and absent from the previous one.
    added: tuple[str, ...]
    #: Paths present in both whose ``mtime_ns`` changed (either direction).
    modified: tuple[str, ...]
    #: Paths present in the previous scan and absent from the current one.
    removed: tuple[str, ...]

    @property
    def touched(self) -> tuple[str, ...]:
        """Paths whose artifacts must be re-materialized (``added`` then ``modified``)."""
        return (*self.added, *self.modified)

    @property
    def is_empty(self) -> bool:
        """True when nothing moved, so a materialization pass would be a no-op."""
        return not (self.added or self.modified or self.removed)

    @property
    def changed_count(self) -> int:
        """Total number of paths in the delta, across all three partitions."""
        return len(self.added) + len(self.modified) + len(self.removed)


def diff_source_mtimes(
    previous: Mapping[str, int],
    current: Mapping[str, int],
) -> SourceDelta:
    """Partition two ``{path: mtime_ns}`` maps into added / modified / removed.

    A path is ``modified`` only when its mtime **changed**. An mtime that went
    backwards (a restored backup, a clock step on the writing host, a file
    replaced by an older copy) also counts as modified: the recorded watermark
    no longer describes the bytes on disk, so the session must be re-read.
    Only an exactly-equal mtime is treated as unchanged.
    """
    added = sorted(path for path in current if path not in previous)
    removed = sorted(path for path in previous if path not in current)
    modified = sorted(
        path
        for path, mtime_ns in current.items()
        if path in previous and previous[path] != mtime_ns
    )
    return SourceDelta(added=tuple(added), modified=tuple(modified), removed=tuple(removed))
