# SPDX-License-Identifier: Apache-2.0

"""The contract's materialized-corpus directory shape, as one value object.

CONTRACT.md §Materialized corpus layout is the ONLY specification of where
artifacts live; atif-duck reads this same shape without importing us, so the
paths must be computed in exactly one place on the writer side. This module
is that place — every writer path in the application layer goes through
:class:`CorpusLayout`, and the layout tests pin the contract strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Per-session artifact filenames fixed by CONTRACT.md.
TRAJECTORY_FILENAME = "trajectory.json"
LOSS_REPORT_FILENAME = "loss_report.json"
EDGES_FILENAME = "edges.jsonl"
META_FILENAME = "meta.json"
WATERMARK_FILENAME = "watermark.json"


@dataclass(frozen=True, slots=True)
class CorpusLayout:
    """Computes every contract path under one ``corpus_root``.

    Pure path arithmetic — nothing here touches the filesystem, so the
    layout can be asserted against the contract without a tmpdir.
    """

    #: Root of the materialized corpus (default ``~/.atif-sql/corpus/<slug>``).
    corpus_root: Path

    @property
    def sessions_dir(self) -> Path:
        """Parent directory of every per-session artifact directory."""
        return self.corpus_root / "sessions"

    @property
    def watermark_path(self) -> Path:
        """``{path: mtime_ns}`` across the source corpus, updated per pass."""
        return self.corpus_root / WATERMARK_FILENAME

    @property
    def staging_dir(self) -> Path:
        """Scratch area for whole-session-dir atomic swaps.

        Deliberately OUTSIDE ``sessions/`` so no reader glob (DuckDB's
        ``read_json`` matches dot-dirs) and no ghost-removal walk can ever
        observe a half-written session dir; same filesystem as
        ``sessions/`` so ``os.replace`` of the staged dir stays atomic.
        """
        return self.corpus_root / ".staging"

    def session_dir(self, session_id: str) -> Path:
        """``<corpus_root>/sessions/<session_id>/``."""
        return self.sessions_dir / session_id

    def trajectory_path(self, session_id: str) -> Path:
        """Compact ATIF-v1.7 trajectory JSON for one session."""
        return self.session_dir(session_id) / TRAJECTORY_FILENAME

    def loss_report_path(self, session_id: str) -> Path:
        """``atif_converter`` LossReport JSON for one session."""
        return self.session_dir(session_id) / LOSS_REPORT_FILENAME

    def edges_path(self, session_id: str) -> Path:
        """One line per RAW record: uuid/parent_uuid edge list."""
        return self.session_dir(session_id) / EDGES_FILENAME

    def meta_path(self, session_id: str) -> Path:
        """Provenance record: source files, mtimes, versions, materialized_at."""
        return self.session_dir(session_id) / META_FILENAME
