# SPDX-License-Identifier: Apache-2.0

"""Contract-layout corpus reader behind :class:`TextRowsPort`.

atif-embed may never import atif-duck (import-linter independence contract),
so this adapter reads the CONTRACT corpus layout
(``<corpus_root>/sessions/<id>/trajectory.json``) directly with its OWN
DuckDB connection + ``read_json`` — the same move atif-corpus made with
``ConverterPort``: depend on the contract's artifact shapes, not on a
sibling package's registry. The trajectory paths reach ``read_json`` as a
bound list parameter, never as statement text.

Selection semantics (CONTRACT-V2 §VSS):

* the embeddable unit is a step's flattened text (``str | ContentPart[]``,
  the ARRAY branch joined with blank lines — mirrors atif-duck's ``steps``
  view and harbor's own text bundling);
* main chain AND sidechain steps are included (no ``is_sidechain`` filter);
* only texts of >= 32 characters qualify — shorter step texts are acks and
  noise;
* the row key is the step's PRIMARY uuid: the FIRST ``extra.source_uuids``
  entry. Harbor bundles all raw records sharing an assistant ``message.id``
  into one step, so a step maps to 1..n raw uuids; the first entry is the
  bundle's head record, which is the uuid the ``messages`` view (edges.jsonl)
  can join back to. Steps without source uuids are skipped — they cannot be
  keyed against the raw-record surface.

Torn-set guard: like atif-duck's raw readers, only session dirs with a
``meta.json`` (written last by atif-corpus) contribute rows.

Stored names: materialize stores the trajectory compressed, as
``trajectory.json.zst``; a corpus written before that keeps the plain
``trajectory.json`` until ``atif-sql corpus slim`` compresses it. Each
session's stored file is resolved once (compressed first) and DuckDB
decompresses it, so the rows are the same either way. Batches are sized by
the decompressed bytes, which is what a statement holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from atif_embed.domain.discovery import PendingSelection

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from atif_embed.domain.text_stamp import PendingText

#: Minimum characters for a step text to be worth embedding.
MIN_TEXT_CHARS = 32

#: Ceiling for a batch's ``read_json`` ``maximum_object_size``. Live
#: trajectory.json files reach 85 MB (harbor inlines subagent sidechains);
#: 1 GiB gives ~10x headroom.
_OBJECT_SIZE_CAP = 1_073_741_824

#: Floor for the same bound: DuckDB's own default.
_OBJECT_SIZE_FLOOR = 16_777_216

#: Headroom over a batch's largest document: a quarter of it plus one MiB, so
#: a file that grows a little between the stat and the read still parses.
#: The same rule as atif-duck's registry, which this package may not import.
_OBJECT_SIZE_HEADROOM_DIVISOR = 4
_OBJECT_SIZE_HEADROOM_BYTES = 1_048_576

#: The trajectory's name, and the suffix it carries when stored compressed
#: (twins of atif-corpus's, pinned in atif-cli's tests).
TRAJECTORY_FILENAME = "trajectory.json"
COMPRESSED_SUFFIX = ".zst"

#: The largest zstd frame header: enough bytes to read any frame's content size.
_FRAME_HEADER_MAX_BYTES = 18

#: Rows pulled off a DuckDB result per ``fetchmany``.
_FETCH_PAGE_ROWS = 512

#: Trajectory BYTES a single DuckDB statement may read. Residency scales with
#: the bytes a statement materializes, not with the file count, so batching by
#: bytes is what makes both ends bounded: thousands of small sessions are read
#: together (few statements) while a session above the budget gets a statement
#: to itself (peak stays near that one session). See this package's README for
#: the measured curve across corpus shapes.
_BATCH_MAX_BYTES = 4 * 1024 * 1024


def _step_texts_sql(max_object_size: int) -> str:
    """SQL selecting ``(uuid, text_content)`` from ONE BATCH of trajectories.

    The batch's paths are NOT in this text: ``read_json(?)`` takes them as one
    bound list parameter at execute time, so a path is data to DuckDB and
    never part of the statement. The only interpolations are integers: the
    batch's ``maximum_object_size`` (:func:`_object_size_bound`) and the text
    floor.

    One trajectory document per file -> ``format='auto'`` (NOT NDJSON). The
    explicit ``columns`` projection is a strict filter AND it skips JSON
    schema inference, the dominant cost on a large corpus. The flattened-text
    CASE lives once in a CTE so the >=32-char floor and the projection read
    the same expression.

    ``filename=true`` plus ``ORDER BY filename, step_id`` keeps row order
    identical to reading the batch's files one at a time, so batch boundaries
    cannot change which rows a ``--limit`` run picks. The two spellings of a
    session's file differ only after its directory name, so a corpus mixing
    them sorts in the same session order.
    """
    return f"""
        WITH step_texts AS (
            SELECT
                t.filename                                           AS filename,
                json_extract(step, '$.step_id')::BIGINT              AS step_id,
                json_extract_string(step, '$.extra.source_uuids[0]') AS uuid,
                CASE WHEN json_type(step, '$.message') = 'ARRAY'
                     THEN array_to_string(
                              list_transform(
                                  json_extract(step, '$.message[*].text'),
                                  lambda part: json_extract_string(part, '$')
                              ),
                              '\n\n'
                          )
                     ELSE json_extract_string(step, '$.message')
                END                                                  AS text_content
            FROM read_json(
                     ?,
                     format='auto',
                     filename=true,
                     columns={{steps: 'JSON[]'}},
                     maximum_object_size={int(max_object_size)}
                 ) t,
                 UNNEST(t.steps) AS s(step)
        )
        SELECT uuid, text_content
        FROM step_texts
        WHERE uuid IS NOT NULL
          AND text_content IS NOT NULL
          AND length(text_content) >= {MIN_TEXT_CHARS}
        ORDER BY filename, step_id
        """  # noqa: S608  # nosec B608 - paths are a bound parameter; both limits are ints


def _decoded_size(path: Path) -> int:
    """The trajectory's size once decompressed: what a statement reading it holds."""
    if not path.name.endswith(COMPRESSED_SUFFIX):
        return path.stat().st_size
    import zstandard

    with path.open("rb") as handle:
        size = zstandard.frame_content_size(handle.read(_FRAME_HEADER_MAX_BYTES))
    # A frame without a recorded size (not written by atif-sql): its stored
    # size is the only cheap stand-in, and it merely under-fills a batch.
    return int(size) if size >= 0 else path.stat().st_size


