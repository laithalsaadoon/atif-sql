# SPDX-License-Identifier: Apache-2.0

"""One corpus's step texts and keys, read from the lake for the embedding store.

atif-embed discovers what to embed from these reads (atif-cli adapts
:class:`LakeSteps` to atif-embed's ``LakeStepsPort``; the two packages may not
import each other). Everything here reads the published catalog copy
``READ_ONLY`` through :func:`atif_duck.infrastructure.lake.attach_lake_for_query`,
so it never waits on a writer and falls back for exactly the reasons ``query``
does (no lake, a stale one, a corpus it doesn't hold).

The rows are the ``steps`` table's, in the per-session reader's order
(session id, then step id), keyed like the store: a step's primary uuid is
the first entry of ``source_uuids``, and its text is the flattened
``message`` the lake already stores.

Change reads use DuckLake's ``ducklake_table_changes``: the sink replaces a
session by deleting and re-inserting its rows, so a session written since a
snapshot shows up as deletes of its old rows and inserts of its new ones.
File merges and rewrites (``lake compact``) change no row and show up as
nothing. Expired snapshots can't be read across, which
:meth:`LakeSteps.can_read_changes_since` reports.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from atif_duck.domain.lake import CORPORA_TABLE, LAKE_ALIAS, SESSION_KEY
from atif_duck.domain.raw_readers import CORPUS_COLUMN
from atif_duck.domain.sql_literal import SqlFragment, sql_literal
from atif_duck.infrastructure.lake import (
    DEFAULT_WRITER_MEMORY_BYTES,
    LakeReader,
    LakeUnavailable,
    attach_lake_for_query,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    import duckdb

    from atif_duck.infrastructure.lake import LakeLayout

#: The lake table the embeddable rows live in.
STEPS_TABLE: str = "steps"

#: A step's primary uuid: the key the embedding store uses.
_PRIMARY_UUID: str = "json_extract_string(source_uuids, '$[0]')"

#: One corpus's steps with the key computed once (param: corpus).
_KEYED_STEPS: str = (
    f"SELECT {SESSION_KEY} AS session_id, step_id, {_PRIMARY_UUID} AS uuid, message "  # noqa: S608  # nosec B608 - module constants only
    f"FROM {LAKE_ALIAS}.{STEPS_TABLE} WHERE {CORPUS_COLUMN} = ?"
)

#: What makes a row embeddable (param: the minimum text length).
_QUALIFIES: str = "uuid IS NOT NULL AND message IS NOT NULL AND length(message) >= ?"

#: Every qualifying row of one corpus, in corpus order (params: corpus, min chars).
STEP_TEXTS_SQL: SqlFragment = SqlFragment(
    f"SELECT uuid, message FROM ({_KEYED_STEPS}) WHERE {_QUALIFIES} "  # noqa: S608  # nosec B608 - module constants only
    "ORDER BY session_id, step_id"
)

#: The qualifying rows of every uuid a step row inserted or deleted after a
#: snapshot touched, in corpus order (params: first snapshot, last snapshot,
#: corpus, corpus, min chars).
CHANGED_STEP_TEXTS_SQL: SqlFragment = SqlFragment(
    f"WITH changed AS (SELECT DISTINCT {_PRIMARY_UUID} AS uuid "  # noqa: S608  # nosec B608 - module constants only
    f"FROM ducklake_table_changes({sql_literal(LAKE_ALIAS)}, 'main', {sql_literal(STEPS_TABLE)}, ?, ?) "
    f"WHERE {CORPUS_COLUMN} = ?) "
    f"SELECT uuid, message FROM ({_KEYED_STEPS}) WHERE {_QUALIFIES} "
    "AND uuid IN (SELECT uuid FROM changed) ORDER BY session_id, step_id"
)

#: Every step's primary uuid in one corpus, text or none (param: corpus).
STEP_KEYS_SQL: SqlFragment = SqlFragment(
    f"SELECT DISTINCT uuid FROM ({_KEYED_STEPS}) WHERE uuid IS NOT NULL"  # noqa: S608  # nosec B608 - module constants only
)

#: When the corpus was last loaded whole (param: corpus). A rebuild and a
#: first load both reset it, which is what makes it the lineage.
REGISTERED_AT_SQL: SqlFragment = SqlFragment(
    f"SELECT registered_at FROM {LAKE_ALIAS}.{CORPORA_TABLE} WHERE {CORPUS_COLUMN} = ?"  # noqa: S608  # nosec B608 - module constants only
)

#: The oldest and newest snapshot the attached catalog still holds.
SNAPSHOT_RANGE_SQL: SqlFragment = SqlFragment(
    f"SELECT min(snapshot_id), max(snapshot_id) FROM ducklake_snapshots({sql_literal(LAKE_ALIAS)})"  # noqa: S608  # nosec B608 - module constants only
)

#: Rows pulled off a result per ``fetchmany``.
_FETCH_PAGE_ROWS: int = 2048

#: Threads for a step read. The reads are one scan and one sort; more
#: threads only raise the peak.
_READ_THREADS: int = 4


@dataclass(slots=True)
class LakeSteps:
    """One corpus's steps at the published catalog's current snapshot. Close it when done."""

    con: duckdb.DuckDBPyConnection
    corpus: str
    #: The corpus's ``registered_at``: changes when its rows are loaded from scratch.
    lineage: str
    snapshot_id: int
    oldest_snapshot_id: int
    spill_dir: Path

    def can_read_changes_since(self, snapshot_id: int) -> bool:
        """True when every snapshot after ``snapshot_id`` is still in the catalog."""
        return self.oldest_snapshot_id <= snapshot_id + 1 and snapshot_id <= self.snapshot_id

    def step_texts(
        self, *, min_chars: int, since_snapshot: int | None
    ) -> Iterator[tuple[str, str]]:
        """``(uuid, text)`` rows in corpus order; with ``since_snapshot``, only the changed uuids'."""
        if since_snapshot is None:
            result = self.con.execute(STEP_TEXTS_SQL, [self.corpus, int(min_chars)])
        elif since_snapshot >= self.snapshot_id:
            return
        else:
            result = self.con.execute(
                CHANGED_STEP_TEXTS_SQL,
                [
                    int(since_snapshot) + 1,
                    self.snapshot_id,
                    self.corpus,
                    self.corpus,
                    int(min_chars),
                ],
            )
        while page := result.fetchmany(_FETCH_PAGE_ROWS):
            for uuid, text in page:
                yield str(uuid), str(text)

    def step_keys(self) -> frozenset[str]:
        """Every step's primary uuid in the corpus."""
        return frozenset(
            str(row[0]) for row in self.con.execute(STEP_KEYS_SQL, [self.corpus]).fetchall()
        )

    def close(self) -> None:
        """Close the connection and remove its spill directory."""
        self.con.close()
        shutil.rmtree(self.spill_dir, ignore_errors=True)

    def __enter__(self) -> Self:
        """Use as a context manager: the connection closes on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close on the way out, whatever happened."""
        self.close()


