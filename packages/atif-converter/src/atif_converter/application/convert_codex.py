# SPDX-License-Identifier: Apache-2.0

"""Use case: convert one Codex rollout AND account for what conversion lost.

The Codex counterpart of
:mod:`atif_converter.application.convert_and_audit`, composed the same way and
under the same discipline: the rollout is read once, fingerprinted and parsed
in the same pass, the converter and the audit consume that one list of
records, and the fingerprints are re-checked once the artifacts are built, so
the census, the trajectory and ``edges.jsonl`` all describe the same bytes of a
rollout that Codex may still be appending to.

What differs is only what a rollout IS. One file, no side-file tree, a record
taxonomy of its own, and a set of gaps of its own — so the census, the edges
and the enrichment come from the Codex modules while the snapshot machinery,
the error taxonomy and the returned ``(ConversionResult, LossReport)`` pair are
shared with the Claude Code path.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from atif_converter.domain.codex_edges import codex_edges_jsonl_lines
from atif_converter.domain.codex_enrichment import enrich_codex_trajectory
from atif_converter.domain.codex_fidelity import (
    CODEX_STRUCTURAL_GAPS,
    CodexFidelityGap,
    CodexRecordType,
)
from atif_converter.domain.errors import SourceMutatedDuringConversion
from atif_converter.domain.fidelity import AnyRecordType, LossReport
from atif_converter.infrastructure.codex_adapter import convert_loaded_codex_session
from atif_converter.infrastructure.codex_census import (
    CodexSessionCensus,
    codex_census_from_records,
)
from atif_converter.infrastructure.harbor_adapter import (
    ConversionResult,
    read_session,
    validate_trajectory,
)
from atif_converter.infrastructure.raw_records import SessionSnapshot, mutated_files

#: Response-item payload types whose presence in a rollout means a specific
#: content-shaped gap was actually exercised, rather than merely being possible.
_ITEM_TYPE_GAPS: dict[str, CodexFidelityGap] = {
    "reasoning": CodexFidelityGap.REASONING_ENCRYPTED_DROPPED,
    "tool_search_call": CodexFidelityGap.TOOL_SEARCH_CALLS_DROPPED,
    "tool_search_output": CodexFidelityGap.TOOL_SEARCH_CALLS_DROPPED,
}


def _loss_report(census: CodexSessionCensus, *, developer_messages: int) -> LossReport:
    """Loss accounting for one rollout: total records vs items that convert.

    ``records_converted`` counts the CONVERTIBLE RESPONSE ITEMS, not every
    response item and not every record: ``event_msg`` and ``turn_context``
    records are read for token counts and turn ids while producing no step of
    their own, so counting them as converted would claim the trajectory
    carries content it does not.
    """
    total = sum(census.record_counts.values())
    converted = census.convertible_items

    gaps = set(CODEX_STRUCTURAL_GAPS)
    if total != converted:
        gaps.add(CodexFidelityGap.NON_ITEM_RECORDS_DROPPED)
    if census.record_counts.get(CodexRecordType.COMPACTED):
        gaps.add(CodexFidelityGap.COMPACTION_UNHANDLED)
    if developer_messages:
        gaps.add(CodexFidelityGap.DEVELOPER_ROLE_FLATTENED_TO_SYSTEM)
    for item_type, gap in _ITEM_TYPE_GAPS.items():
        if census.item_type_counts.get(item_type):
            gaps.add(gap)

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
        # A Codex sub-agent writes its OWN rollout under its own session id, so
        # a rollout never carries a subagent side-file. Zero here is a fact
        # about the format, not an unmeasured field.
        subagent_files_found=0,
        subagent_files_convertible=0,
        workflow_subagent_files_found=0,
    )


def _developer_message_count(records: list[tuple[dict[str, object], str]]) -> int:
    """How many ``developer``-role messages the rollout carried."""
    count = 0
    for record, _source_file in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "message" and payload.get("role") == "developer":
            count += 1
    return count


def _refuse_if_mutated(snapshot: SessionSnapshot) -> None:
    mutated = mutated_files(snapshot)
    if not mutated:
        return
    logger.warning(
        "convert_codex: {} source file(s) changed while converting {}; refusing",
        len(mutated),
        snapshot.session_jsonl,
    )
    raise SourceMutatedDuringConversion(snapshot.session_jsonl, mutated)


def convert_codex_and_audit(rollout_jsonl: Path) -> tuple[ConversionResult, LossReport]:
    """Convert ``rollout_jsonl`` to ATIF and produce its loss accounting.

    The returned :class:`ConversionResult` carries the ENRICHED trajectory
    (``source_uuids`` on steps, ``cache_creation_total`` and the compaction
    count on the trajectory — see
    :func:`~atif_converter.domain.codex_enrichment.enrich_codex_trajectory`)
    plus the ready-to-write ``edges.jsonl`` lines derived from the RAW records.
    Validation is re-run post-enrichment.

    Raises
    ------
        InvalidSessionInput: ``rollout_jsonl`` is not an existing ``.jsonl`` file.
        SourceMutatedDuringConversion: the rollout changed between the
            read and the end of the audit, so the artifacts would not
            describe the rollout as it stands.
    """
    loaded = read_session(rollout_jsonl)
    snapshot = loaded.snapshot
    result = convert_loaded_codex_session(loaded)

    records = loaded.record_pairs()
    census = codex_census_from_records(rollout_jsonl, records)
    report = _loss_report(census, developer_messages=_developer_message_count(records))
    edges_lines = tuple(codex_edges_jsonl_lines(records))
    # The harbor trajectory has no other consumer, so enrich in place rather
    # than deep-copying a whole rollout's worth of steps.
    enriched = enrich_codex_trajectory(result.trajectory, records, copy_input=False)
    del records, loaded
    _refuse_if_mutated(snapshot)

    return (
        ConversionResult(
            trajectory=enriched,
            validation_errors=validate_trajectory(enriched),
            edges_lines=edges_lines,
        ),
        report,
    )