def _stored_trajectory(session_dir: Path) -> Path | None:
    for name in (f"{TRAJECTORY_FILENAME}{COMPRESSED_SUFFIX}", TRAJECTORY_FILENAME):
        path = session_dir / name
        if path.is_file():
            return path
    return None


def _complete_trajectory_paths(corpus_root: Path) -> list[tuple[str, int]]:
    """``(path, decoded size)`` for every COMPLETE session dir (meta.json present)."""
    sessions_dir = corpus_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    paths: list[tuple[str, int]] = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        if not (session_dir / "meta.json").is_file():
            logger.warning(
                "Skipping incomplete session dir {} (no meta.json); excluded from embed",
                session_dir,
            )
            continue
        trajectory = _stored_trajectory(session_dir)
        if trajectory is not None:
            paths.append((str(trajectory), _decoded_size(trajectory)))
    return paths


def _object_size_bound(sizes: Iterable[int]) -> int:
    """``maximum_object_size`` for one batch: its largest document plus headroom.

    Floored at DuckDB's default and capped at :data:`_OBJECT_SIZE_CAP`. The
    bound is not free: DuckDB's JSON reader reserves buffers of about twice
    it before it parses anything, and on DuckDB 2.0 the number of those
    reservations grows with the files in one statement (to about 50), at any
    thread count. Measured 2026-10-25 over 40 one-kilobyte trajectories in
    one statement at one thread, under the fixed 1 GiB bound this replaced:
    2.0.0 ran out of a 49 GB ``memory_limit`` (44 GiB already reserved, 2 GiB
    at a time), where 1.5.6 finished under 8 GB. A batch is a few MiB of
    documents, so its bound is the floor, and a session above the batch
    budget holds a statement alone with a bound sized to it.
    """
    largest = max(sizes, default=0)
    with_headroom = largest + largest // _OBJECT_SIZE_HEADROOM_DIVISOR + _OBJECT_SIZE_HEADROOM_BYTES
    return max(_OBJECT_SIZE_FLOOR, min(_OBJECT_SIZE_CAP, with_headroom))


