# SPDX-License-Identifier: Apache-2.0

"""Use case: convert one session AND account for what the conversion lost.

Composes the raw-side census (:mod:`atif_converter.infrastructure.census`)
with the harbor adapter
(:mod:`atif_converter.infrastructure.harbor_adapter`) into a
``(ConversionResult, LossReport)`` pair, the honest answer to "what did we
just materialize, and what did upstream drop on the floor?".

ONE READ. The session's files are read once, by
:func:`~atif_converter.infrastructure.harbor_adapter.read_session`, which
fingerprints and parses each file in the same pass. The converter, the census,
the edges emitter and the enrichment pass all consume that one list of
records, so the trajectory, the loss report and ``edges.jsonl`` describe the
same bytes by construction. Before this, the converter and the audit each
parsed the files and the snapshot hashed them three times over.

SNAPSHOT DISCIPLINE: the fingerprints taken by that read are re-checked once
the artifacts are built. A Claude Code session that resumes writing during
the read, or anywhere before the check, fails the session: a stat pair alone
would miss a same-length rewrite inside one mtime tick, so the re-check hashes
the bytes again. That second hash pass is the one read the audit still pays
beyond the parse.
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
    convert_loaded_session,
    read_session,
    validate_trajectory,
)
from atif_converter.infrastructure.raw_records import SessionSnapshot, mutated_files

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
    ``cache_creation_total`` on the trajectory, see
    :func:`~atif_converter.domain.enrichment.enrich_trajectory`) plus the
    ready-to-write ``edges.jsonl`` lines derived from the RAW records, per
    the corpus contract. Validation is re-run post-enrichment.

    The census counts EVERY raw record, so ``LossReport.records_dropped``
    is an upper bound on what the materialized trajectory is missing.

    Note: ``records_converted`` counts raw user/assistant RECORDS, not ATIF
    steps. harbor legitimately bundles several assistant events (one
    ``message.id``) into a single agent step, so the two numbers differ by
    design.

    Raises
    ------
        InvalidSessionInput: ``session_jsonl`` is not an existing ``.jsonl`` file.
        SourceMutatedDuringConversion: a source file changed anywhere between
            the read and the end of the audit, so the artifacts would not
            describe the session as it stands.
    """
    loaded = read_session(session_jsonl)
    snapshot = loaded.snapshot
    result = convert_loaded_session(loaded, include_subagents=include_subagents)

    pairs = loaded.record_pairs()
    report = _loss_report(census_from_snapshot(snapshot, pairs))
    edges_lines = tuple(edges_jsonl_lines(pairs))
    # The harbor trajectory has no other consumer, so enrich in place rather
    # than deep-copying a whole large transcript's worth of steps.
    enriched = enrich_trajectory(
        result.trajectory,
        [record for record, _src in pairs],
        copy_input=False,
    )
    del pairs, loaded
    _refuse_if_mutated(snapshot)

    enriched_result = ConversionResult(
        trajectory=enriched,
        validation_errors=validate_trajectory(enriched),
        edges_lines=edges_lines,
    )
    return enriched_result, report
