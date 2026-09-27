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

ATTACHMENTS AND TYPED SIGNALS. Before conversion, every inline base64
attachment in the loaded records is replaced by a placeholder and its bytes
collected (:mod:`atif_converter.domain.blobs`), so neither the trajectory nor
the loss report nor the edges ever carry base64. After enrichment,
:mod:`atif_converter.domain.result_signals` writes the typed tool-outcome,
attachment and subagent fields, the subagent links read from the same pass's
``agent-*.meta.json`` sidecars.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.domain.blobs import BlobCollector, extract_claude_code_blobs
from atif_converter.domain.edges import edges_jsonl_lines
from atif_converter.domain.enrichment import enrich_trajectory
from atif_converter.domain.errors import SourceMutatedDuringConversion
from atif_converter.domain.fidelity import (
    CONVERTIBLE_RECORD_TYPES,
    AnyRecordType,
    FidelityGap,
    LossReport,
)
from atif_converter.domain.result_signals import annotate_claude_code_trajectory
from atif_converter.domain.session_events import (
    claude_reported_cost,
    claude_session_events,
    session_events_jsonl_lines,
)
from atif_converter.infrastructure.census import SessionCensus, census_from_snapshot
from atif_converter.infrastructure.harbor_adapter import (
    ConversionResult,
    convert_loaded_session,
    read_session,
    validate_trajectory,
)
from atif_converter.infrastructure.raw_records import (
    LoadedSession,
    SessionSnapshot,
    mutated_files,
)
from atif_converter.infrastructure.source_archive import SourceArchiveWriter

#: ``agent-<id>.meta.json``: the sidecar's name carries the subagent id.
_SIDECAR_PREFIX = "agent-"
_SIDECAR_SUFFIX = ".meta.json"

#: Gaps every conversion exhibits regardless of session content. The parent
#: chain is flattened into a timestamp-sorted step list, and nothing after
#: conversion rebuilds it inside the trajectory (``edges.jsonl`` keeps it).
_STRUCTURAL_GAPS: frozenset[FidelityGap] = frozenset({FidelityGap.PARENT_CHAIN_FLATTENED})

_ENRICHMENT_REFUSAL_KEYS: tuple[str, ...] = (
    "enrichment_unattributed_steps",
    "enrichment_truncated_at_step",
)


def _attributed_uuids(trajectory: dict[str, Any]) -> set[str]:
    uuids: set[str] = set()
    for step in trajectory.get("steps") or []:
        extra = step.get("extra") if isinstance(step, dict) else None
        source_uuids = extra.get("source_uuids") if isinstance(extra, dict) else None
        if isinstance(source_uuids, list):
            uuids.update(uuid for uuid in source_uuids if isinstance(uuid, str))
    return uuids


def _has_cache_creation_usage(records: list[dict[str, Any]]) -> bool:
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("cache_creation_input_tokens"), int):
            return True
    return False


def _unrepaired_gaps(enriched: dict[str, Any], records: list[dict[str, Any]]) -> set[FidelityGap]:
    """The three gaps enrichment repairs, reported only where it DIDN'T.

    * ``uuid_not_preserved``: enrichment refused to attribute some step
      (an unattributed-steps count or a truncation marker on the trajectory).
    * ``compact_summary_unhandled``: a compaction-summary record exists and
      its uuid landed in no step's ``source_uuids``, so no step carries the
      ``is_compact_summary`` flag for it.
    * ``cache_split_partial``: assistant usage carries
      ``cache_creation_input_tokens`` but the trajectory has no
      ``cache_creation_total``.
    """
    gaps: set[FidelityGap] = set()
    extra = enriched.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    if any(extra.get(key) is not None for key in _ENRICHMENT_REFUSAL_KEYS):
        gaps.add(FidelityGap.UUID_NOT_PRESERVED)
    compact_uuids = {
        record["uuid"]
        for record in records
        if record.get("isCompactSummary") and isinstance(record.get("uuid"), str)
    }
    if compact_uuids and not compact_uuids <= _attributed_uuids(enriched):
        gaps.add(FidelityGap.COMPACT_SUMMARY_UNHANDLED)
    if _has_cache_creation_usage(records) and "cache_creation_total" not in extra:
        gaps.add(FidelityGap.CACHE_SPLIT_PARTIAL)
    return gaps


