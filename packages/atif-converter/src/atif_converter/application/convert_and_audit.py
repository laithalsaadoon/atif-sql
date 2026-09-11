# SPDX-License-Identifier: Apache-2.0

"""Use case: convert one session AND account for what the conversion lost.

Composes the raw-side census (:mod:`atif_converter.infrastructure.census`)
with the harbor adapter
(:mod:`atif_converter.infrastructure.harbor_adapter`) into a
``(ConversionResult, LossReport)`` pair — the honest answer to "what did we
just materialize, and what did upstream drop on the floor?".

SNAPSHOT DISCIPLINE: a fingerprint snapshot of every source file is taken
BEFORE harbor reads, and re-checked after harbor's read and again after the
raw parse. Any movement anywhere in that window fails the session. A Claude
Code session that resumes writing mid-conversion would otherwise yield a
census, a trajectory, and an edges.jsonl each describing different bytes of
the same session — mutually inconsistent artifacts that only surface as
enrichment desync noise.

The snapshot carries fingerprints, not records: the raw records are parsed
only after harbor's converter has returned and released its own working set,
so the two large allocations do not overlap.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from atif_converter.domain.edges import edges_jsonl_lines
from atif_converter.domain.enrichment import enrich_trajectory
from atif_converter.domain.errors import SourceMutatedDuringConversion
from atif_converter.domain.fidelity import (
    CONVERTIBLE_RECORD_TYPES,
    AnyRecordType,
    FidelityGap,
    LossReport,
)
from atif_converter.infrastructure.census import SessionCensus, census_from_snapshot
from atif_converter.infrastructure.harbor_adapter import (
    ConversionResult,
    convert_session,
    require_transcript_file,
    validate_trajectory,
)
from atif_converter.infrastructure.raw_records import (
    SessionSnapshot,
    mutated_files,
    read_snapshot_records,
    take_session_snapshot,
)

#: Gaps every harbor 0.22.0 conversion exhibits regardless of session content.
_STRUCTURAL_GAPS: frozenset[FidelityGap] = frozenset(
    {
        FidelityGap.PARENT_CHAIN_FLATTENED,
        FidelityGap.CACHE_SPLIT_PARTIAL,
        FidelityGap.UUID_NOT_PRESERVED,
        FidelityGap.COMPACT_SUMMARY_UNHANDLED,
    }
)


def _loss_report(census: SessionCensus) -> LossReport:
    converted = sum(
        count
        for record_type, count in census.record_counts.items()
        if record_type in CONVERTIBLE_RECORD_TYPES
    )
    total = sum(census.record_counts.values())

    gaps = set(_STRUCTURAL_GAPS)
    if total != converted:
        gaps.add(FidelityGap.NON_MESSAGE_RECORDS_DROPPED)
    if census.subagent_files:
        gaps.add(FidelityGap.SUBAGENTS_INLINED)
    # Named rather than inlined: LossReport is shared by both agents, so its
    # key type is the UNION of the two record taxonomies and a same-typed dict
    # of one agent's members does not match the constructor overload without
    # being widened here.
    record_counts: dict[AnyRecordType, int] = {**census.record_counts}
    return LossReport(
        record_counts=record_counts,
        records_converted=converted,
        records_dropped=total - converted,
        gaps_observed=frozenset(gaps),
        subagent_files_found=len(census.subagent_files) + len(census.workflow_subagent_files),
        # Our converter discovers every side file itself, workflow-nested ones
        # included, so all of them are convertible.
        subagent_files_convertible=len(census.subagent_files) + len(census.workflow_subagent_files),
        workflow_subagent_files_found=len(census.workflow_subagent_files),
    )


def _refuse_if_mutated(snapshot: SessionSnapshot) -> None:
    mutated = mutated_files(snapshot)
    if not mutated:
        return
    logger.warning(
        "convert_and_audit: {} source file(s) changed while converting {}; refusing",
        len(mutated),
        snapshot.session_jsonl,
    )
    raise SourceMutatedDuringConversion(snapshot.session_jsonl, mutated)


def convert_and_audit(
    session_jsonl: Path,
    *,
    include_subagents: bool = True,
) -> tuple[ConversionResult, LossReport]:
    """Convert ``session_jsonl`` to ATIF and produce its loss accounting.

    The returned :class:`ConversionResult` carries the ENRICHED trajectory
    (``source_uuids`` / ``is_compact_summary`` on steps,
    ``cache_creation_total`` on the trajectory — see
    :func:`~atif_converter.domain.enrichment.enrich_trajectory`) plus the
    ready-to-write ``edges.jsonl`` lines derived from the RAW records, per
    the corpus contract. Validation is re-run post-enrichment.

    The census counts EVERY raw record, so ``LossReport.records_dropped``
    is an upper bound on what the materialized trajectory is missing.

    Note: ``records_converted`` counts raw user/assistant RECORDS, not ATIF
    steps — harbor legitimately bundles several assistant events (one
    ``message.id``) into a single agent step, so the two numbers differ by
    design.

    Raises
    ------
        InvalidSessionInput: ``session_jsonl`` is not an existing ``.jsonl`` file.
        SourceMutatedDuringConversion: a source file changed anywhere between
            the snapshot and the end of the raw parse, so the artifacts would
            disagree with each other.
    """
    # BEFORE the snapshot: the snapshot stats the file, so an absent path would
    # raise FileNotFoundError from inside it rather than this terminal verdict.
    require_transcript_file(session_jsonl)
    snapshot = take_session_snapshot(session_jsonl)
    result = convert_session(session_jsonl, include_subagents=include_subagents)
    _refuse_if_mutated(snapshot)

    records = read_snapshot_records(snapshot)
    report = _loss_report(census_from_snapshot(snapshot, records))
    edges_lines = tuple(edges_jsonl_lines(records))
    # The harbor trajectory has no other consumer, so enrich in place rather
    # than deep-copying a whole large transcript's worth of steps.
    enriched = enrich_trajectory(
        result.trajectory,
        [record for record, _src in records],
        copy_input=False,
    )
    del records
    _refuse_if_mutated(snapshot)

    enriched_result = ConversionResult(
        trajectory=enriched,
        validation_errors=validate_trajectory(enriched),
        edges_lines=edges_lines,
    )
    return enriched_result, report