def _batch_by_bytes(paths: list[tuple[str, int]], *, max_bytes: int) -> list[list[tuple[str, int]]]:
    """Group ``(path, size)`` pairs into batches of at most ``max_bytes``.

    Order is preserved, so batching cannot reorder rows. A file at or above
    the budget forms a batch of one rather than being dropped or merged: the
    budget is a target, and the true residency floor is the largest single
    session, which no grouping can lower.
    """
    batches: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_bytes = 0
    for path, size in paths:
        if current and current_bytes + size > max_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append((path, size))
        current_bytes += size
    if current:
        batches.append(current)
    return batches


class DuckDbTextRows:
    """:class:`~atif_embed.domain.ports.TextRowsPort` over the contract corpus."""

    #: This reader has one path: every complete session's trajectory.
    discovery = "corpus"

    def iter_unembedded(
        self,
        corpus_root: Path,
        *,
        embedded: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[PendingText]:
        """Yield the texts needing embedding, one byte-bounded batch at a time.

        DuckDB materializes a result set fully inside ``execute()``, so a
        single query over every trajectory path holds the whole corpus's text
        regardless of how it is paged off the result; splitting into
        statements is what bounds the peak, and ``fetchmany`` bounds the
        Python-side row buffer within a batch.

        The split is by total file BYTES
        (:data:`_BATCH_MAX_BYTES`), not per file. Residency tracks the bytes
        one statement reads, while wall time tracks the statement COUNT, so
        bytes is the axis that bounds both: a corpus of thousands of small
        sessions becomes a few statements instead of thousands, and a session
        larger than the budget still gets a statement to itself. Peak resident
        text is therefore the batch budget or the largest single session,
        whichever is larger.

        The staleness join against ``embedded`` (the store's
        ``{uuid: text_hash}`` map) happens in Python BEFORE ``limit`` is
        applied. Ordering matters: filtering first guarantees ``--limit N``
        always makes N rows of forward progress, whereas a SQL LIMIT ahead
        of the filter spends the cap on rows that are already embedded and a
        bounded run can make ZERO progress. At corpus scale the comparison
        is cheap. A uuid whose stored hash differs from the current text is
        STALE and is re-yielded with ``replaces_existing=True``.

        Rows are deterministic: ordered by trajectory path then step_id, and
        de-duplicated on uuid (first occurrence wins) so the Lance store
        never receives two vectors for one key.
        """
        paths = _complete_trajectory_paths(corpus_root)
        if not paths:
            logger.info("No complete sessions under {} — nothing to embed", corpus_root)
            return
        selection = PendingSelection(embedded=embedded, limit=limit)
        yield from selection.select(_step_rows(paths))

    def commit(self, *, stored_rows: int) -> None:
        """Nothing to record: this reader has no watermark and re-reads every session."""
        del stored_rows


def _step_rows(paths: list[tuple[str, int]]) -> Iterator[tuple[str, str]]:
    """``(uuid, text)`` for every qualifying step, one byte-bounded batch per statement.

    A generator, so a consumer that stops early (``--limit``) stops the
    reads too: no batch past the one it's in is ever executed.
    """
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        for batch in _batch_by_bytes(paths, max_bytes=_BATCH_MAX_BYTES):
            bound = _object_size_bound(size for _, size in batch)
            result = con.execute(_step_texts_sql(bound), [[path for path, _ in batch]])
            while page := result.fetchmany(_FETCH_PAGE_ROWS):
                for row in page:
                    yield str(row[0]), str(row[1])
    finally:
        con.close()


__all__ = ["MIN_TEXT_CHARS", "DuckDbTextRows"]