def _loss_report(
    census: SessionCensus,
    *,
    captured: int,
    unrepaired: set[FidelityGap],
) -> LossReport:
    converted = sum(
        count
        for record_type, count in census.record_counts.items()
        if record_type in CONVERTIBLE_RECORD_TYPES
    )
    total = sum(census.record_counts.values())
    dropped = total - converted - captured

    gaps = set(_STRUCTURAL_GAPS) | unrepaired
    if dropped:
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
        records_captured=captured,
        records_dropped=dropped,
        gaps_observed=frozenset(gaps),
        subagent_files_found=len(census.subagent_files) + len(census.workflow_subagent_files),
        # Our converter discovers every side file itself, workflow-nested ones
        # included, so all of them are convertible.
        subagent_files_convertible=len(census.subagent_files) + len(census.workflow_subagent_files),
        workflow_subagent_files_found=len(census.workflow_subagent_files),
    )


def _stamp_reported_cost(enriched: dict[str, Any], reported: dict[str, Any]) -> None:
    """Put Claude Code's own cost figure beside the computed estimate."""
    if not reported:
        return
    final_metrics = enriched.setdefault("final_metrics", {})
    extra = final_metrics.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        final_metrics["extra"] = extra
    extra.update(reported)


def _sidecars_by_agent_id(loaded: LoadedSession) -> dict[str, Any]:
    """Parsed ``agent-<id>.meta.json`` sidecars keyed by ``<id>``; first path wins."""
    by_agent: dict[str, Any] = {}
    for path, value in loaded.sidecars.items():
        name = path.name
        if name.startswith(_SIDECAR_PREFIX) and name.endswith(_SIDECAR_SUFFIX):
            agent_id = name[len(_SIDECAR_PREFIX) : -len(_SIDECAR_SUFFIX)]
            if agent_id:
                by_agent.setdefault(agent_id, value)
    return by_agent


def _refuse_if_mutated(
    snapshot: SessionSnapshot, archive: SourceArchiveWriter | None = None
) -> None:
    mutated = mutated_files(snapshot, archive=archive)
    if not mutated:
        if archive is not None:
            archive.archive_side_files(snapshot.session_jsonl, already=snapshot.files)
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
    archive: SourceArchiveWriter | None = None,
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

    With ``archive``, the verifying re-read also streams every source file
    into that writer (see
    :mod:`atif_converter.infrastructure.source_archive`): the archive costs
    no extra open of a transcript and holds exactly the bytes that were
    parsed. It is complete only when this function returns; on any raise the
    caller must discard it.

    Raises
    ------
        InvalidSessionInput: ``session_jsonl`` is not an existing ``.jsonl`` file.
        SourceMutatedDuringConversion: a source file changed anywhere between
            the read and the end of the audit, so the artifacts would not
            describe the session as it stands.
    """
    loaded = read_session(session_jsonl)
    snapshot = loaded.snapshot
    collector = BlobCollector()
    blob_index = extract_claude_code_blobs(
        (record for records in loaded.records_by_file.values() for record in records),
        collector,
    )
    result = convert_loaded_session(loaded, include_subagents=include_subagents)

    pairs = loaded.record_pairs()
    census = census_from_snapshot(snapshot, pairs)
    edges_lines = tuple(edges_jsonl_lines(pairs))
    events = claude_session_events(pairs)
    events_lines = tuple(session_events_jsonl_lines(events))
    records = [record for record, _src in pairs]
    # The harbor trajectory has no other consumer, so enrich in place rather
    # than deep-copying a whole large transcript's worth of steps.
    enriched = enrich_trajectory(result.trajectory, records, copy_input=False)
    annotate_claude_code_trajectory(
        enriched, records, blob_index, sidecars=_sidecars_by_agent_id(loaded)
    )
    _stamp_reported_cost(enriched, claude_reported_cost(pairs))
    report = _loss_report(
        census,
        captured=len(events),
        unrepaired=_unrepaired_gaps(enriched, records),
    )
    del pairs, loaded, records, events
    _refuse_if_mutated(snapshot, archive)

    enriched_result = ConversionResult(
        trajectory=enriched,
        validation_errors=validate_trajectory(enriched),
        edges_lines=edges_lines,
        events_lines=events_lines,
        blobs=collector.blobs,
    )
    return enriched_result, report
