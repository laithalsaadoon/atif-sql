# SPDX-License-Identifier: Apache-2.0

"""Contract-layout corpus reader behind :class:`TextRowsPort`.

atif-embed may never import atif-duck (import-linter independence contract),
so this adapter reads the CONTRACT corpus layout
(``<corpus_root>/sessions/<id>/trajectory.json``) directly with its OWN
DuckDB connection + ``read_json`` — the same move atif-corpus made with
``ConverterPort``: depend on the contract's artifact shapes, not on a
sibling package's registry.

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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from atif_embed.domain.sql_literal import sql_literal
from atif_embed.domain.text_stamp import PendingText, text_hash

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Minimum characters for a step text to be worth embedding.
MIN_TEXT_CHARS = 32

#: ``read_json`` upper bound — live trajectory.json files reach 85 MB
#: (harbor inlines subagent sidechains); 1 GiB gives ~10x headroom.
_MAX_OBJECT_SIZE = 1_073_741_824

#: Rows pulled off a DuckDB result per ``fetchmany``.
_FETCH_PAGE_ROWS = 512

#: Trajectory BYTES a single DuckDB statement may read. Residency scales with
#: the bytes a statement materializes, not with the file count, so batching by
#: bytes is what makes both ends bounded: thousands of small sessions are read
#: together (few statements) while a session above the budget gets a statement
#: to itself (peak stays near that one session). See this package's README for
#: the measured curve across corpus shapes.
_BATCH_MAX_BYTES = 4 * 1024 * 1024


def _step_texts_sql(paths: list[str]) -> str:
    """SQL selecting ``(uuid, text_content)`` from ONE BATCH of trajectories.

    One trajectory document per file -> ``format='auto'`` (NOT NDJSON). The
    explicit ``columns`` projection is a strict filter AND it skips JSON
    schema inference, the dominant cost on a large corpus. The flattened-text
    CASE lives once in a CTE so the >=32-char floor and the projection read
    the same expression.

    ``filename=true`` plus ``ORDER BY filename, step_id`` keeps row order
    identical to reading the batch's files one at a time, so batch boundaries
    cannot change which rows a ``--limit`` run picks.
    """
    path_list = ", ".join(sql_literal(p) for p in paths)
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
                                  part -> json_extract_string(part, '$')
                              ),
                              '\n\n'
                          )
                     ELSE json_extract_string(step, '$.message')
                END                                                  AS text_content
            FROM read_json(
                     [{path_list}],
                     format='auto',
                     filename=true,
                     columns={{steps: 'JSON[]'}},
                     maximum_object_size={_MAX_OBJECT_SIZE}
                 ) t,
                 UNNEST(t.steps) AS s(step)
        )
        SELECT uuid, text_content
        FROM step_texts
        WHERE uuid IS NOT NULL
          AND text_content IS NOT NULL
          AND length(text_content) >= {MIN_TEXT_CHARS}
        ORDER BY filename, step_id
        """  # noqa: S608 — trajectory paths escaped by sql_literal; both limits are int constants


def _complete_trajectory_paths(corpus_root: Path) -> list[tuple[str, int]]:
    """``(path, size_bytes)`` for every COMPLETE session dir (meta.json present)."""
    sessions_dir = corpus_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    paths: list[tuple[str, int]] = []
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        trajectory = session_dir / "trajectory.json"
        if not (session_dir / "meta.json").is_file():
            logger.warning(
                "Skipping incomplete session dir {} (no meta.json); excluded from embed",
                session_dir,
            )
            continue
        if trajectory.is_file():
            paths.append((str(trajectory), trajectory.stat().st_size))
    return paths


def _batch_by_bytes(paths: list[tuple[str, int]], *, max_bytes: int) -> list[list[str]]:
    """Group ``(path, size)`` pairs into batches of at most ``max_bytes``.

    Order is preserved, so batching cannot reorder rows. A file at or above
    the budget forms a batch of one rather than being dropped or merged: the
    budget is a target, and the true residency floor is the largest single
    session, which no grouping can lower.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for path, size in paths:
        if current and current_bytes + size > max_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


class DuckDbTextRows:
    """:class:`~atif_embed.domain.ports.TextRowsPort` over the contract corpus."""

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
        import duckdb

        paths = _complete_trajectory_paths(corpus_root)
        if not paths:
            logger.info("No complete sessions under {} — nothing to embed", corpus_root)
            return

        already = embedded or {}
        seen: set[str] = set()
        yielded = 0
        batches = _batch_by_bytes(paths, max_bytes=_BATCH_MAX_BYTES)
        con = duckdb.connect(":memory:")
        try:
            for batch in batches:
                if limit is not None and yielded >= limit:
                    return
                result = con.execute(_step_texts_sql(batch))
                while True:
                    if limit is not None and yielded >= limit:
                        return
                    page = result.fetchmany(_FETCH_PAGE_ROWS)
                    if not page:
                        break
                    for row in page:
                        if limit is not None and yielded >= limit:
                            return
                        uuid, text = str(row[0]), str(row[1])
                        if uuid in seen:
                            continue
                        seen.add(uuid)
                        stamp = text_hash(text)
                        stored = already.get(uuid)
                        if stored == stamp:
                            continue
                        yielded += 1
                        yield PendingText(
                            uuid=uuid,
                            text=text,
                            text_hash=stamp,
                            replaces_existing=stored is not None,
                        )
        finally:
            con.close()


__all__ = ["MIN_TEXT_CHARS", "DuckDbTextRows"]
