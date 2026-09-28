# SPDX-License-Identifier: Apache-2.0

"""The contract's materialized-corpus directory shape, as one value object.

CONTRACT.md §Materialized corpus layout is the ONLY specification of where
artifacts live; atif-duck reads this same shape without importing us, so the
paths must be computed in exactly one place on the writer side. This module
is that place — every writer path in the application layer goes through
:class:`CorpusLayout`, and the layout tests pin the contract strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Per-session artifact filenames fixed by CONTRACT.md.
TRAJECTORY_FILENAME = "trajectory.json"
LOSS_REPORT_FILENAME = "loss_report.json"
EDGES_FILENAME = "edges.jsonl"
#: One line per kept non-message record (hooks, API errors, cost-state, ...).
SESSION_EVENTS_FILENAME = "session_events.jsonl"
META_FILENAME = "meta.json"
WATERMARK_FILENAME = "watermark.json"
#: Corpus-level record of sessions whose source converted to nothing.
EMPTY_SESSIONS_FILENAME = "empty_sessions.json"
#: Corpus-level record of published sessions a ``SessionSink`` has not yet
#: taken (a failed or interrupted sync); retried on the next pass.
SINK_PENDING_FILENAME = "sink_pending.json"
#: Per-session directory holding the zstd copy of the raw source files.
SOURCE_ARCHIVE_DIRNAME = "source"

#: A blob's name: the lowercase hex SHA-256 of its bytes.
_BLOB_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
#: A blob's extension: short, lowercase alphanumeric (``png``, ``jpg``, ``bin``).
_BLOB_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,8}$")


class InvalidBlobNameError(ValueError):
    """A blob hash or extension that must not become part of a corpus path."""


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
        ``read_json`` matches dot-dirs) and no source-removal walk can ever
        observe a half-written session dir; same filesystem as
        ``sessions/`` so ``os.replace`` of the staged dir stays atomic.
        """
        return self.corpus_root / ".staging"

    @property
    def empty_sessions_path(self) -> Path:
        """``{session_id: generation}`` for sessions whose source held nothing to convert.

        Kept apart from ``watermark.json`` on purpose: that file's shape is
        ``{path: mtime_ns}`` and every reader coerces its values to ``int``, so
        a structured entry there would read as corruption and cost a full
        re-materialization on any version that predates this file.
        """
        return self.corpus_root / EMPTY_SESSIONS_FILENAME

    @property
    def sink_pending_path(self) -> Path:
        """``{"session_ids": [...]}``: published sessions the session sink still owes.

        Written BEFORE a pass publishes anything (every session it may
        publish, plus what was already pending) and rewritten after the sink
        ran, so a pass that dies between a session's swap and its sync still
        leaves the session recorded.
        """
        return self.corpus_root / SINK_PENDING_FILENAME

    @property
    def blobs_dir(self) -> Path:
        """The content-addressed attachment store shared by every session.

        Outside ``sessions/`` on purpose: no reader glob and no per-session
        swap touches it, retention never deletes from it, and a blob two
        sessions share is stored once.
        """
        return self.corpus_root / "blobs"

    def blob_path(self, sha256: str, extension: str) -> Path:
        """``<corpus_root>/blobs/sha256/<first two hex>/<sha256>.<extension>``.

        Raises
        ------
        InvalidBlobNameError
            ``sha256`` is not 64 lowercase hex digits or ``extension`` is not
            one to eight lowercase letters and digits.
        """
        if not _BLOB_HASH_RE.match(sha256) or not _BLOB_EXTENSION_RE.match(extension):
            msg = f"invalid blob name {sha256!r}.{extension!r}"
            raise InvalidBlobNameError(msg)
        return self.blobs_dir / "sha256" / sha256[:2] / f"{sha256}.{extension}"

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

    def session_events_path(self, session_id: str) -> Path:
        """One line per kept non-message record: hooks, errors, cost-state, modes."""
        return self.session_dir(session_id) / SESSION_EVENTS_FILENAME

    def meta_path(self, session_id: str) -> Path:
        """Provenance record: source files, mtimes, versions, materialized_at."""
        return self.session_dir(session_id) / META_FILENAME

    def source_archive_dir(self, session_id: str) -> Path:
        """The raw source archive: one zstd ``<relative path>.zst`` per source file."""
        return self.session_dir(session_id) / SOURCE_ARCHIVE_DIRNAME
