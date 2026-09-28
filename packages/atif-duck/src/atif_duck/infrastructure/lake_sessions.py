# SPDX-License-Identifier: Apache-2.0

"""Read one corpus's sessions out of the published lake, a batch at a time.

The analytics read path. atif-cli wraps :class:`LakeSessionReader` in
atif-analytics' ``SessionSource`` port (the two packages may not import each
other, so the rows cross as plain tuples). The statements are the constants in
:mod:`atif_duck.domain.lake_sessions`.

The connection is private to the reader and runs only those statements, so
it is not sandboxed the way ``query``'s is. It attaches the published reader
catalog ``READ_ONLY`` through :func:`~atif_duck.infrastructure.lake.attach_lake_for_query`,
so it never waits on the writer, sees the last committed state, and refuses
a lake whose schema is stale or that holds another corpus under this name.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from atif_duck.domain.lake_sessions import (
    EDGE_UUIDS_SQL,
    ERROR_STEPS_SQL,
    LAST_EDGE_TS_SQL,
    SESSION_FRESHNESS_SQL,
    TOOL_CALLS_SQL,
    TOOL_RESULTS_SQL,
    TURNS_SQL,
)
from atif_duck.domain.sql_literal import sql_literal
from atif_duck.infrastructure.lake import (
    DEFAULT_WRITER_MEMORY_BYTES,
    LakeReader,
    LakeUnavailable,
    attach_lake_for_query,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import duckdb

    from atif_duck.infrastructure.lake import LakeLayout

#: The reader's DuckDB memory cap (lowered further by the caller's). A batch
#: of sessions' tool results is the largest thing it holds.
DEFAULT_READER_MEMORY_BYTES: int = DEFAULT_WRITER_MEMORY_BYTES

#: DuckDB threads for the reader's connection.
_READER_THREADS: int = 4


@dataclass(frozen=True, slots=True)
class SessionFreshness:
    """What a session's ``meta.json`` said when the lake loaded it."""

    materialized_at: str | None
    source_mtime_ns: int | None


def _utc(value: datetime | None) -> datetime | None:
    """The lake's naive UTC timestamp as an aware one (what the file path yields)."""
    return None if value is None else value.replace(tzinfo=UTC)


class LakeSessionReader:
    """One corpus's session rows from the published lake."""

    def __init__(self, con: duckdb.DuckDBPyConnection, corpus: str, spill: Path) -> None:
        self._con = con
        self._corpus = corpus
        self._spill = spill

    @classmethod
    def open(
        cls,
        layout: LakeLayout,
        corpus_root: Path,
        *,
        memory_limit_bytes: int | None = None,
    ) -> LakeSessionReader | LakeUnavailable:
        """Attach the lake for ``corpus_root``'s corpus, or say why not (nothing is installed)."""
        import duckdb

        spill = Path(tempfile.mkdtemp(prefix="atif-sql-lake-read-"))
        con = duckdb.connect()
        try:
            cap = min(
                memory_limit_bytes or DEFAULT_READER_MEMORY_BYTES, DEFAULT_READER_MEMORY_BYTES
            )
            con.execute(f"SET memory_limit='{int(cap)}B'")
            con.execute(f"SET threads={int(_READER_THREADS)}")
            con.execute(f"SET temp_directory={sql_literal(str(spill))}")
            con.execute("SET autoinstall_known_extensions=false")
            con.execute("SET autoload_known_extensions=false")
            attached = attach_lake_for_query(
                con, layout, corpus_root=corpus_root, all_corpora=False
            )
        except BaseException:
            con.close()
            shutil.rmtree(spill, ignore_errors=True)
            raise
        if not isinstance(attached, LakeReader) or attached.corpus is None:
            con.close()
            shutil.rmtree(spill, ignore_errors=True)
            if isinstance(attached, LakeUnavailable):
                return attached
            return LakeUnavailable("the lake attached without a corpus scope")
        return cls(con, attached.corpus, spill)

    @property
    def corpus(self) -> str:
        """The corpus name the reads are scoped to."""
        return self._corpus

    def close(self) -> None:
        """Close the connection and remove its spill directory."""
        self._con.close()
        shutil.rmtree(self._spill, ignore_errors=True)

    def _batch(self, statement: str, session_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        if not session_ids:
            return []
        return self._con.execute(statement, [self._corpus, list(session_ids)]).fetchall()

    def freshness(self) -> dict[str, SessionFreshness]:
        """``{session_id: SessionFreshness}`` for every session the lake holds for the corpus."""
        rows = self._con.execute(SESSION_FRESHNESS_SQL, [self._corpus]).fetchall()
        return {
            str(sid): SessionFreshness(
                materialized_at=None if materialized is None else str(materialized),
                source_mtime_ns=None if mtime is None else int(mtime),
            )
            for sid, materialized, mtime in rows
        }

    def last_edge_ts(self) -> dict[str, datetime | None]:
        """``{session_id: newest record ts}`` (aware, UTC) over the corpus's edges."""
        rows = self._con.execute(LAST_EDGE_TS_SQL, [self._corpus]).fetchall()
        return {str(sid): _utc(ts) for sid, ts in rows}

    def turns(self, session_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        """``(session_id, step_id, ts, source, message, is_sidechain, is_compact_summary, source_uuids)``.

        In session, then step order; ``source_uuids`` is JSON text.
        """
        return self._batch(TURNS_SQL, session_ids)

    def error_steps(self, session_ids: Sequence[str]) -> set[tuple[str, int]]:
        """``(session_id, step_id)`` of every step with an error result."""
        return {(str(sid), int(step)) for sid, step in self._batch(ERROR_STEPS_SQL, session_ids)}

    def tool_calls(self, session_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        """``(session_id, step_id, tool_name, tool_input)`` in source order (input as JSON text)."""
        return self._batch(TOOL_CALLS_SQL, session_ids)

    def tool_results(self, session_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        """``(session_id, step_id, tool_use_id, content, is_error)`` in source order (content as JSON text)."""
        return self._batch(TOOL_RESULTS_SQL, session_ids)

    def edge_uuids(self, session_id: str) -> set[str]:
        """The session's non-empty raw-record uuids."""
        rows = self._con.execute(EDGE_UUIDS_SQL, [self._corpus, session_id]).fetchall()
        return {str(uuid) for (uuid,) in rows}


__all__ = [
    "DEFAULT_READER_MEMORY_BYTES",
    "LakeSessionReader",
    "SessionFreshness",
]