def open_lake_steps(
    layout: LakeLayout, corpus_root: Path, *, memory_limit_bytes: int | None = None
) -> LakeSteps | LakeUnavailable:
    """Attach the published lake for ``corpus_root``'s steps, or say why not.

    DuckDB runs capped at ``memory_limit_bytes`` (at most the writer's cap),
    spilling to a private temporary directory the returned object removes on
    close.
    """
    import duckdb

    spill = Path(tempfile.mkdtemp(prefix="atif-sql-lake-steps-"))
    con = duckdb.connect()
    try:
        cap = min(memory_limit_bytes or DEFAULT_WRITER_MEMORY_BYTES, DEFAULT_WRITER_MEMORY_BYTES)
        con.execute(f"SET memory_limit='{int(cap)}B'")
        con.execute(f"SET threads={int(_READ_THREADS)}")
        con.execute(f"SET temp_directory={sql_literal(str(spill))}")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
        attached = attach_lake_for_query(con, layout, corpus_root=corpus_root, all_corpora=False)
        if not isinstance(attached, LakeReader) or attached.corpus is None:
            con.close()
            shutil.rmtree(spill, ignore_errors=True)
            return (
                attached
                if isinstance(attached, LakeUnavailable)
                else LakeUnavailable("the lake did not scope to one corpus")
            )
        registered = con.execute(REGISTERED_AT_SQL, [attached.corpus]).fetchone()
        snapshots = con.execute(SNAPSHOT_RANGE_SQL).fetchone()
    except BaseException:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
        raise
    return LakeSteps(
        con=con,
        corpus=attached.corpus,
        lineage=str(registered[0]) if registered else "",
        snapshot_id=int(snapshots[1]) if snapshots and snapshots[1] is not None else 0,
        oldest_snapshot_id=int(snapshots[0]) if snapshots and snapshots[0] is not None else 0,
        spill_dir=spill,
    )


__all__ = [
    "CHANGED_STEP_TEXTS_SQL",
    "REGISTERED_AT_SQL",
    "SNAPSHOT_RANGE_SQL",
    "STEPS_TABLE",
    "STEP_KEYS_SQL",
    "STEP_TEXTS_SQL",
    "LakeSteps",
    "open_lake_steps",
]
